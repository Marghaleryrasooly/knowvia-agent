from __future__ import annotations

from knowvia.answer_grading import AnswerGrade
from knowvia.eval_dataset import EvaluationCase, ExpectedSource
from knowvia.models import EvidenceStatus, SourceCitation
from knowvia.pipeline_metrics import CaseEvaluation, FailureStage, calculate_pipeline_metrics, classify_failures


PASS = AnswerGrade(True, True, True, (), ())
FAIL = AnswerGrade(False, False, True, ("missing",), ())


def answer_case(case_id: str, sources=(ExpectedSource("a.md", "A"),)) -> EvaluationCase:
    return EvaluationCase(case_id, "q", "ANSWER", sources, "answer", ({"type": "contains_all", "values": ["answer"]},), (), (), (), "")


def abstain_case(case_id: str) -> EvaluationCase:
    return EvaluationCase(case_id, "q", "ABSTAIN", (), None, (), (), (), (), "")


def outcome(case, status, sources, grade=None) -> CaseEvaluation:
    failures = classify_failures(case, status, sources, grade)
    return CaseEvaluation(case, status, "reason", sources, "answer" if status is EvidenceStatus.SUFFICIENT else None, grade, failures, failures[0] if failures else None)


def test_pipeline_metrics_cover_retrieval_routing_answers_and_citations() -> None:
    results = [
        outcome(answer_case("good"), EvidenceStatus.SUFFICIENT, (SourceCitation("a.md", "A"),), PASS),
        outcome(answer_case("bad-answer"), EvidenceStatus.SUFFICIENT, (SourceCitation("a.md", "A"), SourceCitation("x.md", "X")), FAIL),
        outcome(answer_case("over-abstain"), EvidenceStatus.INSUFFICIENT, (SourceCitation("x.md", "X"),), None),
        outcome(abstain_case("safe"), EvidenceStatus.INSUFFICIENT, (SourceCitation("x.md", "X"),), None),
        outcome(abstain_case("unsafe"), EvidenceStatus.SUFFICIENT, (SourceCitation("x.md", "X"),), None),
    ]
    metrics = calculate_pipeline_metrics(results)
    assert metrics.retrieval_recall_at_1 == 2 / 3
    assert metrics.retrieval_recall_at_3 == 2 / 3
    assert metrics.case_hit_at_1 == 2 / 3
    assert metrics.case_complete_at_3 == 2 / 3
    assert metrics.routing.overall_accuracy == 3 / 5
    assert metrics.conditional_answer_accuracy == 1 / 2
    assert metrics.end_to_end_answer_success == 1 / 3
    assert metrics.expected_source_recall == 1.0
    assert metrics.source_precision == 2 / 3
    assert metrics.complete_citation_accuracy == 1 / 2
    assert metrics.failure_counts[FailureStage.RETRIEVAL.value] == 1
    assert metrics.failure_counts[FailureStage.ROUTING.value] == 2
    assert metrics.failure_counts[FailureStage.ANSWER.value] == 1
    assert metrics.failure_counts[FailureStage.CITATION.value] == 1


def test_failure_precedence_is_retrieval_then_routing_then_answer_then_citation() -> None:
    item = answer_case("all")
    failures = classify_failures(item, EvidenceStatus.INSUFFICIENT, (SourceCitation("x", "X"),), None)
    assert failures == (FailureStage.RETRIEVAL, FailureStage.ROUTING)
