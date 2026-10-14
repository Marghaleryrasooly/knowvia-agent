import json
import pytest
from knowvia.evidence import LLMEvidenceEvaluator, EvidenceEvaluationError
from knowvia.condition_preservation import CONDITION_VALIDATION_SYSTEM_PROMPT
from knowvia.models import Chunk, RetrievalResult
from test_general_evidence_scope import output


def judge(fact, verdict):
    class Generator:
        model_name = 'offline'
        def __init__(self): self.calls = []
        def generate(self, system, user):
            self.calls.append((system, user))
            return json.dumps(verdict)
    generator = Generator()
    evaluator = LLMEvidenceEvaluator(generator, require_scope=True)
    results = [RetrievalResult(Chunk('rule', 'guide.md', 'Guide', 'Procedure',
                                     'If the connection fails, restart the client.'), .9)]
    return evaluator, generator, results, output(directly_supported_facts=[fact])


def test_equivalent_trigger_requires_positive_source_aware_semantic_verdict():
    evaluator, generator, results, raw = judge('Connection failures require restarting the client.',
        dict(grounded=True, preserved_condition_ids=[1], violations=[]))
    assert evaluator._parse(raw, results, 'Connection failure').supporting_evidence_ids == (1,)
    assert len(generator.calls) == 1
    assert generator.calls[0][0] == CONDITION_VALIDATION_SYSTEM_PROMPT
    assert len(json.loads(generator.calls[0][1])['validated_supporting_evidence']) == 1


@pytest.mark.parametrize('fact,violation', [
    ('Restart the client.', 'OMITTED'),
    ('Restart only when the connection works.', 'REVERSED'),
    ('Connection failures may optionally be ignored.', 'WEAKENED'),
    ('Connection failures require purchasing equipment.', 'INVENTED'),
])
def test_non_equivalent_facts_fail_closed(fact, violation):
    evaluator, generator, results, raw = judge(fact,
        dict(grounded=False, preserved_condition_ids=[], violations=[violation]))
    with pytest.raises(EvidenceEvaluationError, match='omitted or altered'):
        evaluator._parse(raw, results, 'Connection failure')
    assert len(generator.calls) == 1


def test_invalid_semantic_output_is_not_authority():
    evaluator, generator, results, raw = judge('Connection failures require restarting.', {})
    with pytest.raises(EvidenceEvaluationError): evaluator._parse(raw, results)


def test_unanchored_condition_cannot_reach_semantic_checker():
    evaluator, generator, results, raw = judge('A new prerequisite applies.', {})
    raw = json.loads(raw); raw['conditions'] = ['Invented prerequisite']
    with pytest.raises(EvidenceEvaluationError, match='not anchored'):
        evaluator._parse(json.dumps(raw), results)
    assert generator.calls == []
