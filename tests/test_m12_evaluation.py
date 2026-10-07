from knowvia.m12_evaluation import run_evaluation


def test_m12_evaluation_is_complete_and_safe() -> None:
    metrics, cases = run_evaluation()
    assert len(cases) == 12
    assert all(case["correct"] for case in cases)
    assert metrics.unintended_execution_rate == 0.0
    assert all(
        value == 1.0
        for name, value in vars(metrics).items()
        if name != "unintended_execution_rate"
    )
