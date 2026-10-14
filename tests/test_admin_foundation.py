from concurrent.futures import ThreadPoolExecutor
import json
import pytest
from starlette.testclient import TestClient
from knowvia.admin import SqliteAuthority, AdministrationError, LoginAbuseGuard
from knowvia.accounts import RegistrationService, RegistrationRequest
from knowvia.authentication import AuthenticationService, LoginRequest, AuthenticationError
from knowvia.employee_directory import EmployeeRecord, EmployeeDirectoryIdentityService
from knowvia.identity import IdentityError
from knowvia.runtime_paths import RuntimePaths
from knowvia.web import create_web_app, KnowviaWebService
from knowvia.deployment import DeploymentConfig, ApplicationEnvironment

PASSWORD = "Foundation-test-phrase!"


def employee(identifier, role="Employee"):
    return EmployeeRecord(identifier, "Test", identifier, identifier + "@novatech.example", "Operations", role)


@pytest.fixture
def authority(tmp_path):
    value = SqliteAuthority(RuntimePaths(tmp_path))
    value.bootstrap(employee("administrator"), PASSWORD, PASSWORD)
    return value


def auth(authority):
    return AuthenticationService(authority.accounts, EmployeeDirectoryIdentityService(authority.directory, authority), authority.sessions)


def test_bootstrap_is_private_one_time_and_no_default_credentials(authority):
    with pytest.raises(AdministrationError): authority.bootstrap(employee("another"), PASSWORD, PASSWORD)
    assert "admin.accounts.manage" in authority.resolve("administrator").permissions
    assert auth(authority).login(LoginRequest("administrator@novatech.example", PASSWORD))
    with pytest.raises(AuthenticationError): auth(authority).login(LoginRequest("administrator@novatech.example", "incorrect"))


def test_provisioning_uses_trusted_assignments_not_directory_role(authority):
    authority.provision("administrator", employee("reader", role="Administrator"), PASSWORD, PASSWORD, ["employee", "hr-reader"])
    assert authority.resolve("reader").permissions == frozenset({"knowledge.hr.read"})
    principal = auth(authority).login(LoginRequest("reader@novatech.example", PASSWORD)).principal.identity
    assert principal.permissions == authority.resolve("reader").permissions
    assert principal.roles == authority.resolve("reader").roles
    with pytest.raises(AdministrationError): authority.assign_roles("reader", "reader", ["administrator"])
    with pytest.raises(AdministrationError): authority.assign_roles("administrator", "reader", ["arbitrary-superuser"])


def test_deactivation_revokes_all_sessions_and_reactivation_does_not_restore_tokens(authority):
    authority.provision("administrator", employee("reader"), PASSWORD, PASSWORD, ["employee", "hr-reader"])
    service = auth(authority)
    tokens = [service.login(LoginRequest("reader@novatech.example", PASSWORD)).issued_session.token for _ in range(2)]
    authority.set_active("administrator", "reader", False)
    with pytest.raises(IdentityError): authority.resolve("reader")
    authority.set_active("administrator", "reader", True)
    for token in tokens:
        with pytest.raises(AuthenticationError): service.resolve_principal(token)
    assert service.login(LoginRequest("reader@novatech.example", PASSWORD))


def test_last_active_admin_cannot_be_disabled_or_demoted(authority):
    with pytest.raises(AdministrationError): authority.set_active("administrator", "administrator", False)
    with pytest.raises(AdministrationError): authority.assign_roles("administrator", "administrator", ["employee"])
    assert "admin.accounts.manage" in authority.resolve("administrator").permissions


def test_concurrent_admin_removal_preserves_one_active_admin(authority):
    authority.provision("administrator", employee("second"), PASSWORD, PASSWORD, ["administrator"])
    def remove(target):
        try: authority.assign_roles("administrator", target, ["employee"]); return True
        except (AdministrationError, IdentityError): return False
    with ThreadPoolExecutor(2) as pool: outcomes = list(pool.map(remove, ["administrator", "second"]))
    assert sum(outcomes) == 1


def test_initial_demo_seed_preserves_administrative_changes(authority):
    authority.seed_demo_once()
    registration = RegistrationService(EmployeeDirectoryIdentityService(authority.directory, authority), authority.accounts)
    registration.register(RegistrationRequest("employee-001", "amina.khan@novatech.example", PASSWORD, PASSWORD))
    authority.assign_roles("administrator", "employee-001", ["employee", "finance-reader"])
    authority.set_active("administrator", "employee-001", False)
    recreated = SqliteAuthority(authority.paths)
    recreated.seed_demo_once()
    assert not recreated.directory.get_by_employee_id("employee-001").active
    recreated.set_active("administrator", "employee-001", True)
    assert recreated.resolve("employee-001").permissions == frozenset({"knowledge.finance.read"})


def test_audit_contains_only_safe_change_metadata(authority):
    authority.provision("administrator", employee("reader"), PASSWORD, PASSWORD, ["employee"])
    data = json.dumps(authority.events("administrator"))
    assert PASSWORD not in data and "password" not in data and "@" not in data
    assert "PROVISION" in data
    with pytest.raises(AdministrationError): authority.events("reader")


def test_login_limit_uses_both_client_and_account_and_expires():
    clock = [0]
    guard = LoginAbuseGuard(limit=2, window=10, clock=lambda: clock[0])
    assert guard.allow("client-a", "employee")
    assert guard.allow("client-b", "employee")
    assert not guard.allow("client-c", "employee")
    clock[0] = 11
    assert guard.allow("client-c", "employee")


def test_admin_api_requires_auth_origin_and_trusted_roles(authority):
    config = DeploymentConfig(environment=ApplicationEnvironment.TEST, secure_cookie=False)
    app = create_web_app(deployment_config=config, runtime_paths=authority.paths, admin_authority=authority)
    with TestClient(app) as client:
        assert client.get("/api/admin/me").status_code == 401
        assert client.post("/api/auth/login", json={"work_email": "administrator@novatech.example", "password": PASSWORD}).status_code == 200
        assert client.get("/api/admin/me").status_code == 200
        payload = {"employee_id": "reader", "first_name": "Test", "last_name": "Reader", "work_email": "reader@novatech.example", "department": "Operations", "role": "Administrator", "password": PASSWORD, "password_confirmation": PASSWORD, "roles": ["employee", "hr-reader"]}
        assert client.post("/api/admin/accounts", json=payload).status_code == 403
        assert client.post("/api/admin/accounts", json=payload, headers={"origin": "https://attacker.example"}).status_code == 403
        assert client.post("/api/admin/accounts", json=payload, headers={"origin": "http://testserver"}).status_code == 200
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"work_email": "reader@novatech.example", "password": PASSWORD})
        assert client.get("/api/admin/me").status_code == 403
        assert client.post("/api/admin/accounts/reader/roles", json={"roles": ["administrator"]}, headers={"origin": "http://testserver"}).status_code == 403


def test_web_service_and_auth_use_same_authority(authority):
    authority.provision("administrator", employee("reader"), PASSWORD, PASSWORD, ["hr-reader"])
    web = KnowviaWebService(object(), identity_provider=authority, conversation_store=object())
    principal = auth(authority).login(LoginRequest("reader@novatech.example", PASSWORD)).principal
    assert web._identity(principal.identity.subject_id).permissions == principal.identity.permissions
    assert web._identity(principal.identity.subject_id).roles == principal.identity.roles
    authority.assign_roles("administrator", "reader", ["it-reader"])
    assert web._identity("reader").permissions == auth(authority).resolve_principal(auth(authority).login(LoginRequest("reader@novatech.example", PASSWORD)).issued_session.token).identity.permissions


def test_legacy_authority_migration_requires_existing_directory_records(tmp_path):
    from knowvia.employee_directory import SqliteEmployeeDirectory, demo_employee_records
    paths = RuntimePaths(tmp_path)
    directory = SqliteEmployeeDirectory(paths.employee_directory)
    directory.add(demo_employee_records()[0])
    trusted = SqliteAuthority(paths)
    assert "action.it_ticket.create_own" in trusted.resolve("employee-001").permissions
    assert not any(p.startswith("admin.") for p in trusted.resolve("employee-001").permissions)
    assert directory.get_by_employee_id("employee-002") is None


def test_failed_bootstrap_rolls_back_and_does_not_adopt_existing_account(tmp_path):
    from knowvia.employee_directory import SqliteEmployeeDirectory
    paths = RuntimePaths(tmp_path)
    SqliteEmployeeDirectory(paths.employee_directory).add(employee("existing"))
    service = SqliteAuthority(paths)
    with pytest.raises(Exception): service.bootstrap(employee("existing"), PASSWORD, PASSWORD)
    assert service.accounts.get_by_employee_id("existing") is None
    with service.transaction() as db:
        assert db.execute("SELECT count(*) FROM role_assignments WHERE role='administrator'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM authority_meta").fetchone()[0] == 0
    service.bootstrap(employee("actual-admin"), PASSWORD, PASSWORD)


def test_admin_api_deactivation_and_role_mutations_recheck_authority(authority):
    authority.provision("administrator", employee("reader"), PASSWORD, PASSWORD, ["hr-reader"])
    config = DeploymentConfig(environment=ApplicationEnvironment.TEST, secure_cookie=False)
    with TestClient(create_web_app(deployment_config=config, runtime_paths=authority.paths, admin_authority=authority)) as client:
        client.post("/api/auth/login", json={"work_email": "administrator@novatech.example", "password": PASSWORD})
        headers = {"origin": "http://testserver"}
        assert client.post("/api/admin/accounts/reader/roles", json={"roles": ["it-reader"]}, headers=headers).status_code == 200
        assert authority.resolve("reader").permissions == frozenset({"knowledge.it.read"})
        assert client.post("/api/admin/accounts/administrator/activation", json={"active": False}, headers=headers).status_code == 400
        assert client.post("/api/admin/accounts/reader/activation", json={"active": False}, headers=headers).status_code == 200
        assert client.get("/api/admin/audit").status_code == 200


def test_admin_endpoints_never_accept_development_identity_override(authority):
    config = DeploymentConfig(environment=ApplicationEnvironment.TEST, secure_cookie=False, development_identity_override=True)
    with TestClient(create_web_app(deployment_config=config, runtime_paths=authority.paths, admin_authority=authority)) as client:
        response = client.post("/api/admin/accounts/reader/roles", json={"roles": ["administrator"], "identity_id": "administrator"}, headers={"origin": "http://testserver"})
        assert response.status_code == 401


def test_privileged_directory_identity_cannot_be_claimed_by_public_registration(authority):
    from knowvia.accounts import RegistrationError
    authority.seed_demo_once()
    with pytest.raises(AdministrationError):
        authority.assign_roles("administrator", "employee-001", ["administrator"])
    # Defense in depth for a privileged directory identity lacking an account:
    # even a trusted internal assignment cannot turn public registration into bootstrap.
    with authority.transaction() as db:
        authority._assign(db, "employee-001", ["administrator"])
    service = RegistrationService(EmployeeDirectoryIdentityService(authority.directory, authority), authority.accounts)
    with pytest.raises(RegistrationError):
        service.register(RegistrationRequest("employee-001", "amina.khan@novatech.example", PASSWORD, PASSWORD))
    assert authority.accounts.get_by_employee_id("employee-001") is None
