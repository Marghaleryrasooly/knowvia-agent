from __future__ import annotations

from typing import Mapping

from knowvia.authorization import ActionAuthorizer
from knowvia.identity import IdentityContext


KNOWLEDGE_PERMISSION_BY_SKILL: Mapping[str, str] = {
    "hr": "knowledge.hr.read",
    "it_support": "knowledge.it.read",
    "finance": "knowledge.finance.read",
}

ACTION_PERMISSION_BY_TOOL: Mapping[str, str] = {
    "create_it_ticket": "action.it_ticket.create_own",
}

KNOWLEDGE_SOURCES_BY_PERMISSION: Mapping[str, tuple[str, ...]] = {
    "knowledge.hr.read": (
        "01_employee_handbook.md",
        "02_remote_work_policy.md",
        "04_employee_benefits_guide.md",
    ),
    "knowledge.it.read": (
        "03_it_access_security_policy.md",
        "05_it_setup_guide.md",
        "06_engineering_practices.md",
        "07_information_classification_policy.md",
    ),
    "knowledge.finance.read": ("08_expense_policy.md",),
}


class IdentityKnowledgeSourcePolicy:
    """Build a general retrieval scope solely from trusted permissions."""

    def authorized_sources(self, identity: IdentityContext) -> tuple[str, ...]:
        if not identity.authenticated:
            return ()
        sources: list[str] = []
        for permission, permission_sources in KNOWLEDGE_SOURCES_BY_PERMISSION.items():
            if permission in identity.permissions:
                sources.extend(permission_sources)
        return tuple(dict.fromkeys(sources))


class IdentityPermissionPolicy:
    """Deterministic permission checks over a trusted identity context."""

    def can_access_skill(self, identity: IdentityContext, skill_id: str) -> bool:
        required = KNOWLEDGE_PERMISSION_BY_SKILL.get(skill_id)
        return bool(identity.authenticated and required in identity.permissions)

    def can_use_tool(self, identity: IdentityContext, tool_name: str) -> bool:
        required = ACTION_PERMISSION_BY_TOOL.get(tool_name)
        return bool(identity.authenticated and required in identity.permissions)


class IdentityActionAuthorizer:
    """Adds identity permission checks to the existing action authorizer."""

    def __init__(
        self,
        identity: IdentityContext,
        permission_policy: IdentityPermissionPolicy,
        delegate: ActionAuthorizer,
    ) -> None:
        self.identity = identity
        self.permission_policy = permission_policy
        self.delegate = delegate

    def authorize(self, tool_name: str, arguments) -> bool:
        return (
            self.permission_policy.can_use_tool(self.identity, tool_name)
            and self.delegate.authorize(tool_name, arguments)
        )
