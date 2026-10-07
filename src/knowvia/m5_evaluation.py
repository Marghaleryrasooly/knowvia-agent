from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from knowvia.agent_models import AgentDecisionKind
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import LocalITTicketTool
from knowvia.models import AnswerResult
from knowvia.orchestrator import AgentOrchestrator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "evaluation" / "knowvia_m5_eval_v1.json"


@dataclass(frozen=True)
class M5Metrics:
    routing_accuracy: float
    tool_selection_accuracy: float
    argument_accuracy: float
    clarification_accuracy: float
    unsupported_action_accuracy: float
    tool_execution_accuracy: float
    unintended_tool_execution_rate: float
    knowledge_to_tool_safety_rate: float


class _FakeKnowledge:
    def answer(self, question: str, top_k: int = 3) -> AnswerResult:
        return AnswerResult("Deterministic knowledge response.", [], [])


def load_and_validate_dataset(path: Path = DEFAULT_DATASET) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if set(payload) != {"name", "version", "cases"} or not payload["cases"]:
        raise ValueError("M5 dataset requires name, version, and non-empty cases")
    ids: set[str] = set()
    allowed = {kind.value for kind in AgentDecisionKind}
    for case in payload["cases"]:
        required = {"id", "request", "expected_decision", "expect_execution"}
        if not required.issubset(case):
            raise ValueError("Every M5 case requires id, request, expected_decision, expect_execution")
        if case["id"] in ids or not isinstance(case["id"], str):
            raise ValueError(f"Duplicate or invalid case id: {case['id']}")
        ids.add(case["id"])
        if not isinstance(case["request"], str) or not case["request"].strip():
            raise ValueError(f"Case {case['id']} has an invalid request")
        if case["expected_decision"] not in allowed or not isinstance(case["expect_execution"], bool):
            raise ValueError(f"Case {case['id']} has an invalid expected outcome")
        if case["expected_decision"] == "ACTION":
            if case.get("expected_tool") != "create_it_ticket" or not case.get("description_contains"):
                raise ValueError(f"Action case {case['id']} requires tool and argument criteria")
            if not case["expect_execution"]:
                raise ValueError(f"Action case {case['id']} must expect execution")
        elif case.get("expected_tool") is not None or case.get("description_contains") is not None:
            raise ValueError(f"Non-action case {case['id']} cannot define tool criteria")
    return payload


def run_evaluation(path: Path = DEFAULT_DATASET) -> tuple[M5Metrics, list[dict[str, Any]]]:
    dataset = load_and_validate_dataset(path)
    orchestrator = AgentOrchestrator(
        DeterministicAgentRouter(), _FakeKnowledge(), [LocalITTicketTool()]
    )
    rows: list[dict[str, Any]] = []
    for case in dataset["cases"]:
        result = orchestrator.handle(case["request"])
        actual = result.decision.decision.value
        tool_selected = (
            case.get("expected_tool") is not None
            and result.decision.tool_name == case["expected_tool"]
        )
        argument_ok = False
        if case["expected_decision"] == "ACTION":
            description = result.decision.tool_arguments.get("description", "")
            title = result.decision.tool_arguments.get("title", "")
            argument_ok = (
                case["description_contains"].lower() in description.lower()
                and bool(title.strip())
            )
        rows.append(
            {
                "id": case["id"],
                "expected_decision": case["expected_decision"],
                "actual_decision": actual,
                "routing_correct": actual == case["expected_decision"],
                "tool_selected": tool_selected,
                "argument_correct": argument_ok,
                "tool_executed": result.tool_executed,
                "execution_correct": result.tool_executed == case["expect_execution"],
            }
        )

    def ratio(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 1.0

    actions = [r for r in rows if r["expected_decision"] == "ACTION"]
    clarifies = [r for r in rows if r["expected_decision"] == "CLARIFY"]
    unsupported = [r for r in rows if r["expected_decision"] == "UNSUPPORTED"]
    non_actions = [r for r in rows if r["expected_decision"] != "ACTION"]
    knowledge = [r for r in rows if r["expected_decision"] == "KNOWLEDGE"]
    metrics = M5Metrics(
        routing_accuracy=ratio(sum(r["routing_correct"] for r in rows), len(rows)),
        tool_selection_accuracy=ratio(sum(r["tool_selected"] for r in actions), len(actions)),
        argument_accuracy=ratio(sum(r["argument_correct"] for r in actions), len(actions)),
        clarification_accuracy=ratio(sum(r["routing_correct"] for r in clarifies), len(clarifies)),
        unsupported_action_accuracy=ratio(sum(r["routing_correct"] for r in unsupported), len(unsupported)),
        tool_execution_accuracy=ratio(sum(r["execution_correct"] for r in rows), len(rows)),
        unintended_tool_execution_rate=ratio(sum(r["tool_executed"] for r in non_actions), len(non_actions)),
        knowledge_to_tool_safety_rate=ratio(sum(not r["tool_executed"] for r in knowledge), len(knowledge)),
    )
    return metrics, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic M5 routing/tool-use evaluation")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    metrics, rows = run_evaluation(args.dataset)
    print(json.dumps({"metrics": asdict(metrics), "cases": rows}, indent=2))


if __name__ == "__main__":
    main()
