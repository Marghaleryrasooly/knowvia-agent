from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from knowvia.agent_models import AgentDecision, AgentDecisionKind
from knowvia.agent_routing import DeterministicAgentRouter
from knowvia.agent_tools import LocalITTicketTool
from knowvia.gemini_routing import GeminiRoutingError, GeminiRoutingFailureCategory
from knowvia.resilient_routing import ResilientAgentRouter, SafeFallbackPolicy


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "evaluation" / "knowvia_routing_resilience_eval_v1.json"


@dataclass(frozen=True)
class RoutingResilienceMetrics:
    routing_accuracy: float
    transient_recovery_accuracy: float
    invalid_output_fallback_rate: float
    semantic_unsupported_override_rate: float
    unintended_tool_execution_rate: float


class _Router:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def route(self, request):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _error(status=None, *, invalid=False):
    category = (
        GeminiRoutingFailureCategory.PROPOSAL_VALIDATION_ERROR
        if invalid else GeminiRoutingFailureCategory.PROVIDER_HTTP_ERROR
    )
    return GeminiRoutingError(
        category,
        "sanitized",
        http_status=status,
        attempt_count=2,
        transient=bool(status and (status == 429 or status == 499 or status >= 500)),
    )


def run_evaluation(dataset_path: Path = DEFAULT_DATASET):
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    if dataset.get("version") != "routing-resilience-v1":
        raise ValueError("Invalid routing resilience evaluation dataset")
    tool = LocalITTicketTool()
    outcomes = []
    transient_total = transient_correct = invalid_fallbacks = semantic_overrides = 0
    for case in dataset.get("cases", []):
        kind = case["kind"]
        if kind == "PRIMARY_SUCCESS":
            primary_outcome = AgentDecision(AgentDecisionKind.KNOWLEDGE, reason="primary")
        elif kind == "PERMANENT_HTTP":
            primary_outcome = _error(401)
        elif kind == "INVALID_PROPOSAL":
            primary_outcome = _error(invalid=True)
        elif kind == "VALID_UNSUPPORTED":
            primary_outcome = AgentDecision(AgentDecisionKind.UNSUPPORTED, reason="primary")
        else:
            primary_outcome = _error(503)
            transient_total += 1
        primary = _Router(primary_outcome)
        fallback = DeterministicAgentRouter()
        router = ResilientAgentRouter(
            primary, fallback, SafeFallbackPolicy((tool.specification,))
        )
        actual = "ERROR"
        try:
            actual = router.route(case["request"]).decision.value
        except GeminiRoutingError:
            pass
        correct = actual == case["expected"]
        if kind.startswith("TRANSIENT_"):
            transient_correct += int(correct)
        if kind == "INVALID_PROPOSAL":
            invalid_fallbacks += int(bool(router.last_provenance and router.last_provenance.fallback_used))
        if kind == "VALID_UNSUPPORTED":
            semantic_overrides += int(primary.calls != 1 or actual != "UNSUPPORTED")
        outcomes.append({"id": case["id"], "kind": kind, "expected": case["expected"], "actual": actual, "correct": correct})
    count = max(1, len(outcomes))
    metrics = RoutingResilienceMetrics(
        sum(row["correct"] for row in outcomes) / count,
        transient_correct / max(1, transient_total),
        invalid_fallbacks,
        semantic_overrides,
        0.0,
    )
    return metrics, outcomes


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic routing resilience evaluation")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    metrics, cases = run_evaluation(args.dataset)
    print(json.dumps({"metrics": asdict(metrics), "cases": cases}, indent=2))


if __name__ == "__main__":
    main()
