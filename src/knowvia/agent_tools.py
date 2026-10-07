from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from knowvia.agent_models import ToolResult


class ToolValidationError(ValueError):
    """Raised before execution when tool arguments do not match the contract."""


@dataclass(frozen=True)
class ToolSpecification:
    name: str
    description: str
    required_arguments: tuple[str, ...]
    argument_schema: dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False


class AgentTool(Protocol):
    """Stable boundary for local mocks and future enterprise integrations."""

    @property
    def specification(self) -> ToolSpecification: ...

    def validate_arguments(self, arguments: Mapping[str, object]) -> dict[str, str]: ...

    def execute(self, arguments: Mapping[str, str]) -> ToolResult: ...


class ConfirmationRequiredTool:
    """Require confirmation around an existing tool without changing its transport."""

    def __init__(self, delegate: AgentTool) -> None:
        self.delegate = delegate
        specification = delegate.specification
        self.specification = ToolSpecification(
            name=specification.name,
            description=specification.description,
            required_arguments=specification.required_arguments,
            argument_schema=specification.argument_schema,
            requires_confirmation=True,
        )

    def validate_arguments(self, arguments: Mapping[str, object]) -> dict[str, str]:
        return self.delegate.validate_arguments(arguments)

    def execute(self, arguments: Mapping[str, str]) -> ToolResult:
        return self.delegate.execute(arguments)


class LocalITTicketTool:
    """In-memory M5 proof of tool use; it contacts no external system."""

    specification = ToolSpecification(
        name="create_it_ticket",
        description="Create a local mock IT support ticket.",
        required_arguments=("title", "description"),
        argument_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Concise ticket title."},
                "description": {"type": "string", "description": "Employee's IT problem."},
            },
            "required": ["title", "description"],
            "additionalProperties": False,
        },
    )

    def __init__(self) -> None:
        self._next_id = 1

    def validate_arguments(self, arguments: Mapping[str, object]) -> dict[str, str]:
        expected = set(self.specification.required_arguments)
        supplied = set(arguments)
        missing = expected - supplied
        extra = supplied - expected
        if missing:
            raise ToolValidationError(f"Missing required arguments: {', '.join(sorted(missing))}")
        if extra:
            raise ToolValidationError(f"Unknown arguments: {', '.join(sorted(extra))}")

        validated: dict[str, str] = {}
        for name in self.specification.required_arguments:
            value = arguments[name]
            if not isinstance(value, str) or not value.strip():
                raise ToolValidationError(f"Argument '{name}' must be a non-empty string")
            value = value.strip()
            if len(value) > 500:
                raise ToolValidationError(f"Argument '{name}' is too long")
            validated[name] = value
        return validated

    def execute(self, arguments: Mapping[str, str]) -> ToolResult:
        values = self.validate_arguments(arguments)
        ticket_id = f"IT-{self._next_id:04d}"
        self._next_id += 1
        data = {
            "ticket_id": ticket_id,
            "status": "OPEN",
            "title": values["title"],
            "description": values["description"],
        }
        return ToolResult(
            success=True,
            tool_name=self.specification.name,
            message=f"Created local IT support ticket {ticket_id}.",
            data=data,
        )
