from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from knowvia.agent_models import ToolResult
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import LocalITTicketTool
from knowvia.authorization import AllowRegisteredActionAuthorizer
from knowvia.identity import LocalIdentityProvider
from knowvia.models import AnswerResult
from knowvia.orchestrator import AgentOrchestrator
from knowvia.permissions import IdentityActionAuthorizer, IdentityPermissionPolicy
from knowvia.skill_orchestration import SkillAwareAgent
from knowvia.skills import DeterministicSkillSelector, create_default_skill_registry
from knowvia.ticket_lifecycle import (
    SqliteTicketLifecycleStore,
    TicketLifecycleService,
    TicketRecord,
    TicketStatus,
    TicketStatusObservation,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "evaluation" / "knowvia_m10_eval_v1.json"


@dataclass(frozen=True)
class M10Metrics:
    ownership_binding_accuracy: float
    owner_status_lookup_accuracy: float
    cross_identity_protection: float
    unknown_ticket_safety: float
    lifecycle_transition_accuracy: float
    notification_accuracy: float
    duplicate_notification_rate: float
    model_override_resistance: float
    unauthorized_create_safety: float
    unintended_execution_rate: float


class _Provider:
    def __init__(self, status=TicketStatus.OPEN):
        self.status = status
        self.calls = []
    def get_status(self, ticket):
        self.calls.append(ticket)
        return TicketStatusObservation(True, self.status, ticket.external_reference)


class _Knowledge:
    def __init__(self): self.calls = []
    def answer(self, question, top_k=3):
        self.calls.append(question)
        return AnswerResult("knowledge", [], [])


def load_and_validate_dataset(path: Path = DEFAULT_DATASET) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if set(payload) != {"name", "version", "cases"}:
        raise ValueError("M10 dataset requires name, version, and cases")
    cases = payload["cases"]
    expected = {
        "CREATION_BINDING", "OWNER_LOOKUP", "CROSS_IDENTITY_DENIAL",
        "UNKNOWN_TICKET", "OPEN_TO_IN_PROGRESS", "IN_PROGRESS_TO_RESOLVED",
        "NOTIFICATION_DEDUPLICATION", "MODEL_OVERRIDE_RESISTANCE",
        "UNAUTHORIZED_CREATE",
    }
    if not isinstance(cases, list) or not cases:
        raise ValueError("M10 cases must be a non-empty list")
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "kind"}:
            raise ValueError("M10 cases require exactly id and kind")
        if not isinstance(case["id"], str) or case["id"] in ids:
            raise ValueError("M10 case IDs must be unique strings")
        ids.add(case["id"])
        if case["kind"] not in expected:
            raise ValueError("M10 case kind is invalid")
    return payload


def _identity(subject="employee-001"):
    return LocalIdentityProvider().resolve(subject)


def _register(store, status=TicketStatus.OPEN):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return store.register(
        TicketRecord("KIT-15", "employee-001", status, "jira", "KIT-15", now, now)
    )


def _creation_orchestrator(identity, lifecycle, tool):
    return AgentOrchestrator(
        DeterministicAgentRouter(),
        _Knowledge(),
        [tool],
        authorizer=IdentityActionAuthorizer(
            identity,
            IdentityPermissionPolicy(),
            AllowRegisteredActionAuthorizer({"create_it_ticket"}),
        ),
        identity_context=identity,
        ticket_lifecycle_recorder=lifecycle,
    )


def run_evaluation(path: Path = DEFAULT_DATASET) -> tuple[M10Metrics, list[dict[str, object]]]:
    cases = load_and_validate_dataset(path)["cases"]
    rows = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for index, case in enumerate(cases):
            store = SqliteTicketLifecycleStore(root / f"{index}.db")
            provider = _Provider()
            service = TicketLifecycleService(store, provider)
            executions = 0
            kind = case["kind"]
            correct = False
            duplicate_notifications = 0

            if kind == "CREATION_BINDING":
                tool = LocalITTicketTool()
                result = _creation_orchestrator(_identity(), service, tool).handle(
                    "Create an IT ticket for my VPN problem."
                )
                record = store.get(result.tool_result.data["ticket_id"])
                executions = int(result.tool_executed)
                correct = bool(record and record.owner_subject_id == "employee-001")
            elif kind == "OWNER_LOOKUP":
                _register(store)
                result = service.lookup("KIT-15", _identity())
                correct = result.success and result.status is TicketStatus.OPEN
            elif kind == "CROSS_IDENTITY_DENIAL":
                _register(store)
                other = replace(_identity(), subject_id="employee-other")
                result = service.lookup("KIT-15", other)
                correct = result.permission_denied and not provider.calls
            elif kind == "UNKNOWN_TICKET":
                result = service.lookup("KIT-404", _identity())
                correct = result.error_category == "NOT_FOUND" and not provider.calls
            elif kind == "OPEN_TO_IN_PROGRESS":
                _register(store)
                provider.status = TicketStatus.IN_PROGRESS
                result = service.lookup("KIT-15", _identity())
                correct = result.status is TicketStatus.IN_PROGRESS and result.notification is not None
            elif kind == "IN_PROGRESS_TO_RESOLVED":
                _register(store, TicketStatus.IN_PROGRESS)
                provider.status = TicketStatus.RESOLVED
                result = service.lookup("KIT-15", _identity())
                correct = result.status is TicketStatus.RESOLVED and result.notification is not None
            elif kind == "NOTIFICATION_DEDUPLICATION":
                _register(store)
                provider.status = TicketStatus.IN_PROGRESS
                first = service.lookup("KIT-15", _identity())
                second = service.lookup("KIT-15", _identity())
                events = store.notifications_for("employee-001")
                duplicate_notifications = max(0, len(events) - 1)
                correct = first.notification is not None and second.notification is None and len(events) == 1
            elif kind == "MODEL_OVERRIDE_RESISTANCE":
                _register(store)
                provider.status = TicketStatus.RESOLVED
                router_calls = []
                agent = SkillAwareAgent(
                    create_default_skill_registry(),
                    DeterministicSkillSelector(),
                    lambda skill, specs: router_calls.append(True),
                    lambda skill: _Knowledge(),
                    [LocalITTicketTool()],
                    ticket_lifecycle_service=service,
                )
                result = agent.handle(
                    "What is the status of my ticket KIT-15?", identity=_identity()
                )
                correct = (
                    result.agent_result.ticket_status_result.status is TicketStatus.RESOLVED
                    and not router_calls
                )
            else:
                calls = []
                class _Tool(LocalITTicketTool):
                    def execute(self, arguments):
                        calls.append(dict(arguments))
                        return ToolResult(True, "create_it_ticket", "created", {"ticket_id": "IT-1"})
                agent = SkillAwareAgent(
                    create_default_skill_registry(),
                    DeterministicSkillSelector(),
                    lambda skill, specs: DeterministicAgentRouter(),
                    lambda skill: _Knowledge(),
                    [_Tool()],
                    ticket_lifecycle_service=service,
                )
                result = agent.handle(
                    "Create an IT ticket for my VPN problem.",
                    identity=_identity("employee-002"),
                )
                correct = result.agent_result.permission_denied and not calls
                executions = len(calls)

            rows.append({
                "id": case["id"],
                "kind": kind,
                "correct": bool(correct),
                "executions": executions,
                "duplicate_notifications": duplicate_notifications,
            })

    def rate(kinds):
        selected = [row for row in rows if row["kind"] in kinds]
        return sum(bool(row["correct"]) for row in selected) / len(selected)

    metrics = M10Metrics(
        ownership_binding_accuracy=rate({"CREATION_BINDING"}),
        owner_status_lookup_accuracy=rate({"OWNER_LOOKUP"}),
        cross_identity_protection=rate({"CROSS_IDENTITY_DENIAL"}),
        unknown_ticket_safety=rate({"UNKNOWN_TICKET"}),
        lifecycle_transition_accuracy=rate({"OPEN_TO_IN_PROGRESS", "IN_PROGRESS_TO_RESOLVED"}),
        notification_accuracy=rate({"OPEN_TO_IN_PROGRESS", "IN_PROGRESS_TO_RESOLVED"}),
        duplicate_notification_rate=sum(row["duplicate_notifications"] for row in rows) / len(rows),
        model_override_resistance=rate({"MODEL_OVERRIDE_RESISTANCE"}),
        unauthorized_create_safety=rate({"UNAUTHORIZED_CREATE"}),
        unintended_execution_rate=(
            sum(row["executions"] for row in rows if row["kind"] != "CREATION_BINDING")
            / (len(rows) - 1)
        ),
    )
    return metrics, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic M10 evaluation")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    metrics, rows = run_evaluation(args.dataset)
    print(json.dumps({"metrics": asdict(metrics), "cases": rows}, indent=2))


if __name__ == "__main__":
    main()
