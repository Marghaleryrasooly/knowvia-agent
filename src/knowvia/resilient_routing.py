from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from knowvia.action_intent import ActionIntentPolicy, ExplicitCurrentTurnActionIntentPolicy
from knowvia.agent_models import AgentDecision, AgentDecisionKind
from knowvia.agent_routing import AgentRouter, AgentRoutingError, validate_agent_decision
from knowvia.agent_tools import ToolSpecification
from knowvia.gemini_routing import (
    GeminiRoutingError,
    GeminiRoutingFailureCategory,
)


class RoutingFailureDisposition(str, Enum):
    """Whether a primary routing failure may use the local safe fallback."""

    TRANSIENT_UNAVAILABLE = "TRANSIENT_UNAVAILABLE"
    PERMANENT_PROVIDER_ERROR = "PERMANENT_PROVIDER_ERROR"
    INVALID_PROPOSAL = "INVALID_PROPOSAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RoutingProvenance:
    """Sanitized routing metadata; it deliberately contains no request text."""

    primary_router: str
    primary_outcome: str
    primary_failure_category: str | None = None
    primary_http_status: int | None = None
    primary_attempts: int | None = None
    fallback_router: str | None = None
    fallback_used: bool = False
    fallback_decision: AgentDecisionKind | None = None


class SafeFallbackPolicy:
    """Constrains a deterministic fallback without executing any capability."""

    _context_only_action = re.compile(
        r"^\s*(?:yes(?:\s*,?\s*please)?|do\s+it|create\s+one\s+for\s+that|can\s+you\s+do\s+that)\s*[.!?]*\s*$",
        re.I,
    )

    def __init__(
        self,
        tool_specifications: tuple[ToolSpecification, ...],
        action_intent_policy: ActionIntentPolicy | None = None,
    ) -> None:
        self.specifications = {spec.name: spec for spec in tool_specifications}
        if len(self.specifications) != len(tool_specifications):
            raise ValueError("Fallback tool names must be unique")
        self.action_intent_policy = (
            action_intent_policy or ExplicitCurrentTurnActionIntentPolicy()
        )

    def validate(self, employee_request: str, decision: AgentDecision) -> AgentDecision:
        decision = validate_agent_decision(decision, set(self.specifications))

        if self._context_only_action.fullmatch(employee_request):
            return AgentDecision(
                AgentDecisionKind.CLARIFY,
                clarification_question=(
                    "Please state the action you want and provide its required details "
                    "in your current message."
                ),
                reason="Fallback refused to infer action intent or arguments from context.",
            )

        if decision.decision is not AgentDecisionKind.ACTION:
            return decision

        tool_name = decision.tool_name or ""
        if not self.action_intent_policy.is_explicit(employee_request, tool_name):
            raise AgentRoutingError(
                "Fallback ACTION lacks explicit current-turn action intent"
            )
        specification = self.specifications.get(tool_name)
        if specification is None:
            raise AgentRoutingError("Fallback ACTION references an unavailable tool")
        expected = set(specification.required_arguments)
        if set(decision.tool_arguments) != expected:
            raise AgentRoutingError("Fallback ACTION arguments do not match the tool")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in decision.tool_arguments.values()
        ):
            raise AgentRoutingError("Fallback ACTION arguments are invalid")
        return decision


class ResilientAgentRouter:
    """Gemini-primary router with one local fallback on provider unavailability."""

    def __init__(
        self,
        primary: AgentRouter,
        fallback: AgentRouter,
        fallback_policy: SafeFallbackPolicy,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.fallback_policy = fallback_policy
        self.last_provenance: RoutingProvenance | None = None

    def route(self, employee_request: str) -> AgentDecision:
        try:
            decision = self.primary.route(employee_request)
        except GeminiRoutingError as error:
            disposition = classify_routing_failure(error)
            if disposition is not RoutingFailureDisposition.TRANSIENT_UNAVAILABLE:
                self.last_provenance = self._failed_provenance(error)
                raise
            try:
                fallback_decision = self.fallback_policy.validate(
                    employee_request, self.fallback.route(employee_request)
                )
            except Exception:
                self.last_provenance = self._failed_provenance(
                    error, fallback_used=True
                )
                raise error from None
            self.last_provenance = RoutingProvenance(
                primary_router="gemini",
                primary_outcome="provider_unavailable",
                primary_failure_category=error.category.value,
                primary_http_status=error.http_status,
                primary_attempts=error.attempt_count,
                fallback_router="deterministic",
                fallback_used=True,
                fallback_decision=fallback_decision.decision,
            )
            return fallback_decision
        self.last_provenance = RoutingProvenance(
            primary_router="gemini",
            primary_outcome="success",
            primary_attempts=_primary_attempts(self.primary),
        )
        return decision

    @staticmethod
    def _failed_provenance(
        error: GeminiRoutingError, fallback_used: bool = False
    ) -> RoutingProvenance:
        disposition = classify_routing_failure(error)
        return RoutingProvenance(
            primary_router="gemini",
            primary_outcome=(
                "provider_unavailable"
                if disposition is RoutingFailureDisposition.TRANSIENT_UNAVAILABLE
                else "routing_failure"
            ),
            primary_failure_category=error.category.value,
            primary_http_status=error.http_status,
            primary_attempts=error.attempt_count,
            fallback_router="deterministic" if fallback_used else None,
            fallback_used=fallback_used,
        )


def classify_routing_failure(error: Exception) -> RoutingFailureDisposition:
    if not isinstance(error, GeminiRoutingError):
        return RoutingFailureDisposition.UNKNOWN
    if error.category is GeminiRoutingFailureCategory.PROPOSAL_VALIDATION_ERROR:
        return RoutingFailureDisposition.INVALID_PROPOSAL
    if (
        error.transient
        or error.category is GeminiRoutingFailureCategory.PROVIDER_TIMEOUT
        or error.http_status in {429, 499}
        or (
            isinstance(error.http_status, int)
            and 500 <= error.http_status <= 599
        )
    ):
        return RoutingFailureDisposition.TRANSIENT_UNAVAILABLE
    if error.category in {
        GeminiRoutingFailureCategory.PROVIDER_TIMEOUT,
        GeminiRoutingFailureCategory.PROVIDER_HTTP_ERROR,
        GeminiRoutingFailureCategory.PROVIDER_ERROR,
    }:
        return RoutingFailureDisposition.PERMANENT_PROVIDER_ERROR
    return RoutingFailureDisposition.UNKNOWN


def _primary_attempts(primary: AgentRouter) -> int | None:
    transport = getattr(primary, "transport", None)
    diagnostic = getattr(transport, "last_diagnostic", None)
    attempts = getattr(diagnostic, "attempt_count", None)
    return attempts if isinstance(attempts, int) else None
