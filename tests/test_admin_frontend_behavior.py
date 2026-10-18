"""Real headless browser with intercepted requests; no running service needed."""
import os
from pathlib import Path
import shutil
import subprocess
import socket
import threading
import time
import pytest


def browser_environment():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is not installed')
    environment = dict(os.environ)
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'
    environment['NODE_PATH'] = os.pathsep.join(filter(None, [environment.get('NODE_PATH'), str(bundled)]))
    available = subprocess.run([node, '-e', "require.resolve('playwright')"], env=environment, capture_output=True)
    if available.returncode:
        pytest.skip('Optional Playwright browser-test runtime is not installed')
    return node, environment


def run_browser(script, environment):
    node, environment = environment
    result = subprocess.run([node, str(Path(__file__).parent / 'frontend' / script)],
                            env=environment, capture_output=True, text=True, timeout=180)
    if result.returncode == 77:
        pytest.skip('No installed browser executable')
    assert result.returncode == 0, result.stdout + result.stderr


def test_admin_frontend_behavior():
    run_browser('admin_behavior.cjs', browser_environment())


def test_admin_browser_with_disposable_application(tmp_path):
    """Real UI + real session/API/SQLite; synthetic embeddings, no model calls."""
    import uvicorn
    from knowvia.admin import SqliteAuthority
    from knowvia.deployment import ApplicationEnvironment, DeploymentConfig
    from knowvia.employee_directory import EmployeeRecord
    from knowvia.knowledge_management import KnowledgeManagement
    from knowvia.runtime_paths import RuntimePaths
    from knowvia.web import create_web_app
    from test_knowledge_management import Embedder

    node, environment = browser_environment()
    authority = SqliteAuthority(RuntimePaths(tmp_path))
    authority.bootstrap(EmployeeRecord('browser-admin', 'Browser', 'Admin', 'browser-admin@novatech.example', 'IT', 'Administrator'),
                        'Synthetic-test-password!', 'Synthetic-test-password!')
    manager = KnowledgeManagement(authority, Embedder)
    app = create_web_app(runtime_paths=authority.paths, admin_authority=authority, knowledge_manager=manager,
                         deployment_config=DeploymentConfig(environment=ApplicationEnvironment.TEST, secure_cookie=False))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False, lifespan='off'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.02)
            assert server.started, 'Disposable application did not start'
            environment['KNOWVIA_TEST_BASE_URL'] = 'http://127.0.0.1:' + str(listener.getsockname()[1])
            run_browser('admin_integration.cjs', (node, environment))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), 'Disposable application did not stop'
