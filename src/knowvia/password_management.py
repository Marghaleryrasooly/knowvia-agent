"""Existing-account invitations, verified recovery and password changes.

Schema initialization is an explicit private maintenance operation, never a web
request migration. Tokens are retained only as digests; links exist in mail only.
"""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import argparse
import hashlib
import re
import secrets
import sqlite3
from urllib.parse import urlencode

from knowvia.accounts import Argon2idPasswordHashService, PasswordPolicy
from knowvia.admin import SqliteAuthority
from knowvia.admin_recovery import existing_root, connection as recovery_connection
from knowvia.employee_directory import normalize_work_email
from knowvia.password_mail import DisabledMail, MailUnavailable, callback_origin


class PasswordManagementError(ValueError):
    pass


class PasswordManagementUnavailable(PasswordManagementError):
    pass


TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9_-]{43}$')
GENERIC_RESET_MESSAGE = 'If this address is eligible, a password-reset email will be sent. Check your inbox and spam folder.'


@contextmanager
def connection(root, *, writable=False):
    with recovery_connection(root, writable=writable) as db:
        db.row_factory = sqlite3.Row
        yield db


def initialize_schema(directory):
    root = existing_root(directory)
    with connection(root, writable=True) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS credential_state (
            account_id TEXT PRIMARY KEY, recovery_email TEXT NOT NULL,
            email_verified_at TEXT, onboarding_pending INTEGER NOT NULL CHECK(onboarding_pending IN (0,1)))''')
        db.execute('''CREATE TABLE IF NOT EXISTS credential_tokens (
            token_digest TEXT PRIMARY KEY, account_id TEXT NOT NULL, purpose TEXT NOT NULL,
            email TEXT NOT NULL, credential_version TEXT NOT NULL, created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, consumed_at TEXT)''')
        db.execute('CREATE INDEX IF NOT EXISTS credential_tokens_account ON credential_tokens(account_id,purpose)')


class PasswordManagement:
    def __init__(self, paths, mail=None, origin=None, now=None):
        self.paths = paths
        self.mail = mail or DisabledMail()
        self.origin = callback_origin(origin) if origin else None
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.hashes = Argon2idPasswordHashService()

    def _schema(self, db):
        try:
            db.execute('SELECT account_id,recovery_email,email_verified_at,onboarding_pending FROM credential_state LIMIT 0')
            db.execute('SELECT token_digest,credential_version,expires_at,consumed_at FROM credential_tokens LIMIT 0')
        except sqlite3.Error:
            raise PasswordManagementUnavailable('Password-management storage requires private initialization.') from None

    def states(self):
        with connection(self.paths.data_dir) as db:
            try:
                rows = db.execute('''SELECT a.employee_id,s.onboarding_pending FROM credential_state s
                    JOIN account.accounts a ON a.account_id=s.account_id''').fetchall()
            except sqlite3.Error:
                return {}
        return {r['employee_id']:bool(r['onboarding_pending']) for r in rows}

    @staticmethod
    def _account(db, email=None, account_id=None):
        field, value = ('a.work_email', email) if email is not None else ('a.account_id', account_id)
        row = db.execute(f'''SELECT a.account_id,a.employee_id,a.work_email,a.password_hash
            FROM account.accounts a JOIN directory.employee_directory d ON a.employee_id=d.employee_id
            WHERE {field}=? AND a.status='ACTIVE' AND d.active=1
            AND a.work_email=d.work_email COLLATE BINARY''', (value,)).fetchone()
        if row is None:
            raise PasswordManagementError('Password request could not be completed.')
        return row

    @staticmethod
    def _version(row):
        return hashlib.sha256(row['password_hash'].encode()).hexdigest()

    @staticmethod
    def _invalidate(db, account_id, now):
        db.execute('UPDATE credential_tokens SET consumed_at=? WHERE account_id=? AND consumed_at IS NULL', (now, account_id))

    def _send(self, row, purpose):
        if not self.mail.enabled or not self.origin:
            raise MailUnavailable('Email delivery is not configured.')
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode('ascii')).hexdigest()
        now = self.now()
        with connection(self.paths.data_dir, writable=True) as db:
            self._schema(db)
            current = self._account(db, account_id=row['account_id'])
            if self._version(current) != self._version(row):
                raise PasswordManagementError('Password request could not be completed.')
            state = db.execute('SELECT * FROM credential_state WHERE account_id=?', (row['account_id'],)).fetchone()
            if (state is None or state['recovery_email'] != row['work_email']
                    or (purpose == 'invite' and not state['onboarding_pending'])
                    or (purpose == 'reset' and (state['onboarding_pending'] or not state['email_verified_at']))
                    or (purpose == 'verify' and state['onboarding_pending'])):
                raise PasswordManagementError('Password request could not be completed.')
            # One active link per account. A resend invalidates all older links.
            self._invalidate(db, row['account_id'], now.isoformat())
            db.execute('INSERT INTO credential_tokens VALUES (?,?,?,?,?,?,?,NULL)',
                       (digest, row['account_id'], purpose, row['work_email'], self._version(row),
                        now.isoformat(), (now + timedelta(hours=24) if purpose=='invite' else now+timedelta(minutes=30)).isoformat()))
        link = self.origin + '/password#' + urlencode({'purpose':purpose, 'token':token})
        try:
            self.mail.send(row['work_email'], purpose, link)
        except Exception:
            # A failed/uncertain send leaves no usable bearer link. No retries.
            with connection(self.paths.data_dir, writable=True) as db:
                db.execute('UPDATE credential_tokens SET consumed_at=? WHERE token_digest=?', (self.now().isoformat(), digest))
                SqliteAuthority._event(db, 'password-service', row['employee_id'], 'MAIL_DELIVERY_FAILED')
            raise MailUnavailable('Email delivery could not be confirmed.') from None

    def invite(self, authority, actor, employee, roles):
        if not self.mail.enabled or not self.origin:
            raise MailUnavailable('Email delivery is not configured; no account was created.')
        roles = authority._roles(roles)
        # The invitation flow provisions employees only; administrators retain
        # private provisioning and existing administrative protections.
        if 'administrator' in roles:
            raise PasswordManagementError('Administrator invitation is not available.')
        with connection(self.paths.data_dir, writable=True) as db:
            self._schema(db)
            authority._require(db, actor, 'admin.accounts.manage')
            authority._require(db, actor, 'admin.roles.manage')
        random_password = secrets.token_urlsafe(48)
        hashed = self.hashes.hash(random_password)
        with connection(self.paths.data_dir, writable=True) as db:
            self._schema(db)
            authority._require(db, actor, 'admin.accounts.manage')
            authority._require(db, actor, 'admin.roles.manage')
            authority._insert_employee(db, employee, hashed)
            authority._assign(db, employee.employee_id, roles)
            row = self._account(db, email=employee.work_email)
            db.execute('INSERT INTO credential_state VALUES (?,?,NULL,1)', (row['account_id'], row['work_email']))
            authority._event(db, actor, employee.employee_id, 'INVITE_ACCOUNT', roles)
        try:
            self._send(row, 'invite')
            return {'saved':True, 'mail_sent':True}
        except MailUnavailable:
            return {'saved':True, 'mail_sent':False}

    def resend_invitation(self, authority, actor, employee_id):
        with connection(self.paths.data_dir) as db:
            self._schema(db)
            authority._require(db, actor, 'admin.accounts.manage')
            selected = db.execute('SELECT work_email FROM account.accounts WHERE employee_id=?', (employee_id,)).fetchone()
            if selected is None: raise PasswordManagementError('Invitation is unavailable.')
            row = self._account(db, email=selected[0])
        self._send(row, 'invite')
        with connection(self.paths.data_dir, writable=True) as db:
            authority._event(db, actor, employee_id, 'INVITATION_RESENT')

    def _verified_current(self, account_id, password):
        if not isinstance(password, str) or not 1 <= len(password) <= 128:
            raise PasswordManagementError('Current password could not be verified.')
        with connection(self.paths.data_dir) as db:
            self._schema(db)
            row = self._account(db, account_id=account_id)
        if not self.hashes.verify(row['password_hash'], password):
            raise PasswordManagementError('Current password could not be verified.')
        return row

    def request_verification(self, account_id, password):
        row = self._verified_current(account_id, password)
        with connection(self.paths.data_dir, writable=True) as db:
            current = self._account(db, account_id=account_id)
            if self._version(current) != self._version(row): raise PasswordManagementError('Password changed; sign in again.')
            db.execute('INSERT OR IGNORE INTO credential_state VALUES (?,?,NULL,0)', (account_id, row['work_email']))
        self._send(row, 'verify')

    def request_reset(self, email):
        # Called after the generic HTTP response; delivery/errors never disclose
        # existence, activation, onboarding or verification state to the caller.
        try:
            email = normalize_work_email(email)
            with connection(self.paths.data_dir) as db:
                self._schema(db)
                row = self._account(db, email=email)
            self._send(row, 'reset')
        except Exception:
            return

    def change(self, account_id, session_token, current_password, password, confirmation):
        PasswordPolicy().validate(password, confirmation)
        row = self._verified_current(account_id, current_password)
        hashed = self.hashes.hash(password)
        now = self.now().isoformat()
        with connection(self.paths.data_dir, writable=True) as db:
            current = self._account(db, account_id=account_id)
            session = db.execute('SELECT * FROM session.authenticated_sessions WHERE token_digest=? AND account_id=?',
                                 (hashlib.sha256(session_token.encode('ascii')).hexdigest(), account_id)).fetchone()
            if (session is None or session['revoked_at'] or session['expires_at'] <= now
                    or self._version(current) != self._version(row)):
                raise PasswordManagementError('Sign in again before changing your password.')
            self._replace(db, row, hashed, now, 'CHANGE_PASSWORD')

    def _replace(self, db, row, hashed, now, operation):
        db.execute('UPDATE account.accounts SET password_hash=? WHERE account_id=?', (hashed, row['account_id']))
        db.execute('UPDATE session.authenticated_sessions SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL', (now, row['account_id']))
        self._invalidate(db, row['account_id'], now)
        SqliteAuthority._event(db, 'password-service', row['employee_id'], operation)

    def _token_target(self, db, token, purpose, now):
        self._schema(db)
        stored = db.execute('SELECT * FROM credential_tokens WHERE token_digest=? AND purpose=?',
                                (hashlib.sha256(token.encode('ascii')).hexdigest(), purpose)).fetchone()
        if stored is None or stored['consumed_at'] or stored['expires_at'] <= now:
            raise PasswordManagementError('This link is invalid or expired.')
        row = self._account(db, account_id=stored['account_id'])
        state = db.execute('SELECT * FROM credential_state WHERE account_id=?', (row['account_id'],)).fetchone()
        if (state is None or stored['email'] != row['work_email'] or state['recovery_email'] != row['work_email']
                    or stored['credential_version'] != self._version(row)
                    or (purpose=='invite' and not state['onboarding_pending'])
                    or (purpose=='reset' and (state['onboarding_pending'] or not state['email_verified_at']))):
            raise PasswordManagementError('This link is invalid or expired.')
        return stored, row

    def redeem(self, token, purpose, password=None, confirmation=None):
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token) or purpose not in {'invite','reset','verify'}:
            raise PasswordManagementError('This link is invalid or expired.')
        with connection(self.paths.data_dir) as db:
            self._token_target(db, token, purpose, self.now().isoformat())
        if purpose != 'verify':
            PasswordPolicy().validate(password, confirmation)
            hashed = self.hashes.hash(password)
        with connection(self.paths.data_dir, writable=True) as db:
            now = self.now().isoformat()
            stored, row = self._token_target(db, token, purpose, now)
            if purpose == 'verify':
                db.execute('UPDATE credential_tokens SET consumed_at=? WHERE token_digest=?', (now, stored['token_digest']))
                db.execute('UPDATE credential_state SET email_verified_at=? WHERE account_id=?', (now, row['account_id']))
                SqliteAuthority._event(db, 'password-service', row['employee_id'], 'VERIFY_RECOVERY_EMAIL')
            else:
                self._replace(db, row, hashed, now, 'REDEEM_'+purpose.upper())
                db.execute('UPDATE credential_state SET onboarding_pending=0,email_verified_at=COALESCE(email_verified_at,?) WHERE account_id=?',
                           (now, row['account_id']))


def main():
    parser = argparse.ArgumentParser(description='Private additive password schema initialization; maintenance required.')
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--initialize', action='store_true', required=True)
    parser.add_argument('--maintenance-confirmed', action='store_true', required=True)
    args = parser.parse_args()
    try:
        initialize_schema(args.data_dir)
    except Exception:
        raise SystemExit('Password schema initialization failed; no details are displayed.') from None
    print('Password-management schema initialized. No existing email was marked verified.')


if __name__ == '__main__': main()
