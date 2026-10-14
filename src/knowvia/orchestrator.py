from __future__ import annotations

from typing import Protocol

from knowvia.agent_models import AgentDecision, AgentDecisionKind, AgentResult, ToolResult
from knowvia.agent_routing import AgentRouter, AgentRoutingError, validate_agent_decision
from knowvia.agent_tools import AgentTool, ToolValidationError
from knowvia.action_intent import ActionIntentPolicy, ExplicitCurrentTurnActionIntentPolicy
from knowvia.audit import AuditSink, NullAuditSink, make_audit_event
from knowvia.authorization import ActionAuthorizer, AllowRegisteredActionAuthorizer
from knowvia.confirmation import ConfirmationError, PendingActionStore
from knowvia.models import AnswerResult
from knowvia.identity import IdentityContext
from knowvia.knowledge_normalization import (
    DeterministicKnowledgeRequestNormalizer,
    KnowledgeRequestNormalizer,
    validate_normalized_knowledge_request,
)
from knowvia.ticket_lifecycle import TicketLifecycleRecorder


class KnowledgeCapability(Protocol):
    def answer(self, question: str, top_k: int = 3) -> AnswerResult: ...


class AgentOrchestrator:
    """Routes the original employee request, then invokes exactly one capability."""

    def __init__(
        self,
        router: AgentRouter,
        knowledge: KnowledgeCapability,
        tools: list[AgentTool],
        pending_actions: PendingActionStore | None = None,
        authorizer: ActionAuthorizer | None = None,
        audit_sink: AuditSink | None = None,
        action_intent_policy: ActionIntentPolicy | None = None,
        knowledge_request_normalizer: KnowledgeRequestNormalizer | None = None,
        identity_context: IdentityContext | None = None,
        ticket_lifecycle_recorder: TicketLifecycleRecorder | None = None,
    ) -> None:
        self.router = router
        self.knowledge = knowledge
        self.tools: dict[str, AgentTool] = {}
        for tool in tools:
            name = tool.specification.name
            if not name or name in self.tools:
                raise ValueError(f"Invalid or duplicate tool name: {name}")
            self.tools[name] = tool
        self.pending_actions = pending_actions
        self.authorizer = authorizer or AllowRegisteredActionAuthorizer(set(self.tools))
        self.audit_sink = audit_sink or NullAuditSink()
        self.action_intent_policy = (
            action_intent_policy or ExplicitCurrentTurnActionIntentPolicy()
        )
        self.knowledge_request_normalizer = (
            knowledge_request_normalizer or DeterministicKnowledgeRequestNormalizer()
        )
        self.identity_context = identity_context
        self.ticket_lifecycle_recorder = ticket_lifecycle_recorder

    def handle(self, employee_request: str, top_k: int = 3) -> AgentResult:
        try:
            decision = validate_agent_decision(
                self.router.route(employee_request), set(self.tools)
            )
        except Exception as error:
            category = getattr(error, "category", type(error).__name__)
            category = getattr(category, "value", str(category))
            safe = AgentDecision(
                AgentDecisionKind.UNSUPPORTED,
                reason=f"The routing decision could not be validated: {category}.",
            )
            return AgentResult(safe, "I couldn't safely determine an action for that request.")

        if decision.decision is AgentDecisionKind.KNOWLEDGE:
            return self._answer_knowledge(decision, employee_request, top_k)
        if decision.decision is AgentDecisionKind.CLARIFY:
            return AgentResult(decision, decision.clarification_question or "Please provide more information.")
        if decision.decision is AgentDecisionKind.UNSUPPORTED:
            return AgentResult(decision, "That action is not supported by Knowvia in M5.")

        if not self.action_intent_policy.is_explicit(
            employee_request, decision.tool_name or ""
        ):
            knowledge_decision = AgentDecision(
                AgentDecisionKind.KNOWLEDGE,
                reason=(
                    "The current employee utterance describes a problem but does not "
                    "explicitly request the proposed action."
                ),
            )
            return self._answer_knowledge(
                knowledge_decision, employee_request, top_k
            )

        tool = self.tools[decision.tool_name or ""]
        try:
            arguments = tool.validate_arguments(decision.tool_arguments)
        except ToolValidationError as error:
            clarification = AgentDecision(
                AgentDecisionKind.CLARIFY,
                tool_name=decision.tool_name,
                clarification_question="Please provide the required, valid information for this action.",
                reason=f"Tool arguments were rejected: {error}",
            )
            return AgentResult(clarification, clarification.clarification_question or "")

        if not self.authorizer.authorize(decision.tool_name or "", arguments):
            self._audit(
                "AUTHORIZATION_DENIED",
                decision.tool_name or "unknown",
                arguments,
                None,
                False,
                error_category="PERMISSION_DENIED",
                authorization_outcome="DENIED",
            )
            return self._permission_denied(
                "The application authorization policy denied this action."
            )

        if tool.specification.requires_confirmation:
            if self.pending_actions is None:
                safe = AgentDecision(
                    AgentDecisionKind.UNSUPPORTED,
                    reason="A confirmation store is required for this real action.",
                )
                return AgentResult(safe, "The action cannot be proposed safely because confirmation is unavailable.")
            proposal = self.pending_actions.create(
                tool.specification.name,
                arguments,
                self.identity_context.subject_id if self.identity_context else None,
            )
            self._audit("PROPOSED", tool.specification.name, arguments, proposal.proposal_id, None)
            return AgentResult(
                decision,
                "The action is proposed and requires explicit confirmation before execution.",
                action_proposal=proposal,
            )

        return self._execute(decision, tool, arguments)

    def _answer_knowledge(
        self, decision: AgentDecision, employee_request: str, top_k: int
    ) -> AgentResult:
        normalized = validate_normalized_knowledge_request(
            self.knowledge_request_normalizer.normalize(employee_request),
            employee_request,
        )
        answer = self.knowledge.answer(normalized.normalized_request, top_k)
        return AgentResult(
            decision,
            answer.answer,
            knowledge_result=answer,
            normalized_knowledge_request=normalized,
        )

    def confirm(self, proposal_id: str) -> AgentResult:
        if self.pending_actions is None:
            return self._safe_confirmation_failure("Confirmation storage is unavailable.")
        proposal = self.pending_actions.get(proposal_id)
        if proposal is None:
            return self._safe_confirmation_failure("Unknown action proposal")
        subject_id = self.identity_context.subject_id if self.identity_context else None
        if proposal.subject_id != subject_id:
            self._audit(
                "CONFIRMATION_DENIED",
                proposal.tool_name,
                proposal.tool_arguments,
                proposal_id,
                False,
                error_category="IDENTITY_MISMATCH",
                authorization_outcome="DENIED",
            )
            return self._permission_denied(
                "The action proposal belongs to a different identity."
            )
        tool = self.tools.get(proposal.tool_name)
        if tool is None or not tool.specification.requires_confirmation:
            return self._safe_confirmation_failure(
                "The proposed tool is not a valid real-action tool."
            )
        try:
            arguments = tool.validate_arguments(proposal.tool_arguments)
        except ToolValidationError:
            return self._safe_confirmation_failure(
                "The stored action arguments are no longer valid."
            )
        if not self.authorizer.authorize(proposal.tool_name, arguments):
            self._audit(
                "CONFIRMATION_DENIED",
                proposal.tool_name,
                arguments,
                proposal_id,
                False,
                error_category="PERMISSION_DENIED",
                authorization_outcome="DENIED",
            )
            return self._permission_denied(
                "Permission was denied when the action was confirmed."
            )
        try:
            proposal = self.pending_actions.claim(proposal_id, subject_id)
        except ConfirmationError as error:
            return self._safe_confirmation_failure(str(error))
        try:
            arguments = tool.validate_arguments(proposal.tool_arguments)
        except ToolValidationError:
            return self._finish_invalid_proposal(proposal_id, proposal, "The stored action arguments are no longer valid.")
        if not self.authorizer.authorize(proposal.tool_name, arguments):
            return self._finish_invalid_proposal(proposal_id, proposal, "Authorization was denied at confirmation time.")

        decision = AgentDecision(
            AgentDecisionKind.ACTION,
            tool_name=proposal.tool_name,
            tool_arguments=arguments,
            reason="A previously validated proposal received explicit confirmation.",
        )
        result = self._execute(decision, tool, arguments, proposal_id=proposal_id)
        if result.tool_result is not None:
            self.pending_actions.finish(proposal_id, result.tool_result)
        return result

    def reject(self, proposal_id: str) -> AgentResult:
        if self.pending_actions is None:
            return self._safe_confirmation_failure("Confirmation storage is unavailable.")
        try:
            proposal = self.pending_actions.reject(
                proposal_id,
                self.identity_context.subject_id if self.identity_context else None,
            )
        except ConfirmationError as error:
            return self._safe_confirmation_failure(str(error))
        self._audit("REJECTED", proposal.tool_name, proposal.tool_arguments, proposal_id, False)
        decision = AgentDecision(AgentDecisionKind.CLARIFY, clarification_question="Action cancelled.", reason="The employee rejected the proposed action.")
        return AgentResult(decision, "The proposed action was cancelled.", action_proposal=proposal)

    def _execute(self, decision: AgentDecision, tool: AgentTool, arguments: dict[str, str], proposal_id: str | None = None) -> AgentResult:

        try:
            result = tool.execute(arguments)
            if not isinstance(result, ToolResult) or result.tool_name != decision.tool_name:
                raise TypeError("Tool returned an invalid result")
            if result.success and result.error:
                raise ValueError("Successful tool result cannot contain an error")
            if (
                result.success
                and result.tool_name == "create_it_ticket"
                and self.identity_context is not None
                and self.ticket_lifecycle_recorder is not None
            ):
                try:
                    self.ticket_lifecycle_recorder.record_created(
                        result, self.identity_context
                    )
                except Exception:
                    self._audit(
                        "LIFECYCLE_RECORD_FAILED",
                        tool.specification.name,
                        arguments,
                        proposal_id,
                        False,
                        error_category="LIFECYCLE_STORAGE",
                    )
            external_id = str(result.data.get("issue_key") or result.data.get("ticket_id") or "") or None
            self._audit("EXECUTED", tool.specification.name, arguments, proposal_id, result.success, external_id, result.error_category)
            return AgentResult(
                decision,
                result.message,
                tool_result=result,
                tool_executed=True,
            )
        except Exception as error:
            failure = ToolResult(
                success=False,
                tool_name=decision.tool_name or "unknown",
                message="The requested action could not be completed.",
                error="The tool raised an internal execution error.",
                error_category="TOOL_EXECUTION",
            )
            self._audit("EXECUTED", tool.specification.name, arguments, proposal_id, False, error_category=failure.error_category)
            return AgentResult(
                decision,
                failure.message,
                tool_result=failure,
                tool_executed=True,
            )

    def _finish_invalid_proposal(self, proposal_id, proposal, message: str) -> AgentResult:
        failure = ToolResult(False, proposal.tool_name, message, error=message, error_category="VALIDATION")
        if self.pending_actions is not None:
            self.pending_actions.finish(proposal_id, failure)
        self._audit("EXECUTED", proposal.tool_name, proposal.tool_arguments, proposal_id, False, error_category="VALIDATION")
        decision = AgentDecision(AgentDecisionKind.UNSUPPORTED, reason=message)
        return AgentResult(decision, message, tool_result=failure, tool_executed=False)

    @staticmethod
    def _safe_confirmation_failure(message: str) -> AgentResult:
        decision = AgentDecision(AgentDecisionKind.UNSUPPORTED, reason="Confirmation could not be validated.")
        return AgentResult(decision, message, confirmation_failed=True)

    @staticmethod
    def _permission_denied(reason: str) -> AgentResult:
        decision = AgentDecision(AgentDecisionKind.UNSUPPORTED, reason=reason)
        return AgentResult(
            decision,
            "You don't have permission to access that capability.",
            permission_denied=True,
        )

    def _audit(
        self,
        stage,
        tool_name,
        arguments,
        proposal_id,
        success,
        external_id=None,
        error_category=None,
        authorization_outcome=None,
    ) -> None:
        try:
            identity = self.identity_context
            self.audit_sink.record(
                make_audit_event(
                    stage,
                    tool_name,
                    arguments,
                    proposal_id,
                    success,
                    external_id,
                    error_category,
                    subject_id=identity.subject_id if identity else None,
                    identity_source=identity.identity_source if identity else None,
                    authenticated=identity.authenticated if identity else None,
                    authorization_outcome=(
                        authorization_outcome
                        or ("AUTHORIZED" if identity else "NOT_APPLICABLE")
                    ),
                )
            )
        except Exception:
            # Audit storage is deliberately best-effort in this local milestone.
            pass
