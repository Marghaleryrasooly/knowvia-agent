from __future__ import annotations

import re
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from knowvia.identity import IdentityContext, IdentityError, IdentityProvider


class EmployeeDirectoryError(ValueError):
    """The trusted employee directory could not safely process a record."""


class EmployeeDirectoryConflictError(EmployeeDirectoryError):
    """A unique employee-directory identifier is already registered."""


class EmployeeVerificationError(EmployeeDirectoryError):
    """The supplied employee credentials did not identify one active employee."""


_EMPLOYEE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_WORK_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def normalize_employee_id(value: str) -> str:
    if not isinstance(value, str):
        raise EmployeeVerificationError("Employee verification failed")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not _EMPLOYEE_ID_PATTERN.fullmatch(normalized):
        raise EmployeeVerificationError("Employee verification failed")
    return normalized


def normalize_work_email(value: str) -> str:
    if not isinstance(value, str):
        raise EmployeeVerificationError("Employee verification failed")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if len(normalized) > 254 or not _WORK_EMAIL_PATTERN.fullmatch(normalized):
        raise EmployeeVerificationError("Employee verification failed")
    return normalized


def _required_text(value: str, field_name: str, maximum: int = 120) -> str:
    if not isinstance(value, str):
        raise EmployeeDirectoryError(f"Employee {field_name} must be text")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized) > maximum:
        raise EmployeeDirectoryError(
            f"Employee {field_name} must be between 1 and {maximum} characters"
        )
    return normalized


@dataclass(frozen=True)
class EmployeeRecord:
    employee_id: str
    first_name: str
    last_name: str
    work_email: str
    department: str
    role: str
    active: bool = True

    def __post_init__(self) -> None:
        try:
            employee_id = normalize_employee_id(self.employee_id)
            work_email = normalize_work_email(self.work_email)
        except EmployeeVerificationError as error:
            raise EmployeeDirectoryError("Employee identifier or work email is invalid") from error
        object.__setattr__(self, "employee_id", employee_id)
        object.__setattr__(self, "work_email", work_email)
        for field_name in ("first_name", "last_name", "department", "role"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if not isinstance(self.active, bool):
            raise EmployeeDirectoryError("Employee active status must be boolean")

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class EmployeeDirectory(Protocol):
    def get_by_employee_id(self, employee_id: str) -> EmployeeRecord | None: ...

    def get_by_work_email(self, work_email: str) -> EmployeeRecord | None: ...

    def verify_active_employee(
        self, employee_id: str, work_email: str
    ) -> EmployeeRecord: ...


class SqliteEmployeeDirectory:
    """Persistent trusted directory; it contains no authorization permissions."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS employee_directory (
                    employee_id TEXT PRIMARY KEY,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    work_email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    department TEXT NOT NULL,
                    role TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK (active IN (0, 1))
                )
                """
            )
            connection.commit()

    def add(self, employee: EmployeeRecord) -> None:
        if not isinstance(employee, EmployeeRecord):
            raise EmployeeDirectoryError("A typed employee record is required")
        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    INSERT INTO employee_directory
                    (employee_id, first_name, last_name, work_email, department, role, active)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        employee.employee_id,
                        employee.first_name,
                        employee.last_name,
                        employee.work_email,
                        employee.department,
                        employee.role,
                        int(employee.active),
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as error:
            raise EmployeeDirectoryConflictError(
                "Employee ID and work email must be unique"
            ) from error

    def seed(self, employees: Iterable[EmployeeRecord]) -> None:
        for employee in employees:
            existing = self.get_by_employee_id(employee.employee_id)
            if existing is None:
                self.add(employee)
            elif existing != employee:
                raise EmployeeDirectoryConflictError(
                    "Demo employee ID already exists with different directory data"
                )

    def get_by_employee_id(self, employee_id: str) -> EmployeeRecord | None:
        try:
            normalized = normalize_employee_id(employee_id)
        except EmployeeVerificationError:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM employee_directory WHERE employee_id = ?",
                (normalized,),
            ).fetchone()
        return self._record(row) if row else None

    def get_by_work_email(self, work_email: str) -> EmployeeRecord | None:
        try:
            normalized = normalize_work_email(work_email)
        except EmployeeVerificationError:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM employee_directory WHERE work_email = ? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
        return self._record(row) if row else None

    def verify_active_employee(
        self, employee_id: str, work_email: str
    ) -> EmployeeRecord:
        try:
            normalized_id = normalize_employee_id(employee_id)
            normalized_email = normalize_work_email(work_email)
        except EmployeeVerificationError as error:
            raise EmployeeVerificationError("Employee verification failed") from error
        employee = self.get_by_employee_id(normalized_id)
        if (
            employee is None
            or employee.work_email != normalized_email
            or not employee.active
        ):
            raise EmployeeVerificationError("Employee verification failed")
        return employee

    @staticmethod
    def _record(row: sqlite3.Row) -> EmployeeRecord:
        return EmployeeRecord(
            employee_id=row["employee_id"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            work_email=row["work_email"],
            department=row["department"],
            role=row["role"],
            active=bool(row["active"]),
        )


@dataclass(frozen=True)
class VerifiedEmployeeIdentity:
    employee: EmployeeRecord
    identity: IdentityContext


class EmployeeDirectoryIdentityService:
    """Maps verified organizational identity into the existing M9 authority."""

    def __init__(
        self, directory: EmployeeDirectory, identity_provider: IdentityProvider
    ) -> None:
        self.directory = directory
        self.identity_provider = identity_provider

    def verify(
        self, employee_id: str, work_email: str
    ) -> VerifiedEmployeeIdentity:
        employee = self.directory.verify_active_employee(employee_id, work_email)
        try:
            authorization_identity = self.identity_provider.resolve(employee.employee_id)
        except IdentityError as error:
            raise EmployeeVerificationError("Employee verification failed") from error
        if not authorization_identity.authenticated:
            raise EmployeeVerificationError("Employee verification failed")
        identity = IdentityContext(
            subject_id=authorization_identity.subject_id,
            display_name=employee.display_name,
            roles=authorization_identity.roles,
            permissions=authorization_identity.permissions,
            authenticated=True,
            identity_source="employee-directory+" + authorization_identity.identity_source,
        )
        return VerifiedEmployeeIdentity(employee, identity)


def demo_employee_records() -> tuple[EmployeeRecord, ...]:
    """Small fictional NovaTech directory used only for development and tests."""
    return (
        EmployeeRecord(
            "employee-001", "Amina", "Khan", "amina.khan@novatech.example",
            "Engineering", "Software Engineer",
        ),
        EmployeeRecord(
            "employee-002", "Daniel", "Reed", "daniel.reed@novatech.example",
            "Operations", "Operations Analyst",
        ),
        EmployeeRecord(
            "it-employee-001", "Priya", "Shah", "priya.shah@novatech.example",
            "Information Technology", "IT Support Specialist",
        ),
        EmployeeRecord(
            "hr-employee-001", "Maya", "Chen", "maya.chen@novatech.example",
            "Human Resources", "HR Specialist",
        ),
    )


def seed_demo_employee_directory(directory: SqliteEmployeeDirectory) -> None:
    directory.seed(demo_employee_records())
