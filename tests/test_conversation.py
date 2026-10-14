from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from knowvia.agent_models import AgentDecision, AgentDecisionKind, AgentResult
from knowvia.conversation import (
    ConversationState,
    ConversationCoordinator,
    ConversationTurn,
    FollowUpResolver,
    SqliteConversationStore,
)
from knowvia.models import AnswerResult, EvidenceDecision, EvidenceStatus, SourceCitation
from knowvia.presentation import DeterministicEmployeeResponsePresenter, EmployeeResponseKind
from knowvia.skill_orchestration import SkillAgentResult
from knowvia.skills import DeterministicSkillSelector, SkillSelectionStatus, create_default_skill_registry


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.mark.parametrize("question", [
    "How does NovaTech determine which public non-working dates it observes?",
    "How does Atlas decide which equipment it supports?",
    "How does the organization decide which standards it follows?",
])
def test_same_question_subject_does_not_require_history(question):
    registry = create_default_skill_registry()
    resolver = FollowUpResolver(DeterministicSkillSelector())
    for turns in [(), (ConversationTurn("My VPN failed", "My VPN failed", "it_support",
                                      "KNOWLEDGE", datetime.now(timezone.utc)),)]:
        result = resolver.resolve(question, ConversationState("local-subject", turns), registry.discover())
        assert result.standalone_request == question
        assert result.status.value == "READY"
        assert result.inherited_skill_id is None


def test_local_subject_does_not_hide_a_separate_unresolved_reference():
    registry = create_default_skill_registry()
    result = FollowUpResolver(DeterministicSkillSelector()).resolve(
        "How does Atlas decide which standards it follows after that?",
        ConversationState("no-history"), registry.discover(),
    )
    assert result.status.value == "CLARIFY"


def test_explicit_embedded_subject_reaches_agent_without_stale_topic(tmp_path):
    coordinator, agent, _, _ = make_coordinator(tmp_path)
    coordinator.handle("embedded-subject", "My VPN isn't working.")
    question = "How does NovaTech determine which public non-working dates it observes?"
    result = coordinator.handle("embedded-subject", question)
    assert result.response.kind is not EmployeeResponseKind.CLARIFICATION
    assert agent.calls[-1][0] == question
    assert agent.calls[-1][2] is None


class RecordingKnowledgeAgent:
    def __init__(self, selector, summaries) -> None:
        self.selector = selector
        self.summaries = summaries
        self.calls = []

    def handle(self, request, top_k=3, selected_skill_id=None):
        if selected_skill_id is None:
            selection = self.selector.select(request, self.summaries)
            skill_id = selection.skill_id if selection.status is SkillSelectionStatus.SELECTED else None
        else:
            skill_id = selected_skill_id
        self.calls.append((request, top_k, selected_skill_id, skill_id))
        if skill_id is None:
            decision = AgentDecision(AgentDecisionKind.UNSUPPORTED, reason="No skill")
            return SkillAgentResult(None, AgentResult(decision, "unsupported"))

        lowered = request.casefold()
        if "contractors" in lowered:
            evidence = EvidenceDecision(
                EvidenceStatus.INSUFFICIENT,
                "The policy does not establish contractor eligibility.",
                "Contractor eligibility.",
            )
            answer = AnswerResult("Insufficient company information.", [], [], evidence)
        else:
            section = (
                "Sick Leave" if "sick leave" in lowered
                else "Meals" if skill_id == "finance"
                else "VPN" if skill_id == "it_support"
                else "Annual Leave"
            )
            source = {
                "hr": "01_employee_handbook.md",
                "finance": "08_expense_policy.md",
                "it_support": "05_it_setup_guide.md",
            }[skill_id]
            evidence = EvidenceDecision(EvidenceStatus.SUFFICIENT, "direct", "", (1,))
            answer = AnswerResult(
                f"Fresh answer from {section}.",
                [SourceCitation(source, section)],
                [],
                evidence,
            )
        decision = AgentDecision(AgentDecisionKind.KNOWLEDGE, reason="Knowledge request")
        return SkillAgentResult(
            skill_id,
            AgentResult(decision, answer.answer, knowledge_result=answer),
            knowledge_sources=tuple(source.source for source in answer.sources),
        )


class ClarifyingKnowledgeAgent(RecordingKnowledgeAgent):
    def handle(self, request, top_k=3, selected_skill_id=None):
        if request in {"my vpn dose work", "my laptop maybe works"}:
            self.calls.append((request, top_k, selected_skill_id, "it_support"))
            decision = AgentDecision(
                AgentDecisionKind.CLARIFY,
                clarification_question="Is the device working, or is it not working?",
                reason="The employee's reported status is ambiguous.",
            )
            return SkillAgentResult(
                "it_support",
                AgentResult(decision, decision.clarification_question or ""),
            )
        return super().handle(request, top_k, selected_skill_id)


def make_coordinator(tmp_path, *, ttl=1800, clock=None):
    clock = clock or Clock()
    registry = create_default_skill_registry()
    summaries = registry.discover()
    selector = DeterministicSkillSelector()
    agent = RecordingKnowledgeAgent(selector, summaries)
    store = SqliteConversationStore(
        tmp_path / "conversation.db",
        max_turns=4,
        max_context_characters=1000,
        ttl_seconds=ttl,
        now=clock,
    )
    coordinator = ConversationCoordinator(
        store,
        FollowUpResolver(selector),
        summaries,
        agent,
        DeterministicEmployeeResponsePresenter(),
        now=clock,
    )
    return coordinator, agent, store, clock


def make_clarifying_coordinator(tmp_path):
    clock = Clock()
    registry = create_default_skill_registry()
    summaries = registry.discover()
    selector = DeterministicSkillSelector()
    agent = ClarifyingKnowledgeAgent(selector, summaries)
    store = SqliteConversationStore(
        tmp_path / "clarification-conversation.db",
        max_turns=4,
        max_context_characters=1000,
        ttl_seconds=1800,
        now=clock,
    )
    coordinator = ConversationCoordinator(
        store,
        FollowUpResolver(selector),
        summaries,
        agent,
        DeterministicEmployeeResponsePresenter(),
        now=clock,
    )
    return coordinator, agent, store


def test_problem_clarification_answer_recovers_bounded_subject_and_skill(tmp_path) -> None:
    coordinator, agent, store = make_clarifying_coordinator(tmp_path)

    first = coordinator.handle("clarification-flow", "my vpn dose work")
    second = coordinator.handle("clarification-flow", "It is not working.")

    assert first.response.kind is EmployeeResponseKind.CLARIFICATION
    assert second.response.kind is EmployeeResponseKind.KNOWLEDGE
    assert second.resolved_request.inherited_skill_id == "it_support"
    assert "vpn is not working" in second.resolved_request.standalone_request
    assert second.agent_result.tool_executed is False
    assert agent.calls[-1][2] == "it_support"
    turns = store.load("clarification-flow").turns
    assert [turn.outcome for turn in turns] == ["CLARIFICATION", "KNOWLEDGE"]
    assert turns[0].original_request == "my vpn dose work"
    assert turns[1].original_request == "It is not working."


def test_incomplete_clarification_answer_does_not_inherit_skill(tmp_path) -> None:
    coordinator, agent, _ = make_clarifying_coordinator(tmp_path)
    coordinator.handle("incomplete-clarification", "my vpn dose work")
    calls_before = len(agent.calls)

    result = coordinator.handle("incomplete-clarification", "It is.")

    assert result.resolved_request.inherited_skill_id is None
    assert result.response.kind is EmployeeResponseKind.UNSUPPORTED
    assert len(agent.calls) == calls_before + 1


def test_clarification_answer_never_inherits_across_sessions(tmp_path) -> None:
    coordinator, agent, _ = make_clarifying_coordinator(tmp_path)
    coordinator.handle("source-clarification", "my vpn dose work")

    result = coordinator.handle("other-clarification", "It is not working.")

    assert result.resolved_request.inherited_skill_id is None
    assert result.response.kind is EmployeeResponseKind.UNSUPPORTED
    assert agent.calls[-1][2] is None


def test_ambiguous_action_language_remains_non_actionable_after_clarification(tmp_path) -> None:
    coordinator, agent, _ = make_clarifying_coordinator(tmp_path)
    coordinator.handle("clarification-action-safety", "my vpn dose work")
    calls_before = len(agent.calls)

    result = coordinator.handle(
        "clarification-action-safety", "Create one for that"
    )

    assert result.response.kind is EmployeeResponseKind.CLARIFICATION
    assert result.agent_result.tool_executed is False
    assert len(agent.calls) == calls_before


def test_vacation_followed_by_sick_leave_retrieves_fresh_hr_evidence(tmp_path) -> None:
    coordinator, agent, _, _ = make_coordinator(tmp_path)

    first = coordinator.handle("employee-1", "How many vacation days do I get?")
    second = coordinator.handle("employee-1", "What about sick leave?")

    assert first.skill_result and first.skill_result.selected_skill_id == "hr"
    assert second.skill_result and second.skill_result.selected_skill_id == "hr"
    assert second.resolved_request.inherited_skill_id is None
    assert second.response.sources[0].section == "Sick Leave"
    assert len(agent.calls) == 2
    assert agent.calls[1][0] == "What about sick leave?"


def test_explicit_domain_terms_override_finance_history(tmp_path) -> None:
    coordinator, _, _, _ = make_coordinator(tmp_path)

    finance = coordinator.handle("employee-2", "What is the meal allowance?")
    hr = coordinator.handle("employee-2", "What about sick leave?")

    assert finance.skill_result and finance.skill_result.selected_skill_id == "finance"
    assert hr.skill_result and hr.skill_result.selected_skill_id == "hr"
    assert hr.resolved_request.inherited_skill_id is None
    assert {(source.source, source.section) for source in hr.response.sources} == {
        ("01_employee_handbook.md", "Sick Leave")
    }


def test_referential_contractors_inherits_topic_but_still_abstains(tmp_path) -> None:
    coordinator, agent, _, _ = make_coordinator(tmp_path)
    coordinator.handle("employee-3", "How many vacation days do I get?")

    result = coordinator.handle("employee-3", "What about contractors?")

    assert result.resolved_request.inherited_skill_id == "hr"
    assert "vacation days" in result.resolved_request.standalone_request
    assert agent.calls[-1][2] == "hr"
    assert result.response.kind is EmployeeResponseKind.ABSTENTION
    assert result.response.sources == ()


def test_explicit_contextual_ticket_followup_is_reconstructed_before_agent(tmp_path) -> None:
    coordinator, agent, _, _ = make_coordinator(tmp_path)
    coordinator.handle("employee-4", "My VPN isn't working.")
    calls_before = len(agent.calls)

    result = coordinator.handle("employee-4", "Can you create a ticket for that?")

    assert result.resolved_request.standalone_request == (
        "Create an IT ticket because My VPN isn't working."
    )
    assert result.resolved_request.inherited_skill_id == "it_support"
    assert len(agent.calls) == calls_before + 1
    assert agent.calls[-1][0] == result.resolved_request.standalone_request
    assert result.agent_result.tool_executed is False


def test_affirmative_followup_never_confirms_or_executes(tmp_path) -> None:
    coordinator, agent, _, _ = make_coordinator(tmp_path)
    coordinator.handle("employee-5", "My VPN isn't working.")
    calls_before = len(agent.calls)

    result = coordinator.handle("employee-5", "Yes, please.")

    assert result.response.kind is EmployeeResponseKind.CLARIFICATION
    assert result.skill_result is None
    assert result.agent_result.tool_executed is False
    assert len(agent.calls) == calls_before


def test_stale_session_does_not_inherit_old_skill_context(tmp_path) -> None:
    coordinator, agent, store, clock = make_coordinator(tmp_path, ttl=30)
    coordinator.handle("employee-6", "How many vacation days do I get?")
    clock.advance(31)

    result = coordinator.handle("employee-6", "What about contractors?")

    assert result.response.kind is EmployeeResponseKind.CLARIFICATION
    assert result.resolved_request.inherited_skill_id is None
    assert len(agent.calls) == 1
    assert len(store.load("employee-6").turns) == 1


def test_ambiguous_reference_clarifies_without_inventing_context(tmp_path) -> None:
    coordinator, agent, _, _ = make_coordinator(tmp_path)
    coordinator.handle("employee-7", "How many vacation days do I get?")
    calls_before = len(agent.calls)

    result = coordinator.handle("employee-7", "What about that?")

    assert result.response.kind is EmployeeResponseKind.CLARIFICATION
    assert len(agent.calls) == calls_before


@pytest.mark.parametrize(
    "follow_up",
    (
        "What if that doesn't fix it?",
        "What should I do then?",
        "And after that?",
        "What if it keeps happening?",
    ),
)
def test_structural_knowledge_followup_uses_one_bounded_recent_topic(
    tmp_path, follow_up
) -> None:
    coordinator, agent, _, _ = make_coordinator(tmp_path)
    first = coordinator.handle(
        "contextual-knowledge", "My VPN isn't working. What should I do?"
    )

    result = coordinator.handle("contextual-knowledge", follow_up)

    assert first.response.kind is EmployeeResponseKind.KNOWLEDGE
    assert result.response.kind is EmployeeResponseKind.KNOWLEDGE
    assert result.resolved_request.inherited_skill_id == "it_support"
    assert "My VPN isn't working" in result.resolved_request.standalone_request
    assert follow_up in result.resolved_request.standalone_request
    assert agent.calls[-1][0] == result.resolved_request.standalone_request
    assert agent.calls[-1][2] == "it_support"


def test_pronoun_policy_followup_retrieves_fresh_evidence(tmp_path) -> None:
    coordinator, agent, _, _ = make_coordinator(tmp_path)
    coordinator.handle("policy-reference", "How many vacation days do I get?")

    result = coordinator.handle("policy-reference", "Does that apply to me?")

    assert result.response.kind is EmployeeResponseKind.KNOWLEDGE
    assert result.resolved_request.inherited_skill_id == "hr"
    assert "vacation days" in result.resolved_request.standalone_request
    assert agent.calls[-1][0] == result.resolved_request.standalone_request


def test_plural_pronoun_policy_followup_uses_bounded_recent_topic(tmp_path) -> None:
    coordinator, agent, _, _ = make_coordinator(tmp_path)
    coordinator.handle("plural-policy-reference", "How many vacation days do I get?")

    result = coordinator.handle(
        "plural-policy-reference", "What happens if I don't use all of them?"
    )

    assert result.response.kind is EmployeeResponseKind.KNOWLEDGE
    assert result.resolved_request.inherited_skill_id == "hr"
    assert "vacation days" in result.resolved_request.standalone_request
    assert "all of them" in result.resolved_request.standalone_request
    assert agent.calls[-1][0] == result.resolved_request.standalone_request


def test_multiple_recent_knowledge_topics_make_deictic_followup_clarify(tmp_path) -> None:
    coordinator, agent, _, _ = make_coordinator(tmp_path)
    coordinator.handle("ambiguous-topics", "My VPN isn't working. What should I do?")
    coordinator.handle("ambiguous-topics", "How do I reset my password?")
    calls_before = len(agent.calls)

    result = coordinator.handle("ambiguous-topics", "What if that doesn't work?")

    assert result.response.kind is EmployeeResponseKind.CLARIFICATION
    assert result.skill_result is None
    assert len(agent.calls) == calls_before


@pytest.mark.parametrize(
    "follow_up", ("Yes", "Yes please", "Do it", "Create one for that")
)
def test_context_never_turns_ambiguous_action_language_into_execution(
    tmp_path, follow_up
) -> None:
    coordinator, agent, _, _ = make_coordinator(tmp_path)
    coordinator.handle("action-safety", "My VPN isn't working.")
    calls_before = len(agent.calls)

    result = coordinator.handle("action-safety", follow_up)

    assert result.response.kind is EmployeeResponseKind.CLARIFICATION
    assert result.skill_result is None
    assert result.agent_result.tool_executed is False
    assert len(agent.calls) == calls_before


def test_contextual_knowledge_is_session_isolated(tmp_path) -> None:
    coordinator, agent, _, _ = make_coordinator(tmp_path)
    coordinator.handle("source-session", "My VPN isn't working. What should I do?")
    calls_before = len(agent.calls)

    result = coordinator.handle("other-session", "What if that doesn't fix it?")

    assert result.response.kind is EmployeeResponseKind.CLARIFICATION
    assert result.skill_result is None
    assert len(agent.calls) == calls_before


def test_store_enforces_turn_and_character_bounds(tmp_path) -> None:
    clock = Clock()
    store = SqliteConversationStore(
        tmp_path / "bounded.db", max_turns=2, max_context_characters=30, now=clock
    )
    for number in range(3):
        store.append(
            "bounded",
            ConversationTurn(
                f"q{number}", f"resolved-{number}", "hr", "KNOWLEDGE", clock()
            ),
        )

    state = store.load("bounded")

    assert len(state.turns) == 2
    assert state.turns[-1].original_request == "q2"


def test_oversized_turn_cannot_become_inherited_context(tmp_path) -> None:
    clock = Clock()
    store = SqliteConversationStore(
        tmp_path / "oversized.db", max_context_characters=80, now=clock
    )
    store.append(
        "oversized",
        ConversationTurn("x" * 60, "vacation " * 20, "hr", "KNOWLEDGE", clock()),
    )

    state = store.load("oversized")

    assert state.latest_knowledge_turn is None
    assert state.turns == ()
