from pathlib import Path

from knowvia.eval_dataset import load_evaluation_dataset
from knowvia.knowledge_reliability_evaluation import calculate_metrics


ROOT = Path(__file__).resolve().parents[1]


def test_paraphrase_dataset_is_isolated_valid_and_cross_domain() -> None:
    dataset = load_evaluation_dataset(
        ROOT / "evaluation" / "knowvia_knowledge_paraphrase_eval_v1.json",
        ROOT / "knowvia_m1_dataset",
    )

    assert len(dataset.cases) == 40
    tags = {tag for case in dataset.cases for tag in case.tags}
    assert {
        "annual_leave", "sick_leave", "remote_work", "working_hours",
        "vpn", "password", "expenses", "general_company", "insufficient",
    } <= tags
    assert sum(case.expected_behavior == "ABSTAIN" for case in dataset.cases) == 7


def test_reliability_metrics_separate_safety_support_and_answer_quality() -> None:
    cases = [
        {
            "expected_behavior": "ANSWER", "actual_status": "SUFFICIENT",
            "retrieval_hit": True, "supporting_precision": 1.0,
            "complete_support": True, "answer_grade": {"passed": True},
            "evidence_diagnostics": {"validation_error_message": None},
        },
        {
            "expected_behavior": "ANSWER", "actual_status": "INSUFFICIENT",
            "retrieval_hit": True, "supporting_precision": 0.0,
            "complete_support": False, "answer_grade": None,
            "evidence_diagnostics": {"validation_error_message": "invalid"},
        },
        {
            "expected_behavior": "ABSTAIN", "actual_status": "INSUFFICIENT",
            "retrieval_hit": None, "supporting_precision": 1.0,
            "complete_support": True, "answer_grade": None,
            "evidence_diagnostics": {"validation_error_message": None},
        },
    ]

    metrics = calculate_metrics(cases)

    assert metrics["answerable_paraphrase_consistency"] == 0.5
    assert metrics["abstention_precision"] == 1.0
    assert metrics["unsafe_answer_rate"] == 0.0
    assert metrics["supporting_source_precision"] == 1.0
    assert metrics["validation_failure_count"] == 1
