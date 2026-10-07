from dataclasses import dataclass

from knowvia.agent_models import AgentDecisionKind, ToolResult
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import LocalITTicketTool, ToolSpecification
from knowvia.answering import AnsweringService
from knowvia.confirmation import SqlitePendingActionStore
from knowvia.models import (
    AnswerResult,
    Chunk,
    EvidenceDecision,
    EvidenceStatus,
    RetrievalResult,
    SourceCitation,
)
from knowvia.orchestrator import AgentOrchestrator
from knowvia.presentation import DeterministicEmployeeResponsePresenter
from knowvia.skill_orchestration import SkillAwareAgent, SourceScopedRetriever
from knowvia.skills import (
    DeterministicSkillSelector,
    SkillRegistry,
    SkillSelection,
    SkillSelectionStatus,
    SkillSummary,
    create_default_skill_registry,
)


class RecordingRegistry:
    def __init__(self):
        self.registry = create_default_skill_registry()
        self.loaded = []
        self.load_attempts = []

    def discover(self):
        return self.registry.discover()

    def load(self, skill_id):
        self.load_attempts.append(skill_id)
        definition = self.registry.load(skill_id)
        self.loaded.append(skill_id)
        return definition


class RecordingSelector:
    def __init__(self, delegate=None, selection=None):
        self.delegate = delegate or DeterministicSkillSelector()
        self.selection = selection
        self.received = []

    def select(self, request, summaries):
        self.received.append(summaries)
        assert all(isinstance(summary, SkillSummary) for summary in summaries)
        return self.selection or self.delegate.select(request, summaries)


class RecordingKnowledge:
    def __init__(self, skill, calls):
        self.skill = skill
        self.calls = calls

    def answer(self, question, top_k=3):
        self.calls.append((self.skill.skill_id, self.skill.knowledge_sources, question, top_k))
        return AnswerResult("Scoped answer", [SourceCitation(self.skill.knowledge_sources[0], "Section")], [])


def make_agent(registry=None, selector=None, tools=None, pending=None, audit=None):
    registry = registry or RecordingRegistry()
    selector = selector or RecordingSelector()
    runtime_calls, knowledge_calls = [], []

    def router_factory(skill, specifications):
        runtime_calls.append((skill.skill_id, skill.instructions, tuple(s.name for s in specifications)))
        return DeterministicAgentRouter()

    def knowledge_factory(skill):
        return RecordingKnowledge(skill, knowledge_calls)

    agent = SkillAwareAgent(
        registry,
        selector,
        router_factory,
        knowledge_factory,
        tools or [LocalITTicketTool()],
        pending,
        audit_sink=audit,
    )
    return agent, registry, selector, runtime_calls, knowledge_calls


def test_progressive_disclosure_loads_exactly_one_selected_skill() -> None:
    agent, registry, selector, runtimes, knowledge = make_agent()

    result = agent.handle("My VPN is broken.")

    assert result.selected_skill_id == "it_support"
    assert registry.loaded == ["it_support"]
    assert len(selector.received) == 1
    assert all(isinstance(item, SkillSummary) for item in selector.received[0])
    assert [runtime[0] for runtime in runtimes] == ["it_support"]
    assert knowledge[0][0] == "it_support"


def test_ambiguous_and_unsupported_requests_load_no_skill() -> None:
    agent, registry, _, runtimes, knowledge = make_agent()

    ambiguous = agent.handle("What are the vacation and meal rules?")
    unsupported = agent.handle("Write a marketing campaign.")

    assert ambiguous.agent_result.decision.decision is AgentDecisionKind.CLARIFY
    assert unsupported.agent_result.decision.decision is AgentDecisionKind.UNSUPPORTED
    assert registry.loaded == []
    assert runtimes == [] and knowledge == []


def test_unknown_selected_skill_fails_closed_without_loading_runtime() -> None:
    selector = RecordingSelector(
        selection=SkillSelection(SkillSelectionStatus.SELECTED, "missing", "test")
    )
    agent, registry, _, runtimes, knowledge = make_agent(selector=selector)

    result = agent.handle("VPN")

    assert result.agent_result.decision.decision is AgentDecisionKind.UNSUPPORTED
    assert registry.load_attempts == ["missing"]
    assert registry.loaded == []
    assert runtimes == [] and knowledge == []


def test_bounded_conversation_can_inherit_one_known_skill() -> None:
    selector = RecordingSelector(
        selection=SkillSelection(SkillSelectionStatus.UNSUPPORTED, None, "no current terms")
    )
    agent, registry, _, runtimes, knowledge = make_agent(selector=selector)

    result = agent.handle(
        "Regarding vacation leave, what does policy say about contractors?",
        selected_skill_id="hr",
    )

    assert result.selected_skill_id == "hr"
    assert registry.loaded == ["hr"]
    assert [runtime[0] for runtime in runtimes] == ["hr"]
    assert knowledge[0][0] == "hr"


def test_tool_filtering_occurs_before_router_and_orchestrator() -> None:
    agent, _, _, runtimes, _ = make_agent()

    hr = agent.handle("How many vacation days do I get?")
    it = agent.handle("My VPN is broken.")
    finance = agent.handle("Can I claim a meal expense?")

    assert hr.exposed_tool_names == ()
    assert it.exposed_tool_names == ("create_it_ticket",)
    assert finance.exposed_tool_names == ()
    assert [runtime[2] for runtime in runtimes] == [(), ("create_it_ticket",), ()]


def test_mismatched_action_tool_fails_closed() -> None:
    agent, *_ = make_agent()
    result = agent.handle("Create an IT ticket for my vacation request.")
    # IT and HR tie, so no capability is loaded and no tool can run.
    assert result.agent_result.decision.decision is AgentDecisionKind.CLARIFY
    assert result.agent_result.tool_executed is False


def test_source_scoped_retriever_excludes_irrelevant_sources() -> None:
    class FakeRetriever:
        def __init__(self): self.top_k = None
        def search(self, question, top_k=3):
            self.top_k = top_k
            return [
                RetrievalResult(Chunk("1", "08_expense_policy.md", "Expense", "Meals", "meal"), .9),
                RetrievalResult(Chunk("2", "01_employee_handbook.md", "Handbook", "Leave", "leave"), .8),
                RetrievalResult(Chunk("3", "04_employee_benefits_guide.md", "Benefits", "Health", "health"), .7),
            ]

    base = FakeRetriever()
    scoped = SourceScopedRetriever(
        base,
        ("01_employee_handbook.md", "04_employee_benefits_guide.md"),
        candidate_limit=50,
    )

    results = scoped.search("employee policy", 2)

    assert base.top_k == 50
    assert [result.chunk.source for result in results] == [
        "01_employee_handbook.md", "04_employee_benefits_guide.md"
    ]


def test_skill_scope_applies_before_supporting_evidence_selection() -> None:
    class BaseRetriever:
        def search(self, question, top_k=3):
            return [
                RetrievalResult(Chunk("x", "08_expense_policy.md", "Expense", "Meals", "meal"), .99),
                RetrievalResult(Chunk("h1", "01_employee_handbook.md", "Handbook", "Leave", "24 days"), .9),
                RetrievalResult(Chunk("h2", "01_employee_handbook.md", "Handbook", "Hours", "weekdays"), .8),
            ]

    class Evaluator:
        evaluator_name = "scoped"

        def evaluate(self, question, results):
            assert [item.chunk.section for item in results] == ["Leave", "Hours"]
            return EvidenceDecision(EvidenceStatus.SUFFICIENT, "direct", "", (1,))

    class Generator:
        model_name = "fake"

        def __init__(self): self.prompt = ""
        def generate(self, system_prompt, user_prompt):
            self.prompt = user_prompt
            return "24 days"

    generator = Generator()
    service = AnsweringService(
        SourceScopedRetriever(BaseRetriever(), ("01_employee_handbook.md",)),
        generator,
        Evaluator(),
    )

    answer = service.answer("How much leave?", top_k=2)

    assert [item.chunk.section for item in answer.retrieval_results] == ["Leave", "Hours"]
    assert [item.chunk.section for item in answer.supporting_results] == ["Leave"]
    assert "24 days" in generator.prompt
    assert "weekdays" not in generator.prompt
    assert [(source.source, source.section) for source in answer.sources] == [
        ("01_employee_handbook.md", "Leave")
    ]


class ConfirmationTool:
    specification = ToolSpecification(
        "create_it_ticket", "Create ticket", ("title", "description"),
        {"type": "object", "properties": {}, "required": ["title", "description"]},
        True,
    )

    def __init__(self): self.calls = []
    def validate_arguments(self, arguments): return dict(arguments)
    def execute(self, arguments):
        self.calls.append(dict(arguments))
        return ToolResult(True, self.specification.name, "created", {"issue_key": "KIT-1"})


class RecordingAudit:
    def __init__(self): self.events = []
    def record(self, event): self.events.append(event)


def test_skill_layer_preserves_confirmation_and_one_time_execution(tmp_path) -> None:
    tool = ConfirmationTool()
    audit = RecordingAudit()
    pending = SqlitePendingActionStore(tmp_path / "pending.db")
    agent, *_ = make_agent(tools=[tool], pending=pending, audit=audit)

    proposed = agent.handle("Create an IT ticket because my VPN is broken.").agent_result

    assert proposed.action_proposal is not None
    assert proposed.tool_executed is False
    assert tool.calls == []
    confirmation_orchestrator = AgentOrchestrator(
        DeterministicAgentRouter(), object(), [tool], pending, audit_sink=audit
    )
    first = confirmation_orchestrator.confirm(proposed.action_proposal.proposal_id)
    second = confirmation_orchestrator.confirm(proposed.action_proposal.proposal_id)
    assert first.tool_result and first.tool_result.success is True
    assert second.tool_executed is False
    assert len(tool.calls) == 1
    assert [event.stage for event in audit.events] == ["PROPOSED", "EXECUTED"]


def test_employee_presentation_does_not_expose_skill_internals() -> None:
    agent, *_ = make_agent()
    result = agent.handle("How many vacation days do I get?")

    response = DeterministicEmployeeResponsePresenter().present_agent(result.agent_result)

    assert response.text == "Scoped answer"
    assert "hr" not in response.text.casefold()
    assert "instructions" not in response.text.casefold()
