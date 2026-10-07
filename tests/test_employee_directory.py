import sqlite3

import pytest

from knowvia.employee_directory import (
    EmployeeDirectoryConflictError,
    EmployeeDirectoryError,
    EmployeeDirectoryIdentityService,
    EmployeeRecord,
    EmployeeVerificationError,
    SqliteEmployeeDirectory,
    demo_employee_records,
    seed_demo_employee_directory,
)
from knowvia.identity import LocalIdentityProvider


def directory(tmp_path) -> SqliteEmployeeDirectory:
    value = SqliteEmployeeDirectory(tmp_path / "employees.db")
    seed_demo_employee_directory(value)
    return value


def test_demo_employee_lookup_by_id_and_email(tmp_path) -> None:
    store = directory(tmp_path)

    by_id = store.get_by_employee_id("employee-001")
    by_email = store.get_by_work_email("amina.khan@novatech.example")

    assert by_id == by_email
    assert by_id is not None
    assert by_id.department == "Engineering"
    assert by_id.role == "Software Engineer"
    assert by_id.active is True


def test_verification_matches_same_active_employee_and_normalizes_email(tmp_path) -> None:
    store = directory(tmp_path)

    verified = store.verify_active_employee(
        "employee-001", "  AMINA.KHAN@NOVATECH.EXAMPLE  "
    )

    assert verified.employee_id == "employee-001"
    assert verified.work_email == "amina.khan@novatech.example"


@pytest.mark.parametrize(
    ("employee_id", "work_email"),
    (
        ("unknown", "amina.khan@novatech.example"),
        ("employee-001", "unknown@novatech.example"),
        ("employee-001", "daniel.reed@novatech.example"),
        ("../employee-001", "not-an-email"),
        ("", ""),
    ),
)
def test_verification_fails_closed_for_unknown_mismatch_and_malformed_input(
    tmp_path, employee_id, work_email
) -> None:
    store = directory(tmp_path)

    with pytest.raises(EmployeeVerificationError, match="Employee verification failed"):
        store.verify_active_employee(employee_id, work_email)


def test_inactive_employee_cannot_be_verified(tmp_path) -> None:
    store = directory(tmp_path)
    store.add(
        EmployeeRecord(
            "inactive-001", "Alex", "Morgan", "alex.morgan@novatech.example",
            "Operations", "Analyst", active=False,
        )
    )

    assert store.get_by_employee_id("inactive-001").active is False
    with pytest.raises(EmployeeVerificationError):
        store.verify_active_employee("inactive-001", "alex.morgan@novatech.example")


def test_employee_id_and_case_insensitive_email_are_unique(tmp_path) -> None:
    store = directory(tmp_path)
    duplicate_id = EmployeeRecord(
        "employee-001", "Other", "Person", "other.person@novatech.example",
        "Finance", "Analyst",
    )
    duplicate_email = EmployeeRecord(
        "employee-999", "Other", "Person", "AMINA.KHAN@NOVATECH.EXAMPLE",
        "Finance", "Analyst",
    )

    with pytest.raises(EmployeeDirectoryConflictError):
        store.add(duplicate_id)
    with pytest.raises(EmployeeDirectoryConflictError):
        store.add(duplicate_email)


def test_database_enforces_email_uniqueness_case_insensitively(tmp_path) -> None:
    store = directory(tmp_path)
    with sqlite3.connect(store.path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO employee_directory VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "employee-999", "Other", "Person",
                "AMINA.KHAN@NOVATECH.EXAMPLE", "Finance", "Analyst", 1,
            ),
        )


def test_record_rejects_permissions_or_untyped_fields() -> None:
    with pytest.raises(TypeError):
        EmployeeRecord(
            employee_id="employee-999",
            first_name="Other",
            last_name="Person",
            work_email="other.person@novatech.example",
            department="Finance",
            role="Analyst",
            active=True,
            permissions=frozenset({"action.it_ticket.create_own"}),
        )
    with pytest.raises(EmployeeDirectoryError):
        EmployeeRecord(
            "employee-999", "Other", "Person", "invalid-email",
            "Finance", "Analyst",
        )


@pytest.mark.parametrize("employee", demo_employee_records())
def test_verified_directory_employee_maps_to_existing_m9_authority(
    tmp_path, employee
) -> None:
    store = directory(tmp_path)
    provider = LocalIdentityProvider()
    service = EmployeeDirectoryIdentityService(store, provider)

    verified = service.verify(employee.employee_id, employee.work_email)
    existing = provider.resolve(employee.employee_id)

    assert verified.employee == employee
    assert verified.identity.subject_id == existing.subject_id
    assert verified.identity.display_name == employee.display_name
    assert verified.identity.roles == existing.roles
    assert verified.identity.permissions == existing.permissions
    assert verified.identity.identity_source.startswith("employee-directory+")


def test_directory_fields_cannot_escalate_existing_permissions(tmp_path) -> None:
    store = directory(tmp_path)
    store.add(
        EmployeeRecord(
            "employee-without-m9-identity", "Admin", "Claim",
            "admin.claim@novatech.example", "Executive", "Administrator",
        )
    )
    service = EmployeeDirectoryIdentityService(store, LocalIdentityProvider())

    with pytest.raises(EmployeeVerificationError):
        service.verify(
            "employee-without-m9-identity", "admin.claim@novatech.example"
        )
