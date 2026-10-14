import pytest

from knowvia.agent_models import AgentDecisionKind
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import LocalITTicketTool
from knowvia.knowledge_normalization import (
    DeclarativeStatusAmbiguityPolicy,
    DeterministicKnowledgeRequestNormalizer,
    KnowledgeNormalizationKind,
)
from knowvia.models import AnswerResult
from knowvia.orchestrator import AgentOrchestrator


@pytest.mark.parametrize(
    "utterance",
    [
        "My VPN isn't working.",
        "I can't access company email.",
        "My laptop won't connect to Wi-Fi.",
        "I'm having trouble with MFA.",
        "My VPN is giving error 809.",
        "My VPN does not work.",
        "Our work account doesn't accept the sign-in.",
        "my vpn dose not work",
    ],
)
def test_declarative_problem_reports_become_informational_questions(utterance) -> None:
    result = DeterministicKnowledgeRequestNormalizer().normalize(utterance)

    assert result.normalization_applied is True
    assert result.normalization_kind is KnowledgeNormalizationKind.DECLARATIVE_PROBLEM
    assert result.original_request == utterance
    assert utterance in result.normalized_request
    assert result.normalized_request.startswith(
        "What documented company guidance directly addresses this reported problem: "
    )
    assert "ticket" not in result.normalized_request.casefold()


@pytest.mark.parametrize(
    "utterance",
    [
        "How many vacation days do I get?",
        "What is the meal allowance?",
        "What is the VPN policy?",
        "How do I reset my password?",
    ],
)
def test_clear_questions_are_not_unnecessarily_normalized(utterance) -> None:
    result = DeterministicKnowledgeRequestNormalizer().normalize(utterance)

    assert result.normalization_applied is False
    assert result.normalized_request == utterance


@pytest.mark.parametrize(
    "utterance",
    [
        "Create an IT ticket for my VPN problem.",
        "Open a support ticket because email isn't working.",
    ],
)
def test_explicit_actions_are_never_rewritten_as_knowledge(utterance) -> None:
    result = DeterministicKnowledgeRequestNormalizer().normalize(utterance)

    assert result.normalization_applied is False
    assert result.normalized_request == utterance


def test_ambiguous_statement_is_left_unchanged() -> None:
    request = "Things are difficult today."
    result = DeterministicKnowledgeRequestNormalizer().normalize(request)
    assert result.normalization_applied is False
    assert result.normalized_request == request


def test_minor_auxiliary_typo_does_not_insert_missing_negation() -> None:
    status = DeclarativeStatusAmbiguityPolicy().classify("my vpn dose work")

    assert status is not None
    assert status.subject == "vpn"
    assert "is working" in status.clarification_question
    assert "is not working" in status.clarification_question


def test_clear_negative_problem_is_not_reclassified_as_ambiguous_positive() -> None:
    status = DeclarativeStatusAmbiguityPolicy().classify("my vpn does not work")

    assert status is None


def test_clear_positive_status_requests_purpose_instead_of_inventing_failure() -> None:
    status = DeclarativeStatusAmbiguityPolicy().classify("my vpn works")

    assert status is not None
    assert status.clarification_question == (
        "What help or company information do you need about your vpn?"
    )


class _Knowledge:
    def __init__(self): self.calls = []
    def answer(self, question, top_k=3):
        self.calls.append((question, top_k))
        return AnswerResult("documented guidance", [], [])


class _RecordingTool(LocalITTicketTool):
    def __init__(self):
        super().__init__()
        self.calls = []
    def execute(self, arguments):
        self.calls.append(dict(arguments))
        return super().execute(arguments)


def test_orchestrator_sends_normalized_request_only_to_knowledge() -> None:
    knowledge, tool = _Knowledge(), _RecordingTool()
    result = AgentOrchestrator(
        DeterministicAgentRouter(), knowledge, [tool]
    ).handle("My VPN isn't working.")

    assert result.decision.decision is AgentDecisionKind.KNOWLEDGE
    assert result.normalized_knowledge_request
    assert knowledge.calls[0][0] == result.normalized_knowledge_request.normalized_request
    assert "My VPN isn't working." in knowledge.calls[0][0]
    assert tool.calls == []


def test_explicit_action_bypasses_normalization_and_remains_action() -> None:
    knowledge, tool = _Knowledge(), _RecordingTool()
    result = AgentOrchestrator(
        DeterministicAgentRouter(), knowledge, [tool]
    ).handle("Create an IT ticket for my VPN problem.")

    assert result.decision.decision is AgentDecisionKind.ACTION
    assert result.normalized_knowledge_request is None
    assert knowledge.calls == []
    assert len(tool.calls) == 1
