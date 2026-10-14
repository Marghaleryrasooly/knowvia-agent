from __future__ import annotations

import pytest

from knowvia.generation import OllamaGenerationProvider


def test_ollama_provider_builds_non_streaming_grounded_chat_request() -> None:
    captured: dict[str, object] = {}

    def fake_transport(url: str, payload: dict[str, object], timeout: float) -> dict:
        captured.update(url=url, payload=payload, timeout=timeout)
        return {"message": {"role": "assistant", "content": "  Local answer.  "}}

    provider = OllamaGenerationProvider(
        model="llama-test",
        host="http://ollama.test/",
        timeout=12.0,
        transport=fake_transport,
    )

    answer = provider.generate("Grounding rules", "Question and context")

    assert answer == "Local answer."
    assert provider.model_name == "llama-test"
    assert captured["url"] == "http://ollama.test/api/chat"
    assert captured["timeout"] == 12.0
    payload = captured["payload"]
    assert payload["model"] == "llama-test"
    assert payload["stream"] is False
    assert payload["messages"] == [
        {"role": "system", "content": "Grounding rules"},
        {"role": "user", "content": "Question and context"},
    ]
    assert payload["options"]["temperature"] == 0.1
    assert "format" not in payload


def test_evidence_factory_constrains_initial_and_repair_without_changing_prompts(monkeypatch):
    import json
    from knowvia.cli import create_evidence_evaluator
    from knowvia.evidence import EVIDENCE_RESPONSE_SCHEMA, evaluate_evidence_safely
    from knowvia.models import Chunk, RetrievalResult, EvidenceStatus
    calls = []
    outputs = ['not JSON', json.dumps({
        "requested_claim": "The documented procedure", "directly_supported_facts": ["Use the portal."],
        "supporting_evidence_ids": [1], "missing_fact": "", "decision": "SUFFICIENT",
        "answer_scope": "GENERAL", "policy_support": "NOT_APPLICABLE",
        "personal_eligibility": "UNKNOWN", "conditions": []})]
    def transport(url, payload, timeout):
        calls.append(payload)
        return {"message": {"content": outputs.pop(0)}}
    monkeypatch.setattr("knowvia.generation._post_json", transport)
    evaluator = create_evidence_evaluator("ollama", structured_output=True)
    results = [RetrievalResult(Chunk("one", "policy.md", "Policy", "Procedure", "Use the portal."), 0.8)]
    decision = evaluate_evidence_safely(evaluator, "How do I submit a request?", results)
    assert decision.status is EvidenceStatus.SUFFICIENT
    assert len(calls) == 2
    assert all(p["format"] == EVIDENCE_RESPONSE_SCHEMA for p in calls)
    assert all(p["options"] == {"temperature": 0.0, "num_predict": 320} for p in calls)


@pytest.mark.parametrize("value", [
    {"decision": "INSUFFICIENT", "supporting_evidence_ids": [1], "missing_fact": "Unknown channel"},
    {"decision": "INSUFFICIENT", "supporting_evidence_ids": [], "missing_fact": ""},
    {"decision": "SUFFICIENT", "supporting_evidence_ids": [2], "missing_fact": ""},
    {"decision": "SUFFICIENT", "supporting_evidence_ids": [1], "missing_fact": "", "requested_claim": "non-empty string"},
])
def test_schema_does_not_bypass_semantic_validation_or_allow_generation(value):
    import json
    from knowvia.answering import AnsweringService, ABSTENTION_RESPONSE
    from knowvia.evidence import LLMEvidenceEvaluator, EVIDENCE_RESPONSE_SCHEMA
    from knowvia.models import Chunk, RetrievalResult
    calls = []
    output = {"requested_claim": "A procedure", "directly_supported_facts": ["Use the portal."], **value}
    def transport(url, payload, timeout):
        calls.append(payload)
        return {"message": {"content": json.dumps(output)}}
    evidence = OllamaGenerationProvider(transport=transport, response_schema=EVIDENCE_RESPONSE_SCHEMA)
    class Retriever:
        def search(self, question, top_k):
            return [RetrievalResult(Chunk("one", "policy.md", "Policy", "Procedure", "Use the portal."), 0.8)]
    class NoAnswer:
        def generate(self, *args):
            pytest.fail("Invalid evidence must never reach answer generation")
    result = AnsweringService(Retriever(), NoAnswer(), LLMEvidenceEvaluator(evidence)).answer("What procedure applies?")
    assert result.answer == ABSTENTION_RESPONSE
    assert result.supporting_results == []
    assert len(calls) == 2


def test_ollama_provider_rejects_empty_response() -> None:
    provider = OllamaGenerationProvider(
        transport=lambda *_: {"message": {"content": "  "}}
    )

    with pytest.raises(RuntimeError, match="empty or invalid"):
        provider.generate("system", "user")


def test_unaccepted_schema_candidate_is_not_enabled_by_default():
    from knowvia.cli import create_evidence_evaluator
    assert create_evidence_evaluator("ollama").generator._response_schema is None


def test_ollama_provider_reports_connection_failure() -> None:
    def failing_transport(*_: object) -> dict:
        raise OSError("connection refused")

    provider = OllamaGenerationProvider(transport=failing_transport)

    with pytest.raises(RuntimeError, match="Make sure Ollama is installed and running"):
        provider.generate("system", "user")
