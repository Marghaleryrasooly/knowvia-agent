from __future__ import annotations

from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.confirmation import SqlitePendingActionStore
from knowvia.deployment import (
    ApplicationEnvironment,
    DemoITTicketTool,
    DeploymentConfig,
    DeploymentConfigurationError,
)
from knowvia.orchestrator import AgentOrchestrator
from knowvia.web import WebAuthenticationConfig, create_web_app
from knowvia.web import ProductionRuntimeFactory
from knowvia.runtime_paths import RuntimePaths
from knowvia.identity import LocalIdentityProvider


def demo_config(**changes) -> DeploymentConfig:
    base = DeploymentConfig(
        environment=ApplicationEnvironment.DEMO,
        secure_cookie=True,
        seed_demo_employee_directory=True,
        registration_enabled=True,
        action_backend="mock",
        allowed_hosts=("testserver",),
    )
    result = replace(base, **changes)
    result.validate()
    return result


def test_production_and_demo_defaults_are_fail_closed() -> None:
    production = DeploymentConfig.from_environment({"GROQ_API_KEY": "fictional"})
    assert production.environment is ApplicationEnvironment.PRODUCTION
    assert production.secure_cookie is True
    assert production.development_identity_override is False
    assert production.registration_enabled is False
    assert production.developer_details_enabled is False
    assert production.real_jira_mutations_enabled is False
    assert production.action_backend == "mock"

    demo = demo_config()
    assert demo.development_identity_override is False
    assert demo.real_jira_mutations_enabled is False


def test_authenticated_web_mock_ticketing_is_confirmation_gated(tmp_path) -> None:
    config = DeploymentConfig(
        environment=ApplicationEnvironment.DEVELOPMENT,
        secure_cookie=False,
        development_identity_override=True,
        action_backend="mock",
        router_provider="deterministic",
    )
    config.validate()
    factory = ProductionRuntimeFactory(config, RuntimePaths(tmp_path))
    factory._knowledge_components = (object(), object(), object())

    runtime = factory.build(LocalIdentityProvider().resolve("employee-001"))

    assert runtime.confirmation_agent.tools["create_it_ticket"].specification.requires_confirmation is True


def test_public_demo_rejects_development_identity_and_insecure_cookie() -> None:
    with pytest.raises(DeploymentConfigurationError):
        demo_config(development_identity_override=True)
    with pytest.raises(DeploymentConfigurationError):
        demo_config(secure_cookie=False)


def test_public_demo_rejects_debug_details_and_real_jira() -> None:
    for changes in (
        {"debug_enabled": True},
        {"developer_details_enabled": True},
        {"real_jira_mutations_enabled": True},
        {"action_backend": "jira"},
        {"action_backend": "mcp", "mcp_ticket_backend": "jira"},
    ):
        with pytest.raises(DeploymentConfigurationError):
            demo_config(**changes)


def test_jira_credentials_alone_never_enable_real_jira() -> None:
    config = DeploymentConfig.from_environment({
        "GROQ_API_KEY": "fictional",
        "KNOWVIA_ENVIRONMENT": "production",
        "JIRA_BASE_URL": "https://example.atlassian.net",
        "JIRA_USER_EMAIL": "person@example.com",
        "JIRA_API_TOKEN": "secret-token",
        "JIRA_PROJECT_KEY": "IT",
    })
    assert config.action_backend == "mock"
    assert config.real_jira_mutations_enabled is False
    assert "secret-token" not in repr(config)


def test_selected_jira_requires_explicit_server_side_enablement() -> None:
    with pytest.raises(DeploymentConfigurationError):
        DeploymentConfig.from_environment({
            "KNOWVIA_ENVIRONMENT": "production",
            "KNOWVIA_AGENT_TOOL": "jira",
        })
    enabled = DeploymentConfig.from_environment({
        "GROQ_API_KEY": "fictional",
        "KNOWVIA_ENVIRONMENT": "production",
        "KNOWVIA_AGENT_TOOL": "jira",
        "KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS": "true",
        "JIRA_BASE_URL": "https://example.atlassian.net",
        "JIRA_USER_EMAIL": "person@example.com",
        "JIRA_API_TOKEN": "placeholder",
        "JIRA_PROJECT_KEY": "IT",
        "JIRA_ISSUE_TYPE": "Task",
    })
    assert enabled.real_jira_mutations_enabled is True


def test_selected_external_providers_require_complete_server_configuration() -> None:
    with pytest.raises(DeploymentConfigurationError):
        DeploymentConfig.from_environment({
            "KNOWVIA_ENVIRONMENT": "production",
            "KNOWVIA_AGENT_ROUTER": "gemini",
        })
    with pytest.raises(DeploymentConfigurationError):
        DeploymentConfig.from_environment({
            "KNOWVIA_ENVIRONMENT": "production",
            "KNOWVIA_AGENT_TOOL": "jira",
            "KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS": "true",
        })


def test_demo_registration_requires_fictional_seed_directory() -> None:
    with pytest.raises(DeploymentConfigurationError):
        DeploymentConfig(
            environment=ApplicationEnvironment.DEMO,
            registration_enabled=True,
            allowed_hosts=("testserver",),
        ).validate()


def test_demo_ticket_is_local_named_and_confirmation_gated(tmp_path) -> None:
    tool = DemoITTicketTool()
    assert tool.specification.requires_confirmation is True
    orchestrator = AgentOrchestrator(
        DeterministicAgentRouter(), object(), [tool],
        SqlitePendingActionStore(tmp_path / "pending.db"),
    )
    proposed = orchestrator.handle("Create an IT ticket because my VPN is broken.")
    assert proposed.action_proposal is not None
    assert proposed.tool_executed is False
    completed = orchestrator.confirm(proposed.action_proposal.proposal_id)
    assert completed.tool_result is not None
    assert completed.tool_result.data["ticket_id"].startswith("DEMO_IT-")


class ExplodingService:
    def identities(self):
        raise AssertionError("Health/config must not initialize application services")


def test_health_is_lightweight_and_security_headers_are_present() -> None:
    client = TestClient(create_web_app(ExplodingService(), deployment_config=demo_config()))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "strict-transport-security" not in response.headers


def test_hsts_is_explicit_and_never_enabled_for_local_development() -> None:
    with pytest.raises(DeploymentConfigurationError):
        DeploymentConfig(environment=ApplicationEnvironment.DEVELOPMENT, hsts_enabled=True).validate()
    client = TestClient(create_web_app(ExplodingService(), deployment_config=demo_config(hsts_enabled=True)))
    assert client.get("/health").headers["strict-transport-security"].startswith("max-age=")


def test_public_api_config_exposes_no_secrets_or_identity_override() -> None:
    client = TestClient(create_web_app(ExplodingService(), deployment_config=demo_config()))
    response = client.get("/api/config")
    assert response.status_code == 200
    payload = response.json()
    assert payload["identities"] == []
    assert payload["development_identity_override"] is False
    assert payload["developer_details_enabled"] is False
    serialized = response.text.lower()
    for forbidden in ("api_key", "api token", "jira", "gemini", "password", "session"):
        assert forbidden not in serialized


def test_public_mode_requires_authentication() -> None:
    client = TestClient(create_web_app(ExplodingService(), deployment_config=demo_config()))
    response = client.post("/api/chat", json={
        "identity_id": "employee-001", "session_id": "demo-session", "message": "Hello",
    })
    assert response.status_code == 401


def test_web_factory_rejects_authentication_override_that_weakens_demo() -> None:
    with pytest.raises(DeploymentConfigurationError):
        create_web_app(
            ExplodingService(),
            authentication_config=WebAuthenticationConfig.local_development(),
            deployment_config=demo_config(),
        )


def test_normal_public_request_cannot_enable_developer_payloads() -> None:
    class Service:
        def identities(self): return []

    client = TestClient(create_web_app(Service(), deployment_config=demo_config()))
    config = client.get("/api/config").json()
    assert config["developer_details_enabled"] is False
