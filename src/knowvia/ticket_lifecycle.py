from __future__ import annotations

import base64
import json
import re
import sqlite3
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import quote

from knowvia.agent_models import ToolResult
from knowvia.audit import AuditSink, NullAuditSink, make_audit_event
from knowvia.identity import IdentityContext
from knowvia.jira import HttpResponse, JiraConfig


class TicketLifecycleError(ValueError):
    pass


class TicketStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TicketRecord:
    ticket_id: str
    owner_subject_id: str
    status: TicketStatus
    external_system: str
    external_reference: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class NotificationEvent:
    event_id: str
    subject_id: str
    ticket_id: str
    previous_status: TicketStatus
    new_status: TicketStatus
    message: str
    created_at: datetime


@dataclass(frozen=True)
class TicketStatusObservation:
    success: bool
    status: TicketStatus
    external_reference: str
    error_category: str | None = None


@dataclass(frozen=True)
class TicketStatusResult:
    success: bool
    ticket_id: str
    status: TicketStatus
    message: str
    notification: NotificationEvent | None = None
    permission_denied: bool = False
    error_category: str | None = None


class TicketLifecycleRecorder(Protocol):
    def record_created(
        self, result: ToolResult, identity: IdentityContext
    ) -> TicketRecord: ...


class TicketStatusProvider(Protocol):
    def get_status(self, ticket: TicketRecord) -> TicketStatusObservation: ...


class TicketLifecycleStore(Protocol):
    def register(self, record: TicketRecord) -> TicketRecord: ...
    def get(self, ticket_id: str) -> TicketRecord | None: ...
    def observe_status(
        self, ticket_id: str, status: TicketStatus
    ) -> tuple[TicketRecord, NotificationEvent | None]: ...
    def notifications_for(self, subject_id: str) -> list[NotificationEvent]: ...


class SqliteTicketLifecycleStore:
    """Small local lifecycle and notification store with atomic transitions."""

    def __init__(
        self,
        path: Path,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path
        self._now = now or (lambda: datetime.now(timezone.utc))
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    owner_subject_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    external_system TEXT NOT NULL,
                    external_reference TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ticket_status_events (
                    event_id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL,
                    previous_status TEXT NOT NULL,
                    new_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_events (
                    event_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    previous_status TEXT NOT NULL,
                    new_status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_notifications_subject
                ON notification_events(subject_id, created_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def register(self, record: TicketRecord) -> TicketRecord:
        if not isinstance(record, TicketRecord):
            raise TicketLifecycleError("A validated ticket record is required")
        with closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (record.ticket_id,)
            ).fetchone()
            if existing:
                current = self._record(existing)
                if (
                    current.owner_subject_id == record.owner_subject_id
                    and current.external_system == record.external_system
                    and current.external_reference == record.external_reference
                ):
                    return current
                raise TicketLifecycleError("Ticket identifier is already bound")
            connection.execute(
                "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.ticket_id,
                    record.owner_subject_id,
                    record.status.value,
                    record.external_system,
                    record.external_reference,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def get(self, ticket_id: str) -> TicketRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
        return self._record(row) if row else None

    def observe_status(
        self, ticket_id: str, status: TicketStatus
    ) -> tuple[TicketRecord, NotificationEvent | None]:
        if not isinstance(status, TicketStatus):
            raise TicketLifecycleError("A validated ticket status is required")
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise TicketLifecycleError("Unknown tracked ticket")
            current = self._record(row)
            if current.status is status:
                connection.commit()
                return current, None
            connection.execute(
                "UPDATE tickets SET status = ?, updated_at = ? WHERE ticket_id = ?",
                (status.value, now.isoformat(), ticket_id),
            )
            transition_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO ticket_status_events VALUES (?, ?, ?, ?, ?)",
                (
                    transition_id,
                    ticket_id,
                    current.status.value,
                    status.value,
                    now.isoformat(),
                ),
            )
            notification = None
            if status is not TicketStatus.UNKNOWN:
                notification = NotificationEvent(
                    event_id=uuid.uuid4().hex,
                    subject_id=current.owner_subject_id,
                    ticket_id=ticket_id,
                    previous_status=current.status,
                    new_status=status,
                    message=_notification_message(ticket_id, status),
                    created_at=now,
                )
                connection.execute(
                    "INSERT INTO notification_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        notification.event_id,
                        notification.subject_id,
                        notification.ticket_id,
                        notification.previous_status.value,
                        notification.new_status.value,
                        notification.message,
                        notification.created_at.isoformat(),
                    ),
                )
            connection.commit()
        return (
            TicketRecord(
                current.ticket_id,
                current.owner_subject_id,
                status,
                current.external_system,
                current.external_reference,
                current.created_at,
                now,
            ),
            notification,
        )

    def notifications_for(self, subject_id: str) -> list[NotificationEvent]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM notification_events WHERE subject_id = ? "
                "ORDER BY created_at, event_id",
                (subject_id,),
            ).fetchall()
        return [
            NotificationEvent(
                row["event_id"],
                row["subject_id"],
                row["ticket_id"],
                TicketStatus(row["previous_status"]),
                TicketStatus(row["new_status"]),
                row["message"],
                datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    @staticmethod
    def _record(row: sqlite3.Row) -> TicketRecord:
        return TicketRecord(
            row["ticket_id"],
            row["owner_subject_id"],
            TicketStatus(row["status"]),
            row["external_system"],
            row["external_reference"],
            datetime.fromisoformat(row["created_at"]),
            datetime.fromisoformat(row["updated_at"]),
        )


class StoredTicketStatusProvider:
    """Development provider that returns the trusted locally stored status."""

    def get_status(self, ticket: TicketRecord) -> TicketStatusObservation:
        return TicketStatusObservation(
            True, ticket.status, ticket.external_reference
        )


class JiraStatusTransport(Protocol):
    def get_json(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse: ...


class UrllibJiraStatusTransport:
    def get_json(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return HttpResponse(
                error.code, error.read().decode("utf-8", errors="replace")
            )
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ConnectionError("Jira status request failed") from None


class JiraTicketStatusProvider:
    """Read-only Jira status lookup; credentials remain inside the transport."""

    def __init__(
        self,
        config: JiraConfig,
        transport: JiraStatusTransport | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.transport = transport or UrllibJiraStatusTransport()

    def get_status(self, ticket: TicketRecord) -> TicketStatusObservation:
        credential = base64.b64encode(
            f"{self.config.user_email}:{self.config.api_token}".encode("utf-8")
        ).decode("ascii")
        headers = {"Accept": "application/json", "Authorization": f"Basic {credential}"}
        try:
            response = self.transport.get_json(
                f"{self.config.base_url}/rest/api/3/issue/"
                f"{quote(ticket.external_reference, safe='')}?fields=status",
                headers,
                self.config.timeout_seconds,
            )
        except ConnectionError:
            return TicketStatusObservation(
                False, TicketStatus.UNKNOWN, ticket.external_reference, "NETWORK"
            )
        if response.status != 200:
            category = {
                401: "AUTHENTICATION",
                403: "PERMISSION",
                404: "NOT_FOUND",
                429: "RATE_LIMIT",
            }.get(response.status, "SERVER" if response.status >= 500 else "JIRA_ERROR")
            return TicketStatusObservation(
                False, TicketStatus.UNKNOWN, ticket.external_reference, category
            )
        try:
            value = json.loads(response.body)
            name = str(value["fields"]["status"]["name"])
        except (json.JSONDecodeError, KeyError, TypeError):
            return TicketStatusObservation(
                False, TicketStatus.UNKNOWN, ticket.external_reference, "INVALID_RESPONSE"
            )
        return TicketStatusObservation(
            True, normalize_ticket_status(name), ticket.external_reference
        )


class TicketLifecycleService:
    """Registers creations and performs owner-authorized status observation."""

    _ticket_id = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")

    def __init__(
        self,
        store: TicketLifecycleStore,
        status_provider: TicketStatusProvider,
        audit_sink: AuditSink | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.status_provider = status_provider
        self.audit_sink = audit_sink or NullAuditSink()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def record_created(
        self, result: ToolResult, identity: IdentityContext
    ) -> TicketRecord:
        if not result.success or result.tool_name != "create_it_ticket":
            raise TicketLifecycleError("Only successful IT ticket creation is tracked")
        ticket_id = result.data.get("issue_key") or result.data.get("ticket_id")
        if not isinstance(ticket_id, str) or not self._ticket_id.fullmatch(ticket_id):
            raise TicketLifecycleError("Ticket creation returned an invalid identifier")
        external_system = "jira" if result.data.get("issue_key") else "local-mock"
        external_reference = str(result.data.get("issue_key") or ticket_id)
        status = normalize_ticket_status(str(result.data.get("status", "OPEN")))
        if status is TicketStatus.UNKNOWN:
            status = TicketStatus.OPEN
        now = self._now()
        record = self.store.register(
            TicketRecord(
                ticket_id,
                identity.subject_id,
                status,
                external_system,
                external_reference,
                now,
                now,
            )
        )
        self._audit(identity, "TICKET_REGISTERED", record, None, "AUTHORIZED")
        return record

    def lookup(
        self, ticket_id: str, identity: IdentityContext
    ) -> TicketStatusResult:
        ticket_id = ticket_id.strip().upper()
        if not self._ticket_id.fullmatch(ticket_id):
            return self._failure(ticket_id, "INVALID_TICKET_ID")
        if (
            not identity.authenticated
            or "action.it_ticket.read_own" not in identity.permissions
        ):
            self._audit_denied(identity, ticket_id, "PERMISSION_DENIED")
            return self._denied(ticket_id)
        ticket = self.store.get(ticket_id)
        if ticket is None:
            return self._failure(ticket_id, "NOT_FOUND")
        if ticket.owner_subject_id != identity.subject_id:
            self._audit_denied(identity, ticket_id, "OWNERSHIP_MISMATCH")
            return self._denied(ticket_id)

        try:
            observation = self.status_provider.get_status(ticket)
        except Exception:
            observation = TicketStatusObservation(
                False, TicketStatus.UNKNOWN, ticket.external_reference, "PROVIDER_ERROR"
            )
        if (
            not isinstance(observation, TicketStatusObservation)
            or not isinstance(observation.status, TicketStatus)
            or observation.external_reference != ticket.external_reference
        ):
            observation = TicketStatusObservation(
                False, TicketStatus.UNKNOWN, ticket.external_reference, "INVALID_RESPONSE"
            )

        current = self.store.get(ticket_id)
        if (
            current is None
            or current.owner_subject_id != identity.subject_id
            or "action.it_ticket.read_own" not in identity.permissions
        ):
            self._audit_denied(identity, ticket_id, "REVALIDATION_FAILED")
            return self._denied(ticket_id)
        if not observation.success:
            self._audit(identity, "TICKET_STATUS_FAILED", current, None, "AUTHORIZED", observation.error_category)
            return TicketStatusResult(
                False,
                ticket_id,
                TicketStatus.UNKNOWN,
                "I couldn't verify the current status of that ticket.",
                error_category=observation.error_category,
            )

        updated, notification = self.store.observe_status(ticket_id, observation.status)
        self._audit(identity, "TICKET_STATUS_OBSERVED", updated, ticket.status, "AUTHORIZED")
        return TicketStatusResult(
            True,
            ticket_id,
            updated.status,
            f"Your IT ticket {ticket_id} is {status_label(updated.status)}.",
            notification=notification,
        )

    def notifications_for(self, identity: IdentityContext) -> list[NotificationEvent]:
        if not identity.authenticated:
            return []
        return self.store.notifications_for(identity.subject_id)

    def _audit(
        self,
        identity: IdentityContext,
        stage: str,
        ticket: TicketRecord,
        previous_status: TicketStatus | None,
        authorization_outcome: str,
        error_category: str | None = None,
    ) -> None:
        try:
            self.audit_sink.record(
                make_audit_event(
                    stage,
                    "get_it_ticket_status",
                    {"ticket_id": ticket.ticket_id},
                    success=error_category is None,
                    external_resource_id=ticket.external_reference,
                    error_category=error_category,
                    subject_id=identity.subject_id,
                    identity_source=identity.identity_source,
                    authenticated=identity.authenticated,
                    authorization_outcome=authorization_outcome,
                    lifecycle_status_from=(previous_status.value if previous_status else None),
                    lifecycle_status_to=ticket.status.value,
                )
            )
        except Exception:
            pass

    def _audit_denied(
        self, identity: IdentityContext, ticket_id: str, category: str
    ) -> None:
        try:
            self.audit_sink.record(
                make_audit_event(
                    "TICKET_STATUS_DENIED",
                    "get_it_ticket_status",
                    {"ticket_id": ticket_id},
                    success=False,
                    error_category=category,
                    subject_id=identity.subject_id,
                    identity_source=identity.identity_source,
                    authenticated=identity.authenticated,
                    authorization_outcome="DENIED",
                )
            )
        except Exception:
            pass

    @staticmethod
    def _denied(ticket_id: str) -> TicketStatusResult:
        return TicketStatusResult(
            False,
            ticket_id,
            TicketStatus.UNKNOWN,
            "You don't have permission to access that ticket.",
            permission_denied=True,
            error_category="PERMISSION_DENIED",
        )

    @staticmethod
    def _failure(ticket_id: str, category: str) -> TicketStatusResult:
        message = (
            "I couldn't find a tracked ticket with that ID."
            if category == "NOT_FOUND"
            else "I couldn't safely check that ticket status."
        )
        return TicketStatusResult(
            False, ticket_id, TicketStatus.UNKNOWN, message, error_category=category
        )


class TicketStatusRequestParser:
    _pattern = re.compile(
        r"\b(?:status\s+of|status\s+for|check\s+(?:the\s+)?status\s+of)\s+"
        r"(?:my\s+)?(?:it\s+)?ticket\s+([A-Z][A-Z0-9_]*-\d+)\b",
        re.I,
    )

    def parse(self, employee_request: str) -> str | None:
        if not isinstance(employee_request, str):
            return None
        match = self._pattern.search(employee_request)
        return match.group(1).upper() if match else None


def normalize_ticket_status(value: str) -> TicketStatus:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")
    if normalized in {"open", "created", "new", "to_do", "todo", "backlog"}:
        return TicketStatus.OPEN
    if normalized in {"in_progress", "inprogress", "working", "active"}:
        return TicketStatus.IN_PROGRESS
    if normalized in {"resolved", "done", "fixed"}:
        return TicketStatus.RESOLVED
    if normalized in {"closed", "complete", "completed"}:
        return TicketStatus.CLOSED
    return TicketStatus.UNKNOWN


def status_label(status: TicketStatus) -> str:
    return {
        TicketStatus.OPEN: "open",
        TicketStatus.IN_PROGRESS: "in progress",
        TicketStatus.RESOLVED: "resolved",
        TicketStatus.CLOSED: "closed",
        TicketStatus.UNKNOWN: "unknown",
    }[status]


def _notification_message(ticket_id: str, status: TicketStatus) -> str:
    if status is TicketStatus.OPEN:
        return f"Your IT ticket {ticket_id} is now open."
    if status is TicketStatus.IN_PROGRESS:
        return f"Your IT ticket {ticket_id} is now in progress."
    if status is TicketStatus.RESOLVED:
        return f"Your IT ticket {ticket_id} has been resolved."
    if status is TicketStatus.CLOSED:
        return f"Your IT ticket {ticket_id} has been closed."
    return f"The status of your IT ticket {ticket_id} is unknown."
