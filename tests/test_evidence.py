from __future__ import annotations

import json

import pytest

from knowvia.evidence import (
    EVIDENCE_SYSTEM_PROMPT,
    EvidenceEvaluationError,
    LLMEvidenceEvaluator,
    build_evidence_user_prompt,
    evaluate_evidence_safely,
    parse_evidence_decision,
)
from knowvia.models import Chunk, EvidenceStatus, RetrievalResult


RESULTS = [
    RetrievalResult(
        Chunk(
            "confidential",
            "07_information_classification_policy.md",
            "NovaTech Information Classification Policy",
            "Confidential",
            "Confidential information includes private source code.",
        ),
        0.8,
    )
]


def test_evidence_prompt_requires_minimal_support_and_consistent_fields() -> None:
    assert "Select the smallest set of evidence IDs" in EVIDENCE_SYSTEM_PROMPT
    assert "Do not broaden a request about one category" in EVIDENCE_SYSTEM_PROMPT
    assert "SUFFICIENT => one or more minimal supporting IDs" in EVIDENCE_SYSTEM_PROMPT
    assert "INSUFFICIENT => zero supporting IDs" in EVIDENCE_SYSTEM_PROMPT


class FakeGenerator:
    model_name = "fake-judge"

    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.output


class SequenceGenerator:
    model_name = "sequence-judge"

    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.outputs.pop(0)


@pytest.mark.parametrize("status", ["SUFFICIENT", "INSUFFICIENT"])
def test_strict_decisions_parse(status: str) -> None:
    decision = parse_evidence_decision(
        json.dumps(
            {
                "requested_claim": "Whether the action is allowed.",
                "directly_supported_facts": ["The policy directly addresses it."],
                "supporting_evidence_ids": [1] if status == "SUFFICIENT" else [],
                "missing_fact": "" if status == "SUFFICIENT" else "Approval rule",
                "decision": status,
            }
        )
    )

    assert decision.status.value == status
    assert "The policy directly addresses it." in decision.reason
    assert decision.missing_information == (
        "" if status == "SUFFICIENT" else "Approval rule"
    )


@pytest.mark.parametrize(
    "output",
    [
        "",
        "SUFFICIENT",
        "```json\n{\"decision\":\"SUFFICIENT\"}\n```",
        '{"requested_claim":"claim","directly_supported_facts":["fact"],"supporting_evidence_ids":[1],"missing_fact":"x","decision":"MAYBE"}',
        '{"requested_claim":"","directly_supported_facts":["fact"],"supporting_evidence_ids":[1],"missing_fact":"","decision":"SUFFICIENT"}',
        '{"requested_claim":"claim","directly_supported_facts":[],"supporting_evidence_ids":[],"missing_fact":"","decision":"SUFFICIENT"}',
        '{"requested_claim":"claim","directly_supported_facts":["fact"],"supporting_evidence_ids":[1],"missing_fact":"still missing","decision":"SUFFICIENT"}',
        '{"requested_claim":"claim","directly_supported_facts":[],"supporting_evidence_ids":[],"missing_fact":"","decision":"INSUFFICIENT"}',
        '{"requested_claim":"claim","directly_supported_facts":"fact","supporting_evidence_ids":[],"missing_fact":"x","decision":"INSUFFICIENT"}',
        '{"requested_claim":"claim","directly_supported_facts":[""],"supporting_evidence_ids":[],"missing_fact":"x","decision":"INSUFFICIENT"}',
        '{"requested_claim":"claim","directly_supported_facts":[],"supporting_evidence_ids":[],"missing_fact":"x","decision":"INSUFFICIENT","extra":1}',
        '[{"decision":"SUFFICIENT"}]',
    ],
)
def test_malformed_or_ambiguous_output_is_rejected(output: str) -> None:
    with pytest.raises(EvidenceEvaluationError):
        parse_evidence_decision(output)


def test_llm_evaluator_receives_question_evidence_and_strict_instructions() -> None:
    generator = FakeGenerator(
        '{"requested_claim":"Whether ChatGPT is approved for confidential source code",'
        '"directly_supported_facts":["Private source code is confidential"],'
        '"supporting_evidence_ids":[],'
        '"missing_fact":"Whether ChatGPT is approved.","decision":"INSUFFICIENT"}'
    )
    evaluator = LLMEvidenceEvaluator(generator)

    decision = evaluator.evaluate(
        "Can I paste confidential source code into ChatGPT?", RESULTS
    )

    assert decision.status is EvidenceStatus.INSUFFICIENT
    assert evaluator.evaluator_name == "llm:fake-judge"
    system_prompt, user_prompt = generator.calls[0]
    assert system_prompt == EVIDENCE_SYSTEM_PROMPT
    assert "A related fact is NOT automatically sufficient" in system_prompt
    assert "Can I paste confidential source code into ChatGPT?" in user_prompt
    assert "Section: Confidential" in user_prompt
    assert "private source code" in user_prompt


def test_v2_rubric_covers_direct_support_and_unsupported_inference() -> None:
    prompt = EVIDENCE_SYSTEM_PROMPT

    assert "Ordinary paraphrases and workplace equivalents count" in prompt
    assert '"prohibited," "must not," or "not allowed"' in prompt
    assert '"At least N" establishes a minimum' in prompt
    assert '"Up to N" establishes a maximum' in prompt
    assert "Six is longer than five" in prompt
    assert "four is more than three" in prompt
    assert "twelve is more than ten" in prompt
    assert "documented procedure supports what to do" in prompt
    assert "Ignore irrelevant distractor chunks" in prompt
    assert "does not establish eligibility OR ineligibility" in prompt
    assert "named product, service, recipient, device, or action" in prompt
    assert "A related fact is NOT automatically sufficient" in prompt


def test_v2_few_shots_are_small_balanced_and_generic() -> None:
    prompt = EVIDENCE_SYSTEM_PROMPT

    assert prompt.count("Output: {") == 4
    assert prompt.count('"decision":"SUFFICIENT"') == 2
    assert prompt.count('"decision":"INSUFFICIENT"') == 2
    assert "NovaTech" not in prompt
    assert "ChatGPT" not in prompt
    assert "main branch" not in prompt


def test_insufficient_may_have_no_related_supported_facts() -> None:
    decision = parse_evidence_decision(
        json.dumps(
            {
                "requested_claim": "The requested company fact.",
                "directly_supported_facts": [],
                "supporting_evidence_ids": [],
                "missing_fact": "Any company evidence about the requested fact.",
                "decision": "INSUFFICIENT",
            }
        )
    )

    assert decision.status is EvidenceStatus.INSUFFICIENT
    assert decision.reason.startswith("Missing:")


@pytest.mark.parametrize(
    ("supporting_ids", "status", "missing"),
    [
        ([1, 1], "SUFFICIENT", ""),
        ([2], "SUFFICIENT", ""),
        (["1"], "SUFFICIENT", ""),
        ([], "SUFFICIENT", ""),
        ([1], "INSUFFICIENT", "Missing approval."),
    ],
)
def test_supporting_evidence_ids_fail_closed_when_invalid(
    supporting_ids, status, missing
) -> None:
    output = json.dumps(
        {
            "requested_claim": "The requested claim.",
            "directly_supported_facts": ["A directly stated fact."],
            "supporting_evidence_ids": supporting_ids,
            "missing_fact": missing,
            "decision": status,
        }
    )

    with pytest.raises(EvidenceEvaluationError):
        parse_evidence_decision(output, evidence_count=1)


def test_empty_retrieval_fails_closed_without_calling_evaluator() -> None:
    class ExplodingEvaluator:
        evaluator_name = "must-not-run"

        def evaluate(self, *_):
            raise AssertionError("Evaluator should not be called")

    decision = evaluate_evidence_safely(ExplodingEvaluator(), "Question", [])

    assert decision.status is EvidenceStatus.INSUFFICIENT
    assert decision.reason == "No company evidence was retrieved."


def test_evaluator_failure_fails_closed() -> None:
    class FailingEvaluator:
        evaluator_name = "broken"

        def evaluate(self, *_):
            raise RuntimeError("Ollama unavailable")

    decision = evaluate_evidence_safely(FailingEvaluator(), "Question", RESULTS)

    assert decision.status is EvidenceStatus.INSUFFICIENT
    assert decision.reason == "Evidence evaluation could not be validated."


def test_evaluator_invalid_return_type_fails_closed() -> None:
    class InvalidEvaluator:
        evaluator_name = "invalid"

        def evaluate(self, *_):
            return "SUFFICIENT"

    decision = evaluate_evidence_safely(InvalidEvaluator(), "Question", RESULTS)

    assert decision.status is EvidenceStatus.INSUFFICIENT


def test_diagnostic_evaluation_retains_raw_validation_failure() -> None:
    raw = "```json\n{\"decision\":\"SUFFICIENT\"}\n```"
    evaluator = LLMEvidenceEvaluator(FakeGenerator(raw))

    decision, diagnostics = evaluator.evaluate_with_diagnostics("Question", RESULTS)

    assert decision.status is EvidenceStatus.INSUFFICIENT
    assert diagnostics.raw_output == raw
    assert diagnostics.exception_type == "EvidenceEvaluationError"
    assert diagnostics.validation_error_message == (
        "Evidence evaluator did not return strict JSON"
    )
    assert not diagnostics.provider_request_failed
    assert diagnostics.final_decision is EvidenceStatus.INSUFFICIENT


def test_diagnostic_evaluation_distinguishes_provider_failure() -> None:
    class FailingGenerator:
        model_name = "broken"

        def generate(self, *_):
            raise RuntimeError("Ollama unavailable")

    evaluator = LLMEvidenceEvaluator(FailingGenerator())

    decision, diagnostics = evaluator.evaluate_with_diagnostics("Question", RESULTS)

    assert decision.status is EvidenceStatus.INSUFFICIENT
    assert diagnostics.raw_output is None
    assert diagnostics.exception_type == "RuntimeError"
    assert diagnostics.validation_error_message == "Ollama unavailable"
    assert diagnostics.provider_request_failed


def test_diagnostic_evaluation_records_success_without_changing_decision() -> None:
    raw = json.dumps(
        {
            "requested_claim": "The exact requested rule.",
            "directly_supported_facts": ["The policy states the rule."],
            "supporting_evidence_ids": [1],
            "missing_fact": "",
            "decision": "SUFFICIENT",
        }
    )
    evaluator = LLMEvidenceEvaluator(FakeGenerator(raw))

    decision, diagnostics = evaluator.evaluate_with_diagnostics("Question", RESULTS)

    assert decision.status is EvidenceStatus.SUFFICIENT
    assert diagnostics.raw_output == raw
    assert diagnostics.exception_type is None
    assert diagnostics.validation_error_message is None
    assert not diagnostics.provider_request_failed


def test_structurally_invalid_decision_gets_one_bounded_re_evaluation() -> None:
    invalid = json.dumps(
        {
            "requested_claim": "Whether remote work is permitted.",
            "directly_supported_facts": [
                "Employees may work remotely up to three days per week."
            ],
            "supporting_evidence_ids": [1],
            "missing_fact": "",
            "decision": "INSUFFICIENT",
        }
    )
    corrected = json.dumps(
        {
            "requested_claim": "Whether remote work is permitted.",
            "directly_supported_facts": [
                "Employees may work remotely up to three days per week."
            ],
            "supporting_evidence_ids": [1],
            "missing_fact": "",
            "decision": "SUFFICIENT",
        }
    )
    generator = SequenceGenerator(invalid, corrected)
    evaluator = LLMEvidenceEvaluator(generator)

    decision, diagnostics = evaluator.evaluate_with_diagnostics(
        "Can I work remotely?", RESULTS
    )

    assert decision.status is EvidenceStatus.SUFFICIENT
    assert decision.supporting_evidence_ids == (1,)
    assert diagnostics.validation_error_message is None
    assert len(generator.calls) == 2
    assert "INSUFFICIENT requires a non-empty missing fact" in generator.calls[1][1]
    assert "final consistency validator" in generator.calls[1][0]


def test_structurally_invalid_decision_still_fails_closed_after_one_retry() -> None:
    invalid = json.dumps(
        {
            "requested_claim": "Whether remote work is permitted.",
            "directly_supported_facts": ["A related fact."],
            "supporting_evidence_ids": [1],
            "missing_fact": "",
            "decision": "INSUFFICIENT",
        }
    )
    generator = SequenceGenerator(invalid, invalid)
    evaluator = LLMEvidenceEvaluator(generator)

    decision, diagnostics = evaluator.evaluate_with_diagnostics("Question", RESULTS)

    assert decision.status is EvidenceStatus.INSUFFICIENT
    assert diagnostics.validation_error_message == (
        "INSUFFICIENT requires a non-empty missing fact"
    )
    assert not diagnostics.provider_request_failed
    assert len(generator.calls) == 2


def test_explicit_first_person_permission_can_be_validated_after_invalid_outputs() -> None:
    invalid = json.dumps(
        {
            "requested_claim": "Whether the employee may work remotely.",
            "directly_supported_facts": [
                "Employees may work remotely up to three days per week."
            ],
            "supporting_evidence_ids": [1],
            "missing_fact": "",
            "decision": "INSUFFICIENT",
        }
    )
    results = [
        RetrievalResult(
            Chunk(
                "remote",
                "02_remote_work_policy.md",
                "Remote Work Policy",
                "Standard Remote Work",
                "Employees may work remotely up to three days per week. "
                "Remote-work schedules must be approved by the employee's direct manager.",
            ),
            0.9,
        )
    ]
    evaluator = LLMEvidenceEvaluator(SequenceGenerator(invalid, invalid))

    decision, diagnostics = evaluator.evaluate_with_diagnostics(
        "Can I work remotely?", results
    )

    assert decision.status is EvidenceStatus.SUFFICIENT
    assert decision.supporting_evidence_ids == (1,)
    assert diagnostics.validation_error_message is None


def test_permission_recovery_does_not_infer_another_groups_eligibility() -> None:
    invalid = json.dumps(
        {
            "requested_claim": "Whether contractors may work remotely.",
            "directly_supported_facts": [
                "Employees may work remotely up to three days per week."
            ],
            "supporting_evidence_ids": [1],
            "missing_fact": "",
            "decision": "INSUFFICIENT",
        }
    )
    results = [
        RetrievalResult(
            Chunk(
                "remote",
                "02_remote_work_policy.md",
                "Remote Work Policy",
                "Standard Remote Work",
                "Employees may work remotely up to three days per week.",
            ),
            0.9,
        )
    ]
    evaluator = LLMEvidenceEvaluator(SequenceGenerator(invalid, invalid))

    decision = evaluate_evidence_safely(
        evaluator, "Can contractors work remotely?", results
    )

    assert decision.status is EvidenceStatus.INSUFFICIENT
