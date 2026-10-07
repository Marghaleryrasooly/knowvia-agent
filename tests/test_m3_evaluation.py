from knowvia.evaluation import EvaluationOutcome, calculate_metrics, expected_status
from knowvia.models import EvidenceStatus


def outcome(expected: EvidenceStatus, actual: EvidenceStatus) -> EvaluationOutcome:
    return EvaluationOutcome("question", expected, actual, "reason", [])


def test_ground_truth_behaviors_map_to_m3_decisions() -> None:
    assert expected_status({"expected_behavior": "retrieve"}) is EvidenceStatus.SUFFICIENT
    assert (
        expected_status({"expected_behavior": "insufficient_evidence"})
        is EvidenceStatus.INSUFFICIENT
    )


def test_metrics_distinguish_all_four_routing_outcomes() -> None:
    metrics = calculate_metrics(
        [
            outcome(EvidenceStatus.SUFFICIENT, EvidenceStatus.SUFFICIENT),
            outcome(EvidenceStatus.SUFFICIENT, EvidenceStatus.SUFFICIENT),
            outcome(EvidenceStatus.SUFFICIENT, EvidenceStatus.INSUFFICIENT),
            outcome(EvidenceStatus.INSUFFICIENT, EvidenceStatus.INSUFFICIENT),
            outcome(EvidenceStatus.INSUFFICIENT, EvidenceStatus.SUFFICIENT),
        ]
    )

    assert metrics.expected_answer_system_answers == 2
    assert metrics.expected_abstention_system_abstains == 1
    assert metrics.expected_abstention_system_answers == 1
    assert metrics.expected_answer_system_abstains == 1
    assert metrics.overall_accuracy == 3 / 5
    assert metrics.answer_routing_accuracy == 2 / 3
    assert metrics.abstention_correctness == 1 / 2
    assert metrics.unsafe_answer_rate == 1 / 2
    assert metrics.over_abstention_rate == 1 / 3
