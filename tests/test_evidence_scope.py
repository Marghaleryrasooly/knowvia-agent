import json
import pytest
from knowvia.evidence import parse_evidence_decision, EvidenceEvaluationError, LLMEvidenceEvaluator
from knowvia.models import EvidenceAnswerScope, EvidenceStatus, Chunk, RetrievalResult
from knowvia.answering import AnsweringService


def record(**changes):
    value = dict(requested_claim="Documented meal allowance", directly_supported_facts=["Overnight-duty staff may claim meals."],
                 supporting_evidence_ids=[1], missing_fact="", decision="SUFFICIENT",
                 answer_scope="POLICY_REPORTING", policy_support="SUPPORTED", personal_eligibility="UNKNOWN", conditions=["Overnight-duty staff"])
    value.update(changes)
    return json.dumps(value)


def test_conditional_policy_is_supported_without_personal_eligibility():
    result = parse_evidence_decision(record(), 1)
    assert result.status is EvidenceStatus.SUFFICIENT
    assert result.answer_scope is EvidenceAnswerScope.POLICY_REPORTING
    assert result.personal_eligibility == "UNKNOWN"


def test_evaluation_diagnostics_record_scope_without_hidden_reasoning():
    from evaluation.groq_safeguards import visible_fields
    status, fields = visible_fields(record(), 1)
    assert status == "SUFFICIENT"
    assert fields["answer_scope"] == "POLICY_REPORTING"
    assert fields["personal_eligibility"] == "UNKNOWN"
    assert "reasoning" not in fields


@pytest.mark.parametrize("changes", [
    {"answer_scope": "PERSONAL_ELIGIBILITY"},
    {"personal_eligibility": "VERIFIED"},
    {"policy_support": "UNSUPPORTED"},
    {"supporting_evidence_ids": [2]},
    {"directly_supported_facts": ["Employees may claim meals."]},
    {"conditions": [""]}, {"answer_scope": "unknown"},
    {"decision": "INSUFFICIENT", "missing_fact": "Unknown eligibility"},
])
def test_scope_contract_rejects_unsafe_or_inconsistent_records(changes):
    with pytest.raises(EvidenceEvaluationError): parse_evidence_decision(record(**changes), 1)


def test_insufficiency_is_never_upgraded_and_unknown_eligibility_abstains():
    output = record(answer_scope="PERSONAL_ELIGIBILITY", decision="INSUFFICIENT", missing_fact="Unknown duty assignment", supporting_evidence_ids=[])
    assert parse_evidence_decision(output, 1).status is EvidenceStatus.INSUFFICIENT


def test_scope_reaches_generation_and_sources_remain_validated():
    class Generator:
        model_name = "mock"
        def __init__(self, output): self.output, self.calls = output, []
        def generate(self, system, user): self.calls.append(user); return self.output
    results = [RetrievalResult(Chunk("meals", "policy.md", "Policy", "Meals", "Overnight-duty staff may claim meals."), .9)]
    class Retriever:
        def search(self, question, top_k=3): return results
    judge, answer = Generator(record()), Generator("Overnight-duty staff may claim meals.")
    result = AnsweringService(Retriever(), answer, LLMEvidenceEvaluator(judge)).answer("What meals can I claim?")
    assert "Answer scope: POLICY_REPORTING" in answer.calls[0]
    assert "Personal eligibility: UNKNOWN" in answer.calls[0]
    assert result.supporting_results == results
    assert result.sources[0].source == "policy.md"


def test_policy_generation_cannot_drop_validated_conditions():
    class Generator:
        model_name = "mock"
        def __init__(self, output): self.output = output
        def generate(self, system, user): return self.output
    class Retriever:
        def search(self, question, top_k=3):
            return [RetrievalResult(Chunk("meals", "policy.md", "Policy", "Meals", "Overnight-duty staff may claim meals."), .9)]
    service = AnsweringService(Retriever(), Generator("You may claim meals."), LLMEvidenceEvaluator(Generator(record()), require_scope=True))
    with pytest.raises(ValueError, match="omitted a validated condition"):
        service.answer("What meals can I claim?")


def test_legacy_reference_record_is_general_not_verified():
    value = json.loads(record())
    for key in ["answer_scope", "policy_support", "personal_eligibility", "conditions"]: value.pop(key)
    result = parse_evidence_decision(json.dumps(value), 1)
    assert result.answer_scope is EvidenceAnswerScope.GENERAL
    assert result.personal_eligibility == "UNKNOWN"


def test_production_mode_requires_scope_even_for_legacy_valid_output():
    value = json.loads(record())
    for key in ["answer_scope", "policy_support", "personal_eligibility", "conditions"]: value.pop(key)
    with pytest.raises(EvidenceEvaluationError): parse_evidence_decision(json.dumps(value), 1, require_scope=True)


def test_invalid_scoped_initial_and_repair_never_generate_or_recover():
    class Generator:
        model_name = "mock"
        def __init__(self): self.calls = 0
        def generate(self, system, user): self.calls += 1; return record(conditions=["An invented condition"], directly_supported_facts=["An invented condition enables meals."])
    results = [RetrievalResult(Chunk("meals", "policy.md", "Policy", "Meals", "Overnight-duty staff may claim meals."), .9)]
    class Retriever:
        def search(self, question, top_k=3): return results
    class NoAnswer:
        def generate(self, *args): pytest.fail("Invalid evidence reached generation")
    generator = Generator()
    result = AnsweringService(Retriever(), NoAnswer(), LLMEvidenceEvaluator(generator, require_scope=True)).answer("Can I claim meals?")
    assert result.evidence_decision.status is EvidenceStatus.INSUFFICIENT
    assert result.sources == []
    assert generator.calls == 2
