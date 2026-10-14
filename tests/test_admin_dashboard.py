import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from knowvia.admin import AdministrationError, SqliteAuthority, ROLE_PERMISSIONS
from knowvia.authentication import AuthenticationService, LoginRequest, AuthenticationError
from knowvia.deployment import ApplicationEnvironment, DeploymentConfig
from knowvia.employee_directory import EmployeeDirectoryIdentityService, EmployeeRecord
from knowvia.runtime_paths import RuntimePaths
from knowvia.web import create_web_app

PASSWORD = "Dashboard-testing-phrase!"
ORIGIN = {"origin": "http://testserver"}


def employee(identifier):
    return EmployeeRecord(identifier, "Test", identifier, identifier + "@novatech.example", "Operations", "Employee")


@pytest.fixture
def authority(tmp_path):
    value = SqliteAuthority(RuntimePaths(tmp_path))
    value.bootstrap(employee("admin"), PASSWORD, PASSWORD)
    value.provision("admin", employee("reader"), PASSWORD, PASSWORD, ["employee", "hr-reader"])
    return value


@pytest.fixture
def client(authority):
    app = create_web_app(runtime_paths=authority.paths, admin_authority=authority,
                         deployment_config=DeploymentConfig(environment=ApplicationEnvironment.TEST, secure_cookie=False))
    with TestClient(app) as value:
        yield value


def login(client, identifier="admin"):
    assert client.post("/api/auth/login", json={"work_email": identifier + "@novatech.example", "password": PASSWORD}).status_code == 200


def test_admin_shell_and_assets_have_no_data_and_existing_employee_page_unchanged(client):
    page = client.get("/admin")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "Administrator access is provisioned privately" in page.text
    assert "reader@" not in page.text and PASSWORD not in page.text
    assert "Employee workspace" in page.text
    assert client.get("/").status_code == 200
    for asset in ("admin.css", "admin.js"):
        assert client.get("/static/" + asset).status_code == 200
    assert "script-src 'self'" in page.headers["content-security-policy"]


@pytest.mark.parametrize("path", ["accounts", "roles", "audit"])
def test_admin_read_endpoints_require_real_authenticated_authority(client, path):
    assert client.get("/api/admin/" + path).status_code == 401
    login(client, "reader")
    response = client.get("/api/admin/" + path)
    assert response.status_code == 403
    assert "reader@" not in response.text


def test_listing_uses_allowlisted_metadata_only_and_catalog_is_server_trusted(client, authority):
    login(client)
    response = client.get("/api/admin/accounts")
    assert response.headers["cache-control"] == "no-store"
    values = response.json()["accounts"]
    assert {row["employee_id"] for row in values} == {"admin", "reader"}
    allowed = {"employee_id", "first_name", "last_name", "work_email", "department", "role", "active", "account_status", "roles"}
    assert all(set(row) == allowed for row in values)
    encoded = json.dumps(values)
    assert PASSWORD not in encoded and "password" not in encoded and "token" not in encoded and "account_id" not in encoded
    roles = client.get("/api/admin/roles").json()["roles"]
    assert {r["name"]: frozenset(r["permissions"]) for r in roles} == ROLE_PERMISSIONS
    authority.directory.add(employee("directory-only"))
    record = next(row for row in client.get("/api/admin/accounts").json()["accounts"] if row["employee_id"] == "directory-only")
    assert record["account_status"] == "NOT_REGISTERED"


def test_dashboard_workflow_provision_role_activation_and_safe_audit(client, authority):
    login(client)
    payload = {"employee_id": "new", "first_name": "New", "last_name": "Employee", "work_email": "new@novatech.example", "department": "IT", "role": "Administrator", "password": PASSWORD, "password_confirmation": PASSWORD, "roles": ["employee", "it-reader"]}
    assert client.post("/api/admin/accounts", json=payload, headers=ORIGIN).status_code == 200
    assert not any(p.startswith("admin.") for p in authority.resolve("new").permissions)
    assert client.post("/api/admin/accounts/new/roles", json={"roles": ["finance-reader"]}, headers=ORIGIN).status_code == 200
    assert authority.resolve("new").permissions == frozenset({"knowledge.finance.read"})
    assert client.post("/api/admin/accounts/new/activation", json={"active": False}, headers=ORIGIN).status_code == 200
    assert client.post("/api/admin/accounts/new/activation", json={"active": True}, headers=ORIGIN).status_code == 200
    events = client.get("/api/admin/audit").json()["events"]
    assert events[0]["operation"] == "ACTIVATE"
    assert all(set(row) == {"event_id", "timestamp", "actor", "target", "operation", "roles"} for row in events)
    assert PASSWORD not in json.dumps(events) and "@" not in json.dumps(events)


@pytest.mark.parametrize("headers", [{}, {"origin": "https://attacker.example"}, {"origin": "null"}, {"origin": "http://testserver/"}])
def test_dashboard_mutations_preserve_strict_origin_protection(client, authority, headers):
    login(client)
    assert client.post("/api/admin/accounts/reader/activation", json={"active": False}, headers=headers).status_code == 403
    assert authority.directory.get_by_employee_id("reader").active


def test_role_escalation_invalid_payload_and_last_admin_are_safe(client, authority):
    login(client)
    assert client.post("/api/admin/accounts/admin/roles", json={"roles": ["employee"]}, headers=ORIGIN).status_code == 400
    assert client.post("/api/admin/accounts/admin/activation", json={"active": False}, headers=ORIGIN).status_code == 400
    assert client.post("/api/admin/accounts/reader/roles", json={"roles": ["root"]}, headers=ORIGIN).status_code == 400
    assert client.post("/api/admin/accounts/reader/roles", json={"roles": ["administrator"], "permissions": ["*"]}, headers=ORIGIN).status_code == 400
    client.post("/api/auth/logout")
    login(client, "reader")
    assert client.post("/api/admin/accounts/reader/roles", json={"roles": ["administrator"]}, headers=ORIGIN).status_code == 403
    assert "administrator" not in authority.resolve("reader").roles


def test_dashboard_deactivation_revokes_sessions_and_role_changes_recheck_on_read(client, authority):
    service = AuthenticationService(authority.accounts, EmployeeDirectoryIdentityService(authority.directory, authority), authority.sessions)
    token = service.login(LoginRequest("reader@novatech.example", PASSWORD)).issued_session.token
    login(client)
    assert client.post("/api/admin/accounts/reader/activation", json={"active": False}, headers=ORIGIN).status_code == 200
    assert client.post("/api/admin/accounts/reader/activation", json={"active": True}, headers=ORIGIN).status_code == 200
    with pytest.raises(AuthenticationError): service.resolve_principal(token)
    authority.provision("admin", employee("second"), PASSWORD, PASSWORD, ["administrator"])
    authority.assign_roles("second", "admin", ["employee"])
    assert client.get("/api/admin/accounts").status_code == 403


def test_catalog_and_listing_cannot_be_called_without_service_permission(authority):
    with pytest.raises(AdministrationError): authority.account_listing("reader")
    with pytest.raises(AdministrationError): authority.role_catalog("reader")


def test_read_authority_revocation_race_returns_safe_permission_denial(client, authority, monkeypatch):
    login(client)
    def denied(actor):
        raise AdministrationError("Changed authority with private diagnostic detail")
    monkeypatch.setattr(authority, "account_listing", denied)
    response = client.get("/api/admin/accounts")
    assert response.status_code == 403
    assert response.json() == {"error": "Administrator permission required."}
    assert "private" not in response.text


def test_unexpected_admin_read_failure_has_no_internal_details(client, authority, monkeypatch):
    login(client)
    def failed(actor):
        raise RuntimeError("private database detail")
    monkeypatch.setattr(authority, "role_catalog", failed)
    response = client.get("/api/admin/roles")
    assert response.status_code == 503
    assert response.json() == {"error": "Administrative data is temporarily unavailable."}
    assert "private" not in response.text


def test_admin_client_uses_safe_dom_no_token_storage_and_duplicate_submission_guard():
    root = Path(__file__).parents[1] / "src" / "knowvia" / "static"
    script = (root / "admin.js").read_text(encoding="utf-8")
    html = (root / "admin.html").read_text(encoding="utf-8")
    assert "innerHTML" not in script and "localStorage" not in script and "sessionStorage" not in script
    assert "textContent" in script and 'credentials: "same-origin"' in script
    assert "if (busy) return" in script and "current !== epoch" in script
    assert 'form.elements.password.value = ""' in script
    assert 'role="status"' in html and 'autocomplete="current-password"' in html
    assert "fetch(" in script and "Authorization" not in script
