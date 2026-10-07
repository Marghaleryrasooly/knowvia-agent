import pytest

from knowvia.agent_models import AgentDecisionKind
from knowvia.agent_tools import LocalITTicketTool
from knowvia.answering import AnsweringService
from knowvia.identity import LocalIdentityProvider
from knowvia.general_knowledge import (
    DeterministicGeneralKnowledgeIntentPolicy,
    GeneralKnowledgeIntentKind,
)
from knowvia.models import (
    AnswerResult,
    Chunk,
    EvidenceDecision,
    EvidenceStatus,
    RetrievalResult,
)
from knowvia.presentation import (
    DeterministicEmployeeResponsePresenter,
    EmployeeResponseKind,
)
from knowvia.skill_orchestration import SkillAwareAgent, SourceScopedRetriever
from knowvia.skills import DeterministicSkillSelector, create_default_skill_registry


PETS = "What is the company's policy for employees bringing pets to the office?"
SUPPORTED = "What does the company require before merging production code?"


class CandidateRetriever:
    def __init__(self):
        self.calls = []

    def search(self, question, top_k=3):
        self.calls.append((question, top_k))
        return [
            RetrievalResult(
                Chunk("it", "06_engineering_practices.md", "Engineering Practices", "Code Review", "All production code changes require review and approval from at least one other engineer before merging."),
                0.92,
            ),
            RetrievalResult(
                Chunk("hr", "01_employee_handbook.md", "Employee Handbook", "Working Hours", "Standard working hours are Monday through Friday, 9:00 AM to 5:00 PM."),
                0.80,
            ),
            RetrievalResult(
                Chunk("finance", "08_expense_policy.md", "Expense Policy", "Receipts", "Receipts are required for individual expenses of USD 25 or more."),
                0.70,
            ),
        ]


class DecisionEvaluator:
    evaluator_name = "deterministic-test"

    def __init__(self, sufficient):
        self.sufficient = sufficient
        self.inputs = []

    def evaluate(self, question, results):
        self.inputs.append(tuple(results))
        if self.sufficient:
            return EvidenceDecision(EvidenceStatus.SUFFICIENT, "direct evidence", "", (1,))
        return EvidenceDecision(EvidenceStatus.INSUFFICIENT, "no policy", "A pet policy is not documented.", ())


class Generator:
    model_name = "deterministic-test"

    def __init__(self):
        self.calls = []

    def generate(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return "Production code changes require another engineer's review and approval before merging."


def build_agent(identity_id, *, sufficient):
    candidates = CandidateRetriever()
    evaluator = DecisionEvaluator(sufficient)
    generator = Generator()
    router_calls = []

    def router_factory(skill, specifications):
        router_calls.append((skill.skill_id, specifications))
        raise AssertionError("The general knowledge-only capability must not construct an action router")

    def knowledge_factory(skill):
        return AnsweringService(
            SourceScopedRetriever(candidates, skill.knowledge_sources),
            generator,
            evaluator,
        )

    agent = SkillAwareAgent(
        create_default_skill_registry(),
        DeterministicSkillSelector(),
        router_factory,
        knowledge_factory,
        [LocalITTicketTool()],
    )
    identity = LocalIdentityProvider().resolve(identity_id)
    return agent, identity, candidates, evaluator, generator, router_calls


def test_pet_policy_reaches_evidence_and_abstains_without_invention() -> None:
    agent, identity, candidates, evaluator, generator, router_calls = build_agent(
        "employee-001", sufficient=False
    )

    result = agent.handle(PETS, identity=identity)
    response = DeterministicEmployeeResponsePresenter().present_agent(result.agent_result)

    assert result.selected_skill_id == "general_company_knowledge"
    assert result.exposed_tool_names == ()
    assert result.agent_result.decision.decision is AgentDecisionKind.KNOWLEDGE
    assert candidates.calls and evaluator.inputs
    assert generator.calls == []
    assert result.agent_result.knowledge_result is not None
    assert result.agent_result.knowledge_result.supporting_results == []
    assert result.agent_result.knowledge_result.sources == []
    assert response.kind is EmployeeResponseKind.ABSTENTION
    assert router_calls == []


def test_supported_unmatched_company_question_answers_from_supporting_source() -> None:
    agent, identity, _, evaluator, generator, _ = build_agent(
        "employee-001", sufficient=True
    )

    result = agent.handle(SUPPORTED, identity=identity)
    answer = result.agent_result.knowledge_result

    assert result.selected_skill_id == "general_company_knowledge"
    assert answer is not None and len(answer.supporting_results) == 1
    assert answer.supporting_results[0].chunk.source == "06_engineering_practices.md"
    assert [(source.source, source.section) for source in answer.sources] == [
        ("06_engineering_practices.md", "Code Review")
    ]
    assert generator.calls
    assert "Working Hours" not in generator.calls[0][1]


def test_hr_only_general_scope_excludes_it_and_finance_before_evidence() -> None:
    agent, identity, _, evaluator, _, _ = build_agent(
        "hr-employee-001", sufficient=False
    )

    result = agent.handle(PETS, identity=identity)

    assert result.knowledge_sources == (
        "01_employee_handbook.md",
        "02_remote_work_policy.md",
        "04_employee_benefits_guide.md",
    )
    received = evaluator.inputs[0]
    assert {item.chunk.source for item in received} == {"01_employee_handbook.md"}
    assert result.agent_result.knowledge_result.sources == []


def test_it_only_general_scope_excludes_hr_and_finance_before_evidence() -> None:
    agent, identity, _, evaluator, _, _ = build_agent(
        "it-employee-001", sufficient=True
    )

    result = agent.handle(SUPPORTED, identity=identity)

    assert set(result.knowledge_sources) == {
        "03_it_access_security_policy.md",
        "05_it_setup_guide.md",
        "06_engineering_practices.md",
        "07_information_classification_policy.md",
    }
    received = evaluator.inputs[0]
    assert {item.chunk.source for item in received} == {"06_engineering_practices.md"}
    assert all(source.source.startswith("06_") for source in result.agent_result.knowledge_result.sources)


def test_specialized_requests_still_select_existing_skills() -> None:
    registry = create_default_skill_registry()
    selector = DeterministicSkillSelector()
    expected = {
        "How many vacation days do I get?": "hr",
        "My VPN is broken.": "it_support",
        "Can I claim a meal expense?": "finance",
        "Create an IT ticket because my VPN is broken.": "it_support",
    }
    assert {
        request: selector.select(request, registry.discover()).skill_id
        for request in expected
    } == expected


def test_unmatched_actions_and_context_phrases_never_enter_general_knowledge() -> None:
    built = []
    agent = SkillAwareAgent(
        create_default_skill_registry(),
        DeterministicSkillSelector(),
        lambda skill, specs: built.append(skill.skill_id),
        lambda skill: built.append(skill.skill_id),
        [LocalITTicketTool()],
    )
    identity = LocalIdentityProvider().resolve("employee-001")

    for request in ("Book a flight for me.", "Submit this for me.", "Yes please."):
        result = agent.handle(request, identity=identity)
        assert result.agent_result.decision.decision is AgentDecisionKind.UNSUPPORTED
        assert result.agent_result.tool_executed is False
    assert built == []


@pytest.mark.parametrize(
    "employee_text",
    (
        "i want know my yearly leave",
        "company give us paid days off?",
        "want to know company leave policy",
        "how many sick day we have",
        "Could you explain the rules for office visitors?",
        "I'd like to understand our hybrid schedule",
        "I'd be grateful to understand what the handbook provides",
        "looking for guidance on workplace closures",
        "Could you outline the paid absence available if I'm unwell?",
        "Would you walk me through the workplace visitor requirements?",
        "Can you give me an overview of the company closure schedule?",
        "Will you break down what staff receive during parental absence?",
    ),
)
def test_structural_information_forms_enter_general_knowledge_without_alias_rules(
    employee_text,
) -> None:
    policy = DeterministicGeneralKnowledgeIntentPolicy()
    assert policy.classify(employee_text).kind is GeneralKnowledgeIntentKind.KNOWLEDGE

    agent, identity, candidates, evaluator, _, router_calls = build_agent(
        "employee-001", sufficient=True
    )
    result = agent.handle(employee_text, identity=identity)

    assert result.selected_skill_id == "general_company_knowledge"
    assert result.exposed_tool_names == ()
    assert result.agent_result.decision.decision is AgentDecisionKind.KNOWLEDGE
    assert candidates.calls and evaluator.inputs
    assert result.agent_result.tool_executed is False
    assert result.agent_result.action_proposal is None
    assert router_calls == []


@pytest.mark.parametrize("employee_text", ("help", "tell me", "what?", "policy"))
def test_vague_information_forms_clarify_without_loading_capabilities(employee_text) -> None:
    built = []
    agent = SkillAwareAgent(
        create_default_skill_registry(),
        DeterministicSkillSelector(),
        lambda skill, specs: built.append(skill.skill_id),
        lambda skill: built.append(skill.skill_id),
        [LocalITTicketTool()],
    )
    identity = LocalIdentityProvider().resolve("employee-001")

    result = agent.handle(employee_text, identity=identity)

    assert result.agent_result.decision.decision is AgentDecisionKind.CLARIFY
    assert result.agent_result.tool_executed is False
    assert result.agent_result.action_proposal is None
    assert built == []


@pytest.mark.parametrize(
    "employee_text",
    (
        "Could you create an IT ticket for my VPN problem?",
        "Would you book a flight for me?",
        "Please submit this for me.",
        "Can you do that?",
    ),
)
def test_modal_or_contextual_actions_never_enter_general_knowledge(employee_text) -> None:
    policy = DeterministicGeneralKnowledgeIntentPolicy()

    assert policy.classify(employee_text).kind is GeneralKnowledgeIntentKind.NOT_KNOWLEDGE


@pytest.mark.parametrize("employee_text", ("Could you help?", "Would you assist me?"))
def test_short_modal_requests_require_clarification(employee_text) -> None:
    policy = DeterministicGeneralKnowledgeIntentPolicy()

    assert policy.classify(employee_text).kind is GeneralKnowledgeIntentKind.AMBIGUOUS


@pytest.mark.parametrize(
    "employee_text",
    (
        "vacation days?",
        "how much vacation i get?",
        "how many day company give for vacation",
        "tell me about our vacation policy",
        "tell me what company says about remote work",
    ),
)
def test_existing_domain_terms_still_use_specialized_progressive_disclosure(employee_text) -> None:
    selection = DeterministicSkillSelector().select(
        employee_text, create_default_skill_registry().discover()
    )
    assert selection.status.name == "SELECTED"
    assert selection.skill_id == "hr"
