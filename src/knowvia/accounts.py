from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from knowvia.employee_directory import (
    EmployeeDirectoryIdentityService,
    EmployeeVerificationError,
    normalize_employee_id,
    normalize_work_email,
)
from knowvia.identity import IdentityContext


class RegistrationError(ValueError):
    """Registration could not safely create an employee account."""


class AccountAlreadyExistsError(RegistrationError):
    """The verified employee already has a Knowvia account."""


class PasswordPolicyError(RegistrationError):
    """A password does not meet Knowvia's bounded password policy."""


class AccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


@dataclass(frozen=True, repr=False)
class RegistrationRequest:
    employee_id: str
    work_email: str
    password: str
    password_confirmation: str


@dataclass(frozen=True)
class Account:
    account_id: str
    employee_id: str
    work_email: str
    created_at: datetime
    status: AccountStatus


@dataclass(frozen=True)
class RegistrationResult:
    account: Account
    identity: IdentityContext


@dataclass(frozen=True, repr=False)
class _StoredCredentials:
    account: Account
    password_hash: str


class AccountRepository(Protocol):
    def create(
        self, employee_id: str, work_email: str, password_hash: str
    ) -> Account: ...

    def get_by_employee_id(self, employee_id: str) -> Account | None: ...

    def get_by_account_id(self, account_id: str) -> Account | None: ...

    def credentials_by_work_email(
        self, work_email: str
    ) -> _StoredCredentials | None: ...


class PasswordHashService(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, password_hash: str, password: str) -> bool: ...


class Argon2idPasswordHashService:
    """Small wrapper around argon2-cffi's established Argon2id implementation."""

    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher(type=Type.ID)

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError, TypeError):
            return False


class PasswordPolicy:
    MIN_LENGTH = 12
    MAX_LENGTH = 128

    def validate(self, password: str, confirmation: str) -> None:
        if not isinstance(password, str) or not isinstance(confirmation, str):
            raise PasswordPolicyError("Password does not meet the registration policy")
        if password != confirmation:
            raise PasswordPolicyError("Password does not meet the registration policy")
        if not self.MIN_LENGTH <= len(password) <= self.MAX_LENGTH:
            raise PasswordPolicyError("Password does not meet the registration policy")


class SqliteAccountRepository:
    """Persistent account data; password hashes never enter safe Account objects."""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], datetime] | None = None,
        account_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._account_id_factory = account_id_factory or (lambda: uuid4().hex)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    employee_id TEXT NOT NULL UNIQUE,
                    work_email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'DISABLED'))
                )
                """
            )
            connection.commit()

    def create(
        self, employee_id: str, work_email: str, password_hash: str
    ) -> Account:
        try:
            normalized_id = normalize_employee_id(employee_id)
            normalized_email = normalize_work_email(work_email)
        except EmployeeVerificationError as error:
            raise RegistrationError("Registration could not be completed") from error
        if not isinstance(password_hash, str) or not password_hash.startswith("$argon2id$"):
            raise RegistrationError("A valid password hash is required")
        account_id = self._account_id_factory()
        if not isinstance(account_id, str) or not account_id.strip():
            raise RegistrationError("A stable account identifier is required")
        created_at = self._now()
        if created_at.tzinfo is None:
            raise RegistrationError("Account timestamps must be timezone-aware")
        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    INSERT INTO accounts
                    (account_id, employee_id, work_email, password_hash, created_at, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id.strip(), normalized_id, normalized_email,
                        password_hash, created_at.isoformat(), AccountStatus.ACTIVE.value,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as error:
            raise AccountAlreadyExistsError("An account already exists") from error
        return Account(
            account_id.strip(), normalized_id, normalized_email,
            created_at, AccountStatus.ACTIVE,
        )

    def get_by_employee_id(self, employee_id: str) -> Account | None:
        try:
            normalized = normalize_employee_id(employee_id)
        except EmployeeVerificationError:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT account_id, employee_id, work_email, created_at, status "
                "FROM accounts WHERE employee_id = ?",
                (normalized,),
            ).fetchone()
        return self._account(row) if row else None

    def get_by_account_id(self, account_id: str) -> Account | None:
        if not isinstance(account_id, str) or not account_id.strip():
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT account_id, employee_id, work_email, created_at, status "
                "FROM accounts WHERE account_id = ?",
                (account_id.strip(),),
            ).fetchone()
        return self._account(row) if row else None

    def set_status(self, account_id: str, status: AccountStatus) -> bool:
        if not isinstance(status, AccountStatus):
            raise RegistrationError("A recognized account status is required")
        if not isinstance(account_id, str) or not account_id.strip():
            return False
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE accounts SET status = ? WHERE account_id = ?",
                (status.value, account_id.strip()),
            )
            connection.commit()
        return cursor.rowcount == 1

    def credentials_by_work_email(
        self, work_email: str
    ) -> _StoredCredentials | None:
        try:
            normalized = normalize_work_email(work_email)
        except EmployeeVerificationError:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE work_email = ? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        return _StoredCredentials(self._account(row), row["password_hash"])

    @staticmethod
    def _account(row: sqlite3.Row) -> Account:
        return Account(
            account_id=row["account_id"],
            employee_id=row["employee_id"],
            work_email=row["work_email"],
            created_at=datetime.fromisoformat(row["created_at"]),
            status=AccountStatus(row["status"]),
        )


class RegistrationService:
    def __init__(
        self,
        directory_identity_service: EmployeeDirectoryIdentityService,
        accounts: AccountRepository,
        password_hashes: PasswordHashService | None = None,
        password_policy: PasswordPolicy | None = None,
    ) -> None:
        self.directory_identity_service = directory_identity_service
        self.accounts = accounts
        self.password_hashes = password_hashes or Argon2idPasswordHashService()
        self.password_policy = password_policy or PasswordPolicy()

    def register(self, request: RegistrationRequest) -> RegistrationResult:
        if not isinstance(request, RegistrationRequest):
            raise RegistrationError("A valid registration request is required")
        self.password_policy.validate(request.password, request.password_confirmation)
        try:
            verified = self.directory_identity_service.verify(
                request.employee_id, request.work_email
            )
        except EmployeeVerificationError as error:
            raise RegistrationError("Registration could not be completed") from error
        password_hash = self.password_hashes.hash(request.password)
        account = self.accounts.create(
            verified.employee.employee_id,
            verified.employee.work_email,
            password_hash,
        )
        return RegistrationResult(account, verified.identity)

    def verify_password(self, work_email: str, password: str) -> bool:
        if not isinstance(password, str) or len(password) > PasswordPolicy.MAX_LENGTH:
            return False
        credentials = self.accounts.credentials_by_work_email(work_email)
        if credentials is None or credentials.account.status is not AccountStatus.ACTIVE:
            return False
        return self.password_hashes.verify(credentials.password_hash, password)
