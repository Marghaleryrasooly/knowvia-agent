from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from knowvia.agent_models import (
    ActionProposalStatus,
    AgentDecisionKind,
    AgentResult,
)
from knowvia.models import AnswerResult, EvidenceStatus, SourceCitation


class EmployeeResponseKind(str, Enum):
    """Interface-neutral states that an employee interface may render."""

    KNOWLEDGE = "KNOWLEDGE"
    ABSTENTION = "ABSTENTION"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    ACTION_SUCCESS = "ACTION_SUCCESS"
    ACTION_FAILURE = "ACTION_FAILURE"
    CLARIFICATION = "CLARIFICATION"
    UNSUPPORTED = "UNSUPPORTED"
    CANCELLED = "CANCELLED"
    TICKET_STATUS = "TICKET_STATUS"
    MULTI_STEP = "MULTI_STEP"


@dataclass(frozen=True)
class EmployeeConfirmation:
    """Opaque confirmation metadata for a CLI today and UI controls later."""

    proposal_id: str


@dataclass(frozen=True)
class EmployeeResponse:
    """Safe presentation output, separate from internal agent state."""

    kind: EmployeeResponseKind
    text: str
    sources: tuple[SourceCitation, ...] = field(default_factory=tuple)
    confirmation: EmployeeConfirmation | None = None


class EmployeeResponsePresenter(Protocol):
    def present_agent(self, result: AgentResult) -> EmployeeResponse: ...

    def present_answer(self, result: AnswerResult) -> EmployeeResponse: ...

    def present_multi_step(self, results: tuple[AgentResult, ...]) -> EmployeeResponse: ...


class ActionPresentationPolicy(Protocol):
    def present_proposal(self, result: AgentResult) -> EmployeeResponse: ...

    def present_result(self, result: AgentResult) -> EmployeeResponse: ...


class ITTicketPresentationPolicy:
    """Deterministic, allowlist-based wording for the ticket capability."""

    def present_proposal(self, result: AgentResult) -> EmployeeResponse:
        proposal = result.action_proposal
        if proposal is None or proposal.status is not ActionProposalStatus.PENDING:
            return EmployeeResponse(
                EmployeeResponseKind.ACTION_FAILURE,
                "I couldn't prepare that action for confirmation.",
            )
        title = proposal.tool_arguments.get("title", "").strip()
        description = proposal.tool_arguments.get("description", "").strip()
        if not title or not description:
            return EmployeeResponse(
                EmployeeResponseKind.ACTION_FAILURE,
                "I couldn't prepare that action for confirmation.",
            )
        return EmployeeResponse(
            EmployeeResponseKind.CONFIRMATION_REQUIRED,
            "I've prepared an IT support ticket for your approval.",
            confirmation=EmployeeConfirmation(proposal.proposal_id),
        )

    def present_result(self, result: AgentResult) -> EmployeeResponse:
        tool_result = result.tool_result
        if tool_result is None or not tool_result.success:
            if tool_result and tool_result.error_category == "UNKNOWN_EXECUTION_OUTCOME":
                text = (
                    "I couldn't verify whether Jira accepted the ticket. "
                    "Please check Jira before trying again."
                )
            else:
                text = (
                    "I couldn't create the IT support ticket. "
                    "No successful ticket creation was confirmed."
                )
            return EmployeeResponse(EmployeeResponseKind.ACTION_FAILURE, text)

        identifier = self._external_identifier(tool_result.data)
        title = result.decision.tool_arguments.get("title", "").strip()
        if identifier and title:
            text = f'Done — I created IT ticket {identifier} for “{title}”.'
        elif identifier:
            text = f"Done — I created IT ticket {identifier}."
        else:
            text = "Done — the IT support ticket was created successfully."
        return EmployeeResponse(EmployeeResponseKind.ACTION_SUCCESS, text)

    @staticmethod
    def _external_identifier(data: dict[str, object]) -> str | None:
        for key in ("issue_key", "ticket_id"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None


class DeterministicEmployeeResponsePresenter:
    """Pure presentation: no routing, validation, confirmation, or execution."""

    def __init__(
        self,
        action_policies: dict[str, ActionPresentationPolicy] | None = None,
    ) -> None:
        self.action_policies = action_policies or {
            "create_it_ticket": ITTicketPresentationPolicy()
        }

    def present_answer(self, result: AnswerResult) -> EmployeeResponse:
        abstained = bool(
            result.evidence_decision
            and result.evidence_decision.status is EvidenceStatus.INSUFFICIENT
        )
        return EmployeeResponse(
            EmployeeResponseKind.ABSTENTION if abstained else EmployeeResponseKind.KNOWLEDGE,
            result.answer,
            sources=tuple(result.sources),
        )

    def present_agent(self, result: AgentResult) -> EmployeeResponse:
        if result.permission_denied:
            return EmployeeResponse(
                EmployeeResponseKind.UNSUPPORTED,
                "You don’t have permission to access that company capability.",
            )

        if (
            result.decision.decision is AgentDecisionKind.UNSUPPORTED
            and result.decision.reason.startswith(
                "The routing decision could not be validated: PROVIDER_"
            )
        ):
            return EmployeeResponse(
                EmployeeResponseKind.UNSUPPORTED,
                "I’m temporarily unable to process your request. Please try again shortly.",
            )

        if result.ticket_status_result is not None:
            return EmployeeResponse(
                EmployeeResponseKind.TICKET_STATUS,
                result.ticket_status_result.message,
            )

        if (
            result.action_proposal is not None
            and result.action_proposal.status is ActionProposalStatus.REJECTED
        ):
            return EmployeeResponse(
                EmployeeResponseKind.CANCELLED,
                "Okay — the proposed action was cancelled. Nothing was submitted.",
            )

        if result.decision.decision is AgentDecisionKind.KNOWLEDGE:
            if result.knowledge_result is None:
                return EmployeeResponse(
                    EmployeeResponseKind.ABSTENTION,
                    "I couldn't retrieve a safe company answer for that request.",
                )
            return self.present_answer(result.knowledge_result)

        if result.action_proposal is not None:
            policy = self.action_policies.get(result.action_proposal.tool_name)
            if policy is None:
                return EmployeeResponse(
                    EmployeeResponseKind.ACTION_FAILURE,
                    "I couldn't safely present that proposed action.",
                )
            return policy.present_proposal(result)

        if result.decision.decision is AgentDecisionKind.ACTION:
            policy = self.action_policies.get(result.decision.tool_name or "")
            if policy is None:
                return EmployeeResponse(
                    EmployeeResponseKind.ACTION_FAILURE,
                    "I couldn't safely present the result of that action.",
                )
            return policy.present_result(result)

        if result.decision.decision is AgentDecisionKind.CLARIFY:
            question = result.decision.clarification_question
            if isinstance(question, str) and question.strip():
                return EmployeeResponse(
                    EmployeeResponseKind.CLARIFICATION,
                    question.strip(),
                )
            return EmployeeResponse(
                EmployeeResponseKind.CLARIFICATION,
                "Please provide more information so I can help safely.",
            )

        return EmployeeResponse(
            EmployeeResponseKind.UNSUPPORTED,
            "I can’t complete that action with the capabilities currently available.",
        )

    def present_multi_step(self, results: tuple[AgentResult, ...]) -> EmployeeResponse:
        """Combine already-safe step presentations without changing their meaning."""
        if not results:
            return EmployeeResponse(
                EmployeeResponseKind.UNSUPPORTED,
                "I couldn't safely plan that request using the available capabilities.",
            )
        responses = tuple(self.present_agent(result) for result in results)
        confirmations = tuple(
            response.confirmation for response in responses if response.confirmation
        )
        if len(confirmations) > 1:
            return EmployeeResponse(
                EmployeeResponseKind.UNSUPPORTED,
                "I couldn't safely present multiple pending actions.",
            )
        sources: list[SourceCitation] = []
        seen_sources: set[tuple[str, str]] = set()
        for response in responses:
            for source in response.sources:
                key = (source.source, source.section)
                if key not in seen_sources:
                    sources.append(source)
                    seen_sources.add(key)
        text = "\n\n".join(response.text for response in responses)
        kind = (
            EmployeeResponseKind.CONFIRMATION_REQUIRED
            if confirmations
            else EmployeeResponseKind.MULTI_STEP
        )
        return EmployeeResponse(
            kind,
            text,
            tuple(sources),
            confirmations[0] if confirmations else None,
        )


def friendly_source_label(source: SourceCitation) -> str:
    """Create a readable label while preserving exact metadata separately."""

    stem = Path(source.source).stem
    stem = re.sub(r"^\d+[_-]*", "", stem)
    document = re.sub(r"[_-]+", " ", stem).strip().title() or source.source
    return f"{document} — {source.section}"
