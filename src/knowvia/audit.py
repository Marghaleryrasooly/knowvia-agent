from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    timestamp: str
    stage: str
    tool_name: str
    proposal_id: str | None
    sanitized_arguments: dict[str, str]
    success: bool | None
    external_resource_id: str | None = None
    error_category: str | None = None
    subject_id: str | None = None
    identity_source: str | None = None
    authenticated: bool | None = None
    authorization_outcome: str | None = None
    lifecycle_status_from: str | None = None
    lifecycle_status_to: str | None = None


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class NullAuditSink:
    def record(self, event: AuditEvent) -> None:
        return None


class JsonLinesAuditSink:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, event: AuditEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(asdict(event), sort_keys=True) + "\n")


_SECRET_PATTERN = re.compile(
    r"(?i)(api[_ -]?token|authorization|password|secret)\s*[:=]\s*\S+"
)


def sanitize_arguments(arguments: Mapping[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for name, value in arguments.items():
        if any(part in name.casefold() for part in ("token", "secret", "password", "authorization")):
            sanitized[name] = "[REDACTED]"
        else:
            sanitized[name] = _SECRET_PATTERN.sub(r"\1=[REDACTED]", value)[:300]
    return sanitized


def make_audit_event(
    stage: str,
    tool_name: str,
    arguments: Mapping[str, str],
    proposal_id: str | None = None,
    success: bool | None = None,
    external_resource_id: str | None = None,
    error_category: str | None = None,
    subject_id: str | None = None,
    identity_source: str | None = None,
    authenticated: bool | None = None,
    authorization_outcome: str | None = None,
    lifecycle_status_from: str | None = None,
    lifecycle_status_to: str | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_id=uuid.uuid4().hex,
        timestamp=datetime.now(timezone.utc).isoformat(),
        stage=stage,
        tool_name=tool_name,
        proposal_id=proposal_id,
        sanitized_arguments=sanitize_arguments(arguments),
        success=success,
        external_resource_id=external_resource_id,
        error_category=error_category,
        subject_id=subject_id,
        identity_source=identity_source,
        authenticated=authenticated,
        authorization_outcome=authorization_outcome,
        lifecycle_status_from=lifecycle_status_from,
        lifecycle_status_to=lifecycle_status_to,
    )
