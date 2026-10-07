from knowvia.m10_evaluation import load_and_validate_dataset, run_evaluation


def test_m10_dataset_is_valid_and_separate() -> None:
    payload = load_and_validate_dataset()
    assert payload["version"] == "1.0.0"
    assert len(payload["cases"]) == 9


def test_m10_deterministic_lifecycle_metrics() -> None:
    metrics, rows = run_evaluation()
    assert all(row["correct"] for row in rows)
    assert metrics.ownership_binding_accuracy == 1.0
    assert metrics.owner_status_lookup_accuracy == 1.0
    assert metrics.cross_identity_protection == 1.0
    assert metrics.unknown_ticket_safety == 1.0
    assert metrics.lifecycle_transition_accuracy == 1.0
    assert metrics.notification_accuracy == 1.0
    assert metrics.duplicate_notification_rate == 0.0
    assert metrics.model_override_resistance == 1.0
    assert metrics.unauthorized_create_safety == 1.0
    assert metrics.unintended_execution_rate == 0.0
