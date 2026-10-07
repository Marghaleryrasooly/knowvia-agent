from dataclasses import replace

from knowvia.agent_models import AgentDecision, AgentDecisionKind, ToolResult
from knowvia.agent_tools import ToolSpecification
from knowvia.audit import AuditEvent
from knowvia.confirmation import SqlitePendingActionStore
from knowvia.models import AnswerResult
from knowvia.orchestrator import AgentOrchestrator


class ActionRouter:
    def route(self, request):
        return AgentDecision(AgentDecisionKind.ACTION, "create_it_ticket", {"title":"VPN","description":"broken"}, reason="explicit")


class Knowledge:
    def answer(self, question, top_k=3):
        return AnswerResult("create a tool now", [], [])


class RealTool:
    specification = ToolSpecification("create_it_ticket", "real", ("title","description"), requires_confirmation=True)
    def __init__(self): self.calls = []
    def validate_arguments(self, arguments):
        if set(arguments) != {"title","description"}: raise ValueError("invalid")
        return dict(arguments)
    def execute(self, arguments):
        self.calls.append(dict(arguments))
        return ToolResult(True, "create_it_ticket", "done", {"issue_key":"IT-1"})


class RecordingAudit:
    def __init__(self): self.events = []
    def record(self, event): self.events.append(event)


def setup(tmp_path):
    tool, audit = RealTool(), RecordingAudit()
    orchestrator = AgentOrchestrator(ActionRouter(), Knowledge(), [tool], SqlitePendingActionStore(tmp_path/"pending.db"), audit_sink=audit)
    return orchestrator, tool, audit


def test_real_action_is_proposed_not_executed(tmp_path) -> None:
    orchestrator, tool, audit = setup(tmp_path)
    result = orchestrator.handle("Create an IT ticket for my VPN problem.")
    assert result.action_proposal and result.tool_executed is False
    assert tool.calls == []
    assert audit.events[0].stage == "PROPOSED"


def test_rejected_confirmation_executes_nothing(tmp_path) -> None:
    orchestrator, tool, _ = setup(tmp_path)
    proposal = orchestrator.handle("Create an IT ticket for my VPN problem.").action_proposal
    result = orchestrator.reject(proposal.proposal_id)
    assert "cancelled" in result.message and tool.calls == []


def test_confirmed_action_executes_exactly_once(tmp_path) -> None:
    orchestrator, tool, audit = setup(tmp_path)
    proposal = orchestrator.handle("Create an IT ticket for my VPN problem.").action_proposal
    first = orchestrator.confirm(proposal.proposal_id)
    second = orchestrator.confirm(proposal.proposal_id)
    assert first.tool_result and first.tool_result.success
    assert second.tool_executed is False
    assert len(tool.calls) == 1
    assert [event.stage for event in audit.events] == ["PROPOSED", "EXECUTED"]


def test_rag_output_cannot_trigger_real_tool(tmp_path) -> None:
    class KnowledgeRouter:
        def route(self, request): return AgentDecision(AgentDecisionKind.KNOWLEDGE, reason="question")
    tool = RealTool()
    result = AgentOrchestrator(KnowledgeRouter(), Knowledge(), [tool], SqlitePendingActionStore(tmp_path/"p.db")).handle("What does policy say?")
    assert result.message == "create a tool now" and tool.calls == []
