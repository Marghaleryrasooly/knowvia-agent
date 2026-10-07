from __future__ import annotations

import re
from typing import Protocol

from knowvia.agent_models import AgentDecision, AgentDecisionKind


class AgentRoutingError(ValueError):
    """A router produced a decision that is unsafe to dispatch."""


class AgentRouter(Protocol):
    def route(self, employee_request: str) -> AgentDecision: ...


class KnowledgeOnlyAgentRouter:
    """A deterministic router for an already validated knowledge-only scope."""

    def route(self, employee_request: str) -> AgentDecision:
        if not isinstance(employee_request, str) or not employee_request.strip():
            return AgentDecision(
                AgentDecisionKind.CLARIFY,
                clarification_question="What company information would you like help with?",
                reason="The knowledge-only request is empty.",
            )
        return AgentDecision(
            AgentDecisionKind.KNOWLEDGE,
            reason="A validated general company-knowledge request uses the knowledge-only capability.",
        )


def validate_agent_decision(
    decision: AgentDecision, known_tools: set[str]
) -> AgentDecision:
    if not isinstance(decision, AgentDecision):
        raise AgentRoutingError("Router did not return an AgentDecision")
    if not isinstance(decision.decision, AgentDecisionKind):
        raise AgentRoutingError("Unknown decision kind")
    if not isinstance(decision.reason, str) or not decision.reason.strip():
        raise AgentRoutingError("Decision reason is required")
    if not isinstance(decision.tool_arguments, dict):
        raise AgentRoutingError("Tool arguments must be an object")

    if decision.decision is AgentDecisionKind.KNOWLEDGE:
        if decision.tool_name or decision.tool_arguments or decision.clarification_question:
            raise AgentRoutingError("KNOWLEDGE cannot contain action fields")
    elif decision.decision is AgentDecisionKind.ACTION:
        if decision.tool_name not in known_tools:
            raise AgentRoutingError("ACTION references an unknown tool")
        if not decision.tool_arguments or decision.clarification_question:
            raise AgentRoutingError("ACTION requires arguments and no clarification")
    elif decision.decision is AgentDecisionKind.CLARIFY:
        if decision.tool_name is not None and decision.tool_name not in known_tools:
            raise AgentRoutingError("CLARIFY references an unknown tool")
        if decision.tool_arguments:
            raise AgentRoutingError("CLARIFY cannot contain executable arguments")
        if not isinstance(decision.clarification_question, str) or not decision.clarification_question.strip():
            raise AgentRoutingError("CLARIFY requires a question")
    else:
        if decision.tool_name or decision.tool_arguments or decision.clarification_question:
            raise AgentRoutingError("UNSUPPORTED cannot contain action fields")
    return decision


class DeterministicAgentRouter:
    """Small, auditable M5 router with conservative action detection."""

    _ticket = re.compile(r"\b(?:(?:it|support|helpdesk)\s+)?ticket\b", re.I)
    _ticket_action = re.compile(
        r"\b(?:create|make|open|raise|file|submit|log)\b.{0,30}\b(?:(?:it|support|helpdesk)\s+)?ticket\b",
        re.I,
    )
    _polite_ticket_action = re.compile(
        r"\bcan\s+you\s+(?:please\s+)?(?:create|make|open|raise|file|submit|log)\b",
        re.I,
    )
    _knowledge_start = re.compile(
        r"^\s*(?:how|what|when|where|why|who|which|is|are|does|do|explain|tell me|summarize)\b",
        re.I,
    )
    _unsupported_action = re.compile(
        r"^\s*(?:please\s+)?(?:reset|book|approve|delete|send|change|purchase|order|schedule|cancel|grant)\b|"
        r"^\s*can\s+you\s+(?:please\s+)?(?:reset|book|approve|delete|send|change|purchase|order|schedule|cancel|grant)\b",
        re.I,
    )

    def route(self, employee_request: str) -> AgentDecision:
        request = employee_request.strip()
        if not request:
            return AgentDecision(
                AgentDecisionKind.CLARIFY,
                clarification_question="What would you like Knowvia to help with?",
                reason="The employee request is empty.",
            )

        # An interrogative/summary framing is informational even if the quoted
        # material happens to contain action-like words.
        if self._knowledge_start.search(request):
            return AgentDecision(
                AgentDecisionKind.KNOWLEDGE,
                reason="The employee explicitly framed the request as a knowledge question.",
            )

        explicit_ticket_action = bool(
            self._ticket.search(request)
            and (self._ticket_action.search(request) or self._polite_ticket_action.search(request))
        )
        if explicit_ticket_action:
            problem = self._extract_problem(request)
            if not problem:
                return AgentDecision(
                    AgentDecisionKind.CLARIFY,
                    tool_name="create_it_ticket",
                    clarification_question="What IT problem should I include in the ticket?",
                    reason="An IT ticket was requested, but its problem description is missing.",
                )
            return AgentDecision(
                AgentDecisionKind.ACTION,
                tool_name="create_it_ticket",
                tool_arguments={
                    "title": self._make_title(problem),
                    "description": problem,
                },
                reason="The employee explicitly requested an IT ticket and supplied the problem.",
            )

        if self._unsupported_action.search(request):
            return AgentDecision(
                AgentDecisionKind.UNSUPPORTED,
                reason="The employee requested an action for which no M5 tool is registered.",
            )

        return AgentDecision(
            AgentDecisionKind.KNOWLEDGE,
            reason="No explicit supported or unsupported action request was detected.",
        )

    @staticmethod
    def _extract_problem(request: str) -> str | None:
        patterns = (
            r"\bbecause\s+(.+)$",
            r"\b(?:ticket)\s+(?:for|about|regarding)\s+(.+)$",
            r"\b(?:for|about|regarding)\s+(.+)$",
            r"\bticket\s*:\s*(.+)$",
        )
        for pattern in patterns:
            match = re.search(pattern, request, re.I)
            if match:
                value = match.group(1).strip(" .")
                if value and value.lower() not in {"me", "myself", "it support"}:
                    return value
        return None

    @staticmethod
    def _make_title(problem: str) -> str:
        concise = problem[0].upper() + problem[1:]
        return f"IT support: {concise}"[:120]
