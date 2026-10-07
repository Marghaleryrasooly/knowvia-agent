from dataclasses import replace

import pytest

from knowvia.agent_models import AgentDecision, AgentDecisionKind
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import LocalITTicketTool
from knowvia.confirmation import SqlitePendingActionStore
from knowvia.gemini_routing import (
    GeminiRoutingError,
    GeminiRoutingFailureCategory,
)
from knowvia.identity import LocalIdentityProvider
from knowvia.models import AnswerResult
from knowvia.orchestrator import AgentOrchestrator
from knowvia.resilient_routing import (
    ResilientAgentRouter,
    RoutingFailureDisposition,
    SafeFallbackPolicy,
    classify_routing_failure,
)
from knowvia.skill_orchestration import SkillAwareAgent
from knowvia.skills import DeterministicSkillSelector, create_default_skill_registry


class FixedRouter:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def route(self, request):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def routing_error(status=None, *, category=GeminiRoutingFailureCategory.PROVIDER_HTTP_ERROR, transient=False):
    return GeminiRoutingError(
        category,
        "sanitized",
        http_status=status,
        attempt_count=2,
        transient=transient,
    )


def resilient(primary, specifications=()):
    fallback = FixedRouter(AgentDecision(AgentDecisionKind.KNOWLEDGE, reason="local"))
    router = ResilientAgentRouter(
        primary, fallback, SafeFallbackPolicy(tuple(specifications))
    )
    return router, fallback


@pytest.mark.parametrize("status", [429, 499, 503, 504, 500, 599])
def test_transient_http_failures_are_fallback_eligible(status) -> None:
    assert classify_routing_failure(routing_error(status, transient=True)) is RoutingFailureDisposition.TRANSIENT_UNAVAILABLE


@pytest.mark.parametrize(
    "error",
    [
        routing_error(None, category=GeminiRoutingFailureCategory.PROVIDER_ERROR, transient=True),
        routing_error(None, category=GeminiRoutingFailureCategory.PROVIDER_TIMEOUT, transient=True),
    ],
)
def test_connect_and_timeout_failures_are_fallback_eligible(error) -> None:
    assert classify_routing_failure(error) is RoutingFailureDisposition.TRANSIENT_UNAVAILABLE


@pytest.mark.parametrize("status", [400, 401, 403])
def test_non_transient_http_failures_do_not_use_fallback(status) -> None:
    primary = FixedRouter(routing_error(status, transient=False))
    router, fallback = resilient(primary)

    with pytest.raises(GeminiRoutingError):
        router.route("What is the leave policy?")

    assert fallback.calls == 0
    assert router.last_provenance and router.last_provenance.fallback_used is False


def test_invalid_model_proposal_does_not_use_fallback() -> None:
    primary = FixedRouter(
        routing_error(
            category=GeminiRoutingFailureCategory.PROPOSAL_VALIDATION_ERROR
        )
    )
    router, fallback = resilient(primary)

    with pytest.raises(GeminiRoutingError):
        router.route("request")

    assert fallback.calls == 0
    assert classify_routing_failure(primary.outcome) is RoutingFailureDisposition.INVALID_PROPOSAL


def test_valid_gemini_unsupported_is_not_overridden() -> None:
    primary = FixedRouter(
        AgentDecision(AgentDecisionKind.UNSUPPORTED, reason="semantic result")
    )
    router, fallback = resilient(primary)

    decision = router.route("Book a flight")

    assert decision.decision is AgentDecisionKind.UNSUPPORTED
    assert fallback.calls == 0
    assert router.last_provenance and router.last_provenance.primary_outcome == "success"


def test_transient_failure_falls_back_once_and_records_sanitized_provenance() -> None:
    primary = FixedRouter(routing_error(504, transient=True))
    router, fallback = resilient(primary)

    decision = router.route("How many vacation days do I get?")

    assert decision.decision is AgentDecisionKind.KNOWLEDGE
    assert fallback.calls == 1
    assert router.last_provenance is not None
    assert router.last_provenance.primary_http_status == 504
    assert router.last_provenance.primary_attempts == 2
    assert router.last_provenance.fallback_used is True
    assert router.last_provenance.fallback_decision is AgentDecisionKind.KNOWLEDGE
    assert "vacation" not in repr(router.last_provenance)


def test_fallback_action_requires_explicit_current_turn_intent() -> None:
    specification = LocalITTicketTool.specification
    primary = FixedRouter(routing_error(503, transient=True))
    fallback = FixedRouter(
        AgentDecision(
            AgentDecisionKind.ACTION,
            "create_it_ticket",
            {"title": "VPN", "description": "VPN is unavailable"},
            reason="local",
        )
    )
    router = ResilientAgentRouter(
        primary, fallback, SafeFallbackPolicy((specification,))
    )

    with pytest.raises(GeminiRoutingError):
        router.route("My VPN is unavailable.")

    assert fallback.calls == 1


@pytest.mark.parametrize(
    "utterance",
    ["Yes please", "Do it", "Create one for that", "Can you do that?"],
)
def test_context_only_action_phrases_clarify_without_action(utterance) -> None:
    primary = FixedRouter(routing_error(503, transient=True))
    router, fallback = resilient(primary)

    decision = router.route(utterance)

    assert decision.decision is AgentDecisionKind.CLARIFY
    assert decision.tool_name is None
    assert decision.tool_arguments == {}
    assert fallback.calls == 1


def test_unknown_or_invalid_fallback_action_fails_closed() -> None:
    for decision in (
        AgentDecision(AgentDecisionKind.ACTION, "unknown", {"x": "y"}, reason="local"),
        AgentDecision(AgentDecisionKind.ACTION, "create_it_ticket", {"title": "VPN"}, reason="local"),
    ):
        primary = FixedRouter(routing_error(503, transient=True))
        fallback = FixedRouter(decision)
        router = ResilientAgentRouter(
            primary,
            fallback,
            SafeFallbackPolicy((LocalITTicketTool.specification,)),
        )
        with pytest.raises(GeminiRoutingError):
            router.route("Create an IT ticket because my VPN is unavailable")


class Knowledge:
    def __init__(self, answer="Grounded answer"):
        self.calls = []
        self.answer_text = answer

    def answer(self, question, top_k=3):
        self.calls.append((question, top_k))
        return AnswerResult(self.answer_text, [], [])


def test_provider_outage_recovers_knowledge_without_tool_execution() -> None:
    primary = FixedRouter(routing_error(503, transient=True))
    router = ResilientAgentRouter(
        primary, DeterministicAgentRouter(), SafeFallbackPolicy(())
    )
    knowledge = Knowledge()

    result = AgentOrchestrator(router, knowledge, []).handle(
        "How many vacation days do I get?"
    )

    assert result.decision.decision is AgentDecisionKind.KNOWLEDGE
    assert knowledge.calls
    assert result.tool_executed is False


class ConfirmationTool(LocalITTicketTool):
    specification = replace(LocalITTicketTool.specification, requires_confirmation=True)

    def __init__(self):
        super().__init__()
        self.executions = 0

    def execute(self, arguments):
        self.executions += 1
        return super().execute(arguments)


def test_explicit_fallback_action_creates_proposal_not_side_effect(tmp_path) -> None:
    tool = ConfirmationTool()
    primary = FixedRouter(routing_error(504, transient=True))
    router = ResilientAgentRouter(
        primary,
        DeterministicAgentRouter(),
        SafeFallbackPolicy((tool.specification,)),
    )
    pending = SqlitePendingActionStore(tmp_path / "pending.sqlite3")

    result = AgentOrchestrator(router, Knowledge(), [tool], pending).handle(
        "Create an IT ticket because my VPN is unavailable."
    )

    assert result.decision.decision is AgentDecisionKind.ACTION
    assert result.action_proposal is not None
    assert result.tool_executed is False
    assert tool.executions == 0


def test_fallback_does_not_retry_tool_execution() -> None:
    tool = LocalITTicketTool()
    calls = 0
    original = tool.execute

    def execute_once(arguments):
        nonlocal calls
        calls += 1
        return original(arguments)

    tool.execute = execute_once
    primary = FixedRouter(routing_error(503, transient=True))
    router = ResilientAgentRouter(
        primary,
        DeterministicAgentRouter(),
        SafeFallbackPolicy((tool.specification,)),
    )

    result = AgentOrchestrator(router, Knowledge(), [tool]).handle(
        "Create an IT ticket because my VPN is unavailable."
    )

    assert result.tool_executed is True
    assert calls == 1


def test_deterministic_router_itself_is_unchanged() -> None:
    router = DeterministicAgentRouter()
    assert router.route("How many vacation days do I get?").decision is AgentDecisionKind.KNOWLEDGE
    assert router.route("Create an IT ticket because VPN is broken").decision is AgentDecisionKind.ACTION


def test_skill_runtime_fallback_keeps_hr_scope_and_excludes_it_tool() -> None:
    tool = LocalITTicketTool()
    knowledge = Knowledge()
    observed = {}

    def router_factory(skill, specifications):
        observed["skill"] = skill.skill_id
        observed["tools"] = tuple(spec.name for spec in specifications)
        return ResilientAgentRouter(
            FixedRouter(routing_error(503, transient=True)),
            DeterministicAgentRouter(),
            SafeFallbackPolicy(tuple(specifications)),
        )

    def knowledge_factory(skill):
        observed["sources"] = skill.knowledge_sources
        return knowledge

    agent = SkillAwareAgent(
        create_default_skill_registry(),
        DeterministicSkillSelector(),
        router_factory,
        knowledge_factory,
        [tool],
    )
    identity = LocalIdentityProvider().resolve("employee-001")

    result = agent.handle("How many vacation days do I get?", identity=identity)

    assert result.selected_skill_id == "hr"
    assert result.agent_result.decision.decision is AgentDecisionKind.KNOWLEDGE
    assert observed["skill"] == "hr"
    assert observed["tools"] == ()
    assert observed["sources"] == create_default_skill_registry().load("hr").knowledge_sources
    assert "05_it_setup_guide.md" not in observed["sources"]
    assert result.exposed_tool_names == ()
    assert result.routing_provenance is not None
    assert result.routing_provenance.fallback_used is True
    assert result.routing_provenance.fallback_decision is AgentDecisionKind.KNOWLEDGE


def test_it_problem_fallback_still_uses_knowledge_normalization() -> None:
    knowledge = Knowledge()

    def router_factory(skill, specifications):
        return ResilientAgentRouter(
            FixedRouter(routing_error(504, transient=True)),
            DeterministicAgentRouter(),
            SafeFallbackPolicy(tuple(specifications)),
        )

    agent = SkillAwareAgent(
        create_default_skill_registry(),
        DeterministicSkillSelector(),
        router_factory,
        lambda skill: knowledge,
        [LocalITTicketTool()],
    )

    result = agent.handle(
        "My VPN isn't working.",
        identity=LocalIdentityProvider().resolve("employee-001"),
    )

    normalized = result.agent_result.normalized_knowledge_request
    assert result.agent_result.decision.decision is AgentDecisionKind.KNOWLEDGE
    assert normalized is not None and normalized.normalization_applied is True
    assert knowledge.calls == [(normalized.normalized_request, 3)]


def test_unauthorized_action_is_denied_before_resilient_router_is_built() -> None:
    built = 0

    def router_factory(skill, specifications):
        nonlocal built
        built += 1
        raise AssertionError("authorization must happen before routing")

    agent = SkillAwareAgent(
        create_default_skill_registry(),
        DeterministicSkillSelector(),
        router_factory,
        lambda skill: Knowledge(),
        [LocalITTicketTool()],
    )

    result = agent.handle(
        "Create an IT ticket because my VPN is broken.",
        identity=LocalIdentityProvider().resolve("employee-002"),
    )

    assert result.agent_result.permission_denied is True
    assert result.agent_result.tool_executed is False
    assert built == 0
