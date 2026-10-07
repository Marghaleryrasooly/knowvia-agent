from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from knowvia.conversation import ConversationTurn, SqliteConversationStore
from knowvia.deployment import ApplicationEnvironment, DemoITTicketTool, DeploymentConfig
from knowvia.deployment_bootstrap import (
    DeploymentBootstrapError,
    DeploymentResources,
    bootstrap_index,
)
from knowvia.deployment_readiness import validate_readiness
from knowvia.identity import LocalIdentityProvider
from knowvia.production import server_settings
from knowvia.runtime_paths import PROJECT_ROOT, RuntimePaths, RuntimeStorageError
from knowvia.ticket_lifecycle import SqliteTicketLifecycleStore, StoredTicketStatusProvider, TicketLifecycleService
from knowvia.web import create_web_app


PASSWORD = "correct horse battery staple"


def test_production_start_honors_port_and_uses_single_public_bind() -> None:
    settings = server_settings({"PORT": "9123"})
    assert settings.host == "0.0.0.0"
    assert settings.port == 9123
    assert settings.workers == 1
    with pytest.raises(ValueError):
        server_settings({"PORT": "invalid"})


def test_runtime_paths_derive_from_configured_root_and_local_default(tmp_path) -> None:
    paths = RuntimePaths.from_environment({"KNOWVIA_DATA_DIR": str(tmp_path / "volume")})
    assert paths.index == (tmp_path / "volume" / "knowvia_index.json").resolve()
    assert paths.accounts.parent == paths.data_dir
    assert paths.authenticated_sessions.parent == paths.data_dir
    assert paths.conversations.parent == paths.data_dir
    assert paths.pending_actions.parent == paths.data_dir
    assert paths.audit_log.parent == paths.data_dir
    assert paths.ticket_lifecycle.parent == paths.data_dir
    assert paths.employee_directory.parent == paths.data_dir
    assert RuntimePaths.from_environment({}).data_dir == (PROJECT_ROOT / "data").resolve()


def test_data_directory_creation_and_writability_failure(tmp_path) -> None:
    paths = RuntimePaths(tmp_path / "new-volume")
    paths.ensure_writable()
    assert paths.data_dir.is_dir()
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file", encoding="utf-8")
    with pytest.raises(RuntimeStorageError, match="not writable"):
        RuntimePaths(blocked).ensure_writable()


def resources(tmp_path: Path) -> DeploymentResources:
    documents = tmp_path / "knowledge"
    static = tmp_path / "static"
    documents.mkdir(); static.mkdir()
    (documents / "policy.md").write_text("# Policy\nSafe demo policy.", encoding="utf-8")
    for name in ("index.html", "app.css", "app.js"):
        (static / name).write_text("asset", encoding="utf-8")
    index = tmp_path / "seed-index.json"
    index.write_text(json.dumps({
        "version": 1,
        "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
        "chunks": [{"id": "safe-placeholder"}],
    }), encoding="utf-8")
    return DeploymentResources(documents, index, static)


def test_index_bootstrap_is_deterministic_and_idempotent(tmp_path) -> None:
    paths = RuntimePaths(tmp_path / "data")
    source = resources(tmp_path)
    assert bootstrap_index(paths, source) is True
    first = paths.index.read_bytes()
    assert bootstrap_index(paths, source) is False
    assert paths.index.read_bytes() == first


def test_bootstrap_fails_clearly_for_missing_resources(tmp_path) -> None:
    missing = DeploymentResources(tmp_path / "missing-docs", tmp_path / "missing-index", tmp_path / "missing-static")
    with pytest.raises(DeploymentBootstrapError, match="Knowledge documents are missing"):
        bootstrap_index(RuntimePaths(tmp_path / "data"), missing)


def test_readiness_validation_bootstraps_and_checks_health_without_providers(tmp_path) -> None:
    config = DeploymentConfig(
        environment=ApplicationEnvironment.TEST,
        secure_cookie=False,
        action_backend="mock",
        router_provider="deterministic",
        allowed_hosts=("localhost", "testserver"),
    )
    result = validate_readiness(config, RuntimePaths(tmp_path / "runtime"), resources(tmp_path))
    assert result == {
        "status": "ready", "environment": "test",
        "index_initialized": True, "health": "ok",
    }


def test_authentication_persists_across_app_recreation_with_data_root(tmp_path) -> None:
    paths = RuntimePaths(tmp_path / "persistent")
    config = DeploymentConfig(
        environment=ApplicationEnvironment.TEST,
        secure_cookie=False,
        seed_demo_employee_directory=True,
        registration_enabled=True,
        allowed_hosts=("testserver",),
    )
    first = TestClient(create_web_app(deployment_config=config, runtime_paths=paths))
    registered = first.post("/api/auth/register", json={
        "employee_id": "employee-001",
        "work_email": "amina.khan@novatech.example",
        "password": PASSWORD,
        "password_confirmation": PASSWORD,
    })
    assert registered.status_code == 201
    assert first.post("/api/auth/login", json={
        "work_email": "amina.khan@novatech.example", "password": PASSWORD,
    }).status_code == 200
    token = first.cookies.get("knowvia_session")

    second = TestClient(create_web_app(deployment_config=config, runtime_paths=paths))
    second.cookies.set("knowvia_session", token)
    assert second.get("/api/auth/me").json()["employee"]["employee_id"] == "employee-001"


def test_conversations_remain_identity_isolated_on_persistent_root(tmp_path) -> None:
    paths = RuntimePaths(tmp_path / "persistent")
    first = SqliteConversationStore(paths.conversations)
    turn = ConversationTurn("Vacation?", "How many vacation days?", "hr", "KNOWLEDGE", datetime.now(timezone.utc))
    first.append("session-1", turn, "employee-001")

    reopened = SqliteConversationStore(paths.conversations)
    assert len(reopened.load("session-1", "employee-001").turns) == 1
    assert reopened.load("session-1", "employee-002").turns == ()


def test_demo_ticket_status_survives_lifecycle_service_recreation(tmp_path) -> None:
    paths = RuntimePaths(tmp_path / "persistent")
    identity = LocalIdentityProvider().resolve("employee-001")
    tool = DemoITTicketTool()
    result = tool.execute({"title": "VPN", "description": "VPN is unavailable"})
    first = TicketLifecycleService(
        SqliteTicketLifecycleStore(paths.ticket_lifecycle), StoredTicketStatusProvider()
    )
    record = first.record_created(result, identity)

    reopened = TicketLifecycleService(
        SqliteTicketLifecycleStore(paths.ticket_lifecycle), StoredTicketStatusProvider()
    )
    status = reopened.lookup(record.ticket_id, identity)
    assert status.success is True
    assert status.ticket_id == record.ticket_id


def test_container_and_demo_template_contain_no_embedded_secrets() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    template = (PROJECT_ROOT / "deploy" / "demo.env.example").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=" not in dockerfile
    assert "JIRA_API_TOKEN" not in dockerfile
    assert "KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS=false" in template
    assert "KNOWVIA_AGENT_TOOL=mock" in template
    assert "SET_IN_PLATFORM_SECRET_STORE" in template
    assert "your-jira-api-token" not in template
