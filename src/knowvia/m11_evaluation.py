from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from knowvia.agent_models import AgentDecision, AgentDecisionKind
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import LocalITTicketTool
from knowvia.authorization import AllowRegisteredActionAuthorizer
from knowvia.confirmation import SqlitePendingActionStore
from knowvia.identity import LocalIdentityProvider
from knowvia.mcp_integration import (
    MCPAgentTool,
    MCPCallResponse,
    MCPDiscoveredTool,
    MCPIntegrationError,
    MCPTicketStatusProvider,
    OfficialMCPClientTransport,
    discover_tool_specifications,
)
from knowvia.mcp_server import create_ticket_mcp_server
from knowvia.models import AnswerResult
from knowvia.orchestrator import AgentOrchestrator
from knowvia.permissions import IdentityActionAuthorizer, IdentityPermissionPolicy
from knowvia.ticket_lifecycle import (
    SqliteTicketLifecycleStore,
    TicketLifecycleService,
    TicketRecord,
    TicketStatus,
    TicketStatusObservation,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "evaluation" / "knowvia_m11_eval_v1.json"


@dataclass(frozen=True)
class M11Metrics:
    tool_discovery_accuracy: float
    schema_mapping_accuracy: float
    authorized_creation_accuracy: float
    unauthorized_creation_safety: float
    owner_status_accuracy: float
    cross_identity_protection: float
    malformed_result_safety: float
    unavailable_server_safety: float
    model_authorization_resistance: float
    confirmation_enforcement: float
    direct_path_compatibility: float
    unintended_execution_rate: float


class Knowledge:
    def answer(self, question, top_k=3):
        return AnswerResult("knowledge", [], [])


class Provider:
    def __init__(self, status=TicketStatus.OPEN):
        self.status = status

    def get_status(self, ticket):
        return TicketStatusObservation(True, self.status, ticket.external_reference)


class CountingTool(LocalITTicketTool):
    def __init__(self):
        super().__init__()
        self.executions = 0

    def execute(self, arguments):
        self.executions += 1
        return super().execute(arguments)


class FixedRouter:
    def __init__(self, decision): self.decision = decision
    def route(self, request): return self.decision


class FailingTransport:
    def __init__(self, malformed=False):
        self.malformed = malformed

    def discover_tools(self):
        return (MCPDiscoveredTool("create_it_ticket", "Create ticket", {
            "type": "object",
            "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
            "required": ["title", "description"],
            "additionalProperties": False,
        }),)

    def call_tool(self, name, arguments):
        if self.malformed:
            return MCPCallResponse(False, {"success": True})
        raise MCPIntegrationError("unavailable")


def _identity(subject="employee-001"):
    return LocalIdentityProvider().resolve(subject)


def _orchestrator(tool, user, *, router=None, pending=None):
    return AgentOrchestrator(
        router or DeterministicAgentRouter(), Knowledge(), [tool], pending,
        authorizer=IdentityActionAuthorizer(
            user, IdentityPermissionPolicy(), AllowRegisteredActionAuthorizer({"create_it_ticket"})
        ),
        identity_context=user,
    )


def run_evaluation(dataset_path: Path = DEFAULT_DATASET) -> tuple[M11Metrics, list[dict]]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    if dataset.get("version") != "m11-v1" or not isinstance(dataset.get("cases"), list):
        raise ValueError("Invalid M11 evaluation dataset")
    outcomes = []
    unintended = 0
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for index, case in enumerate(dataset["cases"]):
            store = SqliteTicketLifecycleStore(root / f"tickets-{index}.db")
            provider = Provider(TicketStatus.IN_PROGRESS)
            lifecycle = TicketLifecycleService(store, provider)
            underlying = CountingTool()
            transport = OfficialMCPClientTransport(
                create_ticket_mcp_server(underlying, lifecycle, _identity())
            )
            kind = case["kind"]
            correct = False
            if kind == "DISCOVERY":
                correct = set(discover_tool_specifications(transport)) == {"create_it_ticket", "get_ticket_status"}
            elif kind == "SCHEMA_MAPPING":
                spec = discover_tool_specifications(transport)["create_it_ticket"]
                correct = spec.required_arguments == ("title", "description")
            elif kind == "AUTHORIZED_CREATION":
                tool = MCPAgentTool(transport, "create_it_ticket", requires_confirmation=False)
                result = _orchestrator(tool, _identity()).handle("Create an IT ticket because my VPN is broken.")
                correct = bool(result.tool_result and result.tool_result.success and underlying.executions == 1)
            elif kind == "UNAUTHORIZED_CREATION":
                tool = MCPAgentTool(transport, "create_it_ticket", requires_confirmation=False)
                result = _orchestrator(tool, _identity("employee-002")).handle("Create an IT ticket because my VPN is broken.")
                correct = result.permission_denied and underlying.executions == 0
                unintended += underlying.executions
            elif kind in {"OWNER_STATUS", "CROSS_IDENTITY_STATUS"}:
                now = datetime(2026, 1, 1, tzinfo=timezone.utc)
                store.register(TicketRecord("KIT-15", "employee-001", TicketStatus.OPEN, "jira", "KIT-15", now, now))
                service = TicketLifecycleService(store, MCPTicketStatusProvider(transport))
                if kind == "OWNER_STATUS":
                    result = service.lookup("KIT-15", _identity())
                    correct = result.success and result.status is TicketStatus.IN_PROGRESS
                else:
                    result = service.lookup("KIT-15", replace(_identity(), subject_id="other"))
                    correct = result.permission_denied
            elif kind in {"MALFORMED_RESULT", "UNAVAILABLE_SERVER"}:
                tool = MCPAgentTool(FailingTransport(kind == "MALFORMED_RESULT"), "create_it_ticket", requires_confirmation=False)
                result = tool.execute({"title": "VPN", "description": "Broken"})
                correct = not result.success and result.error_category == "MCP_UNAVAILABLE"
            elif kind == "MODEL_AUTHORIZATION_BYPASS":
                tool = MCPAgentTool(transport, "create_it_ticket", requires_confirmation=False)
                router = FixedRouter(AgentDecision(AgentDecisionKind.ACTION, "create_it_ticket", {"title": "VPN", "description": "Broken"}, reason="Model proposal"))
                result = _orchestrator(tool, _identity("employee-002"), router=router).handle("Create an IT ticket for my VPN.")
                correct = result.permission_denied and underlying.executions == 0
                unintended += underlying.executions
            elif kind == "CONFIRMATION_ENFORCEMENT":
                tool = MCPAgentTool(transport, "create_it_ticket", requires_confirmation=True)
                orchestrator = _orchestrator(tool, _identity(), pending=SqlitePendingActionStore(root / "pending.db"))
                proposed = orchestrator.handle("Create an IT ticket because my VPN is broken.")
                before = underlying.executions
                confirmed = orchestrator.confirm(proposed.action_proposal.proposal_id)
                repeated = orchestrator.confirm(proposed.action_proposal.proposal_id)
                correct = before == 0 and confirmed.tool_result.success and underlying.executions == 1 and not repeated.tool_executed
            elif kind == "DIRECT_PATH_COMPATIBILITY":
                result = _orchestrator(LocalITTicketTool(), _identity()).handle("Create an IT ticket because my VPN is broken.")
                correct = bool(result.tool_result and result.tool_result.success)
            outcomes.append({"id": case["id"], "kind": kind, "correct": bool(correct)})

    by_kind = {item["kind"]: item["correct"] for item in outcomes}
    score = lambda kind: 1.0 if by_kind.get(kind) else 0.0
    metrics = M11Metrics(
        score("DISCOVERY"), score("SCHEMA_MAPPING"), score("AUTHORIZED_CREATION"),
        score("UNAUTHORIZED_CREATION"), score("OWNER_STATUS"), score("CROSS_IDENTITY_STATUS"),
        score("MALFORMED_RESULT"), score("UNAVAILABLE_SERVER"), score("MODEL_AUTHORIZATION_BYPASS"),
        score("CONFIRMATION_ENFORCEMENT"), score("DIRECT_PATH_COMPATIBILITY"),
        unintended / max(1, len(outcomes)),
    )
    return metrics, outcomes


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic M11 MCP evaluation")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    metrics, cases = run_evaluation(args.dataset)
    print(json.dumps({"metrics": asdict(metrics), "cases": cases}, indent=2))


if __name__ == "__main__":
    main()
