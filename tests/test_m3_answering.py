from __future__ import annotations

from knowvia.answering import ABSTENTION_RESPONSE, AnsweringService
from knowvia.models import (
    Chunk,
    EvidenceDecision,
    EvidenceStatus,
    RetrievalResult,
)

RESULTS = [
    RetrievalResult(
        Chunk(
            "secrets",
            "06_engineering_practices.md",
            "NovaTech Engineering Practices",
            "Secrets",
            "API keys and passwords must never be committed to source control.",
        ),
        0.75,
    ),
    RetrievalResult(
        Chunk(
            "confidential",
            "07_information_classification_policy.md",
            "NovaTech Information Classification Policy",
            "Confidential",
            "Confidential information includes private source code.",
        ),
        0.7,
    ),
]


class FakeRetriever:
    def search(self, question: str, top_k: int = 3) -> list[RetrievalResult]:
        return RESULTS[:top_k]


class FakeEvaluator:
    evaluator_name = "fake"

    def __init__(self, status: EvidenceStatus) -> None:
        self.status = status

    def evaluate(self, question, results) -> EvidenceDecision:
        return EvidenceDecision(
            self.status,
            "Test routing decision.",
            "Whether ChatGPT is approved."
            if self.status is EvidenceStatus.INSUFFICIENT
            else "",
            () if self.status is EvidenceStatus.INSUFFICIENT else (1,),
        )


class RecordingGenerator:
    model_name = "fake-answer-model"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return "Generated grounded answer."


def test_sufficient_evidence_allows_normal_generation() -> None:
    generator = RecordingGenerator()
    service = AnsweringService(
        FakeRetriever(), generator, FakeEvaluator(EvidenceStatus.SUFFICIENT)
    )

    answer = service.answer("What does the policy say?")

    assert answer.answer == "Generated grounded answer."
    assert answer.evidence_decision.status is EvidenceStatus.SUFFICIENT
    assert len(generator.calls) == 1
    system_prompt, user_prompt = generator.calls[0]
    assert "evidence decision is authoritative" in system_prompt
    assert "Validated sufficient supporting company evidence" in user_prompt


def test_insufficient_evidence_blocks_normal_generation() -> None:
    generator = RecordingGenerator()
    service = AnsweringService(
        FakeRetriever(), generator, FakeEvaluator(EvidenceStatus.INSUFFICIENT)
    )

    answer = service.answer("Can I paste confidential source code into ChatGPT?")

    assert answer.answer == ABSTENTION_RESPONSE
    assert answer.evidence_decision.status is EvidenceStatus.INSUFFICIENT
    assert generator.calls == []
    assert not answer.answer.lstrip().lower().startswith(("yes", "no"))
    assert answer.sources == []
    assert answer.supporting_results == []
    assert answer.retrieval_results == RESULTS


def test_evaluator_exception_blocks_generation_and_abstains() -> None:
    class BrokenEvaluator:
        evaluator_name = "broken"

        def evaluate(self, *_):
            raise RuntimeError("invalid evaluator output")

    generator = RecordingGenerator()
    service = AnsweringService(FakeRetriever(), generator, BrokenEvaluator())

    answer = service.answer("Can I use an unmentioned service?")

    assert answer.answer == ABSTENTION_RESPONSE
    assert answer.evidence_decision.status is EvidenceStatus.INSUFFICIENT
    assert answer.evidence_decision.reason == "Evidence evaluation could not be validated."
    assert generator.calls == []
