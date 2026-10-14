from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from knowvia.agent_models import ToolResult
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import LocalITTicketTool, ToolSpecification
from knowvia.authorization import AllowRegisteredActionAuthorizer
from knowvia.confirmation import SqlitePendingActionStore
from knowvia.conversation import SqliteConversationStore
from knowvia.identity import LocalIdentityProvider
from knowvia.mcp_integration import MCPAgentTool, OfficialMCPClientTransport
from knowvia.mcp_server import create_ticket_mcp_server
from knowvia.models import AnswerResult, Chunk, EvidenceDecision, EvidenceStatus, RetrievalResult, SourceCitation
from knowvia.orchestrator import AgentOrchestrator
from knowvia.permissions import IdentityActionAuthorizer, IdentityPermissionPolicy
from knowvia.skill_orchestration import SkillAwareAgent
from knowvia.skills import DeterministicSkillSelector, create_default_skill_registry
from knowvia.ticket_lifecycle import SqliteTicketLifecycleStore, StoredTicketStatusProvider, TicketLifecycleService
from knowvia.web import KnowviaWebService, RuntimeBundle, WebAuthenticationConfig, create_web_app


class DemoKnowledge:
    def __init__(self, skill): self.skill = skill

    def answer(self, question, top_k=3):
        lowered = question.lower()
        if "chatgpt" in lowered or "contractor" in lowered:
            return AnswerResult(
                "I couldn't find enough company information to answer that question.",
                [], [], EvidenceDecision(EvidenceStatus.INSUFFICIENT, "missing", "approval or eligibility"), [],
            )
        if "sick leave" in lowered:
            answer, source, section = "Employees receive documented sick leave.", "01_employee_handbook.md", "Sick Leave"
        elif self.skill.skill_id == "hr":
            answer, source, section = "You receive 24 paid vacation days per calendar year.", "01_employee_handbook.md", "Annual Leave"
        elif self.skill.skill_id == "finance":
            answer, source, section = "The documented meal allowance applies during business travel.", "08_expense_policy.md", "Meals"
        else:
            answer, source, section = "Restart the VPN client and sign in again with MFA.", "05_it_setup_guide.md", "VPN"
        chunk = Chunk("1", source, "Document", section, answer)
        retrieval = RetrievalResult(chunk, 1.0)
        return AnswerResult(
            answer, [SourceCitation(source, section)], [retrieval],
            EvidenceDecision(EvidenceStatus.SUFFICIENT, "direct", "", (1,)), [retrieval],
        )


class ConfirmingTool(LocalITTicketTool):
    specification = ToolSpecification(
        "create_it_ticket", "Create an IT support ticket.",
        ("title", "description"), LocalITTicketTool.specification.argument_schema, True,
    )
    def __init__(self):
        super().__init__(); self.executions = 0
    def execute(self, arguments):
        self.executions += 1
        result = super().execute(arguments)
        return ToolResult(result.success, result.tool_name, result.message, result.data)


class DemoRuntimeFactory:
    def __init__(self, tmp_path, mcp=False):
        self.pending = SqlitePendingActionStore(tmp_path / "pending.db")
        self.lifecycle_store = SqliteTicketLifecycleStore(tmp_path / "lifecycle.db")
        self.lifecycle = TicketLifecycleService(self.lifecycle_store, StoredTicketStatusProvider())
        self.tool = ConfirmingTool()
        self.mcp = mcp

    def build(self, identity):
        tool = self.tool
        if self.mcp:
            server = create_ticket_mcp_server(tool, self.lifecycle, identity)
            tool = MCPAgentTool(OfficialMCPClientTransport(server), "create_it_ticket", requires_confirmation=True)
        registry = create_default_skill_registry()
        skill_agent = SkillAwareAgent(
            registry, DeterministicSkillSelector(),
            lambda skill, specs: DeterministicAgentRouter(),
            lambda skill: DemoKnowledge(skill),
            [tool], self.pending,
            ticket_lifecycle_service=self.lifecycle,
        )
        authorizer = IdentityActionAuthorizer(
            identity, IdentityPermissionPolicy(), AllowRegisteredActionAuthorizer({"create_it_ticket"})
        )
        confirmation = AgentOrchestrator(
            DeterministicAgentRouter(), object(), [tool], self.pending,
            authorizer=authorizer, identity_context=identity,
            ticket_lifecycle_recorder=self.lifecycle,
        )
        return RuntimeBundle(skill_agent, confirmation, self.lifecycle, "MCP" if self.mcp else "Direct")


def client(tmp_path, mcp=False):
    factory = DemoRuntimeFactory(tmp_path, mcp)
    conversation_store = SqliteConversationStore(tmp_path / "conversation.db")
    factory.conversation_store = conversation_store
    service = KnowviaWebService(
        factory,
        LocalIdentityProvider(),
        conversation_store,
        proposal_store=factory.pending,
    )
    return TestClient(create_web_app(
        service, authentication_config=WebAuthenticationConfig.local_development()
    )), factory


def chat(client, message, identity="employee-001", session="session-1", demo=False):
    return client.post("/api/chat", json={
        "session_id": session, "identity_id": identity, "message": message, "demo": demo,
    }).json()


def test_hr_grounded_answer_and_source_and_abstention(tmp_path) -> None:
    web, _ = client(tmp_path)
    answer = chat(web, "How many vacation days do I get?")
    abstention = chat(web, "Can I paste confidential source code into ChatGPT?", session="session-2")
    assert answer["kind"] == "KNOWLEDGE"
    assert answer["sources"][0]["section"] == "Annual Leave"
    assert abstention["kind"] == "ABSTENTION" and abstention["sources"] == []


def test_web_path_resolves_answer_to_skill_scoped_problem_clarification(tmp_path) -> None:
    web, factory = client(tmp_path)

    ambiguous = chat(web, "my vpn dose work", session="status-clarification")
    clarified = chat(web, "It is not working.", session="status-clarification")
    proposed = chat(
        web, "Create an IT ticket for me.", session="status-clarification"
    )

    assert ambiguous["kind"] == "CLARIFICATION"
    assert ambiguous["text"] == (
        "Did you mean that your vpn is working, or that it is not working and "
        "you need help or an IT ticket created?"
    )
    assert clarified["kind"] == "KNOWLEDGE"
    assert clarified["sources"] == [
        {
            "document": "It Setup Guide",
            "section": "VPN",
            "source": "05_it_setup_guide.md",
        }
    ]
    assert clarified.get("action") is None
    assert proposed["kind"] == "CONFIRMATION_REQUIRED"
    assert "vpn is not working" in proposed["action"]["description"].casefold()
    assert factory.tool.executions == 0
    cancelled = web.post(
        f'/api/proposals/{proposed["action"]["proposal_id"]}/cancel',
        json={
            "identity_id": "employee-001",
            "session_id": "status-clarification",
        },
    ).json()
    assert cancelled["kind"] == "CANCELLED"
    assert factory.tool.executions == 0


def test_clear_negative_vpn_statement_remains_a_problem_report(tmp_path) -> None:
    web, factory = client(tmp_path)

    result = chat(web, "my vpn does not work", session="clear-negative-status")

    assert result["kind"] == "KNOWLEDGE"
    assert result["sources"][0]["section"] == "VPN"
    assert result.get("action") is None
    assert factory.tool.executions == 0


def test_clear_positive_vpn_statement_is_not_rewritten_as_a_failure(tmp_path) -> None:
    web, factory = client(tmp_path)

    result = chat(web, "my vpn works", session="clear-positive-status")

    assert result["kind"] == "CLARIFICATION"
    assert "not working" not in result["text"].casefold()
    assert result.get("action") is None
    assert factory.tool.executions == 0


def test_conversation_followup_and_cross_identity_isolation(tmp_path) -> None:
    web, _ = client(tmp_path)
    chat(web, "How many vacation days do I get?", session="shared")
    followup = chat(web, "What about sick leave?", session="shared")
    isolated = chat(web, "What about contractors?", identity="employee-002", session="shared")
    assert followup["sources"][0]["section"] == "Sick Leave"
    assert isolated["kind"] == "CLARIFICATION"


def test_web_path_resolves_contextual_vpn_knowledge_with_fresh_evidence(tmp_path) -> None:
    web, factory = client(tmp_path)
    first = chat(
        web,
        "My VPN isn't working. What should I do?",
        session="vpn-knowledge-continuation",
    )

    follow_up = chat(
        web,
        "What if that doesn't fix it?",
        session="vpn-knowledge-continuation",
    )

    assert first["kind"] == "KNOWLEDGE"
    assert follow_up["kind"] == "KNOWLEDGE"
    assert follow_up["sources"] == [
        {
            "document": "It Setup Guide",
            "section": "VPN",
            "source": "05_it_setup_guide.md",
        }
    ]
    assert follow_up.get("action") is None
    assert factory.tool.executions == 0


def test_contextual_knowledge_cannot_cross_identity_or_session(tmp_path) -> None:
    web, factory = client(tmp_path)
    chat(web, "My VPN isn't working. What should I do?", session="owned-topic")

    other_session = chat(web, "What if that doesn't fix it?", session="other-topic")
    other_identity = chat(
        web,
        "What if that doesn't fix it?",
        identity="employee-002",
        session="owned-topic",
    )

    assert other_session["kind"] == "CLARIFICATION"
    assert other_identity["kind"] == "CLARIFICATION"
    assert factory.tool.executions == 0


def test_declarative_vpn_is_knowledge_and_unauthorized_finance_is_denied(tmp_path) -> None:
    web, factory = client(tmp_path)
    vpn = chat(web, "My VPN isn't working.")
    finance = chat(web, "What is the meal allowance?", identity="it-employee-001", session="finance")
    assert vpn["kind"] == "KNOWLEDGE" and vpn["sources"][0]["section"] == "VPN"
    assert finance["kind"] == "UNSUPPORTED"
    assert factory.tool.executions == 0


@pytest.mark.parametrize(
    "employee_text",
    (
        "vacation days?",
        "how much vacation i get?",
        "how many day company give for vacation",
        "i want know my yearly leave",
        "company give us paid days off?",
        "tell me about our vacation policy",
        "want to know company leave policy",
        "how many sick day we have",
        "tell me what company says about remote work",
        "I would like to understand the workplace visitor rules",
    ),
)
def test_informal_information_requests_reach_knowledge_without_actions(
    tmp_path, employee_text
) -> None:
    web, factory = client(tmp_path)

    result = chat(
        web,
        employee_text,
        session=f"informal-{abs(hash(employee_text))}",
    )

    assert result["kind"] == "KNOWLEDGE"
    assert result.get("action") is None
    assert factory.tool.executions == 0


def test_ticket_proposal_cancel_and_confirmation_use_backend_lifecycle(tmp_path) -> None:
    web, factory = client(tmp_path)
    proposed = chat(web, "Create an IT ticket because my VPN is broken.", session="ticket-1")
    assert proposed["kind"] == "CONFIRMATION_REQUIRED"
    assert proposed["text"] == "I've prepared an IT support ticket for your approval."
    assert proposed["action"]["title"] not in proposed["text"]
    assert proposed["action"]["description"] not in proposed["text"]
    assert factory.tool.executions == 0
    cancelled = web.post(
        f'/api/proposals/{proposed["action"]["proposal_id"]}/cancel',
        json={"identity_id": "employee-001", "session_id": "ticket-1"},
    ).json()
    assert cancelled["kind"] == "CANCELLED" and factory.tool.executions == 0
    assert cancelled["proposal_update"] == {
        "proposal_id": proposed["action"]["proposal_id"],
        "status": "CANCELLED",
    }
    reopened = web.get(
        "/api/conversations/ticket-1?identity_id=employee-001"
    ).json()["messages"]
    proposal_message = next(message for message in reopened if message.get("action"))
    assert proposal_message["action"]["status"] == "CANCELLED"
    assert proposal_message["action"]["title"] == proposed["action"]["title"]
    assert proposal_message["action"]["description"] == proposed["action"]["description"]
    assert reopened[-1]["kind"] == "CANCELLED"

    repeated = web.post(
        f'/api/proposals/{proposed["action"]["proposal_id"]}/confirm',
        json={"identity_id": "employee-001", "session_id": "ticket-1"},
    ).json()
    assert repeated["kind"] == "ACTION_FAILURE"
    assert factory.tool.executions == 0

    second = chat(web, "Create an IT ticket because my VPN is broken.", session="ticket-2")
    confirmed = web.post(
        f'/api/proposals/{second["action"]["proposal_id"]}/confirm',
        json={"identity_id": "employee-001", "session_id": "ticket-2"},
    ).json()
    assert confirmed["kind"] == "ACTION_SUCCESS"
    assert confirmed["ticket"]["ticket_id"] == "IT-0001"
    assert confirmed["proposal_update"] == {
        "proposal_id": second["action"]["proposal_id"],
        "status": "EXECUTED",
        "ticket_id": "IT-0001",
    }
    assert factory.lifecycle_store.get("IT-0001").owner_subject_id == "employee-001"
    assert factory.tool.executions == 1

    reopened_success = web.get(
        "/api/conversations/ticket-2?identity_id=employee-001"
    ).json()["messages"]
    executed_card = next(message for message in reopened_success if message.get("action"))
    assert executed_card["action"]["status"] == "EXECUTED"
    assert executed_card["action"]["ticket_id"] == "IT-0001"
    assert reopened_success[-1]["kind"] == "ACTION_SUCCESS"

    stale_repeat = web.post(
        f'/api/proposals/{second["action"]["proposal_id"]}/confirm',
        json={"identity_id": "employee-001", "session_id": "ticket-2"},
    ).json()
    assert stale_repeat["kind"] == "ACTION_FAILURE"
    assert stale_repeat["proposal_update"]["status"] == "EXECUTED"
    assert factory.tool.executions == 1


def test_reload_reconciles_stale_proposed_transcript_from_authoritative_store(
    tmp_path,
) -> None:
    web, factory = client(tmp_path)
    proposed = chat(
        web,
        "Create an IT ticket because my VPN is broken.",
        session="stale-success",
    )

    confirmed = web.post(
        f'/api/proposals/{proposed["action"]["proposal_id"]}/confirm',
        json={"identity_id": "employee-001"},
    ).json()
    assert confirmed["kind"] == "ACTION_SUCCESS"
    assert factory.tool.executions == 1

    reopened = web.get(
        "/api/conversations/stale-success?identity_id=employee-001"
    ).json()["messages"]
    card = next(message for message in reopened if message.get("action"))
    assert card["action"]["status"] == "EXECUTED"
    assert card["action"]["ticket_id"] == "IT-0001"
    assert factory.tool.executions == 1


def test_reload_fails_closed_for_unverifiable_historical_proposal(tmp_path) -> None:
    web, factory = client(tmp_path)
    factory.conversation_store.append_message(
        "legacy-unverifiable",
        "assistant",
        {
            "kind": "CONFIRMATION_REQUIRED",
            "text": "Prepared.",
            "action": {
                "status": "PROPOSED",
                "proposal_id": "missing-authoritative-proposal",
                "title": "Old request",
                "description": "Historical details.",
            },
        },
        "employee-001",
    )

    reopened = web.get(
        "/api/conversations/legacy-unverifiable?identity_id=employee-001"
    ).json()["messages"]
    card = next(message for message in reopened if message.get("action"))

    assert card["action"]["status"] == "UNAVAILABLE"
    assert factory.tool.executions == 0


def test_explicit_ticket_followup_uses_one_recent_employee_it_problem(tmp_path) -> None:
    web, factory = client(tmp_path)
    guidance = chat(
        web,
        "My VPN isn't working. How can I fix it?",
        session="vpn-followup",
    )

    proposed = chat(web, "Make an IT ticket for me.", session="vpn-followup")

    assert guidance["kind"] == "KNOWLEDGE"
    assert proposed["kind"] == "CONFIRMATION_REQUIRED"
    assert "VPN isn't working" in proposed["action"]["description"]
    assert factory.tool.executions == 0


def test_exact_contextual_ticket_regression_uses_do_support_problem(tmp_path) -> None:
    web, factory = client(tmp_path)
    guidance = chat(web, "My VPN does not work.", session="vpn-do-support")

    proposed = chat(web, "make an it ticket for me", session="vpn-do-support")

    assert guidance["kind"] == "KNOWLEDGE"
    assert proposed["kind"] == "CONFIRMATION_REQUIRED"
    assert "VPN does not work" in proposed["action"]["description"]
    assert factory.tool.executions == 0

    confirmed = web.post(
        f'/api/proposals/{proposed["action"]["proposal_id"]}/confirm',
        json={"identity_id": "employee-001", "session_id": "vpn-do-support"},
    ).json()
    assert confirmed["kind"] == "ACTION_SUCCESS"
    assert factory.tool.executions == 1

    repeated = web.post(
        f'/api/proposals/{proposed["action"]["proposal_id"]}/confirm',
        json={"identity_id": "employee-001", "session_id": "vpn-do-support"},
    ).json()
    assert repeated["kind"] == "ACTION_FAILURE"
    assert factory.tool.executions == 1


def test_exact_misspelled_vpn_problem_reaches_contextual_ticket_proposal(tmp_path) -> None:
    web, factory = client(tmp_path)
    guidance = chat(web, "my vpn dose not work", session="vpn-typo")

    proposed = chat(web, "make an it ticket for me", session="vpn-typo")

    assert guidance["kind"] == "KNOWLEDGE"
    assert proposed["kind"] == "CONFIRMATION_REQUIRED"
    assert proposed["action"]["description"] == "my vpn dose not work"
    assert factory.tool.executions == 0


def test_misspelled_ticket_request_never_executes_immediately(tmp_path) -> None:
    web, factory = client(tmp_path)
    chat(web, "my vpn dose not work", session="ticket-intent-typo")

    result = chat(web, "make an t ticket for me", session="ticket-intent-typo")

    assert result["kind"] != "ACTION_SUCCESS"
    assert "ticket" not in result or result.get("ticket") is None
    if result["kind"] == "CONFIRMATION_REQUIRED":
        assert result["action"]["status"] == "PROPOSED"
    assert factory.tool.executions == 0


def test_terse_it_problem_fragment_can_supply_one_contextual_ticket(tmp_path) -> None:
    web, factory = client(tmp_path)
    guidance = chat(web, "vpn not working", session="vpn-fragment")

    proposed = chat(web, "open an IT ticket for that", session="vpn-fragment")

    assert guidance["kind"] == "KNOWLEDGE"
    assert proposed["kind"] == "CONFIRMATION_REQUIRED"
    assert proposed["action"]["description"] == "vpn not working"
    assert factory.tool.executions == 0


def test_informal_account_problem_can_supply_one_contextual_ticket(tmp_path) -> None:
    web, factory = client(tmp_path)
    guidance = chat(web, "i cant access my work account", session="account-typo")

    proposed = chat(
        web, "please create a ticket for this issue", session="account-typo"
    )

    assert guidance["kind"] == "KNOWLEDGE"
    assert proposed["kind"] == "CONFIRMATION_REQUIRED"
    assert proposed["action"]["description"] == "i cant access my work account"
    assert factory.tool.executions == 0


@pytest.mark.parametrize(
    "employee_request",
    (
        "make an IT ticket for me",
        "create a ticket for this",
        "open an IT ticket for that",
        "raise a ticket for the problem",
        "can you create a ticket for me?",
        "please make a ticket for this issue",
    ),
)
def test_explicit_ticket_variants_use_one_bounded_employee_problem(
    tmp_path, employee_request
) -> None:
    web, factory = client(tmp_path)
    chat(web, "I cannot access my work account.", session="account-problem")

    proposed = chat(web, employee_request, session="account-problem")

    assert proposed["kind"] == "CONFIRMATION_REQUIRED"
    assert "cannot access my work account" in proposed["action"]["description"].lower()
    assert factory.tool.executions == 0


def test_explicit_ticket_without_prior_problem_still_clarifies(tmp_path) -> None:
    web, factory = client(tmp_path)

    result = chat(web, "make an IT ticket for me", session="no-problem")

    assert result["kind"] == "CLARIFICATION"
    assert "action" not in result
    assert factory.tool.executions == 0


def test_current_ticket_details_override_older_problem(tmp_path) -> None:
    web, factory = client(tmp_path)
    chat(web, "My VPN does not work.", session="current-details")

    result = chat(
        web,
        "Create an IT ticket because my company laptop cannot connect to Wi-Fi.",
        session="current-details",
    )

    assert result["kind"] == "CONFIRMATION_REQUIRED"
    assert "laptop cannot connect to Wi-Fi" in result["action"]["description"]
    assert "VPN does not work" not in result["action"]["description"]
    assert factory.tool.executions == 0


def test_ticket_reference_clarifies_when_two_recent_it_problems_compete(tmp_path) -> None:
    web, factory = client(tmp_path)
    chat(web, "My VPN does not work.", session="competing-context")
    chat(web, "My laptop keeps restarting.", session="competing-context")

    result = chat(web, "create a ticket for it", session="competing-context")

    assert result["kind"] == "CLARIFICATION"
    assert "action" not in result
    assert factory.tool.executions == 0


def test_contextual_ticket_followup_supports_clear_laptop_problem(tmp_path) -> None:
    web, factory = client(tmp_path)
    chat(web, "My laptop keeps restarting.", session="laptop-followup")

    proposed = chat(
        web,
        "Create an IT ticket for this problem.",
        session="laptop-followup",
    )

    assert proposed["kind"] == "CONFIRMATION_REQUIRED"
    assert "laptop keeps restarting" in proposed["action"]["description"].lower()
    assert factory.tool.executions == 0


def test_ambiguous_action_followups_never_reconstruct_or_execute(tmp_path) -> None:
    for index, request in enumerate(("Yes please", "Do it", "Create one for that")):
        web, factory = client(tmp_path / str(index))
        chat(web, "My VPN isn't working.", session="ambiguous")

        result = chat(web, request, session="ambiguous")

        assert result["kind"] in {"CLARIFICATION", "UNSUPPORTED"}
        assert "action" not in result
        assert factory.tool.executions == 0


def test_multiple_recent_it_problems_require_clarification(tmp_path) -> None:
    web, factory = client(tmp_path)
    chat(web, "My VPN isn't working.", session="multiple-problems")
    chat(web, "My laptop keeps restarting.", session="multiple-problems")

    result = chat(web, "Make an IT ticket for me.", session="multiple-problems")

    assert result["kind"] == "CLARIFICATION"
    assert "action" not in result
    assert factory.tool.executions == 0


def test_contextual_ticket_details_are_session_and_identity_isolated(tmp_path) -> None:
    web, factory = client(tmp_path)
    chat(web, "My VPN isn't working.", session="owned-context")

    other_session = chat(web, "Make an IT ticket for me.", session="other-context")
    other_identity = chat(
        web,
        "Make an IT ticket for me.",
        identity="employee-002",
        session="owned-context",
    )

    assert other_session["kind"] == "CLARIFICATION"
    assert other_identity["kind"] == "CLARIFICATION"
    assert factory.tool.executions == 0


def test_unauthorized_identity_cannot_propose_contextual_ticket(tmp_path) -> None:
    web, factory = client(tmp_path)
    chat(
        web,
        "My VPN isn't working.",
        identity="employee-002",
        session="knowledge-only",
    )

    denied = chat(
        web,
        "Make an IT ticket for me.",
        identity="employee-002",
        session="knowledge-only",
    )

    assert denied["kind"] == "UNSUPPORTED"
    assert "action" not in denied
    assert factory.tool.executions == 0


def test_explicit_ticket_followup_uses_one_recent_employee_it_problem(tmp_path) -> None:
    web, factory = client(tmp_path)
    guidance = chat(web, "My VPN isn't working.", session="vpn-followup")

    proposed = chat(web, "Make an IT ticket for me.", session="vpn-followup")

    assert guidance["kind"] == "KNOWLEDGE"
    assert proposed["kind"] == "CONFIRMATION_REQUIRED"
    assert "VPN isn't working" in proposed["action"]["description"]
    assert factory.tool.executions == 0


def test_contextual_ticket_followup_supports_clear_laptop_problem(tmp_path) -> None:
    web, factory = client(tmp_path)
    chat(web, "My laptop keeps restarting.", session="laptop-followup")

    proposed = chat(
        web,
        "Create an IT ticket for this problem.",
        session="laptop-followup",
    )

    assert proposed["kind"] == "CONFIRMATION_REQUIRED"
    assert "laptop keeps restarting" in proposed["action"]["description"].lower()
    assert factory.tool.executions == 0


def test_ambiguous_action_followups_never_reconstruct_or_execute(tmp_path) -> None:
    for index, request in enumerate(("Yes please", "Do it", "Create one for that")):
        web, factory = client(tmp_path / str(index))
        chat(web, "My VPN isn't working.", session="ambiguous")

        result = chat(web, request, session="ambiguous")

        assert result["kind"] in {"CLARIFICATION", "UNSUPPORTED"}
        assert "action" not in result
        assert factory.tool.executions == 0


def test_multiple_recent_it_problems_require_clarification(tmp_path) -> None:
    web, factory = client(tmp_path)
    chat(web, "My VPN isn't working.", session="multiple-problems")
    chat(web, "My laptop keeps restarting.", session="multiple-problems")

    result = chat(web, "Make an IT ticket for me.", session="multiple-problems")

    assert result["kind"] == "CLARIFICATION"
    assert "action" not in result
    assert factory.tool.executions == 0


def test_contextual_ticket_details_are_session_and_identity_isolated(tmp_path) -> None:
    web, factory = client(tmp_path)
    chat(web, "My VPN isn't working.", session="owned-context")

    other_session = chat(web, "Make an IT ticket for me.", session="other-context")
    other_identity = chat(
        web,
        "Make an IT ticket for me.",
        identity="employee-002",
        session="owned-context",
    )

    assert other_session["kind"] == "CLARIFICATION"
    assert other_identity["kind"] == "CLARIFICATION"
    assert factory.tool.executions == 0


def test_unauthorized_identity_cannot_propose_contextual_ticket(tmp_path) -> None:
    web, factory = client(tmp_path)
    chat(
        web,
        "My VPN isn't working.",
        identity="employee-002",
        session="knowledge-only",
    )

    denied = chat(
        web,
        "Make an IT ticket for me.",
        identity="employee-002",
        session="knowledge-only",
    )

    assert denied["kind"] == "UNSUPPORTED"
    assert "action" not in denied
    assert factory.tool.executions == 0


def test_unauthorized_action_and_cross_identity_confirmation_are_blocked(tmp_path) -> None:
    web, factory = client(tmp_path)
    denied = chat(web, "Create an IT ticket because my VPN is broken.", identity="employee-002")
    proposal = chat(web, "Create an IT ticket because my VPN is broken.", session="owned")
    cross = web.post(
        f'/api/proposals/{proposal["action"]["proposal_id"]}/confirm',
        json={"identity_id": "employee-002"},
    ).json()
    assert denied["kind"] == "UNSUPPORTED"
    assert cross["kind"] == "UNSUPPORTED"
    assert factory.tool.executions == 0


def test_ticket_owner_status_and_protection(tmp_path) -> None:
    web, _ = client(tmp_path)
    proposal = chat(web, "Create an IT ticket because my VPN is broken.")
    created = web.post(
        f'/api/proposals/{proposal["action"]["proposal_id"]}/confirm',
        json={"identity_id": "employee-001"},
    ).json()
    ticket_id = created["ticket"]["ticket_id"]
    own = chat(web, f"What is the status of my ticket {ticket_id}?", session="status-own")
    other = chat(web, f"What is the status of my ticket {ticket_id}?", identity="it-employee-001", session="status-other")
    assert own["kind"] == "TICKET_STATUS" and own["ticket"]["status"] == "OPEN"
    assert other["kind"] == "UNSUPPORTED"


def test_mcp_ticket_path_and_multi_step_action_card(tmp_path) -> None:
    web, factory = client(tmp_path, mcp=True)
    compound = chat(
        web,
        "My VPN isn't working. Give me the troubleshooting guidance and help me create a ticket.",
        demo=True,
    )
    assert compound["kind"] == "CONFIRMATION_REQUIRED"
    assert compound["sources"][0]["section"] == "VPN"
    assert compound["action"]["status"] == "PROPOSED"
    assert compound["demo"]["action_transport"] == "MCP"
    assert factory.tool.executions == 0


def test_ui_has_no_execution_bypass_and_unknown_identity_fails_closed(tmp_path) -> None:
    web, factory = client(tmp_path)
    unknown = chat(web, "Create an IT ticket because my VPN is broken.", identity="admin")
    bypass = web.post("/api/proposals/fake/execute", json={"identity_id": "employee-001"})
    assert unknown["kind"] == "UNSUPPORTED"
    assert bypass.status_code == 404
    assert factory.tool.executions == 0
