from __future__ import annotations

import argparse

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from knowvia.agent_models import ToolResult
from knowvia.agent_tools import AgentTool, LocalITTicketTool
from knowvia.audit import JsonLinesAuditSink
from knowvia.cli import DEFAULT_AUDIT_LOG, DEFAULT_TICKET_LIFECYCLE
from knowvia.identity import IdentityContext, LocalIdentityProvider
from knowvia.jira import JiraConfig, JiraTicketTool
from knowvia.permissions import IdentityPermissionPolicy
from knowvia.ticket_lifecycle import (
    JiraTicketStatusProvider,
    SqliteTicketLifecycleStore,
    StoredTicketStatusProvider,
    TicketLifecycleService,
)


class MCPToolOutput(BaseModel):
    success: bool
    tool_name: str
    message: str
    data: dict[str, object]
    error: str | None = None
    error_category: str | None = None


def create_ticket_mcp_server(
    ticket_tool: AgentTool,
    lifecycle: TicketLifecycleService,
    identity: IdentityContext,
) -> MCPServer:
    """Expose ticket capabilities; caller-side Knowvia safety remains mandatory."""
    server = MCPServer(
        "knowvia-ticket-capabilities",
        instructions=(
            "Ticket capabilities only. Discovery is not authorization; the Knowvia "
            "host enforces identity, permission, ownership, and confirmation."
        ),
    )

    @server.tool(name="create_it_ticket", structured_output=True)
    def create_it_ticket(title: str, description: str) -> MCPToolOutput:
        """Create an IT support ticket from a validated title and description."""
        if not IdentityPermissionPolicy().can_use_tool(identity, "create_it_ticket"):
            return MCPToolOutput(**_tool_result_dict(
                ToolResult(
                    False,
                    "create_it_ticket",
                    "You don't have permission to access that capability.",
                    error="Permission denied.",
                    error_category="PERMISSION_DENIED",
                )
            ))
        try:
            result = ticket_tool.execute({"title": title, "description": description})
        except Exception:
            result = ToolResult(
                False,
                "create_it_ticket",
                "The requested action could not be completed.",
                error="The ticket capability failed safely.",
                error_category="TOOL_EXECUTION",
            )
        return MCPToolOutput(**_tool_result_dict(result))

    @server.tool(name="get_ticket_status", structured_output=True)
    def get_ticket_status(ticket_id: str) -> MCPToolOutput:
        """Return trusted status for an owner-authorized tracked IT ticket."""
        result = lifecycle.lookup(ticket_id, identity)
        if not result.success:
            return MCPToolOutput(**_tool_result_dict(
                ToolResult(
                    False,
                    "get_ticket_status",
                    result.message,
                    error="Ticket status could not be returned.",
                    error_category=result.error_category or "STATUS_LOOKUP",
                )
            ))
        return MCPToolOutput(**_tool_result_dict(
            ToolResult(
                True,
                "get_ticket_status",
                result.message,
                data={
                    "ticket_id": result.ticket_id,
                    "status": result.status.value,
                    "external_reference": result.ticket_id,
                },
            )
        ))

    return server


def _tool_result_dict(result: ToolResult) -> dict:
    return {
        "success": result.success,
        "tool_name": result.tool_name,
        "message": result.message,
        "data": result.data,
        "error": result.error,
        "error_category": result.error_category,
    }


def build_server(backend: str, identity_id: str) -> MCPServer:
    identity = LocalIdentityProvider().resolve(identity_id)
    store = SqliteTicketLifecycleStore(DEFAULT_TICKET_LIFECYCLE)
    audit = JsonLinesAuditSink(DEFAULT_AUDIT_LOG)
    if backend == "mock":
        tool = LocalITTicketTool()
        provider = StoredTicketStatusProvider()
    elif backend == "jira":
        config = JiraConfig.from_environment()
        tool = JiraTicketTool(config)
        provider = JiraTicketStatusProvider(config)
    else:
        raise ValueError("Unsupported MCP ticket backend")
    return create_ticket_mcp_server(
        tool, TicketLifecycleService(store, provider, audit), identity
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Knowvia local ticket MCP server")
    parser.add_argument("--backend", choices=("mock", "jira"), default="mock")
    parser.add_argument("--identity", required=True)
    args = parser.parse_args()
    build_server(args.backend, args.identity).run("stdio")


if __name__ == "__main__":
    main()
