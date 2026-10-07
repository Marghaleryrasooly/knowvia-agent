from __future__ import annotations

from knowvia.answering import (
    GROUNDING_SYSTEM_PROMPT,
    AnsweringService,
    build_grounded_user_prompt,
    citations_from_results,
)
from knowvia.models import (
    Chunk,
    EvidenceDecision,
    EvidenceStatus,
    RetrievalResult,
)


def result(
    chunk_id: str,
    source: str,
    title: str,
    section: str,
    text: str,
    score: float,
) -> RetrievalResult:
    return RetrievalResult(
        chunk=Chunk(chunk_id, source, title, section, text), score=score
    )


RESULTS = [
    result(
        "leave",
        "01_employee_handbook.md",
        "NovaTech Employee Handbook",
        "Annual Leave",
        "Full-time employees receive 24 paid vacation days per calendar year.",
        0.91,
    ),
    result(
        "hours",
        "01_employee_handbook.md",
        "NovaTech Employee Handbook",
        "Working Hours",
        "Standard working hours are Monday through Friday.",
        0.55,
    ),
]


class FakeRetriever:
    def __init__(self, results: list[RetrievalResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, int]] = []

    def search(self, question: str, top_k: int = 3) -> list[RetrievalResult]:
        self.calls.append((question, top_k))
        return self.results[:top_k]


class FakeGenerationProvider:
    model_name = "fake-generator"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.answer


class SufficientEvidenceEvaluator:
    evaluator_name = "fake-sufficient"

    def evaluate(self, question, results) -> EvidenceDecision:
        return EvidenceDecision(
            EvidenceStatus.SUFFICIENT,
            "The evidence directly answers the question.",
            "",
            (1,),
        )


def test_grounded_prompt_contains_question_and_ranked_chunk_metadata() -> None:
    prompt = build_grounded_user_prompt("How much leave do I get?", RESULTS)

    assert "Employee question:\nHow much leave do I get?" in prompt
    assert prompt.index("[Context 1]") < prompt.index("[Context 2]")
    assert "Source: 01_employee_handbook.md" in prompt
    assert "Document: NovaTech Employee Handbook" in prompt
    assert "Section: Annual Leave" in prompt
    assert "24 paid vacation days" in prompt
    assert "<<<BEGIN COMPANY EVIDENCE>>>" in prompt
    assert "<<<END COMPANY EVIDENCE>>>" in prompt


def test_system_prompt_makes_validated_sufficiency_authoritative() -> None:
    assert "Use only the supplied company context" in GROUNDING_SYSTEM_PROMPT
    assert "Do not invent company policies" in GROUNDING_SYSTEM_PROMPT
    assert "already validated" in GROUNDING_SYSTEM_PROMPT
    assert "evidence decision is authoritative" in GROUNDING_SYSTEM_PROMPT
    assert "Do not reassess evidence sufficiency" in GROUNDING_SYSTEM_PROMPT
    assert "claim that company information is insufficient" in GROUNDING_SYSTEM_PROMPT
    assert "Do not add a Sources section" in GROUNDING_SYSTEM_PROMPT


def test_citations_are_programmatic_ordered_and_deduplicated() -> None:
    duplicate = result(
        "leave-duplicate",
        "01_employee_handbook.md",
        "NovaTech Employee Handbook",
        "Annual Leave",
        "Repeated evidence.",
        0.4,
    )

    citations = citations_from_results([RESULTS[0], duplicate, RESULTS[1]])

    assert [(item.source, item.section) for item in citations] == [
        ("01_employee_handbook.md", "Annual Leave"),
        ("01_employee_handbook.md", "Working Hours"),
    ]


def test_answering_service_connects_retrieval_generation_and_sources() -> None:
    retriever = FakeRetriever(RESULTS)
    generator = FakeGenerationProvider("Employees receive 24 paid vacation days.")
    service = AnsweringService(retriever, generator, SufficientEvidenceEvaluator())

    answer = service.answer("How many vacation days do I get?", top_k=2)

    assert retriever.calls == [("How many vacation days do I get?", 2)]
    assert len(generator.calls) == 1
    assert generator.calls[0][0] == GROUNDING_SYSTEM_PROMPT
    assert "24 paid vacation days" in generator.calls[0][1]
    assert "Validated sufficient supporting company evidence" in generator.calls[0][1]
    assert answer.answer == "Employees receive 24 paid vacation days."
    assert answer.evidence_decision.status is EvidenceStatus.SUFFICIENT
    assert answer.retrieval_results == RESULTS
    assert answer.supporting_results == [RESULTS[0]]
    assert "Working Hours" not in generator.calls[0][1]
    assert [(source.source, source.section) for source in answer.sources] == [
        ("01_employee_handbook.md", "Annual Leave"),
    ]


def test_two_required_supporting_chunks_are_the_only_generation_context() -> None:
    third = result(
        "approval",
        "02_travel_policy.md",
        "Travel Policy",
        "Approval",
        "Trips costing more than USD 2,000 require director approval.",
        0.52,
    )
    candidates = [RESULTS[0], RESULTS[1], third]

    class TwoChunkEvaluator:
        evaluator_name = "fake-two-supports"

        def evaluate(self, question, results):
            return EvidenceDecision(
                EvidenceStatus.SUFFICIENT,
                "Two policy facts are required.",
                "",
                (1, 3),
            )

    generator = FakeGenerationProvider("The combined policy answer.")
    answer = AnsweringService(
        FakeRetriever(candidates), generator, TwoChunkEvaluator()
    ).answer("What entitlement applies and when is approval required?")

    prompt = generator.calls[0][1]
    assert "24 paid vacation days" in prompt
    assert "director approval" in prompt
    assert "Working Hours" not in prompt
    assert answer.retrieval_results == candidates
    assert answer.supporting_results == [candidates[0], candidates[2]]
    assert [(source.source, source.section) for source in answer.sources] == [
        ("01_employee_handbook.md", "Annual Leave"),
        ("02_travel_policy.md", "Approval"),
    ]
