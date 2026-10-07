from __future__ import annotations

from typing import Mapping, Protocol


class ActionAuthorizer(Protocol):
    def authorize(self, tool_name: str, arguments: Mapping[str, str]) -> bool: ...


class AllowRegisteredActionAuthorizer:
    """M6 local policy; production identity/RBAC intentionally comes later."""

    def __init__(self, allowed_tools: set[str]) -> None:
        self.allowed_tools = set(allowed_tools)

    def authorize(self, tool_name: str, arguments: Mapping[str, str]) -> bool:
        return tool_name in self.allowed_tools
