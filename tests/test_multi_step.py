from datetime import datetime, timedelta, timezone

from knowvia.agent_models import (
    ActionProposal,
    AgentDecision,
    AgentDecisionKind,
    AgentResult,
    ToolResult,
)
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import LocalITTicketTool, ToolSpecification
from knowvia.confirmation import SqlitePendingActionStore
from knowvia.identity import LocalIdentityProvider
from knowvia.models import AnswerResult, SourceCitation
from knowvia.multi_step import (
    DeterministicMultiStepPlanner,
    MultiStepCoordinator,
    MultiStepPlan,
    PlanStep,
    PlanStepKind,
)
from knowvia.presentation import DeterministicEmployeeResponsePresenter, EmployeeResponseKind
from knowvia.skill_orchestration import SkillAgentResult, SkillAwareAgent
from knowvia.skills import DeterministicSkillSelector, create_default_skill_registry
from knowvia.ticket_lifecycle import TicketStatus, TicketStatusResult


class FakeSafeAgent:
    def __init__(self, permission_denied=False, requires_confirmation=False):
        self.calls = []
        self.permission_denied = permission_denied
        self.requires_confirmation = requires_confirmation

    def handle(self, request, top_k=3, selected_skill_id=None, identity=None):
        self.calls.append(request)
        lowered = request.lower()
        if "status of" in lowered:
            result = AgentResult(
                AgentDecision(AgentDecisionKind.KNOWLEDGE, reason="trusted status"),
                "Your IT ticket KIT-15 is open.",
                ticket_status_result=TicketStatusResult(
                    True, "KIT-15", TicketStatus.OPEN, "Your IT ticket KIT-15 is open."
                ),
            )
        elif "create" in lowered and "ticket" in lowered:
            if self.permission_denied:
                result = AgentResult(
                    AgentDecision(AgentDecisionKind.UNSUPPORTED, reason="permission denied"),
                    "Permission denied.",
                    permission_denied=True,
                )
            elif self.requires_confirmation:
                now = datetime.now(timezone.utc)
                proposal = ActionProposal(
                    "proposal-1", "create_it_ticket",
                    {"title": "VPN problem", "description": "VPN is not working."},
                    now, now + timedelta(minutes=10), subject_id="employee-001",
                )
                result = AgentResult(
                    AgentDecision(
                        AgentDecisionKind.ACTION, "create_it_ticket",
                        proposal.tool_arguments, reason="explicit action"
                    ),
                    "Proposed.", action_proposal=proposal,
                )
            else:
                result = AgentResult(
                    AgentDecision(
                        AgentDecisionKind.ACTION, "create_it_ticket",
                        {"title": "VPN problem", "description": "VPN is not working."},
                        reason="explicit action",
                    ),
                    "Created.",
                    tool_result=ToolResult(
                        True, "create_it_ticket", "Created.",
                        {"ticket_id": "IT-0001", "status": "OPEN"},
                    ),
                    tool_executed=True,
                )
        else:
            answer = AnswerResult(
                "Restart the VPN client and sign in again with MFA.",
                [SourceCitation("05_it_setup_guide.md", "VPN")],
                [],
            )
            result = AgentResult(
                AgentDecision(AgentDecisionKind.KNOWLEDGE, reason="knowledge"),
                answer.answer, knowledge_result=answer,
            )
        return SkillAgentResult("it_support", result)


class FixedPlanner:
    def __init__(self, plan): self.value = plan
    def plan(self, request): return self.value


COMPOUND = "My VPN isn't working. Give me the troubleshooting guidance and help me create a ticket."


def test_knowledge_and_action_only_requests_remain_single_step() -> None:
    planner = DeterministicMultiStepPlanner()
    knowledge = planner.plan("How do I reset my password?")
    action = planner.plan("Create an IT ticket because my VPN is broken.")
    assert [step.kind for step in knowledge.steps] == [PlanStepKind.KNOWLEDGE]
    assert [step.kind for step in action.steps] == [PlanStepKind.CREATE_IT_TICKET]


def test_vpn_guidance_and_ticket_are_ordered_and_do_not_pass_answer_to_action() -> None:
    agent = FakeSafeAgent()
    result = MultiStepCoordinator(DeterministicMultiStepPlanner(), agent).handle(COMPOUND)
    assert result.completed
    assert [step.kind for step in result.plan.steps] == [
        PlanStepKind.KNOWLEDGE, PlanStepKind.CREATE_IT_TICKET
    ]
    assert agent.calls[0] == "My VPN isn't working."
    assert agent.calls[1] == "Create an IT ticket because My VPN isn't working."
    assert "Restart the VPN" not in agent.calls[1]


def test_grounded_sources_and_confirmation_are_preserved_in_presentation() -> None:
    agent = FakeSafeAgent(requires_confirmation=True)
    result = MultiStepCoordinator(DeterministicMultiStepPlanner(), agent).handle(COMPOUND)
    response = DeterministicEmployeeResponsePresenter().present_multi_step(
        tuple(step.agent_result for step in result.step_results)
    )
    assert response.kind is EmployeeResponseKind.CONFIRMATION_REQUIRED
    assert response.confirmation and response.confirmation.proposal_id == "proposal-1"
    assert response.sources == (SourceCitation("05_it_setup_guide.md", "VPN"),)
    assert "Restart the VPN" in response.text


def test_unauthorized_action_inside_plan_is_blocked_after_knowledge() -> None:
    agent = FakeSafeAgent(permission_denied=True)
    result = MultiStepCoordinator(DeterministicMultiStepPlanner(), agent).handle(COMPOUND)
    assert result.completed is False
    assert result.failure_reason == "STEP_BLOCKED"
    assert len(result.step_results) == 2
    assert result.step_results[-1].agent_result.permission_denied


def test_ticket_status_remains_trusted_nonexecuting_step() -> None:
    agent = FakeSafeAgent()
    result = MultiStepCoordinator(DeterministicMultiStepPlanner(), agent).handle(
        "What is the status of my ticket KIT-15?"
    )
    status = result.step_results[0].agent_result.ticket_status_result
    assert result.completed and status and status.status is TicketStatus.OPEN
    assert result.step_results[0].agent_result.tool_executed is False


def test_malformed_and_over_limit_plans_fail_without_calling_agent() -> None:
    malformed = MultiStepPlan(
        "request", (PlanStep("wrong-id", PlanStepKind.KNOWLEDGE, "question"),), "bad"
    )
    over_limit = MultiStepPlan(
        "request",
        tuple(PlanStep(f"step-{n}", PlanStepKind.KNOWLEDGE, "question") for n in range(1, 5)),
        "too many",
    )
    agent = FakeSafeAgent()
    assert not MultiStepCoordinator(FixedPlanner(malformed), agent).handle("request").completed
    assert not MultiStepCoordinator(FixedPlanner(over_limit), agent).handle("request").completed
    assert agent.calls == []


def test_repeated_capability_and_model_action_without_user_intent_fail_closed() -> None:
    repeated = MultiStepPlan(
        "Give me VPN guidance.",
        (
            PlanStep("step-1", PlanStepKind.KNOWLEDGE, "VPN guidance"),
            PlanStep("step-2", PlanStepKind.KNOWLEDGE, "More VPN guidance", ("step-1",)),
        ),
        "model repeated a capability",
    )
    injected = MultiStepPlan(
        "Give me VPN guidance.",
        (PlanStep("step-1", PlanStepKind.CREATE_IT_TICKET, "Create an IT ticket."),),
        "model introduced an action",
    )
    agent = FakeSafeAgent()
    assert not MultiStepCoordinator(FixedPlanner(repeated), agent).handle(repeated.original_request).completed
    assert not MultiStepCoordinator(FixedPlanner(injected), agent).handle(injected.original_request).completed
    assert agent.calls == []


class ScopedKnowledge:
    def answer(self, question, top_k=3):
        return AnswerResult(
            "Use the documented VPN troubleshooting steps.",
            [SourceCitation("05_it_setup_guide.md", "VPN")],
            [],
        )


class ConfirmingLocalTool(LocalITTicketTool):
    specification = ToolSpecification(
        "create_it_ticket",
        "Create a test IT ticket.",
        ("title", "description"),
        LocalITTicketTool.specification.argument_schema,
        True,
    )


def real_skill_agent(tool, pending):
    return SkillAwareAgent(
        create_default_skill_registry(),
        DeterministicSkillSelector(),
        lambda skill, specifications: DeterministicAgentRouter(),
        lambda skill: ScopedKnowledge(),
        [tool],
        pending,
    )


def test_real_skill_runtime_applies_scoping_identity_and_confirmation_per_step(tmp_path) -> None:
    agent = real_skill_agent(
        ConfirmingLocalTool(), SqlitePendingActionStore(tmp_path / "pending.db")
    )
    result = MultiStepCoordinator(DeterministicMultiStepPlanner(), agent).handle(
        COMPOUND, identity=LocalIdentityProvider().resolve("employee-001")
    )

    assert result.completed
    assert [item.selected_skill_id for item in result.step_results] == [
        "it_support", "it_support"
    ]
    assert result.step_results[0].knowledge_sources == (
        "03_it_access_security_policy.md",
        "05_it_setup_guide.md",
        "06_engineering_practices.md",
        "07_information_classification_policy.md",
    )
    action = result.step_results[1].agent_result
    assert action.action_proposal is not None
    assert action.tool_executed is False


def test_real_skill_runtime_blocks_unauthorized_compound_action(tmp_path) -> None:
    tool = LocalITTicketTool()
    agent = real_skill_agent(tool, SqlitePendingActionStore(tmp_path / "pending.db"))
    result = MultiStepCoordinator(DeterministicMultiStepPlanner(), agent).handle(
        COMPOUND, identity=LocalIdentityProvider().resolve("employee-002")
    )

    assert result.completed is False
    assert result.step_results[0].agent_result.knowledge_result is not None
    assert result.step_results[1].agent_result.permission_denied is True
