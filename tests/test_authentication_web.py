from starlette.testclient import TestClient

from knowvia.accounts import RegistrationService, SqliteAccountRepository
from knowvia.authentication import AuthenticationService, SqliteAuthenticatedSessionStore
from knowvia.employee_directory import EmployeeDirectoryIdentityService, SqliteEmployeeDirectory, seed_demo_employee_directory
from knowvia.identity import LocalIdentityProvider
from knowvia.web import AuthenticationRuntime, WebAuthenticationConfig, create_web_app


PASSWORD = "correct horse battery staple"


class StubService:
    def __init__(self): self.last_identity = None
    def identities(self): return [{"id": "employee-001", "name": "Demo Employee"}, {"id": "employee-002", "name": "Other"}]
    def chat(self, session_id, message, identity_id, demo=False):
        self.last_identity = identity_id
        return {"kind": "KNOWLEDGE", "text": "Safe answer.", "sources": []}
    def record_conversation_exchange(self, *args): pass


def runtime(tmp_path):
    directory = SqliteEmployeeDirectory(tmp_path / "employees.db")
    seed_demo_employee_directory(directory)
    bridge = EmployeeDirectoryIdentityService(directory, LocalIdentityProvider())
    accounts = SqliteAccountRepository(tmp_path / "accounts.db")
    registration = RegistrationService(bridge, accounts)
    authentication = AuthenticationService(accounts, bridge, SqliteAuthenticatedSessionStore(tmp_path / "sessions.db"))
    return AuthenticationRuntime(registration, authentication), accounts


def register(client, employee_id="employee-001", email="amina.khan@novatech.example"):
    return client.post("/api/auth/register", json={"employee_id": employee_id, "work_email": email, "password": PASSWORD, "password_confirmation": PASSWORD})


def login(client, email="amina.khan@novatech.example", password=PASSWORD):
    return client.post("/api/auth/login", json={"work_email": email, "password": password})


def test_registration_response_is_safe_and_login_cookie_is_httponly_lax_local(tmp_path) -> None:
    auth, _ = runtime(tmp_path)
    client = TestClient(create_web_app(StubService(), auth, WebAuthenticationConfig.local_development()))
    registered = register(client)
    assert registered.status_code == 201
    assert "password" not in registered.text.lower()

    response = login(client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie
    assert "max-age=28800" in cookie
    assert "password" not in response.text.lower()


def test_production_cookie_is_secure(tmp_path) -> None:
    auth, _ = runtime(tmp_path)
    client = TestClient(create_web_app(StubService(), auth, WebAuthenticationConfig()))
    assert register(client).status_code == 201
    response = login(client)
    assert "secure" in response.headers["set-cookie"].lower()


def test_me_logout_and_server_revocation(tmp_path) -> None:
    auth, _ = runtime(tmp_path)
    client = TestClient(create_web_app(StubService(), auth, WebAuthenticationConfig.local_development()))
    register(client); login(client)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["employee"]["employee_id"] == "employee-001"
    assert "permissions" not in me.text and "session" not in me.text and "password" not in me.text
    stale_token = client.cookies.get("knowvia_session")
    logout = client.post("/api/auth/logout")
    assert logout.status_code == 200
    assert "max-age=0" in logout.headers["set-cookie"].lower()
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.set("knowvia_session", stale_token)
    assert client.get("/api/auth/me").status_code == 401


def test_me_rejects_unauthenticated_request(tmp_path) -> None:
    auth, _ = runtime(tmp_path)
    client = TestClient(create_web_app(StubService(), auth, WebAuthenticationConfig.local_development()))
    assert client.get("/api/auth/me").status_code == 401


def test_authenticated_request_ignores_client_identity_impersonation(tmp_path) -> None:
    auth, _ = runtime(tmp_path)
    service = StubService()
    client = TestClient(create_web_app(service, auth, WebAuthenticationConfig.local_development()))
    register(client); login(client)
    response = client.post("/api/chat", json={"session_id": "auth-chat", "identity_id": "employee-002", "message": "Hello"})
    assert response.status_code == 200
    assert service.last_identity == "employee-001"


def test_failed_cookie_never_falls_back_to_development_identity(tmp_path) -> None:
    auth, _ = runtime(tmp_path)
    service = StubService()
    client = TestClient(create_web_app(service, auth, WebAuthenticationConfig.local_development()))
    client.cookies.set("knowvia_session", "x" * 43)
    response = client.post("/api/chat", json={"session_id": "auth-chat", "identity_id": "employee-001", "message": "Hello"})
    assert response.status_code == 401
    assert service.last_identity is None


def test_development_override_requires_explicit_configuration(tmp_path) -> None:
    auth, _ = runtime(tmp_path)
    body = {"session_id": "dev-chat", "identity_id": "employee-001", "message": "Hello"}
    production = TestClient(create_web_app(StubService(), auth, WebAuthenticationConfig()))
    assert production.post("/api/chat", json=body).status_code == 401
    development = TestClient(create_web_app(StubService(), auth, WebAuthenticationConfig.local_development()))
    assert development.post("/api/chat", json=body).status_code == 200


def test_auth_payload_rejects_client_controlled_identity_fields(tmp_path) -> None:
    auth, _ = runtime(tmp_path)
    client = TestClient(create_web_app(StubService(), auth, WebAuthenticationConfig.local_development()))
    for field, value in (
        ("department", "Executive"),
        ("role", "Administrator"),
        ("permissions", ["admin"]),
        ("identity_id", "employee-002"),
    ):
        response = client.post("/api/auth/register", json={"employee_id": "employee-001", "work_email": "amina.khan@novatech.example", "password": PASSWORD, "password_confirmation": PASSWORD, field: value})
        assert response.status_code == 400
        assert field not in response.text
