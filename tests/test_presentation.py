from datetime import datetime, timedelta, timezone

from knowvia.agent_models import (
    ActionProposal,
    ActionProposalStatus,
    AgentDecision,
    AgentDecisionKind,
    AgentResult,
    ToolResult,
)
from knowvia.answering import ABSTENTION_RESPONSE
from knowvia.models import AnswerResult, EvidenceDecision, EvidenceStatus, SourceCitation
from knowvia.presentation import (
    DeterministicEmployeeResponsePresenter,
    EmployeeResponseKind,
    friendly_source_label,
)


def decision(kind, **kwargs):
    return AgentDecision(kind, reason="PROVIDER_TIMEOUT secret-token", **kwargs)


def proposal(status=ActionProposalStatus.PENDING):
    now = datetime.now(timezone.utc)
    return ActionProposal(
        "proposal-1",
        "create_it_ticket",
        {"title": "VPN problem", "description": "My VPN is broken"},
        now,
        now + timedelta(minutes=10),
        status,
    )


def action_decision():
    return decision(
        AgentDecisionKind.ACTION,
        tool_name="create_it_ticket",
        tool_arguments={"title": "VPN problem", "description": "My VPN is broken"},
    )


def test_natural_knowledge_response_preserves_answer_and_sources() -> None:
    answer = AnswerResult(
        "You receive 24 paid vacation days per calendar year.",
        [SourceCitation("01_employee_handbook.md", "Annual Leave")],
        [],
        EvidenceDecision(EvidenceStatus.SUFFICIENT, "direct", ""),
    )
    result = AgentResult(decision(AgentDecisionKind.KNOWLEDGE), answer.answer, knowledge_result=answer)

    response = DeterministicEmployeeResponsePresenter().present_agent(result)

    assert response.kind is EmployeeResponseKind.KNOWLEDGE
    assert response.text == answer.answer
    assert response.sources == tuple(answer.sources)
    assert friendly_source_label(response.sources[0]) == "Employee Handbook — Annual Leave"


def test_abstention_and_consulted_sources_are_preserved() -> None:
    answer = AnswerResult(
        ABSTENTION_RESPONSE,
        [SourceCitation("05_information_classification.md", "Confidential")],
        [],
        EvidenceDecision(EvidenceStatus.INSUFFICIENT, "missing", "approval"),
    )

    response = DeterministicEmployeeResponsePresenter().present_answer(answer)

    assert response.kind is EmployeeResponseKind.ABSTENTION
    assert response.text == ABSTENTION_RESPONSE
    assert response.sources == tuple(answer.sources)


def test_proposed_action_is_natural_and_does_not_bypass_confirmation() -> None:
    result = AgentResult(action_decision(), "internal", action_proposal=proposal())

    response = DeterministicEmployeeResponsePresenter().present_agent(result)

    assert response.kind is EmployeeResponseKind.CONFIRMATION_REQUIRED
    assert response.text == "I've prepared an IT support ticket for your approval."
    assert "VPN problem" not in response.text
    assert "My VPN is broken" not in response.text
    assert response.confirmation and response.confirmation.proposal_id == "proposal-1"
    assert result.tool_executed is False and result.tool_result is None


def test_success_preserves_exact_ticket_id_without_sla_or_followup_claims() -> None:
    tool_result = ToolResult(
        True,
        "create_it_ticket",
        "internal",
        {"issue_key": "KIT-947", "issue_id": "secret-internal-id"},
    )
    result = AgentResult(action_decision(), "internal", tool_result=tool_result, tool_executed=True)

    response = DeterministicEmployeeResponsePresenter().present_agent(result)

    assert response.kind is EmployeeResponseKind.ACTION_SUCCESS
    assert response.text.count("KIT-947") == 1
    assert "secret-internal-id" not in response.text
    for unsupported_claim in ("within", "SLA", "will contact", "will review", "resolution time"):
        assert unsupported_claim.casefold() not in response.text.casefold()


def test_mock_ticket_identifier_is_preserved() -> None:
    tool_result = ToolResult(True, "create_it_ticket", "internal", {"ticket_id": "IT-0001"})
    response = DeterministicEmployeeResponsePresenter().present_agent(
        AgentResult(action_decision(), "internal", tool_result=tool_result, tool_executed=True)
    )

    assert response.text.count("IT-0001") == 1


def test_failed_and_ambiguous_actions_never_sound_successful() -> None:
    presenter = DeterministicEmployeeResponsePresenter()
    failure = ToolResult(
        False, "create_it_ticket", "provider stack secret-token",
        error="Authorization: Bearer secret-token", error_category="SERVER",
    )
    ambiguous = ToolResult(
        False, "create_it_ticket", "timeout",
        error="raw timeout", error_category="UNKNOWN_EXECUTION_OUTCOME",
    )

    failed_response = presenter.present_agent(
        AgentResult(action_decision(), failure.message, tool_result=failure, tool_executed=True)
    )
    ambiguous_response = presenter.present_agent(
        AgentResult(action_decision(), ambiguous.message, tool_result=ambiguous, tool_executed=True)
    )

    assert failed_response.kind is EmployeeResponseKind.ACTION_FAILURE
    assert "No successful ticket creation was confirmed" in failed_response.text
    assert "secret-token" not in failed_response.text and "SERVER" not in failed_response.text
    assert ambiguous_response.kind is EmployeeResponseKind.ACTION_FAILURE
    assert "check Jira before trying again" in ambiguous_response.text
    assert "created IT ticket" not in ambiguous_response.text


def test_clarification_unsupported_and_cancellation_hide_internal_state() -> None:
    presenter = DeterministicEmployeeResponsePresenter()
    clarify = AgentResult(
        decision(
            AgentDecisionKind.CLARIFY,
            clarification_question="What IT problem should I include in the ticket?",
        ),
        "internal",
    )
    unsupported = AgentResult(decision(AgentDecisionKind.UNSUPPORTED), "stack trace secret-token")
    cancelled = AgentResult(
        decision(AgentDecisionKind.CLARIFY, clarification_question="Action cancelled."),
        "internal",
        action_proposal=proposal(ActionProposalStatus.REJECTED),
    )

    clarify_response = presenter.present_agent(clarify)
    unsupported_response = presenter.present_agent(unsupported)
    cancelled_response = presenter.present_agent(cancelled)

    assert clarify_response.text == "What IT problem should I include in the ticket?"
    assert unsupported_response.kind is EmployeeResponseKind.UNSUPPORTED
    assert unsupported_response.text == "I can’t complete that action with the capabilities currently available."
    assert cancelled_response.kind is EmployeeResponseKind.CANCELLED
    assert "Nothing was submitted" in cancelled_response.text
    combined = " ".join((clarify_response.text, unsupported_response.text, cancelled_response.text))
    for internal in ("PROVIDER_TIMEOUT", "create_it_ticket", "secret-token", "Decision:", "Tool:"):
        assert internal not in combined


def test_provider_routing_failure_has_neutral_employee_wording() -> None:
    result = AgentResult(
        AgentDecision(
            AgentDecisionKind.UNSUPPORTED,
            reason="The routing decision could not be validated: PROVIDER_HTTP_ERROR.",
        ),
        "internal provider failure",
    )

    response = DeterministicEmployeeResponsePresenter().present_agent(result)

    assert response.kind is EmployeeResponseKind.UNSUPPORTED
    assert response.text == "I’m temporarily unable to process your request. Please try again shortly."
    assert "provider" not in response.text.casefold()
