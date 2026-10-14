import json
import pytest
from knowvia.evidence_scope_intent import question_scope_constraint
from knowvia.evidence import LLMEvidenceEvaluator, EvidenceEvaluationError
from knowvia.models import Chunk, RetrievalResult, EvidenceAnswerScope, EvidenceStatus


@pytest.mark.parametrize('question', [
    'How many vacation days do I get?', 'How much equipment allowance can we receive?',
    'How many training hours do I receive?', 'What does the company policy say about travel?',
    'Explain the expense policy.', 'What does the policy say about eligibility?', 'Who is eligible for training?',
])
def test_general_policy_reporting_is_question_intent_not_personal_qualification(question):
    assert question_scope_constraint(question) is EvidenceAnswerScope.POLICY_REPORTING


@pytest.mark.parametrize('question', [
    'Do I qualify for this allowance?', 'Am I eligible?', 'Are temporary staff entitled to this benefit?',
    'How many training hours do I get as a visiting researcher?',
    'Given my circumstances, what allowance can I claim?', 'Do I get this benefit?',
    'How much allowance do I receive based on my contract?',
    'How many hours do I get and am I eligible for more?',
    'How many hours do I have left?', 'Am I covered by this plan?',
])
def test_verification_and_personal_qualifiers_take_conservative_precedence(question):
    assert question_scope_constraint(question) is EvidenceAnswerScope.PERSONAL_ELIGIBILITY


@pytest.mark.parametrize('question', ['What should I do if equipment breaks?', 'How do I recover access?', 'How can I fix this failure?'])
def test_procedures_are_general_and_do_not_gain_evidence_support(question):
    assert question_scope_constraint(question) is EvidenceAnswerScope.GENERAL


@pytest.mark.parametrize('question', ['What about that?', 'Can I use it?', 'Tell me more.', 'How long will repairs take?', 'Am I allowed to work remotely?', 'Do I get to request a review?', 'How many hours do I have?'])
def test_unclassified_ambiguous_questions_do_not_gain_policy_reporting(question):
    assert question_scope_constraint(question) is None


def record(scope='POLICY_REPORTING', sufficient=True):
    return json.dumps(dict(requested_claim='Documented conditional training allowance',
        directly_supported_facts=['Night-duty staff receive six training hours.'] if sufficient else [],
        supporting_evidence_ids=[1] if sufficient else [], missing_fact='' if sufficient else 'No documented answer',
        decision='SUFFICIENT' if sufficient else 'INSUFFICIENT', answer_scope=scope,
        policy_support='SUPPORTED' if sufficient else 'UNSUPPORTED', personal_eligibility='UNKNOWN',
        conditions=['Night-duty staff'] if sufficient else []))


RESULTS = [RetrievalResult(Chunk('training', 'training.md', 'Policy', 'Training', 'Night-duty staff receive six training hours.'), .9)]


class Generator:
    model_name = 'offline'
    def __init__(self, outputs): self.outputs, self.calls = iter(outputs), []
    def generate(self, system, user): self.calls.append(user); return next(self.outputs)


def test_scope_conflict_uses_existing_single_repair_not_automatic_sufficient_override():
    generator = Generator([record('PERSONAL_ELIGIBILITY', False), record()])
    evaluator = LLMEvidenceEvaluator(generator, require_scope=True)
    result = evaluator.evaluate('How many training hours do I get?', RESULTS)
    assert result.status is EvidenceStatus.SUFFICIENT
    assert len(generator.calls) == 2
    assert all('answer_scope must be POLICY_REPORTING' in p for p in generator.calls)


def test_consistent_insufficient_policy_decision_is_never_promoted():
    generator = Generator([record(sufficient=False)])
    result = LLMEvidenceEvaluator(generator, require_scope=True).evaluate('How many training hours do I get?', RESULTS)
    assert result.status is EvidenceStatus.INSUFFICIENT and len(generator.calls) == 1


def test_conflicting_initial_and_repair_fail_closed_without_loop():
    generator = Generator([record('PERSONAL_ELIGIBILITY', False)] * 2)
    with pytest.raises(EvidenceEvaluationError, match='scope conflicts'):
        LLMEvidenceEvaluator(generator, require_scope=True).evaluate('How many training hours do I get?', RESULTS)
    assert len(generator.calls) == 2


def test_personal_verification_cannot_be_answered_as_policy_reporting():
    generator = Generator([record(), record('PERSONAL_ELIGIBILITY', False)])
    result = LLMEvidenceEvaluator(generator, require_scope=True).evaluate('Do I qualify for the training allowance?', RESULTS)
    assert result.status is EvidenceStatus.INSUFFICIENT
    assert len(generator.calls) == 2


def test_diagnostics_path_enforces_same_scope_and_invalid_evidence_ids_remain_invalid():
    value = json.loads(record()); value['supporting_evidence_ids'] = [2]
    generator = Generator([json.dumps(value)] * 2)
    result, diagnostic = LLMEvidenceEvaluator(generator, require_scope=True).evaluate_with_diagnostics('How many training hours do I get?', RESULTS)
    assert result.status is EvidenceStatus.INSUFFICIENT
    assert diagnostic.validation_error_message


def test_unsupported_procedure_still_abstains_under_general_constraint():
    generator = Generator([record('GENERAL', False)])
    result = LLMEvidenceEvaluator(generator, require_scope=True).evaluate('How do I recover access?', RESULTS)
    assert result.status is EvidenceStatus.INSUFFICIENT and not result.supporting_evidence_ids
