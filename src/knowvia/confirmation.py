from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from knowvia.agent_models import ActionProposal, ActionProposalStatus, ToolResult


class ConfirmationError(ValueError):
    pass


class PendingActionStore(Protocol):
    def create(self, tool_name: str, arguments: dict[str, str], subject_id: str | None = None) -> ActionProposal: ...
    def get(self, proposal_id: str) -> ActionProposal | None: ...
    def claim(self, proposal_id: str, subject_id: str | None = None) -> ActionProposal: ...
    def finish(self, proposal_id: str, result: ToolResult) -> ActionProposal: ...
    def reject(self, proposal_id: str, subject_id: str | None = None) -> ActionProposal: ...


class SqlitePendingActionStore:
    """Small local store with an atomic one-time execution claim."""

    def __init__(self, path: Path, ttl_seconds: int = 600) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.path = path
        self.ttl_seconds = ttl_seconds
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS pending_actions (
                proposal_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL, created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL, status TEXT NOT NULL,
                result_json TEXT, subject_id TEXT)"""
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(pending_actions)")
            }
            if "subject_id" not in columns:
                connection.execute("ALTER TABLE pending_actions ADD COLUMN subject_id TEXT")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def create(
        self, tool_name: str, arguments: dict[str, str], subject_id: str | None = None
    ) -> ActionProposal:
        now = datetime.now(timezone.utc)
        proposal = ActionProposal(
            proposal_id=uuid.uuid4().hex,
            tool_name=tool_name,
            tool_arguments=dict(arguments),
            created_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            subject_id=subject_id,
        )
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO pending_actions "
                "(proposal_id, tool_name, arguments_json, created_at, expires_at, "
                "status, result_json, subject_id) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    proposal.proposal_id, proposal.tool_name,
                    json.dumps(proposal.tool_arguments, sort_keys=True),
                    proposal.created_at.isoformat(), proposal.expires_at.isoformat(),
                    proposal.status.value, proposal.subject_id,
                ),
            )
        return proposal

    def get(self, proposal_id: str) -> ActionProposal | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM pending_actions WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def claim(
        self, proposal_id: str, subject_id: str | None = None
    ) -> ActionProposal:
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pending_actions WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ConfirmationError("Unknown action proposal")
            proposal = self._from_row(row)
            self._validate_subject(proposal, subject_id)
            if proposal.status is not ActionProposalStatus.PENDING:
                connection.rollback()
                raise ConfirmationError(f"Proposal is already {proposal.status.value.lower()}")
            if proposal.expires_at <= now:
                connection.execute(
                    "UPDATE pending_actions SET status = ? WHERE proposal_id = ?",
                    (ActionProposalStatus.EXPIRED.value, proposal_id),
                )
                connection.commit()
                raise ConfirmationError("Action proposal has expired")
            connection.execute(
                "UPDATE pending_actions SET status = ? WHERE proposal_id = ?",
                (ActionProposalStatus.EXECUTING.value, proposal_id),
            )
            connection.commit()
        return replace(proposal, status=ActionProposalStatus.EXECUTING)

    def finish(self, proposal_id: str, result: ToolResult) -> ActionProposal:
        status = ActionProposalStatus.EXECUTED if result.success else ActionProposalStatus.FAILED
        result_json = json.dumps(asdict(result), sort_keys=True)
        with closing(self._connect()) as connection:
            changed = connection.execute(
                "UPDATE pending_actions SET status = ?, result_json = ? "
                "WHERE proposal_id = ? AND status = ?",
                (status.value, result_json, proposal_id, ActionProposalStatus.EXECUTING.value),
            ).rowcount
        if changed != 1:
            raise ConfirmationError("Proposal was not claimed for execution")
        proposal = self.get(proposal_id)
        if proposal is None:
            raise ConfirmationError("Proposal disappeared")
        return proposal

    def reject(
        self, proposal_id: str, subject_id: str | None = None
    ) -> ActionProposal:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM pending_actions WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise ConfirmationError("Unknown action proposal")
            self._validate_subject(self._from_row(row), subject_id)
            changed = connection.execute(
                "UPDATE pending_actions SET status = ? WHERE proposal_id = ? AND status = ?",
                (ActionProposalStatus.REJECTED.value, proposal_id, ActionProposalStatus.PENDING.value),
            ).rowcount
        if changed != 1:
            raise ConfirmationError("Proposal cannot be rejected")
        proposal = self.get(proposal_id)
        if proposal is None:
            raise ConfirmationError("Proposal disappeared")
        return proposal

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ActionProposal:
        raw_result = json.loads(row["result_json"]) if row["result_json"] else None
        return ActionProposal(
            proposal_id=row["proposal_id"], tool_name=row["tool_name"],
            tool_arguments=json.loads(row["arguments_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            status=ActionProposalStatus(row["status"]),
            result=ToolResult(**raw_result) if raw_result else None,
            subject_id=row["subject_id"],
        )

    @staticmethod
    def _validate_subject(
        proposal: ActionProposal, subject_id: str | None
    ) -> None:
        if proposal.subject_id != subject_id:
            raise ConfirmationError("Action proposal belongs to a different identity")
