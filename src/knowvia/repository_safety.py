from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from knowvia.runtime_paths import PROJECT_ROOT


_TEXT_LIMIT_BYTES = 2_000_000
_BINARY_SUFFIXES = {
    ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".pdf", ".png", ".pyc",
    ".safetensors", ".so", ".sqlite", ".sqlite3", ".whl", ".zip",
}
_RUNTIME_DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_CREDENTIAL_FILENAMES = (
    "credentials*.json",
    "service-account*.json",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.key",
)
_SAFE_EXAMPLE_VALUES = {
    "", "false", "none", "null", "placeholder", "true",
    "set_in_platform_secret_store", "set_to_server_side_ollama_endpoint",
    "your-api-key-here", "your-gemini-api-key-here",
    "your-jira-api-token-here",
}
_SECRET_PATTERNS = (
    ("Google/Gemini API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|github_pat)_[0-9A-Za-z_]{20,}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "credential assignment",
        re.compile(
            r"(?im)^\s*[A-Za-z0-9_.-]*(?:api[_-]?key|api[_-]?token|access[_-]?token|"
            r"auth[_-]?token|client[_-]?secret|jira[_-]?api[_-]?token)\s*[:=]\s*"
            r"[\"']?([^\s\"'#]+)"
        ),
    ),
)


@dataclass(frozen=True)
class SafetyFinding:
    path: str
    category: str


@dataclass(frozen=True)
class RepositorySafetyReport:
    files_scanned: int
    source: str
    findings: tuple[SafetyFinding, ...]

    @property
    def safe(self) -> bool:
        return not self.findings


def _git_publication_files(root: Path) -> list[Path] | None:
    if not (root / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return [root / part.decode("utf-8") for part in result.stdout.split(b"\0") if part]


def _ignore_rules(root: Path) -> list[str]:
    path = root / ".gitignore"
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _rule_matches(relative: str, rule: str) -> bool:
    pattern = rule.lstrip("/")
    if pattern.endswith("/"):
        prefix = pattern.rstrip("/")
        return relative == prefix or relative.startswith(prefix + "/")
    if "/" in pattern:
        return fnmatch.fnmatch(relative, pattern)
    return fnmatch.fnmatch(Path(relative).name, pattern) or fnmatch.fnmatch(relative, pattern)


def is_ignored(relative: Path, rules: Sequence[str]) -> bool:
    normalized = relative.as_posix()
    ignored = False
    for raw_rule in rules:
        negated = raw_rule.startswith("!")
        rule = raw_rule[1:] if negated else raw_rule
        if _rule_matches(normalized, rule):
            ignored = not negated
    return ignored


def publication_files(root: Path) -> tuple[list[Path], str]:
    git_files = _git_publication_files(root)
    if git_files is not None:
        return sorted(path for path in git_files if path.is_file()), "git"
    rules = _ignore_rules(root)
    files: list[Path] = []
    for current, directories, filenames in os.walk(root):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if name != ".git"
            and not is_ignored((current_path / name).relative_to(root), rules)
        ]
        for filename in filenames:
            path = current_path / filename
            if not is_ignored(path.relative_to(root), rules):
                files.append(path)
    return sorted(files), "filesystem-fallback"


def _safe_assignment(match: re.Match[str]) -> bool:
    value = match.group(1).strip().strip("\"'").lower()
    if value in _SAFE_EXAMPLE_VALUES:
        return True
    if len(value) < 20 or any(marker in value for marker in ("(", "{", "$")):
        return True
    return any(marker in value for marker in ("example", "placeholder", "replace_me", "your-"))


def _scan_path(root: Path, path: Path) -> Iterable[SafetyFinding]:
    relative = path.relative_to(root).as_posix()
    name = path.name.lower()
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        yield SafetyFinding(relative, "forbidden environment file")
    if path.suffix.lower() in _RUNTIME_DATABASE_SUFFIXES or name.endswith((".db-wal", ".db-shm")):
        yield SafetyFinding(relative, "runtime database")
    if name.endswith((".log", ".jsonl")):
        yield SafetyFinding(relative, "runtime log or audit data")
    if any(fnmatch.fnmatch(name, pattern) for pattern in _CREDENTIAL_FILENAMES):
        yield SafetyFinding(relative, "credential/private-key file")
    if path.suffix.lower() in _BINARY_SUFFIXES or path.stat().st_size > _TEXT_LIMIT_BYTES:
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for category, pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if category == "credential assignment" and _safe_assignment(match):
                continue
            yield SafetyFinding(relative, f"likely {category}")
            break


def scan_repository(root: Path = PROJECT_ROOT) -> RepositorySafetyReport:
    root = root.resolve()
    files, source = publication_files(root)
    findings = {
        finding
        for path in files
        for finding in _scan_path(root, path)
    }
    return RepositorySafetyReport(
        files_scanned=len(files),
        source=source,
        findings=tuple(sorted(findings, key=lambda item: (item.path, item.category))),
    )


def format_report(report: RepositorySafetyReport) -> str:
    lines = [f"Repository safety: {'PASS' if report.safe else 'FAIL'}"]
    lines.append(f"Publication source: {report.source}")
    lines.append(f"Files scanned: {report.files_scanned}")
    for finding in report.findings:
        lines.append(f"- {finding.path}: {finding.category}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan publishable Knowvia files for unsafe content.")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)
    report = scan_repository(args.root)
    print(format_report(report))
    return 0 if report.safe else 1


if __name__ == "__main__":
    sys.exit(main())
