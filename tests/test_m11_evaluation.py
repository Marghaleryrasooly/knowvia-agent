from knowvia.m11_evaluation import run_evaluation


def test_m11_evaluation_is_complete_and_safe() -> None:
    metrics, cases = run_evaluation()

    assert len(cases) == 11
    assert all(case["correct"] for case in cases)
    values = vars(metrics)
    assert values["unintended_execution_rate"] == 0.0
    assert all(value == 1.0 for name, value in values.items() if name != "unintended_execution_rate")
