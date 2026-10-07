from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from knowvia.accounts import Account, AccountRepository, AccountStatus, Argon2idPasswordHashService, PasswordHashService, PasswordPolicy
from knowvia.employee_directory import EmployeeDirectoryIdentityService, EmployeeVerificationError
from knowvia.identity import IdentityContext


class AuthenticationError(ValueError):
    """Credentials or session state could not establish a trusted principal."""


@dataclass(frozen=True, repr=False)
class LoginRequest:
    work_email: str
    password: str


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    account: Account
    identity: IdentityContext
    department: str
    organizational_role: str


@dataclass(frozen=True)
class AuthenticatedSession:
    session_id: str
    account_id: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True, repr=False)
class IssuedSession:
    token: str
    session: AuthenticatedSession


@dataclass(frozen=True, repr=False)
class LoginResult:
    principal: AuthenticatedPrincipal
    issued_session: IssuedSession


class SqliteAuthenticatedSessionStore:
    TOKEN_BYTES = 32
    TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
    DEFAULT_LIFETIME = timedelta(hours=8)

    def __init__(self, path: Path, *, lifetime: timedelta = DEFAULT_LIFETIME, now: Callable[[], datetime] | None = None, token_factory: Callable[[int], str] | None = None) -> None:
        if lifetime <= timedelta(0) or lifetime > timedelta(days=7):
            raise ValueError("Session lifetime must be positive and at most seven days")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lifetime = lifetime
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._token_factory = token_factory or secrets.token_urlsafe
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS authenticated_sessions (
                token_digest TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL, created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL, revoked_at TEXT)""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_authenticated_sessions_account ON authenticated_sessions(account_id)")
            connection.commit()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def issue(self, account_id: str) -> IssuedSession:
        if not isinstance(account_id, str) or not account_id.strip():
            raise AuthenticationError("Authentication failed")
        token = self._token_factory(self.TOKEN_BYTES)
        if not isinstance(token, str) or not self.TOKEN_PATTERN.fullmatch(token):
            raise AuthenticationError("Authentication failed")
        created_at = self._now()
        if created_at.tzinfo is None:
            raise AuthenticationError("Authentication failed")
        session = AuthenticatedSession(uuid4().hex, account_id.strip(), created_at, created_at + self.lifetime, None)
        with closing(self._connect()) as connection:
            connection.execute("INSERT INTO authenticated_sessions (token_digest, session_id, account_id, created_at, expires_at, revoked_at) VALUES (?, ?, ?, ?, ?, NULL)", (self._digest(token), session.session_id, session.account_id, session.created_at.isoformat(), session.expires_at.isoformat()))
            connection.commit()
        return IssuedSession(token, session)

    def resolve(self, token: str) -> AuthenticatedSession:
        if not isinstance(token, str) or not self.TOKEN_PATTERN.fullmatch(token):
            raise AuthenticationError("Authentication required")
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT session_id, account_id, created_at, expires_at, revoked_at FROM authenticated_sessions WHERE token_digest = ?", (self._digest(token),)).fetchone()
        if row is None:
            raise AuthenticationError("Authentication required")
        session = self._session(row)
        if session.revoked_at is not None or session.expires_at <= self._now():
            raise AuthenticationError("Authentication required")
        return session

    def revoke(self, token: str) -> bool:
        if not isinstance(token, str) or not self.TOKEN_PATTERN.fullmatch(token):
            return False
        with closing(self._connect()) as connection:
            cursor = connection.execute("UPDATE authenticated_sessions SET revoked_at = ? WHERE token_digest = ? AND revoked_at IS NULL", (self._now().isoformat(), self._digest(token)))
            connection.commit()
        return cursor.rowcount == 1

    @staticmethod
    def _session(row: sqlite3.Row) -> AuthenticatedSession:
        return AuthenticatedSession(row["session_id"], row["account_id"], datetime.fromisoformat(row["created_at"]), datetime.fromisoformat(row["expires_at"]), datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None)


class AuthenticationService:
    def __init__(self, accounts: AccountRepository, directory_identities: EmployeeDirectoryIdentityService, sessions: SqliteAuthenticatedSessionStore, password_hashes: PasswordHashService | None = None) -> None:
        self.accounts = accounts
        self.directory_identities = directory_identities
        self.sessions = sessions
        self.password_hashes = password_hashes or Argon2idPasswordHashService()
        self._dummy_hash = self.password_hashes.hash("Knowvia timing-only dummy secret")

    def login(self, request: LoginRequest) -> LoginResult:
        if not isinstance(request, LoginRequest) or not isinstance(request.password, str) or len(request.password) > PasswordPolicy.MAX_LENGTH:
            raise AuthenticationError("Authentication failed")
        credentials = self.accounts.credentials_by_work_email(request.work_email)
        password_hash = credentials.password_hash if credentials else self._dummy_hash
        password_ok = self.password_hashes.verify(password_hash, request.password)
        if credentials is None or not password_ok or credentials.account.status is not AccountStatus.ACTIVE:
            raise AuthenticationError("Authentication failed")
        principal = self._principal(credentials.account)
        return LoginResult(principal, self.sessions.issue(credentials.account.account_id))

    def resolve_principal(self, token: str) -> AuthenticatedPrincipal:
        session = self.sessions.resolve(token)
        account = self.accounts.get_by_account_id(session.account_id)
        if account is None or account.status is not AccountStatus.ACTIVE:
            raise AuthenticationError("Authentication required")
        return self._principal(account)

    def logout(self, token: str) -> bool:
        return self.sessions.revoke(token)

    def _principal(self, account: Account) -> AuthenticatedPrincipal:
        try:
            verified = self.directory_identities.verify(account.employee_id, account.work_email)
        except EmployeeVerificationError as error:
            raise AuthenticationError("Authentication failed") from error
        return AuthenticatedPrincipal(account, verified.identity, verified.employee.department, verified.employee.role)
