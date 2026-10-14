from __future__ import annotations

from pathlib import Path

from knowvia.repository_safety import _ignore_rules, format_report, is_ignored, scan_repository
from knowvia.runtime_paths import PROJECT_ROOT


def _write_ignore(root: Path) -> None:
    (root / ".gitignore").write_text(
        "\n".join((
            ".env", ".env.*", "!.env.example", "*.db", "*.db-wal",
            "*.db-shm", "*.log", ".cache/", "build/", "dist/",
        )),
        encoding="utf-8",
    )


def test_repository_ignore_contract_protects_local_state_and_keeps_source() -> None:
    rules = _ignore_rules(PROJECT_ROOT)
    ignored = (
        ".env", ".env.local", "data/accounts.db", "data/accounts.db-wal",
        "data/accounts.db-shm", "logs/server.log",
        ".cache/huggingface/model.bin", "dist/knowvia.whl",
        "build/package/file.txt", "artifacts/evaluation/result.json",
    )
    for value in ignored:
        assert is_ignored(Path(value), rules), value

    publishable = (
        ".env.example", "src/knowvia/web.py", "tests/test_web.py", "README.md",
        "docs/local-demo-reference.md", "src/knowvia/static/app.js",
        "knowvia_m1_dataset/01_employee_handbook.md", "data/knowvia_index.json",
    )
    for value in publishable:
        assert not is_ignored(Path(value), rules), value


def test_scanner_detects_fake_provider_secrets_without_exposing_them(tmp_path: Path) -> None:
    _write_ignore(tmp_path)
    fake_gemini = "AIza" + "A" * 35
    fake_jira = "jira_" + "B" * 40
    (tmp_path / "unsafe.txt").write_text(
        f"GEMINI_API_KEY={fake_gemini}\nJIRA_API_TOKEN={fake_jira}\n",
        encoding="utf-8",
    )
    report = scan_repository(tmp_path)
    rendered = format_report(report)
    assert not report.safe
    assert "Google/Gemini API key" in rendered
    assert "credential assignment" in rendered
    assert fake_gemini not in rendered
    assert fake_jira not in rendered


def test_scanner_detects_publishable_runtime_database(tmp_path: Path) -> None:
    (tmp_path / "accounts.db").write_bytes(b"SQLite format 3\x00")
    report = scan_repository(tmp_path)
    assert not report.safe
    assert any(item.category == "runtime database" for item in report.findings)


def test_ignored_runtime_database_is_not_publishable(tmp_path: Path) -> None:
    _write_ignore(tmp_path)
    (tmp_path / "accounts.db").write_bytes(b"SQLite format 3\x00")
    assert scan_repository(tmp_path).safe


def test_valid_repository_fixture_passes(tmp_path: Path) -> None:
    _write_ignore(tmp_path)
    (tmp_path / ".env.example").write_text("GEMINI_API_KEY=your-gemini-api-key-here\n")
    source = tmp_path / "src" / "knowvia"
    source.mkdir(parents=True)
    (source / "app.py").write_text("APP_NAME = 'Knowvia'\n", encoding="utf-8")
    (tmp_path / ".env").write_text("GEMINI_API_KEY=local-only\n", encoding="utf-8")
    assert scan_repository(tmp_path).safe


def test_public_configuration_examples_are_secret_free() -> None:
    report = scan_repository(PROJECT_ROOT)
    example_paths = {".env.example", "deploy/demo.env.example"}
    assert [item for item in report.findings if item.path in example_paths] == []
