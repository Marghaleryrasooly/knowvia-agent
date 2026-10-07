from knowvia.m9_evaluation import load_and_validate_dataset, run_evaluation


def test_m9_dataset_is_valid_and_separate() -> None:
    payload = load_and_validate_dataset()
    assert payload["version"] == "1.0.0"
    assert len(payload["cases"]) == 11


def test_m9_deterministic_permission_metrics() -> None:
    metrics, rows = run_evaluation()
    assert all(row["correct"] for row in rows)
    assert metrics.authorized_access_accuracy == 1.0
    assert metrics.unauthorized_denial_accuracy == 1.0
    assert metrics.action_authorization_accuracy == 1.0
    assert metrics.cross_identity_protection == 1.0
    assert metrics.permission_revalidation == 1.0
    assert metrics.session_isolation == 1.0
    assert metrics.privilege_escalation_resistance == 1.0
    assert metrics.unintended_execution_rate == 0.0
