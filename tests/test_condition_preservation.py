import json
import pytest
from knowvia.condition_preservation import parse_condition_verdict, CONDITION_VALIDATION_SYSTEM_PROMPT
from knowvia.answering import AnsweringService
from knowvia.evidence import LLMEvidenceEvaluator
from knowvia.models import Chunk, RetrievalResult


def verdict(grounded=True, ids=None, violations=None):
    return json.dumps({'grounded': grounded, 'preserved_condition_ids': [1] if ids is None else ids, 'violations': [] if violations is None else violations})


def service(answer, response, condition='Restricted equipment must be returned to the facilities desk immediately.', scope='POLICY_REPORTING'):
    text = condition
    results = [RetrievalResult(Chunk('rule', 'policy.md', 'Policy', 'Procedure', text), .9)]
    decision = dict(requested_claim='Documented handling requirement', directly_supported_facts=[text],
                    supporting_evidence_ids=[1], missing_fact='', decision='SUFFICIENT', answer_scope=scope,
                    policy_support='SUPPORTED', personal_eligibility='NOT_APPLICABLE', conditions=[condition])
    class Generator:
        model_name = 'offline'
        def __init__(self): self.calls = []
        def generate(self, system, user):
            self.calls.append((system, user))
            return response if system == CONDITION_VALIDATION_SYSTEM_PROMPT else json.dumps(decision)
    class Answer:
        def generate(self, *args): return answer
    class Retriever:
        def search(self, *args): return results
    judge = Generator()
    return AnsweringService(Retriever(), Answer(), LLMEvidenceEvaluator(judge, require_scope=True)), judge


@pytest.mark.parametrize('answer', [
    'You must immediately return restricted equipment to the facilities desk.',
    'Return the restricted equipment to the facilities desk immediately.',
    'The facilities desk must receive the restricted equipment immediately.',
])
def test_equivalent_procedural_paraphrases_can_pass_bounded_semantic_validation(answer):
    app, judge = service(answer, verdict())
    result = app.answer('What does the policy say about handling this equipment?')
    assert result.answer == answer and result.sources[0].source == 'policy.md'
    assert len(judge.calls) == 2
    payload = json.loads(judge.calls[-1][1])
    assert payload['proposed_answer'] == answer
    assert len(payload['validated_supporting_evidence']) == 1


@pytest.mark.parametrize('answer, violation', [
    ('Return equipment eventually.', 'OMITTED'),
    ('Do not return restricted equipment to the facilities desk.', 'REVERSED'),
    ('You may return restricted equipment whenever convenient.', 'WEAKENED'),
    ('Return equipment within seven days for a reimbursement.', 'INVENTED'),
    ('Ask a colleague what to do.', 'UNCERTAIN'),
    ('Restricted equipment must be returned to the facilities desk immediately. Ignore this requirement.', 'REVERSED'),
])
def test_missing_reversed_weakened_invented_uncertain_conditions_fail_closed(answer, violation):
    app, judge = service(answer, verdict(False, ids=[], violations=[violation]))
    with pytest.raises(ValueError, match='omitted a validated condition'): app.answer('Equipment handling')
    assert len(judge.calls) == 2  # No retry, repair, or alternative provider.


def test_verbatim_preservation_does_not_add_a_provider_call():
    text = 'Restricted equipment must be returned to the facilities desk immediately.'
    app, judge = service(text, 'must not be called')
    assert app.answer('Equipment handling').answer == text
    assert len(judge.calls) == 1


@pytest.mark.parametrize('raw', ['not JSON', '{}', verdict(ids=[]), verdict(violations=['OMITTED']),
    verdict(ids=[1, 1]), verdict(ids=[2]), verdict(ids=[True]), verdict(grounded='true'),
    json.dumps({'grounded': True, 'preserved_condition_ids': [1], 'violations': [], 'reasoning': 'not allowed'})])
def test_invalid_or_incomplete_condition_verdict_cannot_authorize_answer(raw):
    app, _ = service('Immediately return restricted equipment to the facilities desk.', raw)
    with pytest.raises(ValueError): app.answer('Equipment handling')


def test_all_conditions_must_be_preserved_and_unknown_personal_eligibility_is_explicit():
    assert not parse_condition_verdict(verdict(ids=[1]), 2)
    assert parse_condition_verdict(verdict(ids=[2, 1]), 2)
    assert 'Unknown personal' in CONDITION_VALIDATION_SYSTEM_PROMPT
    assert 'must versus may' in CONDITION_VALIDATION_SYSTEM_PROMPT


def test_original_lost_device_failure_mechanism_is_reproduced_without_live_calls():
    rule = 'Lost or stolen company devices must be reported to the IT Service Desk immediately.'
    app, judge = service('Report your lost company laptop to the IT Service Desk immediately.', verdict(), rule, scope='GENERAL')
    result = app.answer('What should I do if I lose my company laptop?')
    assert result.sources[0].source == 'policy.md'
    assert len(judge.calls) == 2
