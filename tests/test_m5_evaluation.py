import json

import pytest

from knowvia.m5_evaluation import DEFAULT_DATASET, load_and_validate_dataset, run_evaluation


def test_m5_dataset_is_valid_and_diverse() -> None:
    dataset = load_and_validate_dataset()
    decisions = {case["expected_decision"] for case in dataset["cases"]}
    assert len(dataset["cases"]) == 24
    assert decisions == {"KNOWLEDGE", "ACTION", "CLARIFY", "UNSUPPORTED"}


def test_m5_deterministic_evaluation_metrics() -> None:
    metrics, rows = run_evaluation()
    assert len(rows) == 24
    assert metrics.routing_accuracy == 1.0
    assert metrics.tool_selection_accuracy == 1.0
    assert metrics.argument_accuracy == 1.0
    assert metrics.clarification_accuracy == 1.0
    assert metrics.unsupported_action_accuracy == 1.0
    assert metrics.tool_execution_accuracy == 1.0
    assert metrics.unintended_tool_execution_rate == 0.0
    assert metrics.knowledge_to_tool_safety_rate == 1.0


def test_m5_dataset_validator_rejects_invalid_action(tmp_path) -> None:
    payload = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
    payload["cases"][7].pop("expected_tool")
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="requires tool"):
        load_and_validate_dataset(path)
