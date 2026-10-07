from __future__ import annotations

import re
import sqlite3
import json
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from knowvia.agent_models import AgentDecision, AgentDecisionKind, AgentResult
from knowvia.presentation import EmployeeResponse, EmployeeResponseKind, EmployeeResponsePresenter
from knowvia.skill_orchestration import SkillAgentResult
from knowvia.skills import SkillSelectionStatus, SkillSelector, SkillSummary
from knowvia.identity import IdentityContext
from knowvia.action_intent import ExplicitCurrentTurnActionIntentPolicy
from knowvia.knowledge_normalization import (
    DeclarativeStatusAmbiguityPolicy,
    DeterministicKnowledgeRequestNormalizer,
)


@dataclass(frozen=True)
class ConversationTurn:
    original_request: str
    resolved_request: str
    selected_skill_id: str | None
    outcome: str
    created_at: datetime


@dataclass(frozen=True)
class ConversationState:
    session_id: str
    turns: tuple[ConversationTurn, ...] = ()
    subject_id: str | None = None

    @property
    def latest_knowledge_turn(self) -> ConversationTurn | None:
        for turn in reversed(self.turns):
            if turn.outcome in {
                EmployeeResponseKind.KNOWLEDGE.value,
                EmployeeResponseKind.ABSTENTION.value,
            } and turn.selected_skill_id:
                return turn
        return None


@dataclass(frozen=True)
class ConversationSummary:
    session_id: str
    title: str
    pinned: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ConversationMessage:
    role: str
    payload: dict[str, object]
    created_at: datetime


class ConversationStore(Protocol):
    def load(self, session_id: str, subject_id: str | None = None) -> ConversationState: ...

    def append(
        self, session_id: str, turn: ConversationTurn, subject_id: str | None = None
    ) -> None: ...

    def list_conversations(
        self, subject_id: str | None = None
    ) -> tuple[ConversationSummary, ...]: ...

    def update_metadata(
        self,
        session_id: str,
        subject_id: str | None = None,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> ConversationSummary | None: ...

    def delete(self, session_id: str, subject_id: str | None = None) -> bool: ...

    def load_legacy_user_messages(
        self, session_id: str, subject_id: str | None = None
    ) -> tuple[ConversationMessage, ...]: ...

    def mark_proposal_cancelled(
        self, proposal_id: str, subject_id: str | None = None
    ) -> bool: ...

    def update_proposal_state(
        self,
        proposal_id: str,
        status: str,
        subject_id: str | None = None,
        *,
        session_id: str | None = None,
        external_identifier: str | None = None,
    ) -> bool: ...


class SqliteConversationStore:
    """Small bounded local store for contextual, non-authoritative turn state."""

    _session_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    MAX_TITLE_LENGTH = 80

    def __init__(
        self,
        path: Path,
        max_turns: int = 6,
        max_context_characters: int = 4000,
        ttl_seconds: int = 1800,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if max_turns <= 0 or max_context_characters <= 0 or ttl_seconds <= 0:
            raise ValueError("Conversation bounds must be positive")
        self.path = path
        self.max_turns = max_turns
        self.max_context_characters = max_context_characters
        self.ttl_seconds = ttl_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS conversation_turns (
                turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                original_request TEXT NOT NULL,
                resolved_request TEXT NOT NULL,
                selected_skill_id TEXT,
                outcome TEXT NOT NULL,
                created_at TEXT NOT NULL,
                subject_id TEXT)"""
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(conversation_turns)")
            }
            if "subject_id" not in columns:
                connection.execute("ALTER TABLE conversation_turns ADD COLUMN subject_id TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_session "
                "ON conversation_turns(session_id, turn_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_identity_session "
                "ON conversation_turns(subject_id, session_id, turn_id)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS conversation_sessions (
                subject_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                title TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (subject_id, session_id))"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_sessions_list "
                "ON conversation_sessions(subject_id, pinned DESC, updated_at DESC)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS conversation_messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL)"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversation_messages_session "
                "ON conversation_messages(subject_id, session_id, message_id)"
            )
            connection.execute(
                """INSERT OR IGNORE INTO conversation_sessions
                (subject_id, session_id, title, pinned, created_at, updated_at)
                SELECT COALESCE(turns.subject_id, ''), turns.session_id,
                    SUBSTR(COALESCE(
                        (SELECT first_turn.original_request
                         FROM conversation_turns AS first_turn
                         WHERE first_turn.session_id = turns.session_id
                           AND first_turn.subject_id IS turns.subject_id
                         ORDER BY first_turn.turn_id ASC LIMIT 1),
                        'New conversation'), 1, ?),
                    0, MIN(turns.created_at), MAX(turns.created_at)
                FROM conversation_turns AS turns
                GROUP BY turns.subject_id, turns.session_id""",
                (self.MAX_TITLE_LENGTH,),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @classmethod
    def _validate_session_id(cls, session_id: str) -> str:
        if not isinstance(session_id, str) or not cls._session_pattern.fullmatch(session_id):
            raise ValueError("Session ID must be 1-128 safe identifier characters")
        return session_id

    @staticmethod
    def _subject_key(subject_id: str | None) -> str:
        return subject_id or ""

    @classmethod
    def _validate_title(cls, title: str) -> str:
        if not isinstance(title, str):
            raise TypeError("Conversation title must be text")
        normalized = title.strip()
        if not normalized:
            raise ValueError("Conversation title cannot be empty")
        if len(normalized) > cls.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Conversation title cannot exceed {cls.MAX_TITLE_LENGTH} characters"
            )
        return normalized

    def load(
        self, session_id: str, subject_id: str | None = None
    ) -> ConversationState:
        session_id = self._validate_session_id(session_id)
        cutoff = self._now() - timedelta(seconds=self.ttl_seconds)
        with closing(self._connect()) as connection:
            latest = connection.execute(
                "SELECT created_at FROM conversation_turns "
                "WHERE session_id = ? AND subject_id IS ? "
                "ORDER BY turn_id DESC LIMIT 1",
                (session_id, subject_id),
            ).fetchone()
            if latest and datetime.fromisoformat(latest["created_at"]) <= cutoff:
                connection.execute(
                    "DELETE FROM conversation_turns WHERE session_id = ? AND subject_id IS ?",
                    (session_id, subject_id),
                )
                connection.commit()
                return ConversationState(session_id, subject_id=subject_id)
            rows = connection.execute(
                "SELECT * FROM conversation_turns "
                "WHERE session_id = ? AND subject_id IS ? "
                "ORDER BY turn_id DESC LIMIT ?",
                (session_id, subject_id, self.max_turns),
            ).fetchall()

        selected: list[sqlite3.Row] = []
        size = 0
        for row in rows:
            row_size = len(row["original_request"]) + len(row["resolved_request"])
            if selected and size + row_size > self.max_context_characters:
                break
            if not selected and row_size > self.max_context_characters:
                continue
            selected.append(row)
            size += row_size
        turns = tuple(
            ConversationTurn(
                original_request=row["original_request"],
                resolved_request=row["resolved_request"],
                selected_skill_id=row["selected_skill_id"],
                outcome=row["outcome"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in reversed(selected)
        )
        return ConversationState(session_id, turns, subject_id)

    def append(
        self,
        session_id: str,
        turn: ConversationTurn,
        subject_id: str | None = None,
    ) -> None:
        session_id = self._validate_session_id(session_id)
        if not isinstance(turn, ConversationTurn):
            raise TypeError("Conversation turn is required")
        if not turn.original_request.strip() or not turn.resolved_request.strip():
            raise ValueError("Conversation requests cannot be empty")
        if len(turn.original_request) + len(turn.resolved_request) > self.max_context_characters:
            turn = ConversationTurn(
                original_request=turn.original_request[: self.max_context_characters // 2],
                resolved_request="(Oversized turn omitted from conversational context.)",
                selected_skill_id=None,
                outcome=turn.outcome,
                created_at=turn.created_at,
            )
        with closing(self._connect()) as connection:
            timestamp = turn.created_at.isoformat()
            default_title = self._validate_title(
                turn.original_request[: self.MAX_TITLE_LENGTH]
            )
            connection.execute(
                """INSERT INTO conversation_sessions
                (subject_id, session_id, title, pinned, created_at, updated_at)
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(subject_id, session_id) DO UPDATE SET
                    updated_at = excluded.updated_at""",
                (
                    self._subject_key(subject_id),
                    session_id,
                    default_title,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO conversation_turns "
                "(session_id, original_request, resolved_request, selected_skill_id, "
                "outcome, created_at, subject_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    turn.original_request,
                    turn.resolved_request,
                    turn.selected_skill_id,
                    turn.outcome,
                    turn.created_at.isoformat(),
                    subject_id,
                ),
            )
            connection.execute(
                "DELETE FROM conversation_turns WHERE session_id = ? AND subject_id IS ? "
                "AND turn_id NOT IN "
                "(SELECT turn_id FROM conversation_turns "
                "WHERE session_id = ? AND subject_id IS ? "
                "ORDER BY turn_id DESC LIMIT ?)",
                (session_id, subject_id, session_id, subject_id, self.max_turns),
            )
            connection.commit()

    def list_conversations(
        self, subject_id: str | None = None
    ) -> tuple[ConversationSummary, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT session_id, title, pinned, created_at, updated_at "
                "FROM conversation_sessions WHERE subject_id = ? "
                "ORDER BY pinned DESC, updated_at DESC, session_id ASC",
                (self._subject_key(subject_id),),
            ).fetchall()
        return tuple(self._summary(row) for row in rows)

    def update_metadata(
        self,
        session_id: str,
        subject_id: str | None = None,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> ConversationSummary | None:
        session_id = self._validate_session_id(session_id)
        if title is None and pinned is None:
            raise ValueError("At least one metadata field is required")
        normalized_title = self._validate_title(title) if title is not None else None
        if pinned is not None and not isinstance(pinned, bool):
            raise TypeError("Pinned must be a boolean")
        assignments: list[str] = []
        values: list[object] = []
        if normalized_title is not None:
            assignments.append("title = ?")
            values.append(normalized_title)
        if pinned is not None:
            assignments.append("pinned = ?")
            values.append(int(pinned))
        assignments.append("updated_at = ?")
        values.append(self._now().isoformat())
        values.extend((self._subject_key(subject_id), session_id))
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                f"UPDATE conversation_sessions SET {', '.join(assignments)} "
                "WHERE subject_id = ? AND session_id = ?",
                values,
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT session_id, title, pinned, created_at, updated_at "
                "FROM conversation_sessions WHERE subject_id = ? AND session_id = ?",
                (self._subject_key(subject_id), session_id),
            ).fetchone()
            connection.commit()
        return self._summary(row) if row else None

    def delete(self, session_id: str, subject_id: str | None = None) -> bool:
        session_id = self._validate_session_id(session_id)
        subject_key = self._subject_key(subject_id)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM conversation_sessions WHERE subject_id = ? AND session_id = ?",
                (subject_key, session_id),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "DELETE FROM conversation_turns WHERE session_id = ? AND subject_id IS ?",
                (session_id, subject_id),
            )
            connection.execute(
                "DELETE FROM conversation_messages WHERE subject_id = ? AND session_id = ?",
                (subject_key, session_id),
            )
            connection.commit()
        return True

    def append_message(
        self,
        session_id: str,
        role: str,
        payload: dict[str, object],
        subject_id: str | None = None,
    ) -> None:
        session_id = self._validate_session_id(session_id)
        if role not in {"user", "assistant"}:
            raise ValueError("Unsupported conversation message role")
        if not isinstance(payload, dict):
            raise TypeError("Conversation message payload must be an object")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 50_000:
            raise ValueError("Conversation message payload is too large")
        now = self._now().isoformat()
        title_value = payload.get("text") if role == "user" else None
        title = str(title_value).strip()[: self.MAX_TITLE_LENGTH] if title_value else "New conversation"
        title = title or "New conversation"
        subject_key = self._subject_key(subject_id)
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO conversation_sessions
                (subject_id, session_id, title, pinned, created_at, updated_at)
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(subject_id, session_id) DO UPDATE SET
                    updated_at = excluded.updated_at""",
                (subject_key, session_id, title, now, now),
            )
            connection.execute(
                "INSERT INTO conversation_messages "
                "(subject_id, session_id, role, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (subject_key, session_id, role, encoded, now),
            )
            connection.commit()

    def mark_proposal_cancelled(
        self, proposal_id: str, subject_id: str | None = None
    ) -> bool:
        return self.update_proposal_state(
            proposal_id, "CANCELLED", subject_id
        )

    def update_proposal_state(
        self,
        proposal_id: str,
        status: str,
        subject_id: str | None = None,
        *,
        session_id: str | None = None,
        external_identifier: str | None = None,
    ) -> bool:
        """Persist an authoritative terminal card state for an owned transcript."""
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            raise ValueError("A proposal ID is required")
        terminal_status = status.strip().upper() if isinstance(status, str) else ""
        if terminal_status not in {
            "CANCELLED", "EXECUTED", "FAILED", "EXPIRED", "UNAVAILABLE"
        }:
            raise ValueError("A recognized terminal proposal status is required")
        if external_identifier is not None:
            if not isinstance(external_identifier, str) or not external_identifier.strip():
                raise ValueError("External identifier must be a non-empty string")
            external_identifier = external_identifier.strip()
        subject_key = self._subject_key(subject_id)
        if session_id is not None:
            session_id = self._validate_session_id(session_id)
        updated = False
        with closing(self._connect()) as connection:
            if session_id is None:
                rows = connection.execute(
                    "SELECT message_id, payload_json FROM conversation_messages "
                    "WHERE subject_id = ? AND role = 'assistant' ORDER BY message_id DESC",
                    (subject_key,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT message_id, payload_json FROM conversation_messages "
                    "WHERE subject_id = ? AND session_id = ? AND role = 'assistant' "
                    "ORDER BY message_id DESC",
                    (subject_key, session_id),
                ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                action = payload.get("action") if isinstance(payload, dict) else None
                if not isinstance(action, dict) or action.get("proposal_id") != proposal_id:
                    continue
                action["status"] = terminal_status
                if external_identifier is not None:
                    action["ticket_id"] = external_identifier
                connection.execute(
                    "UPDATE conversation_messages SET payload_json = ? WHERE message_id = ?",
                    (
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        row["message_id"],
                    ),
                )
                updated = True
                break
            connection.commit()
        return updated

    def load_messages(
        self, session_id: str, subject_id: str | None = None
    ) -> tuple[ConversationMessage, ...]:
        session_id = self._validate_session_id(session_id)
        with closing(self._connect()) as connection:
            owned = connection.execute(
                "SELECT 1 FROM conversation_sessions "
                "WHERE subject_id = ? AND session_id = ?",
                (self._subject_key(subject_id), session_id),
            ).fetchone()
            if owned is None:
                return ()
            rows = connection.execute(
                "SELECT role, payload_json, created_at FROM conversation_messages "
                "WHERE subject_id = ? AND session_id = ? ORDER BY message_id ASC",
                (self._subject_key(subject_id), session_id),
            ).fetchall()
        messages: list[ConversationMessage] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                messages.append(
                    ConversationMessage(
                        role=row["role"],
                        payload=payload,
                        created_at=datetime.fromisoformat(row["created_at"]),
                    )
                )
        return tuple(messages)

    def load_legacy_user_messages(
        self, session_id: str, subject_id: str | None = None
    ) -> tuple[ConversationMessage, ...]:
        """Recover only employee-authored text from pre-presentation-history turns.

        Legacy turns never stored assistant presentation payloads, so their outcome and
        resolved request must not be reconstructed as employee-visible assistant text.
        """
        session_id = self._validate_session_id(session_id)
        subject_key = self._subject_key(subject_id)
        with closing(self._connect()) as connection:
            owned = connection.execute(
                "SELECT 1 FROM conversation_sessions "
                "WHERE subject_id = ? AND session_id = ?",
                (subject_key, session_id),
            ).fetchone()
            if owned is None:
                return ()
            rows = connection.execute(
                "SELECT original_request, created_at FROM conversation_turns "
                "WHERE session_id = ? AND subject_id IS ? ORDER BY turn_id ASC",
                (session_id, subject_id),
            ).fetchall()
        return tuple(
            ConversationMessage(
                role="user",
                payload={"text": row["original_request"]},
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
            if row["original_request"].strip()
        )

    @staticmethod
    def _summary(row: sqlite3.Row) -> ConversationSummary:
        return ConversationSummary(
            session_id=row["session_id"],
            title=row["title"],
            pinned=bool(row["pinned"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class ResolutionStatus(str, Enum):
    READY = "READY"
    CLARIFY = "CLARIFY"


@dataclass(frozen=True)
class ResolvedRequest:
    original_request: str
    standalone_request: str
    status: ResolutionStatus
    inherited_skill_id: str | None = None
    clarification_question: str | None = None
    reason: str = ""


class FollowUpResolver:
    """Resolves knowledge references without inheriting action or confirmation intent."""

    _affirmative = re.compile(
        r"^\s*(?:yes|yes[, ]+please|please do|do it|go ahead|sure|ok(?:ay)?)[.! ]*\s*$",
        re.I,
    )
    _contextual_action = re.compile(
        r"\b(?:create|open|raise|file|submit|log)\b.{0,40}\b(?:ticket|it)\b.*\b(?:that|this|it)\b|"
        r"\b(?:create|open|raise|file|submit|log)\b.{0,40}\bticket\b\s+for\s+(?:that|this|it)\b",
        re.I,
    )
    _referential = re.compile(r"^\s*(?:and\s+)?(?:what|how)\s+about\s+(.+?)[?!.]*\s*$", re.I)
    _empty_reference = re.compile(r"^(?:that|this|it|them|those|the same)$", re.I)
    _contextual_knowledge_reference = re.compile(
        r"^\s*(?:(?i:and)\s+)?(?:"
        r"(?i:what|how|when|where|why)\b.*\b(?:that|this|[Ii]t|them|those|then)\b|"
        r"(?i:does|do|is|are|was|were|can|could|would|will)\s+"
        r"(?:that|this|[Ii]t|them|those)\b|"
        r"(?i:after)\s+(?:that|this|[Ii]t|them|those)\b"
        r")",
    )
    _problem_clarification_reference = re.compile(
        r"^\s*(?:it|that|this)\b", re.I
    )
    _ambiguous_action_reference = re.compile(
        r"\b(?:create|make|open|raise|file|submit|log)\s+(?:one|it)\s+"
        r"(?:for\s+)?(?:that|this|it)\b",
        re.I,
    )
    _direct_ticket_problem = (
        re.compile(r"\bbecause\s+(.+)$", re.I),
        re.compile(r"\bticket\s*:\s*(.+)$", re.I),
        re.compile(r"\bticket\s+(?:for|about|regarding)\s+(.+)$", re.I),
    )
    _context_placeholder = re.compile(
        r"^(?:me|myself|that|this|it|that problem|this problem|the problem|"
        r"that issue|this issue|the issue)$",
        re.I,
    )
    _clear_repeated_it_problem = re.compile(
        r"\bkeeps?\s+(?:restarting|rebooting|crashing|freezing|disconnecting|"
        r"dropping|locking|shutting\s+down)\b",
        re.I,
    )
    _employee_problem_context = re.compile(
        r"^\s*(?:i\b|i['’]?m\b|i\s+am\b|my\b|we\b|we['’]?re\b|our\b)",
        re.I,
    )
    _clear_it_problem_signal = re.compile(
        r"\b(?:isn['’]?t|is\s+not|aren['’]?t|are\s+not|won['’]?t|"
        r"will\s+not|can['’]?t|cannot|couldn['’]?t|could\s+not|unable\s+to|"
        r"not\s+working|broken|fails?|failed|having\s+trouble|problems?\s+with|"
        r"issues?\s+with|giving\s+(?:an\s+)?error|reports?\s+(?:an\s+)?error)\b",
        re.I,
    )

    def __init__(self, selector: SkillSelector) -> None:
        self.selector = selector
        self.action_intent = ExplicitCurrentTurnActionIntentPolicy()
        self.knowledge_normalizer = DeterministicKnowledgeRequestNormalizer()
        self.status_ambiguity_policy = DeclarativeStatusAmbiguityPolicy(
            self.knowledge_normalizer
        )

    def resolve(
        self,
        employee_request: str,
        state: ConversationState,
        available_skills: tuple[SkillSummary, ...],
    ) -> ResolvedRequest:
        request = employee_request.strip()
        if not request:
            return self._clarify(employee_request, "What would you like Knowvia to help with?", "Empty follow-up.")
        if self._affirmative.fullmatch(request):
            return self._clarify(
                employee_request,
                "Please state what you would like me to do. Conversational confirmation is not enabled yet.",
                "Affirmative follow-ups cannot confirm or introduce actions in M8.6.",
            )
        contextual_ticket = self._resolve_explicit_ticket_follow_up(request, state)
        if contextual_ticket is not None:
            return contextual_ticket
        if self._contextual_action.search(request):
            return self._clarify(
                employee_request,
                "Please restate the complete action and problem details. I won't use conversation history to perform an action.",
                "Contextual action follow-ups are intentionally blocked in M8.6.",
            )

        clarification_answer = self._resolve_problem_clarification(
            request, state, available_skills
        )
        if clarification_answer is not None:
            return clarification_answer

        current_selection = self.selector.select(request, available_skills)
        reference = self._referential.fullmatch(request)
        contextual_reference = bool(self._contextual_knowledge_reference.search(request))
        if reference is None and not contextual_reference:
            return ResolvedRequest(request, request, ResolutionStatus.READY)

        if current_selection.status is SkillSelectionStatus.SELECTED:
            return ResolvedRequest(
                request,
                request,
                ResolutionStatus.READY,
                reason="The current turn explicitly identifies its domain.",
            )
        if current_selection.status is SkillSelectionStatus.AMBIGUOUS:
            return self._clarify(
                employee_request,
                "Which company policy area do you mean: HR, IT support, or Finance?",
                "The current follow-up matches multiple domains.",
            )

        if reference is not None:
            subject = reference.group(1).strip(" ?.!")
            if not subject or self._empty_reference.fullmatch(subject):
                return self._clarify(
                    employee_request,
                    "What specific policy or topic are you asking about?",
                    "The follow-up reference is ambiguous.",
                )
        else:
            subject = None

        recent_topics = self._recent_knowledge_topics(state)
        if not recent_topics:
            return self._clarify(
                employee_request,
                "What earlier company-policy topic should I apply that follow-up to?",
                "There is no recent knowledge topic to inherit.",
            )
        if len(recent_topics) != 1:
            return self._clarify(
                employee_request,
                "Which recent company topic does your follow-up refer to?",
                "Multiple recent knowledge topics make the reference ambiguous.",
            )

        previous = recent_topics[0]
        if subject is not None:
            standalone = (
                f'Regarding the previous company-policy question "{previous.resolved_request}", '
                f"what does company policy say about {subject}?"
            )
        else:
            standalone = (
                f'Follow-up to the earlier employee request "{previous.resolved_request}": '
                f'"{request}" References such as that, this, it, them, those, or then refer only '
                "to the earlier topic and its documented guidance."
            )
        return ResolvedRequest(
            request,
            standalone,
            ResolutionStatus.READY,
            inherited_skill_id=previous.selected_skill_id,
            reason="A bounded recent knowledge topic resolved the referential follow-up.",
        )

    def _resolve_problem_clarification(
        self,
        request: str,
        state: ConversationState,
        available_skills: tuple[SkillSummary, ...],
    ) -> ResolvedRequest | None:
        """Resolve one immediate employee clarification without inheriting action intent."""
        if (
            not self._problem_clarification_reference.search(request)
            or len(re.findall(r"\w+", request)) > 12
            or not self.knowledge_normalizer.is_declarative_problem(
                request, allow_terse_fragment=True
            )
            or not state.turns
        ):
            return None

        previous = state.turns[-1]
        reconstructed_problem = self.status_ambiguity_policy.reconstruct_problem(
            previous.original_request, request
        )
        if (
            previous.outcome != EmployeeResponseKind.CLARIFICATION.value
            or not previous.selected_skill_id
            or reconstructed_problem is None
            or self.action_intent.is_explicit(
                previous.original_request, "create_it_ticket"
            )
        ):
            return None

        previous_selection = self.selector.select(
            previous.original_request, available_skills
        )
        if (
            previous_selection.status is not SkillSelectionStatus.SELECTED
            or previous_selection.skill_id != previous.selected_skill_id
        ):
            return None

        standalone = (
            "What documented company guidance or troubleshooting applies to this "
            f"reported problem: {reconstructed_problem}?"
        )
        return ResolvedRequest(
            request,
            standalone,
            ResolutionStatus.READY,
            inherited_skill_id=previous.selected_skill_id,
            reason=(
                "A short problem clarification resolved the immediately preceding "
                "skill-scoped employee request."
            ),
        )

    @staticmethod
    def _recent_knowledge_topics(state: ConversationState) -> list[ConversationTurn]:
        """Return bounded candidate topics without crossing an action lifecycle boundary."""
        action_boundary = {
            EmployeeResponseKind.CONFIRMATION_REQUIRED.value,
            EmployeeResponseKind.ACTION_SUCCESS.value,
            EmployeeResponseKind.ACTION_FAILURE.value,
            EmployeeResponseKind.CANCELLED.value,
        }
        topics: list[ConversationTurn] = []
        for turn in reversed(state.turns):
            if turn.outcome in action_boundary:
                break
            if turn.outcome not in {
                EmployeeResponseKind.KNOWLEDGE.value,
                EmployeeResponseKind.ABSTENTION.value,
            } or not turn.selected_skill_id:
                continue
            topics.append(turn)
        return topics

    def _resolve_explicit_ticket_follow_up(
        self, request: str, state: ConversationState
    ) -> ResolvedRequest | None:
        """Use one recent employee-authored IT problem for an explicit ticket request."""
        if not self.action_intent.is_explicit(request, "create_it_ticket"):
            if self._ambiguous_action_reference.search(request):
                return self._clarify(
                    request,
                    "Please state that you want an IT ticket and describe the problem to include.",
                    "The current action reference is ambiguous and cannot inherit intent.",
                )
            return None

        for pattern in self._direct_ticket_problem:
            match = pattern.search(request)
            if match:
                supplied = match.group(1).strip(" .?!")
                if supplied and not self._context_placeholder.fullmatch(supplied):
                    return None
                # A named ticket operation plus one bounded employee-authored
                # problem can safely resolve this/that/it. Bare action forms
                # such as "create one for that" still fail the tool-specific
                # current-turn action-intent policy above.

        problems: list[str] = []
        action_boundary = {
            EmployeeResponseKind.CONFIRMATION_REQUIRED.value,
            EmployeeResponseKind.ACTION_SUCCESS.value,
            EmployeeResponseKind.ACTION_FAILURE.value,
            EmployeeResponseKind.CANCELLED.value,
        }
        for turn in reversed(state.turns):
            if turn.outcome in action_boundary:
                break
            if turn.selected_skill_id != "it_support":
                continue
            if turn.outcome not in {
                EmployeeResponseKind.KNOWLEDGE.value,
                EmployeeResponseKind.ABSTENTION.value,
            }:
                continue
            normalized = self.knowledge_normalizer.normalize(turn.original_request)
            if not (
                normalized.normalization_applied
                or self.knowledge_normalizer.is_declarative_problem(
                    turn.original_request, allow_terse_fragment=True
                )
                or (
                    self._employee_problem_context.search(turn.original_request)
                    and self._clear_it_problem_signal.search(turn.original_request)
                )
                or self._clear_repeated_it_problem.search(turn.original_request)
            ):
                continue
            problem = turn.original_request.strip(" .?!")
            if self._problem_clarification_reference.search(turn.original_request):
                prefix = (
                    "What documented company guidance or troubleshooting applies "
                    "to this reported problem: "
                )
                if turn.resolved_request.startswith(prefix):
                    problem = turn.resolved_request[len(prefix) :].strip(" .?!")
            if problem and problem.casefold() not in {item.casefold() for item in problems}:
                problems.append(problem)

        if not problems:
            return self._clarify(
                request,
                "What IT problem should I include in the ticket?",
                "No clear recent employee-authored IT problem is available.",
            )
        if len(problems) != 1:
            return self._clarify(
                request,
                "Which recent IT problem should I include in the ticket?",
                "Multiple recent employee-authored IT problems make the target ambiguous.",
            )

        standalone = f"Create an IT ticket because {problems[0]}."
        return ResolvedRequest(
            request,
            standalone,
            ResolutionStatus.READY,
            inherited_skill_id="it_support",
            reason=(
                "An explicit current-turn ticket request used one bounded recent "
                "employee-authored IT problem."
            ),
        )

    @staticmethod
    def _clarify(original: str, question: str, reason: str) -> ResolvedRequest:
        return ResolvedRequest(
            original,
            original.strip() or "(empty request)",
            ResolutionStatus.CLARIFY,
            clarification_question=question,
            reason=reason,
        )


@dataclass(frozen=True)
class ConversationResult:
    resolved_request: ResolvedRequest
    skill_result: SkillAgentResult | None
    agent_result: AgentResult
    response: EmployeeResponse


class ConversationalAgent(Protocol):
    def handle(
        self,
        employee_request: str,
        top_k: int = 3,
        selected_skill_id: str | None = None,
        identity: IdentityContext | None = None,
    ) -> SkillAgentResult: ...


class ConversationCoordinator:
    """Adds bounded knowledge context above the unchanged skill-aware agent."""

    def __init__(
        self,
        store: ConversationStore,
        resolver: FollowUpResolver,
        available_skills: tuple[SkillSummary, ...],
        agent: ConversationalAgent,
        presenter: EmployeeResponsePresenter,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.resolver = resolver
        self.available_skills = available_skills
        self.agent = agent
        self.presenter = presenter
        self._now = now or (lambda: datetime.now(timezone.utc))

    def handle(
        self,
        session_id: str,
        employee_request: str,
        top_k: int = 3,
        identity: IdentityContext | None = None,
    ) -> ConversationResult:
        subject_id = identity.subject_id if identity else None
        state = self.store.load(session_id, subject_id)
        resolved = self.resolver.resolve(employee_request, state, self.available_skills)
        if resolved.status is ResolutionStatus.CLARIFY:
            decision = AgentDecision(
                AgentDecisionKind.CLARIFY,
                clarification_question=resolved.clarification_question,
                reason=resolved.reason,
            )
            agent_result = AgentResult(decision, resolved.clarification_question or "Please clarify your request.")
            response = self.presenter.present_agent(agent_result)
            skill_result = None
        else:
            if identity is None:
                skill_result = self.agent.handle(
                    resolved.standalone_request,
                    top_k,
                    selected_skill_id=resolved.inherited_skill_id,
                )
            else:
                skill_result = self.agent.handle(
                    resolved.standalone_request,
                    top_k,
                    selected_skill_id=resolved.inherited_skill_id,
                    identity=identity,
                )
            agent_result = skill_result.agent_result
            response = self.presenter.present_agent(agent_result)

        self.store.append(
            session_id,
            ConversationTurn(
                original_request=employee_request.strip() or "(empty request)",
                resolved_request=resolved.standalone_request,
                selected_skill_id=(skill_result.selected_skill_id if skill_result else None),
                outcome=response.kind.value,
                created_at=self._now(),
            ),
            subject_id,
        )
        return ConversationResult(resolved, skill_result, agent_result, response)
