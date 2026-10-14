"""Production scope regressions: procedural support is not personal eligibility."""
import json
import pytest
from knowvia.evidence import LLMEvidenceEvaluator, parse_evidence_decision, EvidenceEvaluationError, rejected_evidence_fields
from knowvia.answering import AnsweringService
from knowvia.models import Chunk, RetrievalResult, EvidenceStatus


def output(**changes):
    value = dict(requested_claim="Resolve the reported connection failure", directly_supported_facts=["If the connection fails, restart the client."],
                 supporting_evidence_ids=[1], missing_fact="", decision="SUFFICIENT", answer_scope="GENERAL",
                 policy_support="SUPPORTED", personal_eligibility="NOT_APPLICABLE", conditions=["If the connection fails"])
    value.update(changes)
    return json.dumps(value)


class Generator:
    model_name = "offline"
    def __init__(self, text): self.text, self.calls = text, 0
    def generate(self, *args): self.calls += 1; return self.text


class Retriever:
    def search(self, question, top_k=3):
        return [RetrievalResult(Chunk("procedure", "guide.md", "Connection guide", "Procedure", "If the connection fails, restart the client."), .9)]


@pytest.mark.parametrize("policy", ["SUPPORTED", "NOT_APPLICABLE"])
@pytest.mark.parametrize("eligibility", ["UNKNOWN", "NOT_APPLICABLE"])
def test_general_procedure_accepts_documented_conditions_not_eligibility(policy, eligibility):
    judge = Generator(output(policy_support=policy, personal_eligibility=eligibility))
    answer = Generator("If the connection fails, restart the client.")
    result = AnsweringService(Retriever(), answer, LLMEvidenceEvaluator(judge, require_scope=True)).answer("My VPN isn't working. How can I fix it?")
    assert result.evidence_decision.status is EvidenceStatus.SUFFICIENT
    assert result.sources[0].source == "guide.md"
    assert judge.calls == 1 and answer.calls == 1


@pytest.mark.parametrize("changes", [dict(personal_eligibility="VERIFIED"), dict(policy_support="UNSUPPORTED"),
    dict(supporting_evidence_ids=[2]), dict(directly_supported_facts=["Restart the client."])])
def test_general_still_rejects_unverified_or_inconsistent_evidence(changes):
    with pytest.raises(EvidenceEvaluationError): parse_evidence_decision(output(**changes), 1, require_scope=True)


def test_general_can_omit_irrelevant_policy_eligibility_fields():
    value = json.loads(output())
    for key in ['policy_support', 'personal_eligibility', 'conditions']: value.pop(key)
    decision = parse_evidence_decision(json.dumps(value), 1, require_scope=True)
    assert decision.status is EvidenceStatus.SUFFICIENT
    assert decision.personal_eligibility == 'NOT_APPLICABLE'
    value['policy_support'] = 'SUPPORTED'
    with pytest.raises(EvidenceEvaluationError): parse_evidence_decision(json.dumps(value), 1, require_scope=True)


@pytest.mark.parametrize('scope', ['POLICY_REPORTING', 'PERSONAL_ELIGIBILITY'])
def test_policy_scopes_cannot_omit_support_and_eligibility_fields(scope):
    value = json.loads(output(answer_scope=scope))
    for key in ['policy_support', 'personal_eligibility', 'conditions']: value.pop(key)
    with pytest.raises(EvidenceEvaluationError): parse_evidence_decision(json.dumps(value), 1, require_scope=True)


def test_invented_general_condition_fails_bounded_repair_and_never_generates():
    judge = Generator(output(conditions=["An invented condition"], directly_supported_facts=["An invented condition enables access."]))
    answer = Generator("must not be used")
    result = AnsweringService(Retriever(), answer, LLMEvidenceEvaluator(judge, require_scope=True)).answer("I cannot access my account")
    assert result.evidence_decision.status is EvidenceStatus.INSUFFICIENT
    assert not result.sources and answer.calls == 0 and judge.calls == 2


def test_general_answer_must_retain_validated_conditions():
    with pytest.raises(ValueError, match="omitted a validated condition"):
        AnsweringService(Retriever(), Generator("Restart the client."), LLMEvidenceEvaluator(Generator(output()), require_scope=True)).answer("Connection failure")


def test_rejected_diagnostics_are_shape_only_and_never_authority():
    raw = output(requested_claim="private content", conditions=["secret-content"], hidden_reasoning="must never appear", api_key="fake-private-key")
    fields = rejected_evidence_fields(raw)
    assert fields["validated"] is False and fields["conditions_count"] == 1
    assert fields["answer_scope"] == "GENERAL"
    assert not any(s in json.dumps(fields) for s in ["private", "secret", "reasoning", "api_key"])
    assert rejected_evidence_fields("not JSON") == {"json_object": False}
