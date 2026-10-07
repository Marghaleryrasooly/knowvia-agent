from knowvia.m8_7_evaluation import load_and_validate_dataset, run_evaluation


def test_m8_7_dataset_is_valid() -> None:
    payload = load_and_validate_dataset()
    assert payload["version"] == "1.0.0"
    assert len(payload["cases"]) == 10


def test_m8_7_metrics_preserve_normalization_and_action_safety() -> None:
    metrics, rows = run_evaluation()
    assert len(rows) == 10
    assert metrics.normalization_accuracy == 1.0
    assert metrics.unnecessary_normalization_rate == 0.0
    assert metrics.action_intent_preservation_accuracy == 1.0
    assert metrics.invented_detail_rate == 0.0
    assert metrics.tool_execution_safety_rate == 1.0
    assert metrics.source_scope_preservation_accuracy == 1.0
    assert all(row["decision_correct"] for row in rows)
