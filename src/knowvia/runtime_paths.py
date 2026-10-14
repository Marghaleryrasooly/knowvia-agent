from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RuntimeStorageError(RuntimeError):
    """The configured server-side runtime storage cannot be used safely."""


@dataclass(frozen=True)
class RuntimePaths:
    data_dir: Path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "RuntimePaths":
        values = os.environ if environment is None else environment
        configured = values.get("KNOWVIA_DATA_DIR", "").strip()
        root = Path(configured).expanduser() if configured else PROJECT_ROOT / "data"
        return cls(root.resolve())

    @property
    def index(self) -> Path: return self.data_dir / "knowvia_index.json"

    @property
    def pending_actions(self) -> Path: return self.data_dir / "pending_actions.db"

    @property
    def audit_log(self) -> Path: return self.data_dir / "agent_audit.jsonl"

    @property
    def conversations(self) -> Path: return self.data_dir / "conversations.db"

    @property
    def ticket_lifecycle(self) -> Path: return self.data_dir / "ticket_lifecycle.db"

    @property
    def employee_directory(self) -> Path: return self.data_dir / "employee_directory.db"

    @property
    def accounts(self) -> Path: return self.data_dir / "accounts.db"

    @property
    def authority(self) -> Path: return self.data_dir / "authority.db"

    @property
    def authenticated_sessions(self) -> Path: return self.data_dir / "authenticated_sessions.db"

    def ensure_writable(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            probe = self.data_dir / ".knowvia-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise RuntimeStorageError(
                f"Knowvia data directory is not writable: {self.data_dir}"
            ) from exc
