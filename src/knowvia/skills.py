from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class SkillRegistryError(ValueError):
    """A skill definition or lookup could not be validated safely."""


@dataclass(frozen=True)
class SkillSummary:
    """Lightweight metadata safe to expose during skill discovery."""

    skill_id: str
    name: str
    description: str


@dataclass(frozen=True)
class SkillDefinition:
    """Full skill details loaded only after a skill has been selected."""

    skill_id: str
    name: str
    description: str
    instructions: str
    allowed_tools: tuple[str, ...]
    knowledge_sources: tuple[str, ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", self.skill_id):
            raise SkillRegistryError("Skill identifiers must be stable snake_case names")
        for field_name in ("name", "description", "instructions"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise SkillRegistryError(f"Skill {field_name} must be a non-empty string")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise SkillRegistryError("Skill tool names must be unique")
        if len(set(self.knowledge_sources)) != len(self.knowledge_sources):
            raise SkillRegistryError("Skill knowledge sources must be unique")
        if not all(isinstance(name, str) and name.strip() for name in self.allowed_tools):
            raise SkillRegistryError("Skill tool names must be non-empty strings")
        if not self.knowledge_sources or not all(
            isinstance(source, str) and source.strip() for source in self.knowledge_sources
        ):
            raise SkillRegistryError("Skill knowledge sources must be non-empty strings")

    @property
    def summary(self) -> SkillSummary:
        return SkillSummary(self.skill_id, self.name, self.description)


class SkillRegistry:
    """Separates lightweight discovery from explicit full-skill loading."""

    def __init__(self, definitions: list[SkillDefinition]) -> None:
        self._definitions: dict[str, SkillDefinition] = {}
        for definition in definitions:
            if not isinstance(definition, SkillDefinition):
                raise SkillRegistryError("Registry entries must be SkillDefinition objects")
            if definition.skill_id in self._definitions:
                raise SkillRegistryError(f"Duplicate skill identifier: {definition.skill_id}")
            self._definitions[definition.skill_id] = definition

    def discover(self) -> tuple[SkillSummary, ...]:
        return tuple(definition.summary for definition in self._definitions.values())

    def load(self, skill_id: str) -> SkillDefinition:
        try:
            return self._definitions[skill_id]
        except (KeyError, TypeError) as error:
            raise SkillRegistryError("Unknown skill identifier") from error


class SkillSelectionStatus(str, Enum):
    SELECTED = "SELECTED"
    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class SkillSelection:
    status: SkillSelectionStatus
    skill_id: str | None = None
    reason: str = ""


class SkillSelector(Protocol):
    def select(
        self, employee_request: str, available_skills: tuple[SkillSummary, ...]
    ) -> SkillSelection: ...


class DeterministicSkillSelector:
    """Small domain selector that sees summaries, never complete skills."""

    _terms = {
        "hr": (
            "vacation", "annual leave", "sick leave", "holiday", "probation",
            "benefit", "insurance", "dental", "wellness", "learning budget",
            "remote work", "work from home", "working hours", "dependent",
        ),
        "it_support": (
            "it ticket", "support ticket", "helpdesk", "vpn", "laptop",
            "password", "multi-factor", "mfa", "account access", "work account",
            "company account", "corporate account", "email",
            "software", "device", "source code", "code review", "main branch",
            "secret", "confidential", "restricted", "production deployment",
        ),
        "finance": (
            "expense", "reimbursement", "reimburse", "receipt", "meal",
            "hotel", "ground transportation", "taxi", "business trip",
        ),
    }

    def select(
        self, employee_request: str, available_skills: tuple[SkillSummary, ...]
    ) -> SkillSelection:
        if not all(isinstance(summary, SkillSummary) for summary in available_skills):
            raise TypeError("Skill selectors may receive SkillSummary objects only")
        request = employee_request.strip().casefold()
        if not request:
            return SkillSelection(
                SkillSelectionStatus.AMBIGUOUS,
                reason="The employee request is empty.",
            )
        available = {summary.skill_id for summary in available_skills}
        scores = {
            skill_id: sum(term in request for term in terms)
            for skill_id, terms in self._terms.items()
            if skill_id in available
        }
        if not scores or max(scores.values(), default=0) == 0:
            return SkillSelection(
                SkillSelectionStatus.UNSUPPORTED,
                reason="No available skill matches the request.",
            )
        best_score = max(scores.values())
        winners = [skill_id for skill_id, score in scores.items() if score == best_score]
        if len(winners) != 1:
            return SkillSelection(
                SkillSelectionStatus.AMBIGUOUS,
                reason="The request matches more than one skill equally.",
            )
        return SkillSelection(
            SkillSelectionStatus.SELECTED,
            skill_id=winners[0],
            reason="One available skill has the strongest deterministic domain match.",
        )


def initial_skill_definitions() -> list[SkillDefinition]:
    return [
        SkillDefinition(
            skill_id="hr",
            name="HR",
            description="Employee policies, leave, remote work, benefits, and eligibility.",
            instructions=(
                "Use company HR policy evidence for employee-policy questions. "
                "Do not infer undocumented eligibility, exceptions, or entitlements."
            ),
            allowed_tools=(),
            knowledge_sources=(
                "01_employee_handbook.md",
                "02_remote_work_policy.md",
                "04_employee_benefits_guide.md",
            ),
        ),
        SkillDefinition(
            skill_id="it_support",
            name="IT Support",
            description="VPN, laptop, software, account, engineering, and security support.",
            instructions=(
                "Use technical and security documentation for knowledge requests. "
                "A ticket may only be proposed for an explicit employee action request."
            ),
            allowed_tools=("create_it_ticket",),
            knowledge_sources=(
                "03_it_access_security_policy.md",
                "05_it_setup_guide.md",
                "06_engineering_practices.md",
                "07_information_classification_policy.md",
            ),
        ),
        SkillDefinition(
            skill_id="finance",
            name="Finance",
            description="Business expenses, travel costs, receipts, and reimbursements.",
            instructions=(
                "Use the company expense policy for finance questions. "
                "Do not invent approval, reimbursement, or exception rules."
            ),
            allowed_tools=(),
            knowledge_sources=("08_expense_policy.md",),
        ),
        SkillDefinition(
            skill_id="general_company_knowledge",
            name="General Company Knowledge",
            description=(
                "General company-information questions that do not match a specialized domain."
            ),
            instructions=(
                "Use only the identity's permission-filtered company sources. "
                "This capability is knowledge-only and must never expose action tools."
            ),
            allowed_tools=(),
            knowledge_sources=(
                "01_employee_handbook.md",
                "02_remote_work_policy.md",
                "03_it_access_security_policy.md",
                "04_employee_benefits_guide.md",
                "05_it_setup_guide.md",
                "06_engineering_practices.md",
                "07_information_classification_policy.md",
                "08_expense_policy.md",
            ),
        ),
    ]


def create_default_skill_registry() -> SkillRegistry:
    return SkillRegistry(initial_skill_definitions())
