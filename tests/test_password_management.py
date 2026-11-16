from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from knowvia.accounts import Argon2idPasswordHashService
from knowvia.admin import SqliteAuthority
from knowvia.authentication import AuthenticationService, AuthenticationError, LoginRequest
from knowvia.employee_directory import EmployeeRecord, EmployeeDirectoryIdentityService
from knowvia.password_mail import DisabledMail, MailUnavailable, callback_origin, mail_from_environment
from knowvia.password_management import PasswordManagement, PasswordManagementError, initialize_schema, GENERIC_RESET_MESSAGE, connection
from knowvia.runtime_paths import RuntimePaths

OLD='Synthetic-initial-phrase!'
NEW='Synthetic-replacement-phrase!'


class FakeMail:
    enabled=True
    def __init__(self): self.messages=[]; self.fail=False
    def send(self, recipient, purpose, link):
        if self.fail: raise RuntimeError('Synthetic provider error must not escape')
        self.messages.append((recipient,purpose,link))


def employee(name): return EmployeeRecord(name,'Test',name,name+'@example.org','IT','Employee')


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED','1')
    monkeypatch.setenv('GROQ_API_KEY','fictional-offline-key')
    monkeypatch.setenv('KNOWVIA_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('KNOWVIA_ENVIRONMENT','development')
    authority=SqliteAuthority(RuntimePaths(tmp_path))
    authority.bootstrap(employee('administrator'),OLD,OLD)
    authority.provision('administrator',employee('existing'),OLD,OLD,['employee','it-reader'])
    initialize_schema(tmp_path)
    clock=[datetime.now(timezone.utc)]
    mail=FakeMail()
    manager=PasswordManagement(authority.paths,mail,'https://knowvia.example',now=lambda:clock[0])
    auth=AuthenticationService(authority.accounts,EmployeeDirectoryIdentityService(authority.directory,authority),authority.sessions)
    return authority,manager,mail,auth,clock


def link(mail):
    parts=parse_qs(urlsplit(mail.messages[-1][2]).fragment)
    return parts['token'][0],parts['purpose'][0]


def verify_existing(setup):
    authority,manager,mail,_,_=setup
    account=authority.accounts.get_by_employee_id('existing')
    manager.request_verification(account.account_id,OLD)
    manager.redeem(*link(mail))
    return account


def test_invitation_single_use_real_email_roles_and_unknown_initial_credentials(setup):
    authority,manager,mail,auth,_=setup
    record=EmployeeRecord('invitee','Demo','Person','demo.person@example.org','IT','Employee')
    result=manager.invite(authority,'administrator',record,['employee','it-reader'])
    assert result=={'saved':True,'mail_sent':True}
    before=authority.accounts.get_by_employee_id('invitee')
    token,purpose=link(mail)
    assert purpose=='invite' and len(token)==43
    assert urlsplit(mail.messages[-1][2]).query==''
    with connection(authority.paths.data_dir) as db:
        rows=json.dumps([tuple(r) for r in db.execute('SELECT * FROM credential_tokens')])
        assert token not in rows and 'https://' not in rows
    manager.redeem(token,purpose,NEW,NEW)
    assert authority.accounts.get_by_employee_id('invitee')==before
    assert authority.resolve('invitee').roles==frozenset({'employee','it-reader'})
    assert auth.login(LoginRequest(record.work_email,NEW))
    with pytest.raises(PasswordManagementError):manager.redeem(token,purpose,NEW,NEW)
    with connection(authority.paths.data_dir) as db:
        assert db.execute('SELECT email_verified_at,onboarding_pending FROM credential_state WHERE account_id=?',(before.account_id,)).fetchone()[0]


def test_expiration_resend_and_delivery_failure_preserve_saved_account(setup):
    authority,manager,mail,_,clock=setup
    manager.invite(authority,'administrator',employee('invitee'),['employee'])
    old=link(mail)
    manager.resend_invitation(authority,'administrator','invitee')
    fresh=link(mail)
    with pytest.raises(PasswordManagementError):manager.redeem(*old,NEW,NEW)
    clock[0]+=timedelta(hours=25)
    with pytest.raises(PasswordManagementError):manager.redeem(*fresh,NEW,NEW)
    mail.fail=True
    result=manager.invite(authority,'administrator',employee('failed'),['employee'])
    assert result=={'saved':True,'mail_sent':False}
    assert manager.states()['failed']
    with connection(authority.paths.data_dir) as db:
        assert not db.execute('''SELECT 1 FROM credential_tokens t JOIN account.accounts a
            ON a.account_id=t.account_id WHERE t.consumed_at IS NULL AND a.employee_id='failed' ''').fetchone()


def test_disabled_mail_missing_schema_and_admin_roles_do_not_provision(setup,tmp_path):
    authority,manager,_,_,_=setup
    disabled=PasswordManagement(authority.paths)
    with pytest.raises(MailUnavailable):disabled.invite(authority,'administrator',employee('none'),['employee'])
    assert authority.accounts.get_by_employee_id('none') is None
    with pytest.raises(PasswordManagementError):manager.invite(authority,'administrator',employee('promoted'),['administrator'])
    with pytest.raises(Exception):manager.invite(authority,'existing',employee('unauthorized'),['employee'])
    assert authority.accounts.get_by_employee_id('unauthorized') is None
    assert not (tmp_path/'missing').exists()
    with pytest.raises(ValueError):initialize_schema(tmp_path/'missing')
    assert not (tmp_path/'missing').exists()


def test_legacy_emails_are_not_implicitly_verified_and_forgot_is_generic(setup):
    _,manager,mail,_,_=setup
    for email in ['existing@example.org','unknown@example.org','not an email']:
        manager.request_reset(email)
    assert mail.messages==[]
    verify_existing(setup)
    manager.request_reset('existing@example.org')
    assert mail.messages[-1][1]=='reset'


def test_change_password_current_password_sessions_and_identity_preserved(setup):
    authority,manager,_,auth,_=setup
    account=authority.accounts.get_by_employee_id('existing')
    tokens=[auth.login(LoginRequest(account.work_email,OLD)).issued_session.token for _ in range(2)]
    other=auth.login(LoginRequest('administrator@example.org',OLD)).issued_session.token
    roles=authority.resolve('existing').roles
    with pytest.raises(PasswordManagementError):manager.change(account.account_id,tokens[0],'incorrect',NEW,NEW)
    assert auth.resolve_principal(tokens[0])
    manager.change(account.account_id,tokens[0],OLD,NEW,NEW)
    for token in tokens:
        with pytest.raises(AuthenticationError):auth.resolve_principal(token)
    assert auth.resolve_principal(other)
    assert authority.accounts.get_by_employee_id('existing')==account
    assert authority.resolve('existing').roles==roles
    assert auth.login(LoginRequest(account.work_email,NEW))
    with pytest.raises(AuthenticationError):auth.login(LoginRequest(account.work_email,OLD))


def test_reset_revokes_sessions_invalidates_old_links_and_concurrent_redemption(setup):
    authority,manager,mail,auth,_=setup
    account=verify_existing(setup)
    session=auth.login(LoginRequest(account.work_email,OLD)).issued_session.token
    manager.request_reset(account.work_email);old=link(mail)
    manager.request_reset(account.work_email);current=link(mail)
    with pytest.raises(PasswordManagementError):manager.redeem(*old,NEW,NEW)
    def redeem(_):
        try:manager.redeem(*current,NEW,NEW);return True
        except ValueError:return False
    with ThreadPoolExecutor(2) as pool:assert sum(pool.map(redeem,range(2)))==1
    with pytest.raises(AuthenticationError):auth.resolve_principal(session)
    assert auth.login(LoginRequest(account.work_email,NEW))


def test_reset_expiration_wrong_purpose_policy_and_deactivation(setup):
    authority,manager,mail,_,clock=setup
    account=verify_existing(setup)
    manager.request_reset(account.work_email);token,purpose=link(mail)
    with pytest.raises(PasswordManagementError):manager.redeem(token,'invite',NEW,NEW)
    with pytest.raises(ValueError):manager.redeem(token,purpose,'short','short')
    clock[0]+=timedelta(minutes=31)
    with pytest.raises(PasswordManagementError):manager.redeem(token,purpose,NEW,NEW)
    manager.request_reset(account.work_email);fresh=link(mail)
    authority.set_active('administrator','existing',False)
    authority.set_active('administrator','existing',True)
    with pytest.raises(PasswordManagementError):manager.redeem(*fresh,NEW,NEW)


def test_reset_audit_failure_rolls_back_token_password_and_sessions(setup):
    authority,manager,mail,auth,_=setup
    account=verify_existing(setup)
    session=auth.login(LoginRequest(account.work_email,OLD)).issued_session.token
    manager.request_reset(account.work_email);reset=link(mail)
    with sqlite3.connect(authority.paths.authority) as db:
        db.execute("CREATE TRIGGER reject_password BEFORE INSERT ON administration_events BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(ValueError):manager.redeem(*reset,NEW,NEW)
    assert auth.resolve_principal(session)
    assert auth.login(LoginRequest(account.work_email,OLD))
    with connection(authority.paths.data_dir) as db:
        assert db.execute("SELECT consumed_at FROM credential_tokens WHERE purpose='reset' ORDER BY created_at DESC LIMIT 1").fetchone()[0] is None


def test_old_password_login_in_flight_cannot_issue_session_after_reset(setup,monkeypatch):
    authority,manager,mail,auth,_=setup
    account=verify_existing(setup)
    manager.request_reset(account.work_email);reset=link(mail)
    original=auth.password_hashes.verify
    def race(hashed,password):
        result=original(hashed,password)
        if result:manager.redeem(*reset,NEW,NEW)
        return result
    monkeypatch.setattr(auth.password_hashes,'verify',race)
    with pytest.raises(AuthenticationError):auth.login(LoginRequest(account.work_email,OLD))
    with connection(authority.paths.data_dir) as db:
        assert not db.execute('SELECT 1 FROM session.authenticated_sessions WHERE account_id=? AND revoked_at IS NULL',(account.account_id,)).fetchone()


def test_audit_and_exceptions_do_not_leak_credentials_or_bearer_links(setup,caplog):
    authority,manager,mail,_,_=setup
    manager.invite(authority,'administrator',employee('invitee'),['employee'])
    token,purpose=link(mail)
    manager.redeem(token,purpose,NEW,NEW)
    events=json.dumps(authority.events('administrator'))
    for private in (OLD,NEW,token,'$argon2id$',mail.messages[-1][2]):
        assert private not in events and private not in caplog.text
    with pytest.raises(PasswordManagementError) as error:manager.redeem(token,purpose,NEW,NEW)
    assert token not in str(error.value)


def web_client(setup):
    from knowvia.web import create_web_app
    from knowvia.deployment import DeploymentConfig,ApplicationEnvironment
    authority,manager,_,_,_=setup
    config=DeploymentConfig(environment=ApplicationEnvironment.TEST,secure_cookie=False,registration_enabled=False)
    app=create_web_app(runtime_paths=authority.paths,admin_authority=authority,password_manager=manager,deployment_config=config)
    return TestClient(app,base_url='https://testserver')


def login(client,email='administrator@example.org'):
    return client.post('/api/auth/login',json={'work_email':email,'password':OLD})


def post(client,operation,body,origin='https://testserver'):
    return client.post('/api/auth/password/'+operation,json=body,headers={'Origin':origin})


def test_http_forgot_identical_responses_rate_limits_and_csrf(setup):
    client=web_client(setup)
    verify_existing(setup)
    known=post(client,'forgot',{'work_email':'existing@example.org'})
    unknown=post(client,'forgot',{'work_email':'unknown@example.org'})
    assert known.status_code==unknown.status_code==200 and known.json()==unknown.json()=={'message':GENERIC_RESET_MESSAGE}
    assert post(client,'forgot',{'work_email':'existing@example.org'},origin='https://evil.example').status_code==403
    assert client.post('/api/auth/password/forgot',json={'work_email':'existing@example.org'}).status_code==403
    count=len(setup[2].messages)
    for _ in range(10):assert post(client,'forgot',{'work_email':'existing@example.org'}).json()==known.json()
    assert len(setup[2].messages)-count<=3


def test_http_authenticated_change_cookie_revocation_and_storage_ui(setup):
    client=web_client(setup)
    body={'current_password':OLD,'password':NEW,'password_confirmation':NEW}
    assert post(client,'change',body).status_code==401
    assert login(client).status_code==200
    assert client.get('/api/auth/password/status').status_code==200
    assert post(client,'change',body,origin='https://evil.example').status_code==403
    assert post(client,'change',body).status_code==200
    assert client.get('/api/auth/me').status_code==401
    assert client.post('/api/auth/register',json={}).status_code==404
    assert client.get('/password').status_code==client.get('/account').status_code==200
    assert client.get('/password').headers['referrer-policy']=='no-referrer'


def test_http_invite_rbac_and_saved_but_delivery_failed(setup):
    client=web_client(setup)
    body={'employee_id':'new','first_name':'New','last_name':'Employee','work_email':'new@example.org',
          'department':'IT','role':'Employee','roles':['employee','it-reader']}
    assert client.post('/api/admin/accounts/invite',json=body,headers={'Origin':'https://testserver'}).status_code==401
    login(client,'existing@example.org')
    assert client.post('/api/admin/accounts/invite',json=body,headers={'Origin':'https://testserver'}).status_code==403
    client.cookies.clear();login(client)
    assert client.post('/api/admin/accounts/invite',json=body,headers={'Origin':'https://evil.example'}).status_code==403
    setup[2].fail=True
    response=client.post('/api/admin/accounts/invite',json=body,headers={'Origin':'https://testserver'})
    assert response.status_code==201 and response.json()=={'saved':True,'mail_sent':False}
    assert setup[0].accounts.get_by_employee_id('new')
    setup[2].fail=False
    assert client.post('/api/admin/accounts/new/invitation',json={},headers={'Origin':'https://testserver'}).status_code==200
    assert client.post('/api/admin/accounts/invite',json=body,headers={'Origin':'https://testserver'}).status_code==400


@pytest.mark.parametrize('value',['http://example.org','https://example.org/other','https://name:secret@example.org','https://example.org?next=evil','https://localhost'])
def test_insecure_callbacks_rejected(value):
    with pytest.raises(MailUnavailable):callback_origin(value)


def test_email_disabled_default_and_sender_tls_checks():
    mail,origin=mail_from_environment({})
    assert isinstance(mail,DisabledMail) and origin is None
    with pytest.raises(MailUnavailable):mail_from_environment({'KNOWVIA_MAIL_TRANSPORT':'smtp'})


def test_reset_link_uses_configured_origin_not_forwarded_host(setup):
    client=web_client(setup)
    verify_existing(setup)
    response=client.post('/api/auth/password/forgot',json={'work_email':'existing@example.org'},
        headers={'Origin':'https://testserver','X-Forwarded-Host':'attacker.example.org'})
    assert response.status_code==200
    parsed=urlsplit(setup[2].messages[-1][2])
    assert (parsed.scheme,parsed.netloc,parsed.path)==('https','knowvia.example','/password')


def test_smtp_callback_must_be_explicitly_trusted():
    values={'KNOWVIA_MAIL_TRANSPORT':'smtp','KNOWVIA_PUBLIC_BASE_URL':'https://callback.example.org',
        'KNOWVIA_ALLOWED_HOSTS':'other.example.org','KNOWVIA_SMTP_HOST':'smtp.example.org',
        'KNOWVIA_SMTP_USERNAME':'synthetic','KNOWVIA_SMTP_PASSWORD':'synthetic',
        'KNOWVIA_MAIL_FROM':'sender@example.org','KNOWVIA_MAIL_SENDER_VERIFIED':'true'}
    with pytest.raises(MailUnavailable):mail_from_environment(values)
    values['KNOWVIA_ALLOWED_HOSTS']='callback.example.org'
    mail,origin=mail_from_environment(values)
    assert mail.enabled and origin=='https://callback.example.org'


def test_forgot_password_target_limit_cannot_be_bypassed_with_email_whitespace(setup):
    app=web_client(setup).app
    verify_existing(setup)
    before=len(setup[2].messages)
    for attempt in range(6):
        client=TestClient(app,base_url='https://testserver',client=(f'198.51.100.{attempt+1}',50000))
        response=post(client,'forgot',{'work_email':' '*attempt+'existing@example.org'})
        assert response.status_code==200 and response.json()=={'message':GENERIC_RESET_MESSAGE}
    delivered=len(setup[2].messages)-before
    assert delivered==5


def test_forgot_canonical_variants_preserve_last_token_and_independent_mailboxes(setup):
    app=web_client(setup).app
    verify_existing(setup)
    manager,mail=setup[1:3]
    variants=['existing@example.org',' existing@example.org','existing@example.org ',
              'EXISTING@EXAMPLE.ORG','  Existing@Example.Org  ']
    for attempt,email in enumerate(variants):
        client=TestClient(app,base_url='https://testserver',client=(f'198.51.100.{attempt+1}',50000))
        assert post(client,'forgot',{'work_email':email}).json()=={'message':GENERIC_RESET_MESSAGE}
    last_link=link(mail)
    count=len(mail.messages)
    client=TestClient(app,base_url='https://testserver',client=('198.51.100.99',50000))
    for email in ['existing@example.org',' EXISTING@example.org ']:
        assert post(client,'forgot',{'work_email':email}).json()=={'message':GENERIC_RESET_MESSAGE}
    assert len(mail.messages)==count
    manager.redeem(*last_link,NEW,NEW)  # Rejected sends did not invalidate it.
    manager.request_verification(setup[0].accounts.get_by_employee_id('administrator').account_id,OLD)
    manager.redeem(*link(mail))
    count=len(mail.messages)
    assert post(client,'forgot',{'work_email':'administrator@example.org'}).status_code==200
    assert len(mail.messages)==count+1


def test_malformed_emails_consume_client_budget_without_delivery(setup):
    client=web_client(setup)
    verify_existing(setup)
    before=len(setup[2].messages)
    for email in ['bad','a@@example.org','',None,123]:
        response=post(client,'forgot',{'work_email':email})
        assert response.status_code==200 and response.json()=={'message':GENERIC_RESET_MESSAGE}
    assert post(client,'forgot',{'work_email':'existing@example.org'}).json()=={'message':GENERIC_RESET_MESSAGE}
    assert len(setup[2].messages)==before


@pytest.mark.parametrize('port',[465,587])
@pytest.mark.parametrize('purpose',['invite','reset','verify'])
def test_smtp_tls_and_sanitized_failure(monkeypatch,port,purpose):
    from knowvia import password_mail
    events=[]
    class Client:
        def __init__(self,*args,**kwargs):
            assert kwargs['timeout']==10
            if port==465: assert kwargs['context'].check_hostname
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def ehlo(self):events.append('ehlo')
        def starttls(self,context):
            assert context.check_hostname
            events.append('tls')
        def login(self,*args):events.append('login')
        def send_message(self,message):
            assert message['To']=='recipient@example.org'
            assert message.get_content_type()=='multipart/alternative'
            content=message.get_body(preferencelist=('plain',)).get_content()
            assert 'https://knowvia.example/password#synthetic' in content
            assert '24 hours' in content if purpose=='invite' else '30 minutes' in content
            assert 'only once' in content
            assert 'synthetic-secret' not in content
            html=message.get_body(preferencelist=('html',)).get_content()
            assert {'invite':'Create your password','reset':'Reset your password','verify':'Verify your email'}[purpose] in html
            assert 'href="https://knowvia.example/password#synthetic"' in html
            assert '<script' not in html and '<img' not in html
            assert 'synthetic-secret' not in html
            events.append('send')
            return {}
    monkeypatch.setattr(password_mail.smtplib,'SMTP_SSL',Client)
    monkeypatch.setattr(password_mail.smtplib,'SMTP',Client)
    mail=password_mail.SmtpMail('smtp.example.org',port,'synthetic-user','synthetic-secret','sender@example.org')
    mail.send('recipient@example.org',purpose,'https://knowvia.example/password#synthetic')
    assert events==(['ehlo','tls','ehlo','login','send'] if port==587 else ['login','send'])
    def fail(self,message):raise RuntimeError('synthetic-secret sensitive response')
    monkeypatch.setattr(Client,'send_message',fail)
    with pytest.raises(MailUnavailable) as error:mail.send('recipient@example.org','reset','https://knowvia.example/password#synthetic')
    assert str(error.value)=='Email delivery could not be confirmed.'
    assert 'synthetic-secret' not in repr(mail)


def test_html_mail_escapes_exact_callback_and_rejects_unsafe_urls(monkeypatch):
    from html.parser import HTMLParser
    from knowvia import password_mail
    messages=[]
    class Client:
        def __init__(self,*args,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def ehlo(self):pass
        def starttls(self,context):pass
        def login(self,*args):pass
        def send_message(self,message):messages.append(message);return {}
    monkeypatch.setattr(password_mail.smtplib,'SMTP',Client)
    mail=password_mail.SmtpMail('smtp.example.org',587,'synthetic','synthetic','sender@example.org')
    link='https://knowvia.example/password#purpose=invite&token="<tag>'
    mail.send('recipient@example.org','invite',link)
    message=messages[0];html=message.get_body(preferencelist=('html',)).get_content()
    class Links(HTMLParser):
        def __init__(self):super().__init__();self.links=[];self.tags=[]
        def handle_starttag(self,tag,attrs):
            self.tags.append(tag)
            if tag=='a':self.links.append(dict(attrs).get('href'))
    parsed=Links();parsed.feed(html)
    assert parsed.links==[link]
    assert 'tag' not in parsed.tags and 'script' not in parsed.tags and 'img' not in parsed.tags
    assert link in message.get_body(preferencelist=('plain',)).get_content()
    for unsafe in ['javascript:alert(1)','http://knowvia.example/password#synthetic','https://user:pass@knowvia.example/password#synthetic','https://knowvia.example/password?token=synthetic']:
        with pytest.raises(MailUnavailable):mail.send('recipient@example.org','reset',unsafe)
    assert len(messages)==1


def test_schema_idempotence_restart_and_legacy_state(setup):
    authority,manager,mail,auth,_=setup
    original=authority.accounts.get_by_employee_id('existing')
    initialize_schema(authority.paths.data_dir)
    assert authority.accounts.get_by_employee_id('existing')==original
    manager.invite(authority,'administrator',employee('restart'),['employee'])
    restarted=PasswordManagement(authority.paths,mail,'https://knowvia.example')
    restarted.redeem(*link(mail),NEW,NEW)
    assert auth.login(LoginRequest('restart@example.org',NEW))
    manager.request_reset('existing@example.org')
    assert mail.messages[-1][1]=='invite'  # legacy email was not implicitly verified
