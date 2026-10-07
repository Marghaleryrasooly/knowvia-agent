import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from knowvia.accounts import AccountStatus, RegistrationRequest, RegistrationService, SqliteAccountRepository
from knowvia.authentication import AuthenticationError, AuthenticationService, LoginRequest, SqliteAuthenticatedSessionStore
from knowvia.employee_directory import EmployeeDirectoryIdentityService, SqliteEmployeeDirectory, seed_demo_employee_directory
from knowvia.identity import LocalIdentityProvider


PASSWORD = "correct horse battery staple"


class Clock:
    def __init__(self): self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)
    def __call__(self): return self.value


def stack(tmp_path, provider=None, lifetime=timedelta(hours=8)):
    directory = SqliteEmployeeDirectory(tmp_path / "employees.db")
    seed_demo_employee_directory(directory)
    identities = provider or LocalIdentityProvider()
    bridge = EmployeeDirectoryIdentityService(directory, identities)
    accounts = SqliteAccountRepository(tmp_path / "accounts.db")
    registration = RegistrationService(bridge, accounts)
    registration.register(RegistrationRequest("employee-001", "amina.khan@novatech.example", PASSWORD, PASSWORD))
    clock = Clock()
    sessions = SqliteAuthenticatedSessionStore(tmp_path / "sessions.db", lifetime=lifetime, now=clock)
    auth = AuthenticationService(accounts, bridge, sessions)
    return auth, accounts, directory, sessions, clock, identities


def test_correct_credentials_create_resolvable_high_entropy_session(tmp_path) -> None:
    auth, _, _, sessions, _, identities = stack(tmp_path)
    result = auth.login(LoginRequest(" AMINA.KHAN@NOVATECH.EXAMPLE ", PASSWORD))

    assert len(result.issued_session.token) == 43
    assert result.issued_session.token not in repr(result)
    principal = auth.resolve_principal(result.issued_session.token)
    assert principal.account.employee_id == "employee-001"
    assert principal.identity.permissions == identities.resolve("employee-001").permissions
    with sqlite3.connect(sessions.path) as connection:
        row = connection.execute("SELECT token_digest FROM authenticated_sessions").fetchone()
    assert row[0] != result.issued_session.token
    assert len(row[0]) == 64


@pytest.mark.parametrize("email,password", (("amina.khan@novatech.example", "wrong password"), ("unknown@novatech.example", PASSWORD)))
def test_bad_credentials_fail_generically(tmp_path, email, password) -> None:
    auth, *_ = stack(tmp_path)
    with pytest.raises(AuthenticationError, match="Authentication failed"):
        auth.login(LoginRequest(email, password))


def test_disabled_account_rejects_login_and_existing_session(tmp_path) -> None:
    auth, accounts, *_ = stack(tmp_path)
    login = auth.login(LoginRequest("amina.khan@novatech.example", PASSWORD))
    assert accounts.set_status(login.principal.account.account_id, AccountStatus.DISABLED)
    with pytest.raises(AuthenticationError): auth.resolve_principal(login.issued_session.token)
    with pytest.raises(AuthenticationError): auth.login(LoginRequest("amina.khan@novatech.example", PASSWORD))


def test_inactive_directory_employee_rejects_login_and_existing_session(tmp_path) -> None:
    auth, _, directory, *_ = stack(tmp_path)
    login = auth.login(LoginRequest("amina.khan@novatech.example", PASSWORD))
    with sqlite3.connect(directory.path) as connection:
        connection.execute("UPDATE employee_directory SET active = 0 WHERE employee_id = 'employee-001'")
    with pytest.raises(AuthenticationError): auth.resolve_principal(login.issued_session.token)
    with pytest.raises(AuthenticationError): auth.login(LoginRequest("amina.khan@novatech.example", PASSWORD))


def test_identity_no_longer_mapping_through_m9_is_rejected(tmp_path) -> None:
    directory = SqliteEmployeeDirectory(tmp_path / "employees.db")
    seed_demo_employee_directory(directory)
    full_bridge = EmployeeDirectoryIdentityService(directory, LocalIdentityProvider())
    accounts = SqliteAccountRepository(tmp_path / "accounts.db")
    RegistrationService(full_bridge, accounts).register(RegistrationRequest("employee-001", "amina.khan@novatech.example", PASSWORD, PASSWORD))
    class RemovedIdentityProvider:
        def resolve(self, subject_id):
            from knowvia.identity import IdentityError
            raise IdentityError("Identity is no longer available")

    empty_bridge = EmployeeDirectoryIdentityService(directory, RemovedIdentityProvider())
    auth = AuthenticationService(accounts, empty_bridge, SqliteAuthenticatedSessionStore(tmp_path / "sessions.db"))
    with pytest.raises(AuthenticationError): auth.login(LoginRequest("amina.khan@novatech.example", PASSWORD))


@pytest.mark.parametrize("token", ("", "not-a-token", "x" * 43, "../" + "x" * 40))
def test_unknown_and_malformed_tokens_are_rejected(tmp_path, token) -> None:
    auth, *_ = stack(tmp_path)
    with pytest.raises(AuthenticationError, match="Authentication required"):
        auth.resolve_principal(token)


def test_expired_session_is_rejected(tmp_path) -> None:
    auth, _, _, _, clock, _ = stack(tmp_path, lifetime=timedelta(minutes=5))
    login = auth.login(LoginRequest("amina.khan@novatech.example", PASSWORD))
    clock.value += timedelta(minutes=6)
    with pytest.raises(AuthenticationError): auth.resolve_principal(login.issued_session.token)


def test_logout_revokes_once_and_never_reactivates(tmp_path) -> None:
    auth, *_ = stack(tmp_path)
    login = auth.login(LoginRequest("amina.khan@novatech.example", PASSWORD))
    assert auth.logout(login.issued_session.token) is True
    assert auth.logout(login.issued_session.token) is False
    with pytest.raises(AuthenticationError): auth.resolve_principal(login.issued_session.token)
