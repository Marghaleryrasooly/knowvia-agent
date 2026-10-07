from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from jsonschema import Draft202012Validator
from mcp import Client

from knowvia.agent_models import ToolResult
from knowvia.agent_tools import ToolSpecification, ToolValidationError
from knowvia.ticket_lifecycle import (
    TicketRecord,
    TicketStatus,
    TicketStatusObservation,
    normalize_ticket_status,
)


class MCPIntegrationError(RuntimeError):
    """A sanitized MCP discovery, transport, or contract failure."""


@dataclass(frozen=True)
class MCPDiscoveredTool:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class MCPCallResponse:
    is_error: bool
    structured_content: object


class MCPTransport(Protocol):
    def discover_tools(self) -> tuple[MCPDiscoveredTool, ...]: ...

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> MCPCallResponse: ...


class OfficialMCPClientTransport:
    """Synchronous adapter over the official SDK's async MCP client."""

    def __init__(self, server_or_transport: object, timeout_seconds: float = 15.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("MCP timeout must be positive")
        self.server_or_transport = server_or_transport
        self.timeout_seconds = timeout_seconds

    def discover_tools(self) -> tuple[MCPDiscoveredTool, ...]:
        async def operation() -> tuple[MCPDiscoveredTool, ...]:
            async with Client(
                self.server_or_transport,
                read_timeout_seconds=self.timeout_seconds,
                raise_exceptions=True,
            ) as client:
                result = await client.list_tools()
                discovered = []
                for tool in result.tools:
                    schema = tool.input_schema
                    if not isinstance(schema, dict):
                        raise MCPIntegrationError("MCP tool schema is invalid")
                    discovered.append(
                        MCPDiscoveredTool(
                            tool.name,
                            tool.description or "",
                            dict(schema),
                        )
                    )
                return tuple(discovered)

        return self._run(operation())

    def call_tool(self, name: str, arguments: Mapping[str, object]) -> MCPCallResponse:
        async def operation() -> MCPCallResponse:
            async with Client(
                self.server_or_transport,
                read_timeout_seconds=self.timeout_seconds,
                raise_exceptions=True,
            ) as client:
                result = await client.call_tool(name, dict(arguments))
                return MCPCallResponse(bool(result.is_error), result.structured_content)

        return self._run(operation())

    @staticmethod
    def _run(awaitable):
        try:
            return asyncio.run(awaitable)
        except MCPIntegrationError:
            raise
        except Exception:
            raise MCPIntegrationError("MCP server is unavailable or returned an invalid response") from None


def discover_tool_specifications(
    transport: MCPTransport,
) -> dict[str, ToolSpecification]:
    """Validate discovered schemas without granting authorization."""
    specifications: dict[str, ToolSpecification] = {}
    for tool in transport.discover_tools():
        if not tool.name or tool.name in specifications:
            raise MCPIntegrationError("MCP discovered an invalid or duplicate tool")
        schema = tool.input_schema
        if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
            raise MCPIntegrationError("MCP tool schema must describe an object")
        required = schema.get("required", [])
        if (
            not isinstance(required, list)
            or not all(isinstance(name, str) and name for name in required)
            or len(required) != len(set(required))
            or any(name not in schema["properties"] for name in required)
        ):
            raise MCPIntegrationError("MCP tool required arguments are invalid")
        Draft202012Validator.check_schema(schema)
        specifications[tool.name] = ToolSpecification(
            name=tool.name,
            description=tool.description,
            required_arguments=tuple(required),
            argument_schema=dict(schema),
        )
    return specifications


class MCPAgentTool:
    """Maps one discovered MCP tool to Knowvia's existing AgentTool contract."""

    def __init__(
        self,
        transport: MCPTransport,
        tool_name: str,
        *,
        requires_confirmation: bool,
    ) -> None:
        specifications = discover_tool_specifications(transport)
        discovered = specifications.get(tool_name)
        if discovered is None:
            raise MCPIntegrationError("Required MCP tool is unavailable")
        self.transport = transport
        self.specification = ToolSpecification(
            discovered.name,
            discovered.description,
            discovered.required_arguments,
            discovered.argument_schema,
            requires_confirmation,
        )

    def validate_arguments(self, arguments: Mapping[str, object]) -> dict[str, str]:
        try:
            Draft202012Validator(self.specification.argument_schema).validate(dict(arguments))
        except Exception:
            raise ToolValidationError("MCP tool arguments do not match the discovered schema") from None
        if set(arguments) != set(self.specification.required_arguments):
            raise ToolValidationError("MCP tool arguments must exactly match required arguments")
        validated: dict[str, str] = {}
        for name in self.specification.required_arguments:
            value = arguments[name]
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 500:
                raise ToolValidationError(f"Argument '{name}' must be a bounded non-empty string")
            validated[name] = value.strip()
        return validated

    def execute(self, arguments: Mapping[str, str]) -> ToolResult:
        validated = self.validate_arguments(arguments)
        try:
            response = self.transport.call_tool(self.specification.name, validated)
            return _validate_tool_result(response, self.specification.name)
        except MCPIntegrationError:
            return ToolResult(
                False,
                self.specification.name,
                "The requested action could not be completed.",
                error="The MCP capability is unavailable.",
                error_category="MCP_UNAVAILABLE",
            )


class MCPTicketStatusProvider:
    """Trusted status-provider adapter called only after local ownership checks."""

    def __init__(self, transport: MCPTransport) -> None:
        specifications = discover_tool_specifications(transport)
        specification = specifications.get("get_ticket_status")
        if specification is None or specification.required_arguments != ("ticket_id",):
            raise MCPIntegrationError("Required MCP status tool is unavailable")
        self.transport = transport

    def get_status(self, ticket: TicketRecord) -> TicketStatusObservation:
        try:
            response = self.transport.call_tool(
                "get_ticket_status", {"ticket_id": ticket.ticket_id}
            )
            result = _validate_tool_result(response, "get_ticket_status")
        except MCPIntegrationError:
            return TicketStatusObservation(
                False, TicketStatus.UNKNOWN, ticket.external_reference, "MCP_UNAVAILABLE"
            )
        if not result.success:
            return TicketStatusObservation(
                False,
                TicketStatus.UNKNOWN,
                ticket.external_reference,
                result.error_category or "MCP_ERROR",
            )
        status = result.data.get("status")
        reference = result.data.get("external_reference")
        if not isinstance(status, str) or reference != ticket.external_reference:
            return TicketStatusObservation(
                False, TicketStatus.UNKNOWN, ticket.external_reference, "INVALID_RESPONSE"
            )
        normalized = normalize_ticket_status(status)
        if normalized is TicketStatus.UNKNOWN and status.strip().upper() != "UNKNOWN":
            return TicketStatusObservation(
                False, TicketStatus.UNKNOWN, ticket.external_reference, "INVALID_RESPONSE"
            )
        return TicketStatusObservation(True, normalized, ticket.external_reference)


def _validate_tool_result(response: MCPCallResponse, expected_name: str) -> ToolResult:
    value = response.structured_content
    if response.is_error or not isinstance(value, dict):
        raise MCPIntegrationError("MCP tool returned an error or malformed result")
    expected = {"success", "tool_name", "message", "data", "error", "error_category"}
    if set(value) != expected:
        raise MCPIntegrationError("MCP result fields do not match the contract")
    if (
        not isinstance(value["success"], bool)
        or value["tool_name"] != expected_name
        or not isinstance(value["message"], str)
        or not isinstance(value["data"], dict)
        or value["error"] is not None and not isinstance(value["error"], str)
        or value["error_category"] is not None and not isinstance(value["error_category"], str)
        or value["success"] and (value["error"] is not None or value["error_category"] is not None)
    ):
        raise MCPIntegrationError("MCP result values do not match the contract")
    return ToolResult(
        value["success"],
        value["tool_name"],
        value["message"],
        dict(value["data"]),
        value["error"],
        value["error_category"],
    )
