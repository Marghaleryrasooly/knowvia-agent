"""Private operator recovery only. Never import into an HTTP route."""
from contextlib import contextmanager
from datetime import datetime, timezone
import argparse
import getpass
from pathlib import Path
import sqlite3
import sys
import warnings

from knowvia.accounts import Argon2idPasswordHashService, PasswordPolicy
from knowvia.admin import ROLE_PERMISSIONS, SqliteAuthority


class RecoveryError(ValueError):
    """Sanitized recovery failure, never provider or database details."""


def existing_root(value):
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise RecoveryError('An existing absolute data directory is required.')
    root = path.resolve(strict=True)
    if str(root).startswith(('\\\\', '//')):
        raise RecoveryError('Recovery requires local durable storage, not a network path.')
    files = [root / name for name in ('authority.db', 'accounts.db',
             'employee_directory.db', 'authenticated_sessions.db')]
    if not all(p.is_file() for p in files) or len({p.resolve() for p in files}) != 4:
        raise RecoveryError('Required existing databases are unavailable.')
    return root


@contextmanager
def connection(root, *, writable=False):
    db = None
    try:
        mode = 'rw' if writable else 'ro'
        db = sqlite3.connect((root / 'authority.db').as_uri() + '?mode=' + mode, uri=True, timeout=10)
        for alias, name in (('account', 'accounts.db'), ('directory', 'employee_directory.db'),
                            ('session', 'authenticated_sessions.db')):
            db.execute(f'ATTACH DATABASE ? AS {alias}', ((root / name).as_uri() + '?mode=' + mode,))
        # Cross-file atomic commits require disk rollback journals, not WAL.
        for alias in ('main', 'account', 'directory', 'session'):
            if db.execute(f'PRAGMA {alias}.journal_mode').fetchone()[0].lower() not in {'delete', 'truncate', 'persist'}:
                raise RecoveryError('Recovery requires supported rollback-journal databases.')
            if db.execute(f'PRAGMA {alias}.quick_check').fetchall() != [('ok',)]:
                raise RecoveryError('Database integrity validation failed.')
            if writable:
                db.execute(f'PRAGMA {alias}.synchronous=FULL')
        # Validate all required existing structures without schema initialization.
        db.execute('SELECT account_id, employee_id, work_email, status, password_hash FROM account.accounts LIMIT 0')
        db.execute('SELECT employee_id, work_email, active FROM directory.employee_directory LIMIT 0')
        db.execute('SELECT account_id, revoked_at FROM session.authenticated_sessions LIMIT 0')
        db.execute('SELECT employee_id, role FROM role_assignments LIMIT 0')
        db.execute('SELECT event_id,timestamp,actor,target,operation,roles FROM administration_events LIMIT 0')
        if writable:
            db.execute('BEGIN IMMEDIATE')
        else:
            db.execute('PRAGMA query_only=ON')
        yield db
        if writable:
            db.commit()
    except sqlite3.Error:
        raise RecoveryError('Recovery database operation failed; no changes committed.') from None
    finally:
        if db is not None:
            db.rollback()
            db.close()


def target(db, email):
    # Require exact stored email, not a fuzzy or case-folded target selection.
    row = db.execute('''SELECT a.account_id,a.employee_id,a.work_email
        FROM account.accounts a JOIN directory.employee_directory d
        ON a.employee_id=d.employee_id
        WHERE a.work_email=? COLLATE BINARY AND d.work_email=a.work_email COLLATE BINARY
        AND a.status='ACTIVE' AND d.active=1''', (email,)).fetchone()
    if row is None:
        raise RecoveryError('Target is not an active trusted administrator.')
    roles = {r[0] for r in db.execute('SELECT role FROM role_assignments WHERE employee_id=?', (row[1],))}
    if 'administrator' not in roles or not roles <= ROLE_PERMISSIONS.keys():
        raise RecoveryError('Target is not an active trusted administrator.')
    return row


def inspect_target(directory, email):
    root = existing_root(directory)
    with connection(root) as db:
        return root, target(db, email)


def recover(directory, email, confirmed_account_id, password, confirmation):
    """Called only by the private command; caller must establish maintenance."""
    root = existing_root(directory)
    try:
        PasswordPolicy().validate(password, confirmation)
    except ValueError:
        raise RecoveryError('Passwords must match and contain 12–128 characters.') from None
    hashed = Argon2idPasswordHashService().hash(password)
    with connection(root, writable=True) as db:
        account_id, employee_id, _ = target(db, email)
        if account_id != confirmed_account_id:
            raise RecoveryError('Confirmed target changed; recovery refused.')
        changed = db.execute('UPDATE account.accounts SET password_hash=? WHERE account_id=?', (hashed, account_id))
        if changed.rowcount != 1:
            raise RecoveryError('Target update failed.')
        revoked = db.execute('UPDATE session.authenticated_sessions SET revoked_at=? WHERE account_id=? AND revoked_at IS NULL',
                            (datetime.now(timezone.utc).isoformat(), account_id)).rowcount
        SqliteAuthority._event(db, 'private-local-recovery', employee_id, 'ADMIN_CREDENTIAL_RECOVERY')
    return revoked


def hidden_password(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        return getpass.getpass(prompt, stream=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--email', required=True)
    parser.add_argument('--acknowledge-local-recovery', action='store_true', required=True)
    parser.add_argument('--maintenance-confirmed', action='store_true', required=True,
                        help='Operator confirms login traffic and in-flight requests have been stopped.')
    args = parser.parse_args(argv)
    if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        raise SystemExit('Recovery requires a private interactive terminal.')
    try:
        root, account = inspect_target(args.data_dir, args.email)
        print(f'Existing administrator: {account[2]}\nData directory: {root}')
        if input('Confirm by typing the exact administrator email: ') != account[2]:
            raise RecoveryError('Target confirmation cancelled.')
        if input('Confirm by typing the exact displayed data directory: ') != str(root):
            raise RecoveryError('Data directory confirmation cancelled.')
        password = hidden_password('New administrator password: ')
        confirmation = hidden_password('Confirm new password: ')
        revoked = recover(root, args.email, account[0], password, confirmation)
        print(f'Administrator recovery complete; {revoked} sessions revoked.')
    except (Exception, KeyboardInterrupt):
        # No exception text, traceback, SQL values or credential-bearing locals.
        raise SystemExit('Administrator recovery failed or cancelled. No credential details are displayed.') from None


if __name__ == '__main__':
    main()
