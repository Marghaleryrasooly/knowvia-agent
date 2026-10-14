"""Trusted local administration foundation; no public bootstrap or model authority."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from getpass import getpass
import hashlib
import json
import sqlite3
from threading import Lock
from time import monotonic
from uuid import uuid4

from knowvia.accounts import Argon2idPasswordHashService, PasswordPolicy, SqliteAccountRepository
from knowvia.authentication import SqliteAuthenticatedSessionStore
from knowvia.employee_directory import EmployeeRecord, SqliteEmployeeDirectory, demo_employee_records
from knowvia.identity import IdentityContext, IdentityError, default_local_identities
from knowvia.runtime_paths import RuntimePaths


ADMIN_PERMISSIONS = frozenset({"admin.accounts.manage", "admin.roles.manage", "admin.audit.read", "admin.knowledge.manage"})
ROLE_PERMISSIONS = {
    "employee": frozenset(),
    "knowledge-reader": frozenset({"knowledge.hr.read", "knowledge.it.read", "knowledge.finance.read"}),
    "hr-reader": frozenset({"knowledge.hr.read"}),
    "it-reader": frozenset({"knowledge.it.read"}),
    "finance-reader": frozenset({"knowledge.finance.read"}),
    "it-ticket-requester": frozenset({"action.it_ticket.create_own", "action.it_ticket.read_own"}),
    "administrator": ADMIN_PERMISSIONS,
}


class AdministrationError(ValueError):
    """Safe category, never stored credentials or SQL errors."""


class SqliteAuthority:
    """One trusted authority and transaction coordinator for administration.

    Attached SQLite databases share one BEGIN IMMEDIATE administration lock.
    Runtime state must be on a local durable filesystem, not a network share.
    """
    def __init__(self, paths: RuntimePaths):
        self.paths = paths
        self.directory = SqliteEmployeeDirectory(paths.employee_directory)
        self.accounts = SqliteAccountRepository(paths.accounts)
        self.sessions = SqliteAuthenticatedSessionStore(paths.authenticated_sessions)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS authority_subjects (employee_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS role_assignments (employee_id TEXT, role TEXT,
                    PRIMARY KEY(employee_id, role));
                CREATE TABLE IF NOT EXISTS authority_meta (name TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS administration_events (event_id TEXT PRIMARY KEY,
                    timestamp TEXT, actor TEXT, target TEXT, operation TEXT, roles TEXT);
            """)
        # Preserve the pre-existing trusted four-identity mapping for existing
        # directory records, even when demo seeding is disabled. Never create
        # directory employees here or overwrite an administered assignment.
        with self.transaction() as db:
            for identity in default_local_identities():
                if (db.execute("SELECT 1 FROM directory.employee_directory WHERE employee_id = ?", (identity.subject_id,)).fetchone()
                        and not db.execute("SELECT 1 FROM authority_subjects WHERE employee_id = ?", (identity.subject_id,)).fetchone()):
                    self._assign(db, identity.subject_id, self._legacy_roles(identity))

    @staticmethod
    def _legacy_roles(identity):
        roles = ["employee"]
        if "knowledge.hr.read" in identity.permissions and "knowledge.it.read" in identity.permissions:
            roles.append("knowledge-reader")
        else:
            roles += [r for r, p in [("hr-reader", "knowledge.hr.read"), ("it-reader", "knowledge.it.read")] if p in identity.permissions]
        if "action.it_ticket.create_own" in identity.permissions: roles.append("it-ticket-requester")
        return roles

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.paths.authority, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            for alias, path in [("directory", self.paths.employee_directory), ("account", self.paths.accounts), ("session", self.paths.authenticated_sessions)]:
                db.execute(f"ATTACH DATABASE ? AS {alias}", (str(path),))
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _roles(roles):
        if not isinstance(roles, (list, tuple)) or not roles or not all(isinstance(r, str) and r in ROLE_PERMISSIONS for r in roles):
            raise AdministrationError("Invalid trusted role assignment")
        return tuple(sorted(set(roles)))

    @staticmethod
    def _assign(db, employee_id, roles):
        db.execute("INSERT OR IGNORE INTO authority_subjects VALUES (?)", (employee_id,))
        db.execute("DELETE FROM role_assignments WHERE employee_id = ?", (employee_id,))
        db.executemany("INSERT INTO role_assignments VALUES (?, ?)", [(employee_id, r) for r in roles])

    @staticmethod
    def _event(db, actor, target, operation, roles=()):
        # Only allowlisted metadata; no password, email, token, prompt or request body.
        db.execute("INSERT INTO administration_events VALUES (?, ?, ?, ?, ?, ?)",
                   (uuid4().hex, datetime.now(timezone.utc).isoformat(), actor, target, operation, json.dumps(list(roles))))

    def seed_demo_once(self):
        identities = {i.subject_id: i for i in default_local_identities()}
        with self.transaction() as db:
            for employee in demo_employee_records():
                if not db.execute("SELECT 1 FROM directory.employee_directory WHERE employee_id = ?", (employee.employee_id,)).fetchone():
                    db.execute("INSERT INTO directory.employee_directory VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (employee.employee_id, employee.first_name, employee.last_name, employee.work_email, employee.department, employee.role, int(employee.active)))
                if not db.execute("SELECT 1 FROM authority_subjects WHERE employee_id = ?", (employee.employee_id,)).fetchone():
                    self._assign(db, employee.employee_id, self._legacy_roles(identities[employee.employee_id]))

    def _resolve(self, db, employee_id):
        employee = db.execute("SELECT * FROM directory.employee_directory WHERE employee_id = ? AND active = 1", (employee_id,)).fetchone()
        account = db.execute("SELECT status FROM account.accounts WHERE employee_id = ?", (employee_id,)).fetchone()
        roles = frozenset(r[0] for r in db.execute("SELECT role FROM role_assignments WHERE employee_id = ?", (employee_id,)))
        if employee is None or not roles or not roles <= ROLE_PERMISSIONS.keys() or (account and account[0] != "ACTIVE"):
            raise IdentityError("Trusted identity is unavailable")
        permissions = frozenset().union(*(ROLE_PERMISSIONS[r] for r in roles))
        return IdentityContext(employee_id, employee["first_name"] + " " + employee["last_name"], roles, permissions, True, "persistent-authority")

    def resolve(self, subject_id):
        with self.transaction() as db:
            return self._resolve(db, subject_id)

    def list_identities(self):
        values = []
        with self.transaction() as db:
            for row in db.execute("SELECT employee_id FROM authority_subjects ORDER BY employee_id").fetchall():
                try: values.append(self._resolve(db, row[0]))
                except IdentityError: pass
        return tuple(values)

    def _require(self, db, actor, permission):
        identity = self._resolve(db, actor)
        account = db.execute("SELECT 1 FROM account.accounts WHERE employee_id = ? AND status = 'ACTIVE'", (actor,)).fetchone()
        if account is None or permission not in identity.permissions:
            raise AdministrationError("Administrator permission required")

    @staticmethod
    def _last_admin(db, target):
        rows = db.execute("""SELECT DISTINCT r.employee_id FROM role_assignments r
            JOIN account.accounts a ON a.employee_id = r.employee_id
            JOIN directory.employee_directory d ON d.employee_id = r.employee_id
            WHERE r.role = 'administrator' AND a.status = 'ACTIVE' AND d.active = 1""").fetchall()
        if len(rows) == 1 and rows[0][0] == target:
            raise AdministrationError("Cannot remove the last active administrator")

    def bootstrap(self, employee: EmployeeRecord, password: str, confirmation: str):
        if not employee.active: raise AdministrationError("Bootstrap administrator must be active")
        PasswordPolicy().validate(password, confirmation)
        hashed = Argon2idPasswordHashService().hash(password)
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM authority_meta WHERE name = 'admin_bootstrapped'").fetchone() or db.execute("SELECT 1 FROM role_assignments WHERE role = 'administrator'").fetchone():
                raise AdministrationError("Administrator bootstrap is already complete")
            self._insert_employee(db, employee, hashed)
            self._assign(db, employee.employee_id, ["employee", "administrator"])
            db.execute("INSERT INTO authority_meta VALUES ('admin_bootstrapped', '1')")
            self._event(db, "private-bootstrap", employee.employee_id, "BOOTSTRAP")

    @staticmethod
    def _insert_employee(db, employee, hashed):
        db.execute("INSERT INTO directory.employee_directory VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (employee.employee_id, employee.first_name, employee.last_name, employee.work_email, employee.department, employee.role, int(employee.active)))
        db.execute("INSERT INTO account.accounts VALUES (?, ?, ?, ?, ?, ?)",
                   (uuid4().hex, employee.employee_id, employee.work_email, hashed, datetime.now(timezone.utc).isoformat(), "ACTIVE" if employee.active else "DISABLED"))

    def provision(self, actor, employee: EmployeeRecord, password, confirmation, roles):
        roles = self._roles(roles)
        PasswordPolicy().validate(password, confirmation)
        # Check authority before expensive password hashing, then again transactionally.
        with self.transaction() as db:
            self._require(db, actor, "admin.accounts.manage")
            self._require(db, actor, "admin.roles.manage")
        hashed = Argon2idPasswordHashService().hash(password)
        with self.transaction() as db:
            self._require(db, actor, "admin.accounts.manage")
            self._require(db, actor, "admin.roles.manage")
            self._insert_employee(db, employee, hashed)
            self._assign(db, employee.employee_id, roles)
            self._event(db, actor, employee.employee_id, "PROVISION", roles)

    def set_active(self, actor, target, active):
        if type(active) is not bool: raise AdministrationError("Invalid activation state")
        with self.transaction() as db:
            self._require(db, actor, "admin.accounts.manage")
            if not active: self._last_admin(db, target)
            account = db.execute("SELECT account_id FROM account.accounts WHERE employee_id = ?", (target,)).fetchone()
            if account is None: raise AdministrationError("Account is unavailable")
            db.execute("UPDATE directory.employee_directory SET active = ? WHERE employee_id = ?", (int(active), target))
            db.execute("UPDATE account.accounts SET status = ? WHERE employee_id = ?", ("ACTIVE" if active else "DISABLED", target))
            if not active:
                db.execute("UPDATE session.authenticated_sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL", (datetime.now(timezone.utc).isoformat(), account[0]))
            self._event(db, actor, target, "ACTIVATE" if active else "DEACTIVATE")

    def assign_roles(self, actor, target, roles):
        roles = self._roles(roles)
        with self.transaction() as db:
            self._require(db, actor, "admin.roles.manage")
            if "administrator" not in roles: self._last_admin(db, target)
            if not db.execute("SELECT 1 FROM directory.employee_directory WHERE employee_id = ?", (target,)).fetchone():
                raise AdministrationError("Employee is unavailable")
            if "administrator" in roles and not db.execute("SELECT 1 FROM account.accounts WHERE employee_id = ?", (target,)).fetchone():
                raise AdministrationError("Privileged accounts require private provisioning")
            self._assign(db, target, roles)
            self._event(db, actor, target, "ASSIGN_ROLES", roles)

    def events(self, actor):
        with self.transaction() as db:
            self._require(db, actor, "admin.audit.read")
            return [dict(r) for r in db.execute("SELECT event_id, timestamp, actor, target, operation, roles FROM administration_events ORDER BY timestamp DESC LIMIT 100")]

    def account_listing(self, actor):
        """Only explicit administrative directory metadata; never account secrets."""
        with self.transaction() as db:
            self._require(db, actor, "admin.accounts.manage")
            rows = db.execute("""SELECT d.employee_id, d.first_name, d.last_name,
                d.work_email, d.department, d.role, d.active,
                COALESCE(a.status, 'NOT_REGISTERED') AS account_status
                FROM directory.employee_directory d
                LEFT JOIN account.accounts a ON a.employee_id = d.employee_id
                ORDER BY d.employee_id""").fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["active"] = bool(value["active"])
                value["roles"] = [r[0] for r in db.execute(
                    "SELECT role FROM role_assignments WHERE employee_id = ? ORDER BY role", (row["employee_id"],))]
                result.append(value)
            return result

    def role_catalog(self, actor):
        with self.transaction() as db:
            self._require(db, actor, "admin.roles.manage")
            return [{"name": name, "permissions": sorted(permissions)}
                    for name, permissions in sorted(ROLE_PERMISSIONS.items())]


class LoginAbuseGuard:
    """Bounded single-process rate limit, keyed by digests, not raw identifiers."""
    def __init__(self, limit=10, window=300, clock=monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._entries, self._lock = {}, Lock()

    def allow(self, client, email):
        if not isinstance(email, str) or len(email) > 254: return False
        now = self.clock()
        keys = [hashlib.sha256(str(v).encode()).hexdigest() for v in ("ip:" + client, "account:" + str(email).casefold())]
        with self._lock:
            self._entries = {k: v for k, v in self._entries.items() if now - v[0] < self.window}
            if len(self._entries) > 10000: return False
            if any(self._entries.get(k, (now, 0))[1] >= self.limit for k in keys): return False
            for k in keys:
                start, count = self._entries.get(k, (now, 0))
                self._entries[k] = (start, count + 1)
            return True


def main():
    from dotenv import load_dotenv
    import sys
    load_dotenv()
    parser = argparse.ArgumentParser(description="Private first-admin bootstrap; never expose as HTTP")
    parser.add_argument("--employee-id", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--first-name", required=True)
    parser.add_argument("--last-name", required=True)
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise SystemExit("Bootstrap requires a private interactive terminal for hidden password input")
    try:
        paths = RuntimePaths.from_environment()
        paths.ensure_writable()
        SqliteAuthority(paths).bootstrap(
            EmployeeRecord(args.employee_id, args.first_name, args.last_name, args.email, "Administration", "Administrator"),
            getpass("New administrator password: "), getpass("Confirm password: "),
        )
    except Exception:
        raise SystemExit("Administrator bootstrap failed; no credential details are displayed") from None
    print("Administrator bootstrap complete")


if __name__ == "__main__": main()
