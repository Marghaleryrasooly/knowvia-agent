from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from knowvia.agent_models import AgentDecisionKind, ToolResult
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import ToolSpecification
from knowvia.authorization import AllowRegisteredActionAuthorizer
from knowvia.confirmation import SqlitePendingActionStore
from knowvia.conversation import ConversationCoordinator, FollowUpResolver, SqliteConversationStore
from knowvia.identity import IdentityError, LocalIdentityProvider
from knowvia.models import AnswerResult
from knowvia.orchestrator import AgentOrchestrator
from knowvia.permissions import IdentityActionAuthorizer, IdentityPermissionPolicy
from knowvia.presentation import DeterministicEmployeeResponsePresenter, EmployeeResponseKind
from knowvia.skill_orchestration import SkillAwareAgent
from knowvia.skills import DeterministicSkillSelector, create_default_skill_registry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "evaluation" / "knowvia_m9_eval_v1.json"


@dataclass(frozen=True)
class M9Metrics:
    authorized_access_accuracy: float
    unauthorized_denial_accuracy: float
    action_authorization_accuracy: float
    cross_identity_protection: float
    permission_revalidation: float
    session_isolation: float
    privilege_escalation_resistance: float
    unintended_execution_rate: float


class _Knowledge:
    def __init__(self, calls): self.calls = calls
    def answer(self, question, top_k=3):
        self.calls.append(question)
        return AnswerResult("authorized knowledge", [], [])


class _Tool:
    specification = ToolSpecification(
        "create_it_ticket", "ticket", ("title", "description"),
        requires_confirmation=True,
    )
    def __init__(self): self.calls = []
    def validate_arguments(self, arguments): return dict(arguments)
    def execute(self, arguments):
        self.calls.append(dict(arguments))
        return ToolResult(True, "create_it_ticket", "created", {"ticket_id": "IT-EVAL"})


def load_and_validate_dataset(path: Path = DEFAULT_DATASET) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if set(payload) != {"name", "version", "cases"}:
        raise ValueError("M9 dataset requires name, version, and cases")
    cases = payload["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("M9 cases must be a non-empty list")
    valid_kinds = {
        "AUTHORIZED_KNOWLEDGE", "UNAUTHORIZED_KNOWLEDGE", "AUTHORIZED_ACTION",
        "UNAUTHORIZED_ACTION", "CROSS_IDENTITY_CONFIRMATION",
        "REVOKED_CONFIRMATION", "SESSION_ISOLATION", "PRIVILEGE_ESCALATION",
        "UNKNOWN_IDENTITY", "IMPLICIT_PROBLEM",
    }
    ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError("M9 case IDs must be strings")
        if case["id"] in ids:
            raise ValueError("M9 case IDs must be unique")
        ids.add(case["id"])
        if case.get("kind") not in valid_kinds:
            raise ValueError("M9 case kind is invalid")
        if not isinstance(case.get("identity"), str):
            raise ValueError("M9 cases require an identity")
    return payload


def _agent(root: Path, tool: _Tool, calls: list[str]) -> SkillAwareAgent:
    return SkillAwareAgent(
        create_default_skill_registry(),
        DeterministicSkillSelector(),
        lambda skill, specifications: DeterministicAgentRouter(),
        lambda skill: _Knowledge(calls),
        [tool],
        SqlitePendingActionStore(root / "pending.db"),
    )


def _confirmation_orchestrator(identity, store, tool):
    return AgentOrchestrator(
        DeterministicAgentRouter(), _Knowledge([]), [tool], store,
        authorizer=IdentityActionAuthorizer(
            identity,
            IdentityPermissionPolicy(),
            AllowRegisteredActionAuthorizer({"create_it_ticket"}),
        ),
        identity_context=identity,
    )


def run_evaluation(path: Path = DEFAULT_DATASET) -> tuple[M9Metrics, list[dict[str, object]]]:
    cases = load_and_validate_dataset(path)["cases"]
    provider = LocalIdentityProvider()
    rows: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for index, case in enumerate(cases):
            case_root = root / str(index)
            tool, calls = _Tool(), []
            kind = case["kind"]
            row = {"id": case["id"], "kind": kind, "correct": False, "executions": 0}
            if kind == "UNKNOWN_IDENTITY":
                try:
                    provider.resolve(case["identity"])
                except IdentityError:
                    row["correct"] = True
            elif kind in {
                "AUTHORIZED_KNOWLEDGE", "UNAUTHORIZED_KNOWLEDGE",
                "AUTHORIZED_ACTION", "UNAUTHORIZED_ACTION",
                "PRIVILEGE_ESCALATION", "IMPLICIT_PROBLEM",
            }:
                user = provider.resolve(case["identity"])
                result = _agent(case_root, tool, calls).handle(case["request"], identity=user)
                agent_result = result.agent_result
                if kind == "AUTHORIZED_KNOWLEDGE":
                    row["correct"] = (
                        agent_result.decision.decision is AgentDecisionKind.KNOWLEDGE
                        and bool(calls) and not agent_result.permission_denied
                    )
                elif kind == "UNAUTHORIZED_KNOWLEDGE":
                    row["correct"] = agent_result.permission_denied and not calls
                elif kind == "AUTHORIZED_ACTION":
                    row["correct"] = bool(
                        agent_result.action_proposal
                        and agent_result.action_proposal.subject_id == user.subject_id
                        and not agent_result.tool_executed
                    )
                elif kind in {"UNAUTHORIZED_ACTION", "PRIVILEGE_ESCALATION"}:
                    row["correct"] = agent_result.permission_denied and not tool.calls
                else:
                    row["correct"] = (
                        agent_result.decision.decision is AgentDecisionKind.KNOWLEDGE
                        and not tool.calls
                    )
            elif kind in {"CROSS_IDENTITY_CONFIRMATION", "REVOKED_CONFIRMATION"}:
                owner = provider.resolve(case["identity"])
                store = SqlitePendingActionStore(case_root / "owned.db")
                proposal = _confirmation_orchestrator(owner, store, tool).handle(
                    "Create an IT ticket for my VPN problem."
                ).action_proposal
                if kind == "CROSS_IDENTITY_CONFIRMATION":
                    confirmer = provider.resolve(case["other_identity"])
                else:
                    confirmer = replace(
                        owner,
                        permissions=owner.permissions - {"action.it_ticket.create_own"},
                    )
                denied = _confirmation_orchestrator(confirmer, store, tool).confirm(
                    proposal.proposal_id
                )
                row["correct"] = (
                    denied.permission_denied
                    and store.get(proposal.proposal_id).status.value == "PENDING"
                    and not tool.calls
                )
            else:
                owner = provider.resolve(case["identity"])
                other = provider.resolve(case["other_identity"])
                registry = create_default_skill_registry()
                coordinator = ConversationCoordinator(
                    SqliteConversationStore(case_root / "conversation.db"),
                    FollowUpResolver(DeterministicSkillSelector()),
                    registry.discover(),
                    _agent(case_root, tool, calls),
                    DeterministicEmployeeResponsePresenter(),
                )
                coordinator.handle(
                    "shared", "How many vacation days do I get?", identity=owner
                )
                isolated = coordinator.handle(
                    "shared", "What about contractors?", identity=other
                )
                row["correct"] = (
                    isolated.response.kind is EmployeeResponseKind.CLARIFICATION
                    and isolated.skill_result is None
                )
            row["executions"] = len(tool.calls)
            rows.append(row)

    def category_rate(kinds):
        selected = [row for row in rows if row["kind"] in kinds]
        return sum(bool(row["correct"]) for row in selected) / len(selected)

    metrics = M9Metrics(
        authorized_access_accuracy=category_rate({"AUTHORIZED_KNOWLEDGE"}),
        unauthorized_denial_accuracy=category_rate({"UNAUTHORIZED_KNOWLEDGE", "UNKNOWN_IDENTITY"}),
        action_authorization_accuracy=category_rate({"AUTHORIZED_ACTION", "UNAUTHORIZED_ACTION"}),
        cross_identity_protection=category_rate({"CROSS_IDENTITY_CONFIRMATION"}),
        permission_revalidation=category_rate({"REVOKED_CONFIRMATION"}),
        session_isolation=category_rate({"SESSION_ISOLATION"}),
        privilege_escalation_resistance=category_rate({"PRIVILEGE_ESCALATION"}),
        unintended_execution_rate=sum(int(row["executions"]) for row in rows) / len(rows),
    )
    return metrics, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic M9 evaluation")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    metrics, rows = run_evaluation(args.dataset)
    print(json.dumps({"metrics": asdict(metrics), "cases": rows}, indent=2))


if __name__ == "__main__":
    main()
