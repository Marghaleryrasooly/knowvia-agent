"""Offline launcher tests: no live application or private configuration."""
import importlib.util
from pathlib import Path
import socket

import pytest

spec = importlib.util.spec_from_file_location('local_start', Path(__file__).parents[1] / 'deploy/local_start.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def installation(tmp_path):
    data = tmp_path / 'existing'
    data.mkdir()
    for name in ('accounts.db', 'employee_directory.db', 'authority.db', 'authenticated_sessions.db', 'knowvia_index.json'):
        (data / name).write_bytes(b'unchanged-fixture')
    return data


def test_main_preserves_existing_storage_and_files(tmp_path):
    data = installation(tmp_path)
    before = {p.name: p.read_bytes() for p in data.iterdir()}
    values, selected, port = launcher.configuration(tmp_path, {}, {'KNOWVIA_DATA_DIR': 'existing'}, 'main')
    assert selected == data and port == 8000
    assert values['KNOWVIA_DATA_DIR'] == str(data)
    assert values['KNOWVIA_REGISTRATION_ENABLED'] == 'false'
    assert values['KNOWVIA_ALLOW_DEVELOPMENT_IDENTITY_OVERRIDE'] == 'false'
    assert before == {p.name: p.read_bytes() for p in data.iterdir()}


def test_main_missing_storage_never_creates_databases(tmp_path):
    with pytest.raises(ValueError, match='incomplete'):
        launcher.configuration(tmp_path, {}, {}, 'main')
    assert not (tmp_path / 'data').exists()


def test_conflicting_terminal_selection_rejected_without_values(tmp_path):
    with pytest.raises(ValueError) as error:
        launcher.configuration(tmp_path, {'KNOWVIA_DATA_DIR': 'private-path'}, {'KNOWVIA_DATA_DIR': 'other'}, 'main')
    assert 'private-path' not in str(error.value)


def test_review_uses_bundled_resources_and_mock_actions(tmp_path):
    values, _, port = launcher.configuration(tmp_path, {}, {'KNOWVIA_DOCUMENTS_DIR': 'live-uploads'}, 'review')
    assert port == 8002
    assert values['KNOWVIA_DOCUMENTS_DIR'] == str(tmp_path / 'knowvia_m1_dataset')
    assert values['KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS'] == 'false'
    assert values['KNOWVIA_ENVIRONMENT'] == 'test'


def test_port_conflict_refused_and_original_listener_survives():
    with launcher.reserve_port(0) as listener:
        port = listener.getsockname()[1]
        with pytest.raises(ValueError, match='occupied'):
            launcher.reserve_port(port)
        assert listener.fileno() >= 0


def test_powershell_guards_public_processes_and_funnel():
    source = (Path(__file__).parents[1] / 'deploy/local.ps1').read_text()
    assert 'Get-CimInstance Win32_Process' in source
    assert 'funnel status --json' in source
    assert 'Stop-Process' not in source
    assert 'SetEnvironmentVariable' not in source
