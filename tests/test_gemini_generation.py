from types import SimpleNamespace as NS
import json
import pytest

from knowvia.generation import GeminiGenerationProvider, GeminiGenerationError


def response(text=" answer ", finish="STOP", **part):
    return NS(prompt_feedback=None, candidates=[NS(finish_reason=finish,
              content=NS(parts=[NS(text=text, **part)]))])


def provider(output=None, error=None):
    calls = []
    def generate_content(**kwargs):
        calls.append(kwargs)
        if error:
            raise error
        return output
    return GeminiGenerationProvider("fictional-test-credential", client=NS(
        models=NS(generate_content=generate_content))), calls


def test_plain_text_request_preserves_prompts_settings_and_no_tools():
    p, calls = provider(response())
    assert p.generate("unchanged system", "unchanged question") == "answer"
    assert calls == [{"model": "gemini-3.8-flash", "contents": "unchanged question",
        "config": {"system_instruction": "unchanged system", "temperature": .1,
                   "max_output_tokens": 220, "candidate_count": 1,
                   "thinking_config": {"thinking_level": "high", "include_thoughts": False}}}]


@pytest.mark.parametrize("output", [None, {}, NS(candidates=[]), response(""),
    response(" "), response(finish="MAX_TOKENS"), response(finish="SAFETY"),
    response(finish=None), response(function_call={"name": "tool"}),
    response(inline_data={"data": "binary"}), response(text=None),
    NS(prompt_feedback=NS(block_reason="SAFETY"), candidates=[])])
def test_invalid_outputs_fail_closed(output):
    p, calls = provider(output)
    with pytest.raises(GeminiGenerationError):
        p.generate("system", "question")
    assert len(calls) == 1


@pytest.mark.parametrize("code", [401, 403, 429, 500, 503, None])
def test_errors_sanitized_without_retry_or_fallback(code, caplog):
    error = RuntimeError("fictional-test-credential sensitive prompt")
    error.code = code
    p, calls = provider(error=error)
    with pytest.raises(GeminiGenerationError) as exc:
        p.generate("sensitive prompt", "question")
    assert "fictional-test-credential" not in str(exc.value) + caplog.text
    assert "sensitive prompt" not in str(exc.value) + caplog.text
    assert len(calls) == 1


@pytest.mark.parametrize("error", [TimeoutError("private"), ConnectionError("private"),
                                 json.JSONDecodeError("private", "private", 0)])
def test_transport_errors_fail_safely(error):
    p, _ = provider(error=error)
    with pytest.raises(GeminiGenerationError, match="failure"):
        p.generate("system", "question")


def test_factories_keep_defaults_and_separate_inference_from_routing(monkeypatch):
    from knowvia.cli import create_generator, create_evidence_evaluator, create_parser
    from knowvia.generation import OllamaGenerationProvider
    captured = []
    monkeypatch.setattr("knowvia.cli.GeminiGenerationProvider", lambda **kw: captured.append(kw) or NS(**kw))
    monkeypatch.setenv("GEMINI_API_KEY", "fictional-test-credential")
    monkeypatch.setenv("GEMINI_ROUTER_MODEL", "router-only")
    for name in ("KNOWVIA_GENERATION_PROVIDER", "KNOWVIA_EVIDENCE_EVALUATOR"):
        monkeypatch.delenv(name, raising=False)
    assert isinstance(create_generator("ollama"), OllamaGenerationProvider)
    assert create_parser().parse_args(["ask", "question"]).generation_provider == "groq"
    create_generator("gemini")
    create_evidence_evaluator("gemini")
    assert [c["temperature"] for c in captured] == [.1, 0.0]
    assert [c["max_output_tokens"] for c in captured] == [220, 320]
    assert all(c["model"] == "gemini-3.8-flash" and c["timeout"] == 120 for c in captured)


def test_configuration_requires_key_only_when_selected():
    from knowvia.deployment import DeploymentConfig, DeploymentConfigurationError
    assert DeploymentConfig.from_environment({"GROQ_API_KEY": "fictional"}).generation_provider == "groq"
    assert DeploymentConfig.from_environment({"GEMINI_API_KEY": "fictional", "GROQ_API_KEY": "fictional"}).evidence_provider == "groq"
    with pytest.raises(DeploymentConfigurationError):
        DeploymentConfig.from_environment({"KNOWVIA_EVIDENCE_EVALUATOR": "gemini"})
    config = DeploymentConfig.from_environment({"KNOWVIA_GENERATION_PROVIDER": "gemini",
              "KNOWVIA_EVIDENCE_EVALUATOR": "gemini", "GEMINI_API_KEY": "fictional"})
    assert config.router_provider == "deterministic"
    assert "fictional" not in repr(config)


def test_candidate_cannot_overwrite_reference():
    from evaluation.local_acceptance import validate_candidate_output, OUTPUT
    with pytest.raises(ValueError, match="overwrite"):
        validate_candidate_output(OUTPUT, "gemini")
    validate_candidate_output(OUTPUT.with_name("gemini-candidate.json"), "gemini")


def test_evidence_invalid_initial_and_repair_never_generate_answer():
    from knowvia.evidence import LLMEvidenceEvaluator, evaluate_evidence_safely
    from knowvia.models import Chunk, RetrievalResult, EvidenceStatus
    p, calls = provider(response("not JSON"))
    results = [RetrievalResult(Chunk("one", "policy.md", "Policy", "Procedure", "Use the portal."), .8)]
    decision = evaluate_evidence_safely(LLMEvidenceEvaluator(p), "How?", results)
    assert decision.status is EvidenceStatus.INSUFFICIENT
    assert len(calls) == 2


def test_real_sdk_configuration_can_initialize_without_network():
    p = GeminiGenerationProvider("fictional-test-credential")
    assert p.model_name == "gemini-3.8-flash"
    p._client.close()
