import sqlite3
from dataclasses import asdict, fields

import pytest

from knowvia.accounts import (
    AccountAlreadyExistsError,
    Argon2idPasswordHashService,
    PasswordPolicyError,
    RegistrationError,
    RegistrationRequest,
    RegistrationService,
    SqliteAccountRepository,
)
from knowvia.employee_directory import (
    EmployeeDirectoryIdentityService,
    EmployeeRecord,
    SqliteEmployeeDirectory,
    seed_demo_employee_directory,
)
from knowvia.identity import LocalIdentityProvider


PASSWORD = "correct horse battery staple"


def registration_stack(tmp_path):
    directory = SqliteEmployeeDirectory(tmp_path / "employees.db")
    seed_demo_employee_directory(directory)
    identities = LocalIdentityProvider()
    directory_identities = EmployeeDirectoryIdentityService(directory, identities)
    accounts = SqliteAccountRepository(tmp_path / "accounts.db")
    service = RegistrationService(directory_identities, accounts)
    return service, accounts, directory, identities


def request(
    employee_id="employee-001",
    email="amina.khan@novatech.example",
    password=PASSWORD,
    confirmation=PASSWORD,
):
    return RegistrationRequest(employee_id, email, password, confirmation)


def test_verified_active_employee_can_register_safe_account(tmp_path) -> None:
    service, accounts, _, identities = registration_stack(tmp_path)

    result = service.register(request(email=" AMINA.KHAN@NOVATECH.EXAMPLE "))

    assert result.account.account_id
    assert result.account.employee_id == "employee-001"
    assert result.account.work_email == "amina.khan@novatech.example"
    assert result.account.status.value == "ACTIVE"
    assert accounts.get_by_employee_id("employee-001") == result.account
    assert result.identity.permissions == identities.resolve("employee-001").permissions
    assert "password" not in asdict(result.account)
    assert "password_hash" not in {field.name for field in fields(result.account)}


def test_password_is_argon2id_hash_only_and_safe_verification_works(tmp_path) -> None:
    service, accounts, _, _ = registration_stack(tmp_path)
    service.register(request())

    with sqlite3.connect(accounts.path) as connection:
        password_hash = connection.execute(
            "SELECT password_hash FROM accounts WHERE employee_id = ?",
            ("employee-001",),
        ).fetchone()[0]

    assert password_hash != PASSWORD
    assert PASSWORD not in password_hash
    assert password_hash.startswith("$argon2id$")
    assert service.verify_password("AMINA.KHAN@NOVATECH.EXAMPLE", PASSWORD) is True
    assert service.verify_password("amina.khan@novatech.example", "incorrect password") is False
    assert service.verify_password("unknown@novatech.example", PASSWORD) is False


def test_same_password_uses_distinct_salts(tmp_path) -> None:
    service, accounts, _, _ = registration_stack(tmp_path)
    service.register(request())
    service.register(
        request(
            employee_id="employee-002",
            email="daniel.reed@novatech.example",
        )
    )

    with sqlite3.connect(accounts.path) as connection:
        hashes = [row[0] for row in connection.execute(
            "SELECT password_hash FROM accounts ORDER BY employee_id"
        )]

    assert len(hashes) == 2
    assert hashes[0] != hashes[1]
    hasher = Argon2idPasswordHashService()
    assert all(hasher.verify(value, PASSWORD) for value in hashes)


def test_duplicate_employee_registration_is_rejected(tmp_path) -> None:
    service, accounts, _, _ = registration_stack(tmp_path)
    first = service.register(request()).account

    with pytest.raises(AccountAlreadyExistsError, match="already exists"):
        service.register(request(password="another secure password", confirmation="another secure password"))

    assert accounts.get_by_employee_id("employee-001") == first


def test_duplicate_normalized_email_is_database_enforced(tmp_path) -> None:
    _, accounts, _, _ = registration_stack(tmp_path)
    hasher = Argon2idPasswordHashService()
    accounts.create(
        "employee-001", "amina.khan@novatech.example", hasher.hash(PASSWORD)
    )

    with pytest.raises(AccountAlreadyExistsError):
        accounts.create(
            "employee-999", " AMINA.KHAN@NOVATECH.EXAMPLE ", hasher.hash(PASSWORD)
        )


@pytest.mark.parametrize(
    "registration_request",
    (
        request(employee_id="unknown", email="unknown@novatech.example"),
        request(email="daniel.reed@novatech.example"),
        request(employee_id="../employee-001"),
        request(email="not-an-email"),
    ),
)
def test_unknown_mismatched_and_malformed_employees_fail_closed(
    tmp_path, registration_request
) -> None:
    service, accounts, _, _ = registration_stack(tmp_path)

    with pytest.raises(RegistrationError, match="Registration could not be completed"):
        service.register(registration_request)

    assert accounts.get_by_employee_id("employee-001") is None


def test_inactive_employee_is_rejected(tmp_path) -> None:
    service, accounts, directory, _ = registration_stack(tmp_path)
    directory.add(
        EmployeeRecord(
            "inactive-001", "Alex", "Morgan", "alex.morgan@novatech.example",
            "Operations", "Analyst", active=False,
        )
    )

    with pytest.raises(RegistrationError, match="Registration could not be completed"):
        service.register(request("inactive-001", "alex.morgan@novatech.example"))

    assert accounts.get_by_employee_id("inactive-001") is None


@pytest.mark.parametrize(
    "registration_request",
    (
        request(password="too short", confirmation="too short"),
        request(password="x" * 129, confirmation="x" * 129),
        request(password=PASSWORD, confirmation="different confirmation"),
    ),
)
def test_password_policy_rejects_short_excessive_and_mismatched_input(
    tmp_path, registration_request
) -> None:
    service, accounts, _, _ = registration_stack(tmp_path)

    with pytest.raises(PasswordPolicyError):
        service.register(registration_request)

    assert accounts.get_by_employee_id("employee-001") is None


def test_registration_request_rejects_client_controlled_identity_fields() -> None:
    forbidden = {
        "department": "Executive",
        "role": "Administrator",
        "permissions": frozenset({"action.it_ticket.create_own"}),
        "authorization_identity": "employee-001",
        "admin": True,
    }
    for name, value in forbidden.items():
        with pytest.raises(TypeError):
            RegistrationRequest(
                employee_id="employee-001",
                work_email="amina.khan@novatech.example",
                password=PASSWORD,
                password_confirmation=PASSWORD,
                **{name: value},
            )


def test_registration_accepts_only_typed_request(tmp_path) -> None:
    service, _, _, _ = registration_stack(tmp_path)

    with pytest.raises(RegistrationError, match="valid registration request"):
        service.register(
            {
                "employee_id": "employee-001",
                "work_email": "amina.khan@novatech.example",
                "password": PASSWORD,
            }
        )


def test_registration_request_representation_never_contains_password() -> None:
    value = request()

    assert PASSWORD not in repr(value)
    assert "password" not in repr(value).lower()


def test_directory_role_does_not_change_m9_permissions(tmp_path) -> None:
    service, _, directory, identities = registration_stack(tmp_path)
    employee = directory.get_by_employee_id("employee-002")
    assert employee.role == "Operations Analyst"

    result = service.register(
        request("employee-002", "daniel.reed@novatech.example")
    )

    expected = identities.resolve("employee-002")
    assert result.identity.roles == expected.roles
    assert result.identity.permissions == expected.permissions
    assert "action.it_ticket.create_own" not in result.identity.permissions
