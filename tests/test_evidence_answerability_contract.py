"""Offline contract/plumbing tests, not proof of a live model's semantic accuracy."""
import json

import pytest

from knowvia.answering import AnsweringService
from knowvia.evidence import (
    EVIDENCE_REPAIR_SYSTEM_PROMPT,
    EVIDENCE_SYSTEM_PROMPT,
    LLMEvidenceEvaluator,
    parse_evidence_decision,
)
from knowvia.knowledge_normalization import DeterministicKnowledgeRequestNormalizer
from knowvia.models import Chunk, EvidenceStatus, RetrievalResult


def evidence(section, text):
    return RetrievalResult(Chunk(section, section + ".md", "Company policy", section, text), 0.8)


class Retriever:
    def __init__(self, results):
        self.results = results

    def search(self, question, top_k=3):
        return self.results[:top_k]


class Generator:
    model_name = "offline"

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def generate(self, system, user):
        self.calls.append((system, user))
        return self.outputs.pop(0)


def decision(claim, facts, ids, missing=""):
    return json.dumps(dict(requested_claim=claim, directly_supported_facts=facts,
                           supporting_evidence_ids=ids, missing_fact=missing,
                           decision="INSUFFICIENT" if missing else "SUFFICIENT"))


@pytest.mark.parametrize("prompt", [EVIDENCE_SYSTEM_PROMPT, EVIDENCE_REPAIR_SYSTEM_PROMPT])
def test_initial_and_bounded_repair_share_answerability_contract(prompt):
    assert "keep the employee's original problem as the answerability target" in prompt
    assert "Usage restrictions, prerequisites, and neighboring procedures" in prompt
    assert "Do not assume an unstated cause or triggering condition" in prompt
    assert "Documented reporting or escalation requirements" in prompt
    assert "reporting a conditional policy from verifying personal eligibility" in prompt
    assert "with all its conditions" in prompt
    assert "Do not infer eligibility or ineligibility for an undocumented group" in prompt
    assert "Paired example: Evidence: Overnight-duty staff may claim meals." in prompt
    assert "SUFFICIENT to report that conditional rule, not personal eligibility" in prompt
    assert "INSUFFICIENT if overnight-duty status is unknown" in prompt


@pytest.mark.parametrize("problem", [
    "I cannot access my work account.",
    "My printer is not working.",
    "my vpn dose not work",
])
def test_normalization_retains_exact_problem_not_a_related_guidance_target(problem):
    normalized = DeterministicKnowledgeRequestNormalizer().normalize(problem)
    assert normalized.original_request == problem
    assert normalized.normalized_request == (
        "What documented company guidance directly addresses this reported problem: " + problem
    )


@pytest.mark.parametrize("question,results,missing", [
    ("I cannot access my work account.", [
        evidence("Account Access", "Employees must use their own assigned accounts."),
        evidence("VPN", "If the VPN connection fails, restart the VPN client."),
    ], "A documented response to the work-account access failure."),
    ("My printer is not working.", [
        evidence("Equipment", "Employees must use approved office equipment."),
        evidence("VPN", "If the VPN connection fails, restart the VPN client."),
    ], "A documented response to the printer failure."),
    ("Are contractors entitled to annual leave?", [
        evidence("Leave", "Full-time employees receive 24 paid vacation days per year."),
    ], "Contractor leave eligibility."),
    ("Am I personally eligible for the relocation allowance?", [
        evidence("Relocation", "Staff relocating over 100 miles receive an allowance."),
    ], "Whether the employee meets the relocation condition."),
])
def test_related_or_unestablished_eligibility_evidence_abstains_without_generation(
    question, results, missing
):
    judge = Generator(decision(question, [r.chunk.text for r in results], [], missing))
    answer = Generator()  # Any answer-generation call fails the test.
    normalized = DeterministicKnowledgeRequestNormalizer().normalize(question)
    response = AnsweringService(Retriever(results), answer, LLMEvidenceEvaluator(judge)).answer(
        normalized.normalized_request
    )
    assert response.evidence_decision.status is EvidenceStatus.INSUFFICIENT
    assert response.supporting_results == response.sources == []
    assert answer.calls == []
    assert len(judge.calls) == 1  # Valid insufficiency is not repaired into sufficiency.
    assert question in judge.calls[0][1]


@pytest.mark.parametrize("question,section,fact,answer", [
    ("my vpn dose not work", "VPN",
     "If the VPN connection fails, confirm internet connectivity and restart the VPN client.",
     "Check your internet connection and restart the VPN client."),
    ("What should I do if I lose my company laptop?", "Lost Devices",
     "Lost or stolen company devices must be reported to the IT Service Desk immediately.",
     "Report the lost device to the IT Service Desk immediately."),
    ("How many vacation days do I get?", "Annual Leave",
     "Full-time employees receive 24 paid vacation days per calendar year.",
     "Full-time employees receive 24 paid vacation days per calendar year."),
    ("What relocation allowance is available?", "Relocation",
     "Staff relocating over 100 miles receive a relocation allowance.",
     "Staff relocating over 100 miles receive a relocation allowance."),
])
def test_direct_answers_preserve_conditions_and_only_cite_selected_evidence(
    question, section, fact, answer
):
    results = [evidence("Distractor", "Office visitors must sign in."), evidence(section, fact)]
    judge = Generator(decision(question, [fact], [2]))
    generator = Generator(answer)
    normalized = DeterministicKnowledgeRequestNormalizer().normalize(question)
    response = AnsweringService(Retriever(results), generator, LLMEvidenceEvaluator(judge)).answer(
        normalized.normalized_request
    )
    assert response.evidence_decision.status is EvidenceStatus.SUFFICIENT
    assert response.evidence_decision.supporting_evidence_ids == (2,)
    assert response.answer == answer
    assert response.supporting_results == [results[1]]
    assert [source.section for source in response.sources] == [section]
    assert fact in generator.calls[0][1]
    assert "Office visitors must sign in." not in generator.calls[0][1]


def test_repair_preserves_relevance_rules_and_stops_before_answer_on_insufficiency():
    question = "My scanner is not working."
    results = [evidence("Equipment", "Only approved equipment may be used.")]
    judge = Generator("invalid JSON", decision(question, [], [], "Scanner troubleshooting."))
    answer = Generator()
    response = AnsweringService(Retriever(results), answer, LLMEvidenceEvaluator(judge)).answer(question)
    assert len(judge.calls) == 2
    assert judge.calls[1][0] == EVIDENCE_REPAIR_SYSTEM_PROMPT
    assert response.evidence_decision.status is EvidenceStatus.INSUFFICIENT
    assert answer.calls == []


def test_structurally_valid_related_selection_is_not_independent_entailment_proof():
    # This documents the remaining boundary honestly: syntax/ID validation cannot
    # independently reject an LLM's semantically wrong but consistent judgment.
    output = decision("Resolve a scanner failure.", ["Approved equipment is required."], [1])
    assert parse_evidence_decision(output, 1).status is EvidenceStatus.SUFFICIENT
    assert "Never turn a valid-looking selection of related sources into proof of answerability" in EVIDENCE_SYSTEM_PROMPT
