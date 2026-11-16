"""Appearance checks use intercepted requests or a disposable loopback application."""
from test_admin_frontend_behavior import browser_environment, run_browser


def test_appearance_browser():
    run_browser('theme_behavior.cjs', browser_environment())


def test_authenticated_profile_rendering(tmp_path):
    import socket
    import threading
    import time
    import uvicorn
    from knowvia.admin import SqliteAuthority
    from knowvia.deployment import ApplicationEnvironment, DeploymentConfig
    from knowvia.employee_directory import EmployeeRecord
    from knowvia.knowledge_management import KnowledgeManagement
    from knowvia.password_mail import DisabledMail
    from knowvia.password_management import PasswordManagement, initialize_schema
    from knowvia.runtime_paths import RuntimePaths
    from knowvia.web import create_web_app
    from test_knowledge_management import Embedder

    class ConversationFixture:
        def identities(self): return []
        def conversations(self, identity_id):
            assert identity_id == 'profile-employee'
            return {'conversations': [{'session_id': 'profile-rendering', 'title': 'Overlay rendering check',
                                      'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:00:00Z'}]}
        def conversation_messages(self, session_id, identity_id):
            assert (session_id, identity_id) == ('profile-rendering', 'profile-employee')
            return {'history_status': 'complete', 'messages': [{'role': 'assistant', 'kind': 'KNOWLEDGE',
                    'text': '\n\n'.join(['**Disposable conversation content beneath the profile menu.** ' * 8] * 12), 'sources': []}]}
        def notifications(self, identity_id): return {'notifications': []}

    node, environment = browser_environment()
    authority = SqliteAuthority(RuntimePaths(tmp_path))
    password = 'Synthetic-test-password!'
    authority.bootstrap(EmployeeRecord('profile-admin', 'Profile', 'Administrator', 'profile-admin@novatech.example', 'IT', 'Administrator'), password, password)
    authority.provision('profile-admin', EmployeeRecord('profile-employee', 'Example Employee', 'With a Longer Display Name',
                        'profile-employee@novatech.example', 'Internal Technology Support', 'Employee'), password, password, ['employee'])
    initialize_schema(tmp_path)
    app = create_web_app(service=ConversationFixture(), runtime_paths=authority.paths, admin_authority=authority,
                         password_manager=PasswordManagement(authority.paths, DisabledMail(), 'https://fixture.example'),
                         knowledge_manager=KnowledgeManagement(authority, Embedder),
                         deployment_config=DeploymentConfig(environment=ApplicationEnvironment.TEST, secure_cookie=False))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False, lifespan='off'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline: time.sleep(.02)
            assert server.started
            environment['KNOWVIA_TEST_BASE_URL'] = 'http://127.0.0.1:' + str(listener.getsockname()[1])
            run_browser('profile_integration.cjs', (node, environment))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
