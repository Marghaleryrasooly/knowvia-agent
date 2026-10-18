"""Admin error boundaries against disposable SQLite databases only."""
import json

import pytest
from starlette.testclient import TestClient

from knowvia.admin import SqliteAuthority
from knowvia.deployment import ApplicationEnvironment, DeploymentConfig
from knowvia.web import create_web_app
from test_admin_dashboard import authority, client, employee, login, ORIGIN, PASSWORD


def payload(identifier="new-reader"):
    return dict(employee_id=identifier, first_name="New", last_name="Reader",
                work_email=identifier + "@novatech.example", department="IT", role="Employee",
                password=PASSWORD, password_confirmation=PASSWORD, roles=["employee", "it-reader"])


@pytest.mark.parametrize("collision", ["registered-id", "registered-email", "directory-id", "directory-email"])
def test_identity_collisions_have_safe_category_and_atomic_rollback(client, authority, collision):
    authority.directory.add(employee("directory-only"))
    login(client)
    before = client.get("/api/admin/accounts").json()
    events = authority.events("admin")
    body = payload()
    if collision.endswith("id"):
        body["employee_id"] = "reader" if collision.startswith("registered") else "directory-only"
    else:
        body["work_email"] = ("reader" if collision.startswith("registered") else "directory-only") + "@novatech.example"
    response = client.post("/api/admin/accounts", json=body, headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["code"] == "IDENTITY_CONFLICT"
    assert PASSWORD not in response.text and "sqlite" not in response.text.lower()
    assert client.get("/api/admin/accounts").json() == before
    assert authority.events("admin") == events


@pytest.mark.parametrize("change", [dict(password="short", password_confirmation="short"),
    dict(password="x" * 129, password_confirmation="x" * 129), dict(password_confirmation="Different-password!"),
    dict(roles=[]), dict(roles=["invented-administrator"]), dict(first_name=""), dict(work_email="invalid")])
def test_provisioning_validation_preserves_data(client, authority, change):
    login(client)
    before = client.get("/api/admin/accounts").json()
    response = client.post("/api/admin/accounts", json={**payload(), **change}, headers=ORIGIN)
    assert response.status_code == 400
    assert PASSWORD not in response.text
    assert client.get("/api/admin/accounts").json() == before


def test_new_employee_authentication_roles_and_restart_persistence(client, authority):
    login(client)
    assert client.post("/api/admin/accounts", json=payload(), headers=ORIGIN).status_code == 200
    restarted = SqliteAuthority(authority.paths)
    app = create_web_app(runtime_paths=authority.paths, admin_authority=restarted,
                         deployment_config=DeploymentConfig(environment=ApplicationEnvironment.TEST, secure_cookie=False))
    with TestClient(app) as fresh:
        login(fresh, "new-reader")
        assert fresh.get("/api/admin/accounts").status_code == 403
        assert restarted.resolve("new-reader").permissions == frozenset({"knowledge.it.read"})
        assert fresh.get("/api/auth/me").status_code == 200
    listing = client.get("/api/admin/accounts").json()["accounts"]
    assert next(row for row in listing if row["employee_id"] == "new-reader")["roles"] == ["employee", "it-reader"]
    encoded = json.dumps(authority.events("admin"))
    assert "PROVISION" in encoded and PASSWORD not in encoded and "password" not in encoded


def test_origin_denial_and_authentication_are_distinct(client, authority):
    target = "/api/admin/accounts/reader/activation"
    assert client.post(target, json={"active": False}, headers=ORIGIN).status_code == 401
    login(client)
    denied = client.post(target, json={"active": False}, headers={"origin": "https://untrusted.example"})
    assert denied.status_code == 403 and denied.json()["code"] == "ORIGIN_REJECTED"
    assert client.get("/api/admin/me").status_code == 200
    assert authority.directory.get_by_employee_id("reader").active


def test_unknown_provision_failure_is_sanitized(client, authority, monkeypatch):
    login(client)
    def fail(*args, **kwargs):
        raise RuntimeError("private-database-detail")
    monkeypatch.setattr(authority, "provision", fail)
    response = client.post("/api/admin/accounts", json=payload(), headers=ORIGIN)
    assert response.status_code == 400
    assert "private-database-detail" not in response.text and PASSWORD not in response.text
    assert "IDENTITY_CONFLICT" not in response.text
