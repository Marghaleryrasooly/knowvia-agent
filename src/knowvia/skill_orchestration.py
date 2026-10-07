from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from knowvia.agent_models import AgentDecision, AgentDecisionKind, AgentResult
from knowvia.agent_routing import AgentRouter, KnowledgeOnlyAgentRouter
from knowvia.agent_tools import AgentTool, ToolSpecification
from knowvia.audit import AuditSink
from knowvia.authorization import ActionAuthorizer
from knowvia.authorization import AllowRegisteredActionAuthorizer
from knowvia.action_intent import ExplicitCurrentTurnActionIntentPolicy
from knowvia.confirmation import PendingActionStore
from knowvia.identity import IdentityContext
from knowvia.permissions import (
    IdentityActionAuthorizer,
    IdentityKnowledgeSourcePolicy,
    IdentityPermissionPolicy,
)
from knowvia.general_knowledge import (
    GENERAL_COMPANY_KNOWLEDGE_SKILL_ID,
    DeterministicGeneralKnowledgeIntentPolicy,
    GeneralKnowledgeIntentKind,
    GeneralKnowledgeIntentPolicy,
)
from knowvia.resilient_routing import RoutingProvenance
from knowvia.ticket_lifecycle import TicketLifecycleService, TicketStatusRequestParser
from knowvia.models import RetrievalResult
from knowvia.knowledge_normalization import DeclarativeStatusAmbiguityPolicy
from knowvia.orchestrator import AgentOrchestrator, KnowledgeCapability
from knowvia.skills import (
    SkillDefinition,
    SkillRegistry,
    SkillRegistryError,
    SkillSelection,
    SkillSelectionStatus,
    SkillSelector,
)


class SourceScopedRetriever:
    """Restrict existing retrieval results to an exact source allowlist."""

    def __init__(
        self,
        retriever,
        allowed_sources: tuple[str, ...],
        candidate_limit: int = 1000,
    ) -> None:
        if not allowed_sources:
            raise ValueError("A scoped retriever requires at least one source")
        if candidate_limit <= 0:
            raise ValueError("candidate_limit must be positive")
        self.retriever = retriever
        self.allowed_sources = frozenset(allowed_sources)
        self.candidate_limit = candidate_limit

    def search(self, question: str, top_k: int = 3) -> list[RetrievalResult]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        candidates = self.retriever.search(
            question, max(top_k, self.candidate_limit)
        )
        scoped = [
            result
            for result in candidates
            if result.chunk.source in self.allowed_sources
        ]
        return scoped[:top_k]


@dataclass(frozen=True)
class SkillAgentResult:
    """Internal skill metadata plus the unchanged agent result."""

    selected_skill_id: str | None
    agent_result: AgentResult
    exposed_tool_names: tuple[str, ...] = ()
    knowledge_sources: tuple[str, ...] = ()
    routing_provenance: RoutingProvenance | None = None


RouterFactory = Callable[
    [SkillDefinition, tuple[ToolSpecification, ...]], AgentRouter
]
KnowledgeFactory = Callable[[SkillDefinition], KnowledgeCapability]


class SkillAwareAgent:
    """Selects one skill, narrows capabilities, then delegates to M5–M7."""

    def __init__(
        self,
        registry: SkillRegistry,
        selector: SkillSelector,
        router_factory: RouterFactory,
        knowledge_factory: KnowledgeFactory,
        tools: list[AgentTool],
        pending_actions: PendingActionStore | None = None,
        authorizer: ActionAuthorizer | None = None,
        audit_sink: AuditSink | None = None,
        permission_policy: IdentityPermissionPolicy | None = None,
        general_knowledge_intent_policy: GeneralKnowledgeIntentPolicy | None = None,
        knowledge_source_policy: IdentityKnowledgeSourcePolicy | None = None,
        ticket_lifecycle_service: TicketLifecycleService | None = None,
    ) -> None:
        self.registry = registry
        self.selector = selector
        self.router_factory = router_factory
        self.knowledge_factory = knowledge_factory
        self.tools: dict[str, AgentTool] = {}
        for tool in tools:
            name = tool.specification.name
            if not name or name in self.tools:
                raise ValueError(f"Invalid or duplicate tool name: {name}")
            self.tools[name] = tool
        self.pending_actions = pending_actions
        self.authorizer = authorizer
        self.audit_sink = audit_sink
        self.permission_policy = permission_policy or IdentityPermissionPolicy()
        self.general_knowledge_intent_policy = (
            general_knowledge_intent_policy
            or DeterministicGeneralKnowledgeIntentPolicy()
        )
        self.knowledge_source_policy = (
            knowledge_source_policy or IdentityKnowledgeSourcePolicy()
        )
        self.action_intent_policy = ExplicitCurrentTurnActionIntentPolicy()
        self.ticket_lifecycle_service = ticket_lifecycle_service
        self.ticket_status_parser = TicketStatusRequestParser()
        self.status_ambiguity_policy = DeclarativeStatusAmbiguityPolicy()

    def handle(
        self,
        employee_request: str,
        top_k: int = 3,
        selected_skill_id: str | None = None,
        identity: IdentityContext | None = None,
    ) -> SkillAgentResult:
        try:
            summaries = self.registry.discover()
            ticket_id = self.ticket_status_parser.parse(employee_request)
            if selected_skill_id is None:
                if ticket_id and "it_support" in {
                    summary.skill_id for summary in summaries
                }:
                    selection = SkillSelection(
                        SkillSelectionStatus.SELECTED,
                        "it_support",
                        "A deterministic ticket-status request selected IT Support.",
                    )
                else:
                    selection = self.selector.select(employee_request, summaries)
            elif selected_skill_id in {summary.skill_id for summary in summaries}:
                selection = SkillSelection(
                    SkillSelectionStatus.SELECTED,
                    selected_skill_id,
                    "A bounded conversational knowledge follow-up inherited the prior skill.",
                )
            else:
                return self._unsupported("The inherited skill is not available.")
        except Exception:
            return self._unsupported("Skill discovery or selection failed validation.")
        if not isinstance(selection, SkillSelection):
            return self._unsupported("The skill selector returned an invalid result.")
        if selection.status is SkillSelectionStatus.AMBIGUOUS:
            decision = AgentDecision(
                AgentDecisionKind.CLARIFY,
                clarification_question=(
                    "Is your request about HR, IT support, or Finance?"
                ),
                reason="The request did not select exactly one skill.",
            )
            return SkillAgentResult(None, AgentResult(decision, decision.clarification_question or ""))
        if selection.status is SkillSelectionStatus.UNSUPPORTED:
            general_intent = self.general_knowledge_intent_policy.classify(
                employee_request
            )
            if general_intent.kind is GeneralKnowledgeIntentKind.KNOWLEDGE:
                selection = SkillSelection(
                    SkillSelectionStatus.SELECTED,
                    GENERAL_COMPANY_KNOWLEDGE_SKILL_ID,
                    "A clear unmatched company-information request selected the general knowledge capability.",
                )
            elif general_intent.kind is GeneralKnowledgeIntentKind.AMBIGUOUS:
                decision = AgentDecision(
                    AgentDecisionKind.CLARIFY,
                    clarification_question="What company information would you like help with?",
                    reason=general_intent.reason,
                )
                return SkillAgentResult(
                    None,
                    AgentResult(
                        decision, decision.clarification_question or ""
                    ),
                )
            else:
                return self._unsupported("No supported skill matched the request.")
        if selection.status is not SkillSelectionStatus.SELECTED or not selection.skill_id:
            return self._unsupported("No supported skill matched the request.")

        if ticket_id and self.ticket_lifecycle_service is not None:
            if identity is None:
                return self._permission_denied(selection.skill_id)
            status_result = self.ticket_lifecycle_service.lookup(ticket_id, identity)
            decision = AgentDecision(
                AgentDecisionKind.KNOWLEDGE,
                reason="A deterministic owner-authorized ticket status lookup was used.",
            )
            return SkillAgentResult(
                selection.skill_id,
                AgentResult(
                    decision,
                    status_result.message,
                    permission_denied=status_result.permission_denied,
                    ticket_status_result=status_result,
                ),
            )

        if identity is not None:
            explicit_action_tools = tuple(
                name for name in self.tools
                if self.action_intent_policy.is_explicit(employee_request, name)
            )
            if explicit_action_tools and not all(
                self.permission_policy.can_use_tool(identity, name)
                for name in explicit_action_tools
            ):
                return self._permission_denied(selection.skill_id)
            if not explicit_action_tools:
                if selection.skill_id == GENERAL_COMPANY_KNOWLEDGE_SKILL_ID:
                    if not self.knowledge_source_policy.authorized_sources(identity):
                        return self._permission_denied(selection.skill_id)
                elif not self.permission_policy.can_access_skill(
                    identity, selection.skill_id
                ):
                    return self._permission_denied(selection.skill_id)

        try:
            skill = self.registry.load(selection.skill_id)
        except SkillRegistryError:
            return self._unsupported("The selected skill could not be loaded safely.")

        if skill.skill_id == GENERAL_COMPANY_KNOWLEDGE_SKILL_ID:
            if identity is None:
                return self._permission_denied(skill.skill_id)
            authorized_sources = self.knowledge_source_policy.authorized_sources(identity)
            permitted = tuple(
                source for source in skill.knowledge_sources
                if source in frozenset(authorized_sources)
            )
            if not permitted:
                return self._permission_denied(skill.skill_id)
            skill = replace(skill, knowledge_sources=permitted)

        selected_tools = tuple(
            tool
            for name, tool in self.tools.items()
            if name in skill.allowed_tools
            and (
                identity is None
                or self.permission_policy.can_use_tool(identity, name)
            )
        )
        specifications = tuple(tool.specification for tool in selected_tools)
        ambiguous_status = self.status_ambiguity_policy.classify(employee_request)
        if ambiguous_status is not None:
            decision = AgentDecision(
                AgentDecisionKind.CLARIFY,
                clarification_question=ambiguous_status.clarification_question,
                reason=(
                    "A declarative status report did not safely establish whether "
                    "the employee reported a problem."
                ),
            )
            return SkillAgentResult(
                selected_skill_id=skill.skill_id,
                agent_result=AgentResult(
                    decision, decision.clarification_question or ""
                ),
                exposed_tool_names=tuple(
                    specification.name for specification in specifications
                ),
                knowledge_sources=skill.knowledge_sources,
            )
        try:
            router = (
                KnowledgeOnlyAgentRouter()
                if skill.skill_id == GENERAL_COMPANY_KNOWLEDGE_SKILL_ID
                else self.router_factory(skill, specifications)
            )
            knowledge = self.knowledge_factory(skill)
            authorizer = self.authorizer
            if identity is not None:
                delegate = authorizer or AllowRegisteredActionAuthorizer(
                    {tool.specification.name for tool in selected_tools}
                )
                authorizer = IdentityActionAuthorizer(
                    identity, self.permission_policy, delegate
                )
            orchestrator = AgentOrchestrator(
                router,
                knowledge,
                list(selected_tools),
                self.pending_actions,
                authorizer=authorizer,
                audit_sink=self.audit_sink,
                identity_context=identity,
                ticket_lifecycle_recorder=self.ticket_lifecycle_service,
            )
            result = orchestrator.handle(employee_request, top_k)
        except Exception:
            return self._unsupported(
                "The selected skill runtime could not be initialized safely."
            )
        return SkillAgentResult(
            selected_skill_id=skill.skill_id,
            agent_result=result,
            exposed_tool_names=tuple(tool.specification.name for tool in selected_tools),
            knowledge_sources=skill.knowledge_sources,
            routing_provenance=getattr(router, "last_provenance", None),
        )

    @staticmethod
    def _unsupported(reason: str) -> SkillAgentResult:
        decision = AgentDecision(AgentDecisionKind.UNSUPPORTED, reason=reason)
        return SkillAgentResult(
            None,
            AgentResult(decision, "I couldn't safely select a capability for that request."),
        )

    @staticmethod
    def _permission_denied(skill_id: str) -> SkillAgentResult:
        decision = AgentDecision(
            AgentDecisionKind.UNSUPPORTED,
            reason="The trusted identity lacks permission for the selected capability.",
        )
        return SkillAgentResult(
            skill_id,
            AgentResult(
                decision,
                "You don't have permission to access that capability.",
                permission_denied=True,
            ),
        )
