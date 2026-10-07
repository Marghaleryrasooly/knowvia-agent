from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

from knowvia.answer_grading import AnswerGrade
from knowvia.eval_dataset import EvaluationCase, ExpectedSource
from knowvia.evidence import EvidenceEvaluationDiagnostics
from knowvia.evaluation import EvaluationOutcome, RoutingMetrics, calculate_metrics
from knowvia.models import EvidenceStatus, SourceCitation


class FailureStage(str, Enum):
    RETRIEVAL = "RETRIEVAL_FAILURE"
    ROUTING = "ROUTING_FAILURE"
    ANSWER = "ANSWER_FAILURE"
    CITATION = "CITATION_FAILURE"


@dataclass(frozen=True)
class CaseEvaluation:
    case: EvaluationCase
    actual_status: EvidenceStatus
    reason: str
    retrieved_sources: tuple[SourceCitation, ...]
    answer: str | None
    answer_grade: AnswerGrade | None
    failures: tuple[FailureStage, ...]
    primary_failure: FailureStage | None
    evidence_diagnostics: EvidenceEvaluationDiagnostics | None = None


@dataclass(frozen=True)
class EvaluationMetrics:
    retrieval_recall_at_1: float
    retrieval_recall_at_3: float
    case_hit_at_1: float
    case_complete_at_3: float
    routing: RoutingMetrics
    conditional_answer_accuracy: float
    end_to_end_answer_success: float
    expected_source_recall: float
    source_precision: float
    complete_citation_accuracy: float
    failure_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["routing"] = asdict(self.routing)
        value["routing"].update(
            overall_accuracy=self.routing.overall_accuracy,
            answer_routing_accuracy=self.routing.answer_routing_accuracy,
            abstention_correctness=self.routing.abstention_correctness,
            unsafe_answer_rate=self.routing.unsafe_answer_rate,
            over_abstention_rate=self.routing.over_abstention_rate,
        )
        return value


def source_key(source: SourceCitation | ExpectedSource) -> tuple[str, str]:
    return source.source, source.section


def classify_failures(
    case: EvaluationCase,
    actual_status: EvidenceStatus,
    retrieved_sources: tuple[SourceCitation, ...],
    answer_grade: AnswerGrade | None,
) -> tuple[FailureStage, ...]:
    failures: list[FailureStage] = []
    expected = {source_key(source) for source in case.expected_sources}
    retrieved3 = {source_key(source) for source in retrieved_sources[:3]}
    expected_status = EvidenceStatus.SUFFICIENT if case.expected_behavior == "ANSWER" else EvidenceStatus.INSUFFICIENT
    if expected and not expected.issubset(retrieved3):
        failures.append(FailureStage.RETRIEVAL)
    if actual_status is not expected_status:
        failures.append(FailureStage.ROUTING)
    if case.expected_behavior == "ANSWER" and actual_status is EvidenceStatus.SUFFICIENT:
        if answer_grade is None or not answer_grade.passed:
            failures.append(FailureStage.ANSWER)
        if retrieved3 != expected:
            failures.append(FailureStage.CITATION)
    return tuple(failures)


def calculate_pipeline_metrics(results: list[CaseEvaluation]) -> EvaluationMetrics:
    answer_cases = [result for result in results if result.case.expected_behavior == "ANSWER"]
    expected_pairs = sum(len(result.case.expected_sources) for result in answer_cases)

    def recall_at(k: int) -> float:
        hits = 0
        for result in answer_cases:
            expected = {source_key(source) for source in result.case.expected_sources}
            actual = {source_key(source) for source in result.retrieved_sources[:k]}
            hits += len(expected & actual)
        return hits / expected_pairs if expected_pairs else 0.0

    hit1 = sum(
        bool({source_key(s) for s in result.case.expected_sources} & {source_key(s) for s in result.retrieved_sources[:1]})
        for result in answer_cases
    )
    complete3 = sum(
        {source_key(s) for s in result.case.expected_sources}.issubset({source_key(s) for s in result.retrieved_sources[:3]})
        for result in answer_cases
    )
    routing = calculate_metrics([
        EvaluationOutcome(
            result.case.question,
            EvidenceStatus.SUFFICIENT if result.case.expected_behavior == "ANSWER" else EvidenceStatus.INSUFFICIENT,
            result.actual_status,
            result.reason,
            list(result.retrieved_sources),
        )
        for result in results
    ])
    answered = [result for result in answer_cases if result.actual_status is EvidenceStatus.SUFFICIENT]
    good_answers = sum(result.answer_grade is not None and result.answer_grade.passed for result in answered)

    citation_expected = 0
    citation_retrieved = 0
    citation_hits = 0
    exact_citations = 0
    for result in answered:
        expected = {source_key(source) for source in result.case.expected_sources}
        actual = {source_key(source) for source in result.retrieved_sources[:3]}
        citation_expected += len(expected)
        citation_retrieved += len(actual)
        citation_hits += len(expected & actual)
        exact_citations += actual == expected

    return EvaluationMetrics(
        retrieval_recall_at_1=recall_at(1),
        retrieval_recall_at_3=recall_at(3),
        case_hit_at_1=hit1 / len(answer_cases) if answer_cases else 0.0,
        case_complete_at_3=complete3 / len(answer_cases) if answer_cases else 0.0,
        routing=routing,
        conditional_answer_accuracy=good_answers / len(answered) if answered else 0.0,
        end_to_end_answer_success=good_answers / len(answer_cases) if answer_cases else 0.0,
        expected_source_recall=citation_hits / citation_expected if citation_expected else 0.0,
        source_precision=citation_hits / citation_retrieved if citation_retrieved else 0.0,
        complete_citation_accuracy=exact_citations / len(answered) if answered else 0.0,
        failure_counts={stage.value: sum(stage in result.failures for result in results) for stage in FailureStage},
    )
