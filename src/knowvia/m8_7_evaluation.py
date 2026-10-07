from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from knowvia.agent_models import ToolResult
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import ToolSpecification
from knowvia.confirmation import SqlitePendingActionStore
from knowvia.models import AnswerResult
from knowvia.skill_orchestration import SkillAwareAgent
from knowvia.skills import DeterministicSkillSelector, create_default_skill_registry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "evaluation" / "knowvia_m8_7_eval_v1.json"


@dataclass(frozen=True)
class M87Metrics:
    normalization_accuracy: float
    unnecessary_normalization_rate: float
    action_intent_preservation_accuracy: float
    invented_detail_rate: float
    tool_execution_safety_rate: float
    source_scope_preservation_accuracy: float


class _Knowledge:
    def __init__(self, calls: list[str]): self.calls = calls
    def answer(self, question, top_k=3):
        self.calls.append(question)
        return AnswerResult("Deterministic knowledge result.", [], [])


class _ConfirmationTool:
    specification = ToolSpecification(
        "create_it_ticket",
        "Create an IT support ticket after confirmation.",
        ("title", "description"),
        requires_confirmation=True,
    )

    def __init__(self): self.calls = []
    def validate_arguments(self, arguments): return dict(arguments)
    def execute(self, arguments):
        self.calls.append(dict(arguments))
        return ToolResult(True, "create_it_ticket", "created")


def load_and_validate_dataset(path: Path = DEFAULT_DATASET) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if set(payload) != {"name", "version", "cases"}:
        raise ValueError("M8.7 dataset requires name, version, and cases")
    cases = payload["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("M8.7 cases must be a non-empty list")
    ids: set[str] = set()
    for case in cases:
        required = {
            "id", "request", "expected_normalization", "expected_decision",
            "expected_skill", "required_terms", "forbidden_terms",
        }
        if not isinstance(case, dict) or set(case) != required:
            raise ValueError("M8.7 cases have an invalid schema")
        if not isinstance(case["id"], str) or case["id"] in ids:
            raise ValueError("M8.7 case IDs must be unique strings")
        ids.add(case["id"])
        if not isinstance(case["request"], str) or not case["request"].strip():
            raise ValueError("M8.7 requests must be non-empty strings")
        if not isinstance(case["expected_normalization"], bool):
            raise ValueError("M8.7 expected_normalization must be boolean")
        if case["expected_decision"] not in {"KNOWLEDGE", "ACTION", "UNSUPPORTED"}:
            raise ValueError("M8.7 expected_decision is invalid")
        if case["expected_skill"] is not None and not isinstance(case["expected_skill"], str):
            raise ValueError("M8.7 expected_skill must be a string or null")
        for field in ("required_terms", "forbidden_terms"):
            if not isinstance(case[field], list) or not all(
                isinstance(value, str) for value in case[field]
            ):
                raise ValueError(f"M8.7 {field} must be a string list")
    return payload


def run_evaluation(
    path: Path = DEFAULT_DATASET,
) -> tuple[M87Metrics, list[dict[str, object]]]:
    cases = load_and_validate_dataset(path)["cases"]
    rows: list[dict[str, object]] = []
    registry = create_default_skill_registry()
    with tempfile.TemporaryDirectory() as directory:
        for index, case in enumerate(cases):
            knowledge_calls: list[str] = []
            tool = _ConfirmationTool()
            agent = SkillAwareAgent(
                registry,
                DeterministicSkillSelector(),
                lambda skill, specifications: DeterministicAgentRouter(),
                lambda skill: _Knowledge(knowledge_calls),
                [tool],
                SqlitePendingActionStore(Path(directory) / f"{index}.db"),
            )
            result = agent.handle(case["request"])
            agent_result = result.agent_result
            normalization = agent_result.normalized_knowledge_request
            normalized_text = (
                normalization.normalized_request if normalization else case["request"]
            )
            applied = bool(normalization and normalization.normalization_applied)
            lowered = normalized_text.casefold()
            required_present = all(
                term.casefold() in lowered for term in case["required_terms"]
            )
            invented = any(
                term.casefold() in lowered for term in case["forbidden_terms"]
            )
            expected_skill = case["expected_skill"]
            expected_sources = (
                registry.load(expected_skill).knowledge_sources if expected_skill else ()
            )
            rows.append({
                "id": case["id"],
                "normalization_applied": applied,
                "normalized_request": normalized_text,
                "decision": agent_result.decision.decision.value,
                "selected_skill": result.selected_skill_id,
                "normalization_correct": (
                    applied == case["expected_normalization"] and required_present
                ),
                "unnecessary_normalization": (
                    applied and not case["expected_normalization"]
                ),
                "action_intent_preserved": (
                    case["expected_decision"] != "ACTION"
                    or agent_result.decision.decision.value == "ACTION"
                ),
                "invented_detail": invented,
                "tool_execution_safe": not tool.calls,
                "source_scope_preserved": (
                    result.selected_skill_id == expected_skill
                    and result.knowledge_sources == expected_sources
                ),
                "decision_correct": (
                    agent_result.decision.decision.value == case["expected_decision"]
                ),
            })

    def rate(field: str) -> float:
        return sum(bool(row[field]) for row in rows) / len(rows)

    unchanged = [
        row for row, case in zip(rows, cases, strict=True)
        if not case["expected_normalization"]
    ]
    actions = [
        row for row, case in zip(rows, cases, strict=True)
        if case["expected_decision"] == "ACTION"
    ]
    metrics = M87Metrics(
        normalization_accuracy=rate("normalization_correct"),
        unnecessary_normalization_rate=(
            sum(bool(row["unnecessary_normalization"]) for row in unchanged)
            / len(unchanged)
        ),
        action_intent_preservation_accuracy=(
            sum(bool(row["action_intent_preserved"]) for row in actions) / len(actions)
        ),
        invented_detail_rate=rate("invented_detail"),
        tool_execution_safety_rate=rate("tool_execution_safe"),
        source_scope_preservation_accuracy=rate("source_scope_preserved"),
    )
    return metrics, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic M8.7 evaluation")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    metrics, rows = run_evaluation(args.dataset)
    print(json.dumps({"metrics": asdict(metrics), "cases": rows}, indent=2))


if __name__ == "__main__":
    main()
