from __future__ import annotations

import re
from typing import Protocol


class ActionIntentPolicy(Protocol):
    def is_explicit(self, employee_request: str, tool_name: str) -> bool: ...


class ExplicitCurrentTurnActionIntentPolicy:
    """Requires a tool-specific action request in the current employee turn."""

    _ticket_request = re.compile(
        r"\b(?:create|make|open|raise|file|submit|log)\b.{0,50}"
        r"\b(?:(?:it|support|helpdesk|jira)\s+)?(?:ticket|issue)\b",
        re.I,
    )

    def is_explicit(self, employee_request: str, tool_name: str) -> bool:
        if not isinstance(employee_request, str):
            return False
        if tool_name == "create_it_ticket":
            return bool(self._ticket_request.search(employee_request))
        # New tools must opt into an explicit current-turn policy before use.
        return False
