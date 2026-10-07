from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from knowvia.embeddings import DEFAULT_LOCAL_MODEL, LocalEmbeddingProvider
from knowvia.evidence import LLMEvidenceEvaluator, evaluate_evidence_safely
from knowvia.generation import (
    DEFAULT_OLLAMA_HOST,
    DEFAULT_OLLAMA_MODEL,
    OllamaGenerationProvider,
)
from knowvia.models import EvidenceStatus, SourceCitation
from knowvia.retrieval import Retriever
from knowvia.vector_store import JsonVectorStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GROUND_TRUTH = PROJECT_ROOT / "knowvia_m1_dataset" / "m1_ground_truth.json"
DEFAULT_INDEX = PROJECT_ROOT / "data" / "knowvia_index.json"


@dataclass(frozen=True)
class EvaluationOutcome:
    question: str
    expected: EvidenceStatus
    actual: EvidenceStatus
    reason: str
    sources: list[SourceCitation]


@dataclass(frozen=True)
class RoutingMetrics:
    expected_answer_system_answers: int
    expected_abstention_system_abstains: int
    expected_abstention_system_answers: int
    expected_answer_system_abstains: int

    @property
    def total(self) -> int:
        return (
            self.expected_answer_system_answers
            + self.expected_abstention_system_abstains
            + self.expected_abstention_system_answers
            + self.expected_answer_system_abstains
        )

    @staticmethod
    def _rate(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 0.0

    @property
    def overall_accuracy(self) -> float:
        return self._rate(
            self.expected_answer_system_answers
            + self.expected_abstention_system_abstains,
            self.total,
        )

    @property
    def answer_routing_accuracy(self) -> float:
        """Expected-answer questions correctly classified as SUFFICIENT."""
        return self._rate(
            self.expected_answer_system_answers,
            self.expected_answer_system_answers
            + self.expected_answer_system_abstains,
        )

    @property
    def abstention_correctness(self) -> float:
        return self._rate(
            self.expected_abstention_system_abstains,
            self.expected_abstention_system_abstains
            + self.expected_abstention_system_answers,
        )

    @property
    def unsafe_answer_rate(self) -> float:
        return self._rate(
            self.expected_abstention_system_answers,
            self.expected_abstention_system_abstains
            + self.expected_abstention_system_answers,
        )

    @property
    def over_abstention_rate(self) -> float:
        return self._rate(
            self.expected_answer_system_abstains,
            self.expected_answer_system_answers
            + self.expected_answer_system_abstains,
        )


def expected_status(case: dict[str, object]) -> EvidenceStatus:
    behavior = case.get("expected_behavior")
    if behavior == "retrieve":
        return EvidenceStatus.SUFFICIENT
    if behavior == "insufficient_evidence":
        return EvidenceStatus.INSUFFICIENT
    raise ValueError(f"Unknown ground-truth behavior: {behavior}")


def calculate_metrics(outcomes: list[EvaluationOutcome]) -> RoutingMetrics:
    return RoutingMetrics(
        expected_answer_system_answers=sum(
            item.expected is EvidenceStatus.SUFFICIENT
            and item.actual is EvidenceStatus.SUFFICIENT
            for item in outcomes
        ),
        expected_abstention_system_abstains=sum(
            item.expected is EvidenceStatus.INSUFFICIENT
            and item.actual is EvidenceStatus.INSUFFICIENT
            for item in outcomes
        ),
        expected_abstention_system_answers=sum(
            item.expected is EvidenceStatus.INSUFFICIENT
            and item.actual is EvidenceStatus.SUFFICIENT
            for item in outcomes
        ),
        expected_answer_system_abstains=sum(
            item.expected is EvidenceStatus.SUFFICIENT
            and item.actual is EvidenceStatus.INSUFFICIENT
            for item in outcomes
        ),
    )


def run_live_evaluation(
    ground_truth_path: Path = DEFAULT_GROUND_TRUTH,
    index_path: Path = DEFAULT_INDEX,
    top_k: int = 3,
) -> list[EvaluationOutcome]:
    cases = json.loads(ground_truth_path.read_text(encoding="utf-8"))
    embedder = LocalEmbeddingProvider(
        os.getenv("LOCAL_EMBEDDING_MODEL", DEFAULT_LOCAL_MODEL)
    )
    retriever = Retriever(embedder, JsonVectorStore(index_path))
    judge_generator = OllamaGenerationProvider(
        model=os.getenv(
            "OLLAMA_EVIDENCE_MODEL",
            os.getenv("OLLAMA_GENERATION_MODEL", DEFAULT_OLLAMA_MODEL),
        ),
        host=os.getenv("OLLAMA_HOST", DEFAULT_OLLAMA_HOST),
        temperature=0.0,
        max_output_tokens=220,
    )
    evaluator = LLMEvidenceEvaluator(judge_generator)
    outcomes: list[EvaluationOutcome] = []
    for case in cases:
        question = str(case["question"])
        results = retriever.search(question, top_k)
        decision = evaluate_evidence_safely(evaluator, question, results)
        sources = [
            SourceCitation(result.chunk.source, result.chunk.section)
            for result in results
        ]
        outcomes.append(
            EvaluationOutcome(
                question=question,
                expected=expected_status(case),
                actual=decision.status,
                reason=decision.reason,
                sources=sources,
            )
        )
    return outcomes


def print_report(outcomes: list[EvaluationOutcome]) -> None:
    metrics = calculate_metrics(outcomes)
    print("M3 Evidence-Routing Evaluation")
    print("=" * 30)
    for number, outcome in enumerate(outcomes, start=1):
        marker = "PASS" if outcome.expected is outcome.actual else "FAIL"
        print(
            f"{number:02d}. [{marker}] expected={outcome.expected.value} "
            f"actual={outcome.actual.value}"
        )
        print(f"    Question: {outcome.question}")
        print(f"    Reason: {outcome.reason}")
        print(
            "    Sources: "
            + "; ".join(f"{s.source} -> {s.section}" for s in outcome.sources)
        )
    print("\nConfusion counts")
    print(f"Expected answer -> system answers: {metrics.expected_answer_system_answers}")
    print(
        "Expected abstention -> system abstains: "
        f"{metrics.expected_abstention_system_abstains}"
    )
    print(
        "Expected abstention -> system answers: "
        f"{metrics.expected_abstention_system_answers}"
    )
    print(
        "Expected answer -> system abstains: "
        f"{metrics.expected_answer_system_abstains}"
    )
    print("\nMetrics")
    print(f"Overall routing accuracy: {metrics.overall_accuracy:.1%}")
    print(f"Answer-routing accuracy: {metrics.answer_routing_accuracy:.1%}")
    print(f"Abstention correctness: {metrics.abstention_correctness:.1%}")
    print(f"Unsafe-answer rate: {metrics.unsafe_answer_rate:.1%}")
    print(f"Over-abstention rate: {metrics.over_abstention_rate:.1%}")


def main() -> None:
    load_dotenv()
    print_report(run_live_evaluation())


if __name__ == "__main__":
    main()
