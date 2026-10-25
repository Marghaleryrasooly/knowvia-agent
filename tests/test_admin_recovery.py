import io
import json
import sqlite3
import warnings

import pytest

from knowvia import admin_recovery as recovery
from knowvia.admin import SqliteAuthority
from knowvia.authentication import AuthenticationService, AuthenticationError, LoginRequest
from knowvia.employee_directory import EmployeeRecord, EmployeeDirectoryIdentityService
from knowvia.runtime_paths import RuntimePaths

OLD = 'Synthetic-old-phrase!'
NEW = 'Synthetic-new-phrase!'


@pytest.fixture
def setup(tmp_path):
    authority = SqliteAuthority(RuntimePaths(tmp_path))
    employee = lambda name: EmployeeRecord(name, 'Test', name, name+'@novatech.example', 'IT', 'Employee')
    authority.bootstrap(employee('admin'), OLD, OLD)
    authority.provision('admin', employee('reader'), OLD, OLD, ['employee', 'it-reader'])
    auth = AuthenticationService(authority.accounts, EmployeeDirectoryIdentityService(authority.directory, authority), authority.sessions)
    return tmp_path, authority, auth


def tables(root):
    values = {}
    for name in ('accounts.db', 'authority.db', 'employee_directory.db', 'authenticated_sessions.db'):
        with sqlite3.connect((root/name).as_uri()+'?mode=ro', uri=True) as db:
            values[name] = {table: db.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall()
                            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return values


def test_recovery_preserves_identity_others_and_revokes_only_target(setup, capsys):
    root, authority, auth = setup
    admin_tokens = [auth.login(LoginRequest('admin@novatech.example', OLD)).issued_session.token for _ in range(2)]
    other = auth.login(LoginRequest('reader@novatech.example', OLD)).issued_session.token
    account = authority.accounts.get_by_employee_id('admin')
    before = tables(root)
    _, confirmed = recovery.inspect_target(root, 'admin@novatech.example')
    assert recovery.recover(root, confirmed[2], confirmed[0], NEW, NEW) == 2
    assert authority.accounts.get_by_employee_id('admin') == account
    assert auth.login(LoginRequest(confirmed[2], NEW)).principal.account == account
    with pytest.raises(AuthenticationError): auth.login(LoginRequest(confirmed[2], OLD))
    for token in admin_tokens:
        with pytest.raises(AuthenticationError): auth.resolve_principal(token)
    assert auth.resolve_principal(other).account.employee_id == 'reader'
    assert auth.login(LoginRequest('reader@novatech.example', OLD))
    after = tables(root)
    assert before['employee_directory.db'] == after['employee_directory.db']
    for table in ('role_assignments', 'authority_subjects', 'authority_meta'):
        assert before['authority.db'][table] == after['authority.db'][table]
    before_accounts = before['accounts.db']['accounts']
    after_accounts = after['accounts.db']['accounts']
    assert [r for r in before_accounts if r[1]=='reader'] == [r for r in after_accounts if r[1]=='reader']
    old_admin = next(r for r in before_accounts if r[1]=='admin')
    new_admin = next(r for r in after_accounts if r[1]=='admin')
    assert old_admin[:3] + old_admin[4:] == new_admin[:3] + new_admin[4:]
    events = json.dumps(authority.events('admin'))
    assert 'ADMIN_CREDENTIAL_RECOVERY' in events
    assert OLD not in events and NEW not in events and '$argon2' not in events
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('email', ['reader@novatech.example', 'missing@novatech.example', 'ADMIN@novatech.example'])
def test_invalid_target_is_read_only(setup, email):
    root, _, _ = setup
    before = tables(root)
    with pytest.raises(recovery.RecoveryError): recovery.inspect_target(root, email)
    assert tables(root) == before


def test_missing_invalid_relative_and_noninteractive_refused(tmp_path, monkeypatch):
    with pytest.raises(recovery.RecoveryError): recovery.existing_root('relative')
    with pytest.raises(recovery.RecoveryError): recovery.inspect_target(tmp_path, 'admin@novatech.example')
    assert list(tmp_path.iterdir()) == []
    (tmp_path/'authority.db').write_bytes(b'not a database')
    for name in ('accounts.db','employee_directory.db','authenticated_sessions.db'): (tmp_path/name).touch()
    with pytest.raises(recovery.RecoveryError): recovery.inspect_target(tmp_path, 'admin@novatech.example')
    monkeypatch.setattr(recovery.sys, 'stdin', io.StringIO(''))
    with pytest.raises(SystemExit, match='interactive'):
        recovery.main(['--data-dir',str(tmp_path),'--email','admin@novatech.example',
                       '--acknowledge-local-recovery','--maintenance-confirmed'])


def test_audit_failure_rolls_back_hash_and_revocation(setup):
    root, authority, auth = setup
    token = auth.login(LoginRequest('admin@novatech.example', OLD)).issued_session.token
    with sqlite3.connect(root/'authority.db') as db:
        db.execute("CREATE TRIGGER fail_recovery BEFORE INSERT ON administration_events BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
    before = tables(root)
    account = authority.accounts.get_by_employee_id('admin')
    with pytest.raises(recovery.RecoveryError): recovery.recover(root,'admin@novatech.example', account.account_id, NEW, NEW)
    assert tables(root) == before
    assert auth.resolve_principal(token)
    assert auth.login(LoginRequest('admin@novatech.example', OLD))


def test_changed_target_and_password_policy_refused(setup):
    root, _, _ = setup
    before = tables(root)
    with pytest.raises(recovery.RecoveryError): recovery.recover(root,'admin@novatech.example','wrong-account',NEW,NEW)
    with pytest.raises(recovery.RecoveryError): recovery.recover(root,'admin@novatech.example','wrong-account','short','different')
    assert tables(root) == before


def test_hidden_input_never_falls_back(monkeypatch):
    def fallback(*args, **kwargs):
        warnings.warn('synthetic fallback', recovery.getpass.GetPassWarning)
        pytest.fail('Visible input must never be reached')
    monkeypatch.setattr(recovery.getpass, 'getpass', fallback)
    with pytest.raises(recovery.getpass.GetPassWarning): recovery.hidden_password('New password: ')


def test_disabled_admin_refused(setup):
    root, _, _ = setup
    with sqlite3.connect(root/'accounts.db') as db: db.execute("UPDATE accounts SET status='DISABLED' WHERE employee_id='admin'")
    with pytest.raises(recovery.RecoveryError): recovery.inspect_target(root,'admin@novatech.example')


@pytest.mark.parametrize('mutation', ['directory-email', 'directory-inactive', 'untrusted-role', 'wal'])
def test_identity_and_storage_constraints(setup, mutation):
    root, _, _ = setup
    if mutation == 'wal':
        with sqlite3.connect(root/'accounts.db') as db: db.execute('PRAGMA journal_mode=WAL')
    elif mutation == 'untrusted-role':
        with sqlite3.connect(root/'authority.db') as db: db.execute("INSERT INTO role_assignments VALUES ('admin','invented-role')")
    else:
        with sqlite3.connect(root/'employee_directory.db') as db:
            if mutation == 'directory-email': db.execute("UPDATE employee_directory SET work_email='other@novatech.example' WHERE employee_id='admin'")
            else: db.execute("UPDATE employee_directory SET active=0 WHERE employee_id='admin'")
    before = tables(root)
    with pytest.raises(recovery.RecoveryError): recovery.inspect_target(root,'admin@novatech.example')
    assert tables(root) == before


def test_cli_success_uses_hidden_prompts_and_safe_output(setup, monkeypatch):
    root, authority, auth = setup
    class Terminal(io.StringIO):
        def isatty(self): return True
    output = Terminal()
    monkeypatch.setattr(recovery.sys,'stdin',Terminal())
    monkeypatch.setattr(recovery.sys,'stdout',output)
    monkeypatch.setattr(recovery.sys,'stderr',Terminal())
    answers = iter(['admin@novatech.example',str(root.resolve())])
    monkeypatch.setattr('builtins.input',lambda prompt: next(answers))
    prompts=[]
    def hidden(prompt, **kwargs):
        prompts.append(prompt)
        return NEW
    monkeypatch.setattr(recovery.getpass,'getpass',hidden)
    recovery.main(['--data-dir',str(root),'--email','admin@novatech.example',
                   '--acknowledge-local-recovery','--maintenance-confirmed'])
    assert len(prompts)==2
    assert OLD not in output.getvalue() and NEW not in output.getvalue() and '$argon2' not in output.getvalue()
    assert 'recovery complete' in output.getvalue()
    assert auth.login(LoginRequest('admin@novatech.example', NEW))


def test_cli_requires_confirmation_and_sanitizes_failure(setup, monkeypatch, capsys):
    root, _, _ = setup
    class Terminal(io.StringIO):
        def isatty(self): return True
    for name in ('stdin','stdout','stderr'): monkeypatch.setattr(recovery.sys, name, Terminal())
    monkeypatch.setattr('builtins.input', lambda prompt: 'wrong')
    before = tables(root)
    with pytest.raises(SystemExit) as error:
        recovery.main(['--data-dir',str(root),'--email','admin@novatech.example',
                       '--acknowledge-local-recovery','--maintenance-confirmed'])
    assert OLD not in str(error.value) and NEW not in str(error.value)
    assert tables(root) == before
