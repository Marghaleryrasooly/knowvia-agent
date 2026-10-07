from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from knowvia.action_intent import ExplicitCurrentTurnActionIntentPolicy
from knowvia.agent_models import AgentDecision, AgentDecisionKind, AgentResult
from knowvia.identity import IdentityContext
from knowvia.skill_orchestration import SkillAgentResult
from knowvia.ticket_lifecycle import TicketStatusRequestParser


MAX_PLAN_STEPS = 3


class PlanStepKind(str, Enum):
    KNOWLEDGE = "KNOWLEDGE"
    CREATE_IT_TICKET = "CREATE_IT_TICKET"
    GET_TICKET_STATUS = "GET_TICKET_STATUS"


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    kind: PlanStepKind
    request: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class MultiStepPlan:
    original_request: str
    steps: tuple[PlanStep, ...]
    reason: str = ""


@dataclass(frozen=True)
class MultiStepResult:
    plan: MultiStepPlan
    step_results: tuple[SkillAgentResult, ...]
    completed: bool
    failure_reason: str | None = None


class PlanValidationError(ValueError):
    pass


class MultiStepPlanner(Protocol):
    def plan(self, employee_request: str) -> MultiStepPlan: ...


class MultiStepAgent(Protocol):
    def handle(
        self,
        employee_request: str,
        top_k: int = 3,
        selected_skill_id: str | None = None,
        identity: IdentityContext | None = None,
    ) -> SkillAgentResult: ...


class DeterministicMultiStepPlanner:
    """Recognize a small, explicit family of compound ticket-help requests."""

    _knowledge_cue = re.compile(
        r"\b(?:guidance|troubleshoot(?:ing)?|steps|how (?:can|do|should)|help me (?:fix|resolve))\b",
        re.I,
    )
    _problem_sentence = re.compile(
        r"^\s*(.+?(?:isn['’]?t|is not|cannot|can['’]?t|won['’]?t|not working|broken).+?)(?:[.!?]|\band\b)",
        re.I,
    )

    def __init__(self) -> None:
        self.action_intent = ExplicitCurrentTurnActionIntentPolicy()
        self.status_parser = TicketStatusRequestParser()

    def plan(self, employee_request: str) -> MultiStepPlan:
        request = employee_request.strip()
        if not request:
            return MultiStepPlan(request, (), "The request is empty.")
        ticket_id = self.status_parser.parse(request)
        explicit_action = self.action_intent.is_explicit(request, "create_it_ticket")
        if ticket_id:
            return MultiStepPlan(
                request,
                (PlanStep("step-1", PlanStepKind.GET_TICKET_STATUS, request),),
                "A trusted ticket-status lookup is required.",
            )
        if explicit_action and self._knowledge_cue.search(request):
            knowledge_request = self._extract_knowledge_request(request)
            problem = knowledge_request.strip().rstrip(".?!")
            action_request = (
                f"Create an IT ticket because {problem}."
                if knowledge_request != request and problem
                else request
            )
            return MultiStepPlan(
                request,
                (
                    PlanStep("step-1", PlanStepKind.KNOWLEDGE, knowledge_request),
                    PlanStep(
                        "step-2",
                        PlanStepKind.CREATE_IT_TICKET,
                        action_request,
                        ("step-1",),
                    ),
                ),
                "The employee explicitly requested guidance and ticket creation.",
            )
        kind = PlanStepKind.CREATE_IT_TICKET if explicit_action else PlanStepKind.KNOWLEDGE
        return MultiStepPlan(
            request,
            (PlanStep("step-1", kind, request),),
            "The request needs one existing capability.",
        )

    def _extract_knowledge_request(self, request: str) -> str:
        match = self._problem_sentence.search(request)
        if match:
            problem = match.group(1).strip(" ,.;")
            if problem:
                return problem + "."
        # The compound form is explicit but cannot be separated safely. Returning
        # the original will be rejected because it still contains action intent.
        return request


def validate_plan(plan: MultiStepPlan, max_steps: int = MAX_PLAN_STEPS) -> MultiStepPlan:
    if not isinstance(plan, MultiStepPlan) or not isinstance(plan.original_request, str):
        raise PlanValidationError("Planner did not return a valid plan")
    if not plan.original_request.strip() or not 1 <= len(plan.steps) <= max_steps:
        raise PlanValidationError("Plan is empty or exceeds the step limit")
    if not isinstance(plan.reason, str) or not plan.reason.strip():
        raise PlanValidationError("Plan reason is required")
    seen_ids: set[str] = set()
    seen_kinds: set[PlanStepKind] = set()
    intent = ExplicitCurrentTurnActionIntentPolicy()
    status_parser = TicketStatusRequestParser()
    for index, step in enumerate(plan.steps, start=1):
        if not isinstance(step, PlanStep) or step.step_id != f"step-{index}":
            raise PlanValidationError("Plan step identifiers must be ordered and canonical")
        if not isinstance(step.kind, PlanStepKind) or step.kind in seen_kinds:
            raise PlanValidationError("Repeated or unknown capabilities are prohibited")
        if not isinstance(step.request, str) or not step.request.strip():
            raise PlanValidationError("Every plan step requires a request")
        if tuple(step.depends_on) != tuple(f"step-{n}" for n in range(1, index)):
            if step.depends_on:
                raise PlanValidationError("Step dependencies must reference completed prior steps")
        if step.kind is PlanStepKind.CREATE_IT_TICKET:
            if not intent.is_explicit(plan.original_request, "create_it_ticket"):
                raise PlanValidationError("The original request lacks explicit action intent")
            if not intent.is_explicit(step.request, "create_it_ticket"):
                raise PlanValidationError("The action step lacks explicit action intent")
        elif step.kind is PlanStepKind.KNOWLEDGE:
            if intent.is_explicit(step.request, "create_it_ticket"):
                raise PlanValidationError("A knowledge step may not embed an action")
        elif step.kind is PlanStepKind.GET_TICKET_STATUS:
            if not status_parser.parse(step.request):
                raise PlanValidationError("A status step requires a concrete ticket identifier")
        seen_ids.add(step.step_id)
        seen_kinds.add(step.kind)
    return plan


class MultiStepCoordinator:
    """Execute a validated bounded plan through the unchanged safe agent runtime."""

    def __init__(
        self,
        planner: MultiStepPlanner,
        agent: MultiStepAgent,
        max_steps: int = MAX_PLAN_STEPS,
    ) -> None:
        if max_steps <= 0 or max_steps > MAX_PLAN_STEPS:
            raise ValueError(f"max_steps must be between 1 and {MAX_PLAN_STEPS}")
        self.planner = planner
        self.agent = agent
        self.max_steps = max_steps

    def handle(
        self,
        employee_request: str,
        top_k: int = 3,
        identity: IdentityContext | None = None,
    ) -> MultiStepResult:
        try:
            plan = validate_plan(self.planner.plan(employee_request), self.max_steps)
        except Exception:
            failed = MultiStepPlan(
                employee_request,
                (),
                "The proposed plan could not be validated.",
            )
            return MultiStepResult(failed, (), False, "PLAN_VALIDATION")

        results: list[SkillAgentResult] = []
        for step in plan.steps:
            result = self.agent.handle(step.request, top_k, identity=identity)
            if not self._matches(step, result.agent_result):
                results.append(result)
                return MultiStepResult(plan, tuple(results), False, "STEP_VALIDATION")
            results.append(result)
            outcome = result.agent_result
            if outcome.permission_denied or outcome.decision.decision in {
                AgentDecisionKind.CLARIFY,
                AgentDecisionKind.UNSUPPORTED,
            }:
                return MultiStepResult(plan, tuple(results), False, "STEP_BLOCKED")
            if outcome.tool_result is not None and not outcome.tool_result.success:
                return MultiStepResult(plan, tuple(results), False, "STEP_FAILED")
        return MultiStepResult(plan, tuple(results), True)

    @staticmethod
    def _matches(step: PlanStep, result: AgentResult) -> bool:
        if step.kind is PlanStepKind.KNOWLEDGE:
            return (
                result.decision.decision is AgentDecisionKind.KNOWLEDGE
                and result.knowledge_result is not None
                and result.tool_executed is False
            )
        if step.kind is PlanStepKind.CREATE_IT_TICKET:
            return (
                result.decision.decision in {
                    AgentDecisionKind.ACTION,
                    AgentDecisionKind.CLARIFY,
                    AgentDecisionKind.UNSUPPORTED,
                }
                and (
                    result.action_proposal is not None
                    or result.tool_result is not None
                    or result.permission_denied
                    or result.decision.decision is AgentDecisionKind.CLARIFY
                )
            )
        return (
            result.ticket_status_result is not None
            and result.tool_executed is False
        )


def safe_plan_failure_result() -> AgentResult:
    decision = AgentDecision(
        AgentDecisionKind.UNSUPPORTED,
        reason="The bounded multi-step plan could not be validated.",
    )
    return AgentResult(
        decision,
        "I couldn't safely plan that request using the available capabilities.",
    )
