"""Private implementation for local.ps1; no public launcher changes."""
import argparse
import os
from pathlib import Path
import socket
import tempfile

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def configuration(root, inherited, file_values, mode):
    root = root.resolve()
    # Reject inherited overrides of project configuration, without printing values.
    for name, value in inherited.items():
        if name.startswith('KNOWVIA_') and name in file_values and value != file_values[name]:
            raise ValueError('Conflicting inherited configuration: ' + name)
    values = {k: v for k, v in file_values.items() if v is not None}
    values.update(inherited)
    configured = values.get('KNOWVIA_DATA_DIR', '').strip()
    main = Path(configured).expanduser() if configured else root / 'data'
    if not main.is_absolute():
        main = root / main
    main = main.resolve()
    if mode == 'main':
        # Never silently initialize a new main installation.
        required = ('accounts.db', 'employee_directory.db', 'authority.db',
                    'authenticated_sessions.db', 'knowvia_index.json')
        if not all((main / name).is_file() for name in required):
            raise ValueError('Main installation is incomplete; no databases were created. Verify its configured data directory.')
    values.update({
        'KNOWVIA_ENVIRONMENT': 'development' if mode == 'main' else 'test',
        'KNOWVIA_AUTH_COOKIE_SECURE': 'false',
        'KNOWVIA_ENABLE_HSTS': 'false',
        'KNOWVIA_ALLOWED_HOSTS': '127.0.0.1,localhost',
        'KNOWVIA_ALLOW_DEVELOPMENT_IDENTITY_OVERRIDE': 'false',
        'KNOWVIA_ALLOW_DEVELOPER_DETAILS': 'false', 'KNOWVIA_DEBUG': 'false',
        'KNOWVIA_REGISTRATION_ENABLED': 'false',
        'KNOWVIA_SEED_DEMO_EMPLOYEE_DIRECTORY': 'false',
        'KNOWVIA_AGENT_TOOL': 'mock', 'KNOWVIA_MCP_TICKET_BACKEND': 'mock',
        'KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS': 'false',
        'KNOWVIA_DATA_DIR': str(main),
    })
    if mode == 'review':
        # Only bundled fictional source data, never live uploaded knowledge.
        values['KNOWVIA_DOCUMENTS_DIR'] = str(root / 'knowvia_m1_dataset')
        values['KNOWVIA_BUNDLED_INDEX'] = str(root / 'data/knowvia_index.json')
    return values, main, 8000 if mode == 'main' else 8002


def reserve_port(port):
    listener = socket.socket()
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(128)
        return listener
    except OSError:
        listener.close()
        raise ValueError('Local port is occupied or unavailable; no process was stopped.') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('main', 'review'), required=True)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    os.chdir(ROOT)
    try:
        values, data, port = configuration(ROOT, dict(os.environ), dotenv_values(ROOT / '.env'), args.mode)
        with reserve_port(port) as listener:
            if args.check_only:
                print(f'Checks passed. Mode={args.mode}; main data={data}; port={port}. No application initialized.')
                return
            if args.mode == 'review':
                data = Path(tempfile.mkdtemp(prefix='knowvia-review-')).resolve()
                if data == Path(values['KNOWVIA_DATA_DIR']) or Path(values['KNOWVIA_DATA_DIR']) in data.parents or data in Path(values['KNOWVIA_DATA_DIR']).parents:
                    raise ValueError('Review storage overlaps main storage.')
                values['KNOWVIA_DATA_DIR'] = str(data)
            os.environ.update(values)
            from knowvia.deployment import DeploymentConfig
            DeploymentConfig.from_environment()
            if args.mode == 'review':
                from knowvia.deployment_bootstrap import bootstrap_index, DeploymentResources
                from knowvia.runtime_paths import RuntimePaths
                bootstrap_index(RuntimePaths(data), DeploymentResources.from_environment())
            print(f'Mode: {args.mode}\nData: {data}\nEnvironment: {values["KNOWVIA_ENVIRONMENT"]}\nPort: {port}')
            print(f'Workspace: http://127.0.0.1:{port}/\nAdministration: http://127.0.0.1:{port}/admin')
            print('Ctrl+C stops only this instance. No accounts are bootstrapped. Review folders are retained, not deleted.')
            import uvicorn
            from knowvia.web import app
            uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, workers=1,
                                          proxy_headers=False, access_log=False)).run(sockets=[listener])
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == '__main__':
    main()
