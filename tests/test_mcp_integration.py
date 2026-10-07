from dataclasses import replace
from datetime import datetime, timezone

import pytest

from knowvia.agent_models import AgentDecision, AgentDecisionKind, ToolResult
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
    StoredTicketStatusProvider,
    TicketLifecycleService,
    TicketRecord,
    TicketStatus,
)


class Knowledge:
    def answer(self, question, top_k=3):
        return AnswerResult("knowledge", [], [])


class CountingTicketTool(LocalITTicketTool):
    def __init__(self):
        super().__init__()
        self.executions = 0

    def execute(self, arguments):
        self.executions += 1
        return super().execute(arguments)


class FixedRouter:
    def __init__(self, decision):
        self.decision = decision

    def route(self, request):
        return self.decision


def identity(subject="employee-001"):
    return LocalIdentityProvider().resolve(subject)


def runtime(tmp_path, owner="employee-001", provider_status=TicketStatus.OPEN):
    store = SqliteTicketLifecycleStore(tmp_path / "tickets.db")

    class Provider:
        def get_status(self, ticket):
            from knowvia.ticket_lifecycle import TicketStatusObservation
            return TicketStatusObservation(True, provider_status, ticket.external_reference)

    lifecycle = TicketLifecycleService(store, Provider())
    underlying = CountingTicketTool()
    server = create_ticket_mcp_server(underlying, lifecycle, identity(owner))
    transport = OfficialMCPClientTransport(server)
    return store, lifecycle, underlying, transport


def authorized_orchestrator(tool, user):
    return AgentOrchestrator(
        DeterministicAgentRouter(),
        Knowledge(),
        [tool],
        authorizer=IdentityActionAuthorizer(
            user,
            IdentityPermissionPolicy(),
            AllowRegisteredActionAuthorizer({"create_it_ticket"}),
        ),
        identity_context=user,
    )


def test_official_mcp_tool_discovery_and_schema_mapping(tmp_path) -> None:
    _, _, _, transport = runtime(tmp_path)

    specifications = discover_tool_specifications(transport)

    assert set(specifications) == {"create_it_ticket", "get_ticket_status"}
    assert specifications["create_it_ticket"].required_arguments == ("title", "description")
    assert specifications["get_ticket_status"].required_arguments == ("ticket_id",)


def test_authorized_ticket_creation_through_mcp(tmp_path) -> None:
    _, _, underlying, transport = runtime(tmp_path)
    tool = MCPAgentTool(transport, "create_it_ticket", requires_confirmation=False)

    result = authorized_orchestrator(tool, identity()).handle(
        "Create an IT ticket because my VPN is broken."
    )

    assert result.tool_result and result.tool_result.success
    assert result.tool_result.data["ticket_id"] == "IT-0001"
    assert underlying.executions == 1


def test_unauthorized_creation_is_blocked_before_mcp_execution(tmp_path) -> None:
    _, _, underlying, transport = runtime(tmp_path)
    tool = MCPAgentTool(transport, "create_it_ticket", requires_confirmation=False)

    result = authorized_orchestrator(tool, identity("employee-002")).handle(
        "Create an IT ticket because my VPN is broken."
    )

    assert result.permission_denied is True
    assert result.tool_executed is False
    assert underlying.executions == 0


def test_owner_status_lookup_through_mcp_and_cross_identity_denial(tmp_path) -> None:
    store, server_lifecycle, _, transport = runtime(
        tmp_path, provider_status=TicketStatus.IN_PROGRESS
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.register(TicketRecord("KIT-15", "employee-001", TicketStatus.OPEN, "jira", "KIT-15", now, now))
    outer = TicketLifecycleService(store, MCPTicketStatusProvider(transport))

    own = outer.lookup("KIT-15", identity())
    other = outer.lookup("KIT-15", replace(identity(), subject_id="employee-other"))

    assert own.success and own.status is TicketStatus.IN_PROGRESS
    assert other.permission_denied is True


class FakeTransport:
    def __init__(self, response=None, fail_call=False, fail_discovery=False):
        self.response = response
        self.fail_call = fail_call
        self.fail_discovery = fail_discovery
        self.calls = 0

    def discover_tools(self):
        if self.fail_discovery:
            raise MCPIntegrationError("unavailable")
        return (
            MCPDiscoveredTool(
                "create_it_ticket",
                "Create a ticket",
                {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
                    "required": ["title", "description"],
                    "additionalProperties": False,
                },
            ),
        )

    def call_tool(self, name, arguments):
        self.calls += 1
        if self.fail_call:
            raise MCPIntegrationError("unavailable")
        return self.response


def test_malformed_mcp_result_fails_closed() -> None:
    transport = FakeTransport(MCPCallResponse(False, {"success": True}))
    tool = MCPAgentTool(transport, "create_it_ticket", requires_confirmation=False)

    result = tool.execute({"title": "VPN", "description": "Broken"})

    assert result.success is False
    assert result.error_category == "MCP_UNAVAILABLE"


def test_unavailable_mcp_server_fails_safely() -> None:
    with pytest.raises(MCPIntegrationError):
        MCPAgentTool(FakeTransport(fail_discovery=True), "create_it_ticket", requires_confirmation=False)

    transport = FakeTransport(fail_call=True)
    tool = MCPAgentTool(transport, "create_it_ticket", requires_confirmation=False)
    result = tool.execute({"title": "VPN", "description": "Broken"})
    assert result.success is False and result.error_category == "MCP_UNAVAILABLE"


def test_model_action_cannot_bypass_mcp_authorization(tmp_path) -> None:
    _, _, underlying, transport = runtime(tmp_path)
    tool = MCPAgentTool(transport, "create_it_ticket", requires_confirmation=False)
    router = FixedRouter(
        AgentDecision(
            AgentDecisionKind.ACTION,
            "create_it_ticket",
            {"title": "VPN", "description": "Broken"},
            reason="The model proposed the MCP action.",
        )
    )
    user = identity("employee-002")
    orchestrator = AgentOrchestrator(
        router,
        Knowledge(),
        [tool],
        authorizer=IdentityActionAuthorizer(
            user,
            IdentityPermissionPolicy(),
            AllowRegisteredActionAuthorizer({"create_it_ticket"}),
        ),
        identity_context=user,
    )

    result = orchestrator.handle("Create an IT ticket for my VPN.")

    assert result.permission_denied is True
    assert underlying.executions == 0


def test_mcp_real_backend_adapter_preserves_confirmation_and_one_time_execution(tmp_path) -> None:
    _, _, underlying, transport = runtime(tmp_path)
    tool = MCPAgentTool(transport, "create_it_ticket", requires_confirmation=True)
    user = identity()
    orchestrator = AgentOrchestrator(
        DeterministicAgentRouter(),
        Knowledge(),
        [tool],
        SqlitePendingActionStore(tmp_path / "pending.db"),
        authorizer=IdentityActionAuthorizer(
            user,
            IdentityPermissionPolicy(),
            AllowRegisteredActionAuthorizer({"create_it_ticket"}),
        ),
        identity_context=user,
    )

    proposed = orchestrator.handle("Create an IT ticket because my VPN is broken.")
    assert proposed.action_proposal is not None
    assert proposed.tool_executed is False and underlying.executions == 0

    confirmed = orchestrator.confirm(proposed.action_proposal.proposal_id)
    repeated = orchestrator.confirm(proposed.action_proposal.proposal_id)

    assert confirmed.tool_result and confirmed.tool_result.success
    assert underlying.executions == 1
    assert repeated.tool_executed is False


def test_direct_mock_path_remains_compatible() -> None:
    result = authorized_orchestrator(LocalITTicketTool(), identity()).handle(
        "Create an IT ticket because my VPN is broken."
    )
    assert result.tool_result and result.tool_result.success
