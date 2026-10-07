from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowvia.eval_dataset import EvaluationDataset
from knowvia.m4_evaluation import compare_results, save_result


def payload(failure=None, accuracy=0.5):
    metrics = {
        name: accuracy for name in [
            "retrieval_recall_at_1", "retrieval_recall_at_3", "case_hit_at_1", "case_complete_at_3",
            "conditional_answer_accuracy", "end_to_end_answer_success", "expected_source_recall",
            "source_precision", "complete_citation_accuracy",
        ]
    }
    metrics["routing"] = {name: accuracy for name in [
        "overall_accuracy", "answer_routing_accuracy", "abstention_correctness", "unsafe_answer_rate", "over_abstention_rate"
    ]}
    return {
        "schema_version": 1,
        "run": {"dataset_name": "m4", "dataset_version": "1", "case_ids": ["a"]},
        "metrics": metrics,
        "cases": [{"id": "a", "failures": [failure] if failure else [], "primary_failure": failure}],
    }


def test_result_serialization_and_comparison(tmp_path: Path) -> None:
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    save_result(payload("ROUTING_FAILURE", 0.5), before)
    save_result(payload(None, 0.75), after)
    comparison = compare_results(before, after)
    assert comparison["metric_deltas"]["overall_accuracy"] == 0.25
    assert comparison["new_passes"] == ["a"]
    assert comparison["regressions"] == []
    assert comparison["failure_stage_changes"]["a"]["before"] == "ROUTING_FAILURE"


def test_comparison_rejects_different_case_ids(tmp_path: Path) -> None:
    left, right = payload(), payload()
    right["run"]["case_ids"] = ["different"]
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(left), encoding="utf-8")
    b.write_text(json.dumps(right), encoding="utf-8")
    with pytest.raises(ValueError, match="different datasets"):
        compare_results(a, b)
