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
from knowvia.skills import (
    DeterministicSkillSelector,
    SkillSelection,
    SkillSelectionStatus,
    SkillSummary,
    create_default_skill_registry,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "evaluation" / "knowvia_m8_eval_v1.json"


@dataclass(frozen=True)
class M8Metrics:
    skill_selection_accuracy: float
    relevant_tool_exposure_accuracy: float
    irrelevant_tool_exposure_rate: float
    knowledge_scope_accuracy: float
    progressive_disclosure_correctness: float
    unintended_tool_execution_rate: float
    safety_preservation_rate: float


class _RecordingRegistry:
    def __init__(self):
        self.registry = create_default_skill_registry()
        self.loaded: list[str] = []
        self.load_attempts: list[str] = []

    def discover(self):
        return self.registry.discover()

    def load(self, skill_id):
        self.load_attempts.append(skill_id)
        definition = self.registry.load(skill_id)
        self.loaded.append(skill_id)
        return definition


class _RecordingSelector:
    def __init__(self, force_unknown=False):
        self.delegate = DeterministicSkillSelector()
        self.force_unknown = force_unknown
        self.summary_only = True
        self.selection = None

    def select(self, request, summaries):
        self.summary_only = self.summary_only and all(
            isinstance(summary, SkillSummary) for summary in summaries
        )
        if self.force_unknown:
            self.selection = SkillSelection(
                SkillSelectionStatus.SELECTED, "nonexistent", "evaluation"
            )
        else:
            self.selection = self.delegate.select(request, summaries)
        return self.selection


class _Knowledge:
    def answer(self, question, top_k=3):
        return AnswerResult("Scoped deterministic knowledge response.", [], [])


class _ConfirmationTool:
    specification = ToolSpecification(
        "create_it_ticket",
        "Create a deterministic evaluation ticket.",
        ("title", "description"),
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["title", "description"],
            "additionalProperties": False,
        },
        True,
    )

    def __init__(self): self.calls = []
    def validate_arguments(self, arguments): return dict(arguments)
    def execute(self, arguments):
        self.calls.append(dict(arguments))
        return ToolResult(True, self.specification.name, "created", {"issue_key": "KIT-EVAL"})


def load_and_validate_dataset(path: Path = DEFAULT_DATASET) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if set(payload) != {"name", "version", "cases"}:
        raise ValueError("M8 dataset requires name, version, and cases")
    cases = payload["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("M8 dataset cases must be a non-empty list")
    ids: set[str] = set()
    statuses = {"SELECTED", "AMBIGUOUS", "UNSUPPORTED", "UNKNOWN_SKILL"}
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError("M8 case IDs must be strings")
        if case["id"] in ids:
            raise ValueError("M8 case IDs must be unique")
        ids.add(case["id"])
        if case.get("expected_status") not in statuses:
            raise ValueError("M8 cases require a valid expected_status")
        if not isinstance(case.get("request"), str):
            raise ValueError("M8 cases require a request")
        for field in ("expected_tools", "expected_sources"):
            if not isinstance(case.get(field), list) or not all(
                isinstance(value, str) for value in case[field]
            ):
                raise ValueError(f"M8 {field} must be a string list")
    return payload


def run_evaluation(path: Path = DEFAULT_DATASET) -> tuple[M8Metrics, list[dict[str, object]]]:
    cases = load_and_validate_dataset(path)["cases"]
    rows: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as directory:
        for index, case in enumerate(cases):
            registry = _RecordingRegistry()
            selector = _RecordingSelector(bool(case.get("force_unknown_skill")))
            tool = _ConfirmationTool()
            runtime_skills = []

            def router_factory(skill, specifications):
                runtime_skills.append(skill.skill_id)
                return DeterministicAgentRouter()

            agent = SkillAwareAgent(
                registry,
                selector,
                router_factory,
                lambda skill: _Knowledge(),
                [tool],
                SqlitePendingActionStore(Path(directory) / f"{index}.db"),
            )
            result = agent.handle(case["request"])
            expected_skill = case.get("expected_skill")
            selected_correct = result.selected_skill_id == expected_skill
            if case["expected_status"] == "AMBIGUOUS":
                status_correct = (
                    result.agent_result.decision.decision.value == "CLARIFY"
                )
            elif case["expected_status"] in {"UNSUPPORTED", "UNKNOWN_SKILL"}:
                status_correct = (
                    result.agent_result.decision.decision.value == "UNSUPPORTED"
                )
            else:
                status_correct = result.selected_skill_id is not None
            expected_loads = [expected_skill] if expected_skill else []
            disclosure_correct = (
                selector.summary_only
                and registry.loaded == expected_loads
                and runtime_skills == ([expected_skill] if expected_skill else [])
            )
            expected_tools = tuple(case["expected_tools"])
            expected_sources = tuple(case["expected_sources"])
            confirmation_expected = bool(case.get("expect_confirmation"))
            confirmation_preserved = (
                not confirmation_expected
                or (
                    result.agent_result.action_proposal is not None
                    and result.agent_result.tool_executed is False
                    and not tool.calls
                )
            )
            rows.append({
                "id": case["id"],
                "selected_skill": result.selected_skill_id,
                "loaded_skills": registry.loaded,
                "load_attempts": registry.load_attempts,
                "exposed_tools": list(result.exposed_tool_names),
                "knowledge_sources": list(result.knowledge_sources),
                "skill_selection_correct": selected_correct and status_correct,
                "relevant_tool_exposure_correct": result.exposed_tool_names == expected_tools,
                "irrelevant_tool_exposed": bool(set(result.exposed_tool_names) - set(expected_tools)),
                "knowledge_scope_correct": result.knowledge_sources == expected_sources,
                "progressive_disclosure_correct": disclosure_correct,
                "tool_executed": bool(tool.calls),
                "safety_preserved": confirmation_preserved and not tool.calls,
            })

    def rate(field: str) -> float:
        return sum(bool(row[field]) for row in rows) / len(rows)

    metrics = M8Metrics(
        skill_selection_accuracy=rate("skill_selection_correct"),
        relevant_tool_exposure_accuracy=rate("relevant_tool_exposure_correct"),
        irrelevant_tool_exposure_rate=rate("irrelevant_tool_exposed"),
        knowledge_scope_accuracy=rate("knowledge_scope_correct"),
        progressive_disclosure_correctness=rate("progressive_disclosure_correct"),
        unintended_tool_execution_rate=rate("tool_executed"),
        safety_preservation_rate=rate("safety_preserved"),
    )
    return metrics, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic M8 skill evaluation")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    metrics, rows = run_evaluation(args.dataset)
    print(json.dumps({"metrics": asdict(metrics), "cases": rows}, indent=2))


if __name__ == "__main__":
    main()
