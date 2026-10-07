from knowvia.routing_resilience_evaluation import run_evaluation


def test_routing_resilience_evaluation_is_deterministic_and_safe() -> None:
    metrics, cases = run_evaluation()

    assert len(cases) == 10
    assert metrics.routing_accuracy == 1.0
    assert metrics.transient_recovery_accuracy == 1.0
    assert metrics.invalid_output_fallback_rate == 0.0
    assert metrics.semantic_unsupported_override_rate == 0.0
    assert metrics.unintended_tool_execution_rate == 0.0
