import pytest

from knowvia.agent_models import AgentDecision, AgentDecisionKind
from knowvia.agent_routing import (
    AgentRoutingError,
    DeterministicAgentRouter,
    validate_agent_decision,
)


@pytest.mark.parametrize(
    ("employee_text", "expected"),
    [
        ("How many vacation days do I get?", AgentDecisionKind.KNOWLEDGE),
        ("Create an IT ticket because my VPN is broken.", AgentDecisionKind.ACTION),
        ("Create an IT ticket.", AgentDecisionKind.CLARIFY),
        ("Reset my password for me.", AgentDecisionKind.UNSUPPORTED),
    ],
)
def test_deterministic_routes(employee_text, expected) -> None:
    assert DeterministicAgentRouter().route(employee_text).decision is expected


def test_action_arguments_preserve_employee_problem() -> None:
    decision = DeterministicAgentRouter().route(
        "Open a support ticket because my laptop will not boot."
    )
    assert decision.tool_name == "create_it_ticket"
    assert decision.tool_arguments["description"] == "my laptop will not boot"
    assert decision.tool_arguments["title"] == "IT support: My laptop will not boot"


def test_make_ticket_is_explicit_when_problem_is_in_current_turn() -> None:
    decision = DeterministicAgentRouter().route(
        "Make an IT ticket because my VPN is broken."
    )

    assert decision.decision is AgentDecisionKind.ACTION
    assert decision.tool_name == "create_it_ticket"
    assert decision.tool_arguments["description"] == "my VPN is broken"


def test_action_like_document_quote_is_knowledge() -> None:
    decision = DeterministicAgentRouter().route(
        "Summarize this policy phrase: 'open an IT ticket after troubleshooting'."
    )
    assert decision.decision is AgentDecisionKind.KNOWLEDGE


def test_invalid_tool_name_is_rejected() -> None:
    decision = AgentDecision(
        AgentDecisionKind.ACTION,
        tool_name="invented_tool",
        tool_arguments={"value": "x"},
        reason="test",
    )
    with pytest.raises(AgentRoutingError, match="unknown tool"):
        validate_agent_decision(decision, {"create_it_ticket"})


def test_inconsistent_decision_is_rejected() -> None:
    decision = AgentDecision(
        AgentDecisionKind.KNOWLEDGE,
        tool_name="create_it_ticket",
        reason="test",
    )
    with pytest.raises(AgentRoutingError, match="action fields"):
        validate_agent_decision(decision, {"create_it_ticket"})
