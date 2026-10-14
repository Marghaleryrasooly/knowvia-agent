import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from knowvia.generation import GroqGenerationError, GroqGenerationProvider


def response(content=" answer ", finish="stop", **message):
    return {"choices": [{"finish_reason": finish, "message": {
        "role": "assistant", "content": content, **message}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
                  "completion_tokens_details": {"reasoning_tokens": 12}}}


def provider(output=None, error=None, **config):
    calls = []
    def transport(*args):
        calls.append(args)
        if error is not None:
            raise error
        return output
    return GroqGenerationProvider("fictional-test-credential", transport=transport, **config), calls


def test_request_preserves_prompts_settings_and_no_tools():
    p, calls = provider(response(), temperature=0, max_output_tokens=2048, reasoning_effort="medium")
    assert p.generate("unchanged system", "unchanged user") == "answer"
    url, payload, timeout, headers = calls[0]
    assert url == "https://api.groq.com/openai/v1/chat/completions"
    assert payload == {"model": "openai/gpt-oss-120b", "messages": [
        {"role": "system", "content": "unchanged system"}, {"role": "user", "content": "unchanged user"}],
        "stream": False, "n": 1, "temperature": 0, "max_completion_tokens": 2048,
        "reasoning_effort": "medium", "include_reasoning": False, "tool_choice": "none"}
    assert timeout == 120 and headers["Authorization"].startswith("Bearer ")
    assert p.last_call["finish_reason"] == "stop"
    assert p.last_call["token_usage"]["reasoning_tokens"] == 12
    assert "fictional-test-credential" not in repr(p) + str(p.last_call)


@pytest.mark.parametrize("output,category", [
    (None, "invalid_response"), ({}, "invalid_response"),
    ({"error": {"message": "private"}}, "invalid_response"),
    ({"choices": []}, "invalid_response"), ({"choices": [None]}, "invalid_response"),
    (response(""), "empty_response"), (response(" "), "empty_response"),
    (response(None), "invalid_response"), (response([]), "invalid_response"),
    (response(finish="length"), "incomplete_output"),
    (response(finish="content_filter"), "blocked"),
    (response(finish="tool_calls"), "invalid_response"),
    (response(finish=None), "invalid_response"),
    (response(finish={}), "invalid_response"),
    (response(tool_calls=[{"name": "private"}]), "invalid_response"),
    (response(function_call={"name": "private"}), "invalid_response"),
    (response(refusal="private"), "blocked"),
    (response(role="user"), "invalid_response"),
])
def test_response_rejection_is_safe(output, category):
    p, calls = provider(output)
    with pytest.raises(GroqGenerationError) as error:
        p.generate("private prompt", "private evidence")
    assert error.value.category == category
    assert p.last_call["error_category"] == category
    assert len(calls) == 1
    assert "private" not in str(error.value) + str(p.last_call)


@pytest.mark.parametrize("status,category", [(400, "invalid_request"), (401, "authentication"),
    (403, "authentication"), (402, "billing"), (404, "api_access"), (429, "quota"),
    (500, "server"), (503, "server"), (302, "http")])
def test_http_errors_never_read_or_leak_body_retry_or_fallback(status, category, caplog):
    error = HTTPError("https://example.invalid", status, "private credential", {}, io.BytesIO(b"private evidence"))
    p, calls = provider(error=error)
    with pytest.raises(GroqGenerationError) as captured:
        p.generate("private prompt", "private evidence")
    assert captured.value.category == category and captured.value.status == status
    assert captured.value.__suppress_context__
    assert "private" not in str(captured.value) + caplog.text + str(p.last_call)
    assert error.fp.tell() == 0 and len(calls) == 1


@pytest.mark.parametrize("error,category", [(TimeoutError("private"), "timeout"),
    (URLError(TimeoutError("private")), "timeout"), (URLError("private"), "transport"),
    (ConnectionError("private"), "transport"), (RuntimeError("private"), "transport"),
    (json.JSONDecodeError("private", "private", 0), "invalid_response"),
    (UnicodeError("private"), "invalid_response")])
def test_transport_failures_sanitized(error, category):
    p, calls = provider(error=error)
    with pytest.raises(GroqGenerationError) as captured:
        p.generate("private", "private")
    assert captured.value.category == category and len(calls) == 1
    assert "private" not in str(captured.value) + str(p.last_call)


@pytest.mark.parametrize("config", [{"api_key": ""}, {"api_key": "bad\nkey"},
    {"model": "another-model"}, {"reasoning_effort": "none"}, {"timeout": 0},
    {"timeout": float("nan")}, {"temperature": float("inf")},
    {"max_output_tokens": 0}, {"max_output_tokens": 65537}, {"max_output_tokens": True}])
def test_bad_configuration_rejected_without_network(config):
    with pytest.raises(GroqGenerationError, match="configuration"):
        GroqGenerationProvider(**{"api_key": "fictional", **config})


def test_factories_defaults_and_independent_inference_settings(monkeypatch):
    from knowvia.cli import create_generator, create_evidence_evaluator, create_parser
    from knowvia.generation import OllamaGenerationProvider
    captured = []
    monkeypatch.setattr("knowvia.cli.GroqGenerationProvider", lambda **kw: captured.append(kw) or SimpleNamespace(**kw))
    monkeypatch.setenv("GROQ_API_KEY", "fictional")
    monkeypatch.setenv("GEMINI_ROUTER_MODEL", "routing-must-stay-separate")
    for name in ("KNOWVIA_GENERATION_PROVIDER", "KNOWVIA_EVIDENCE_EVALUATOR"):
        monkeypatch.delenv(name, raising=False)
    assert isinstance(create_generator("ollama"), OllamaGenerationProvider)
    assert create_parser().parse_args(["ask", "question"]).generation_provider == "groq"
    create_generator("groq")
    create_evidence_evaluator("groq")
    assert [c["temperature"] for c in captured] == [.1, 0]
    assert [c["max_output_tokens"] for c in captured] == [1024, 2048]
    assert [c["reasoning_effort"] for c in captured] == ["low", "medium"]
    assert all(c["model"] == "openai/gpt-oss-120b" and c["timeout"] == 120 for c in captured)
    monkeypatch.setenv("GROQ_EVIDENCE_MAX_COMPLETION_TOKENS", "invalid-private-value")
    with pytest.raises(ValueError, match="configuration is invalid") as error:
        create_evidence_evaluator("groq")
    assert "invalid-private-value" not in str(error.value)


def test_deployment_requires_key_only_when_selected():
    from knowvia.deployment import DeploymentConfig, DeploymentConfigurationError
    assert DeploymentConfig.from_environment({"GROQ_API_KEY": "fictional"}).generation_provider == "groq"
    for selector in ("KNOWVIA_EVIDENCE_EVALUATOR", "KNOWVIA_GENERATION_PROVIDER"):
        with pytest.raises(DeploymentConfigurationError):
            DeploymentConfig.from_environment({selector: "groq"})
    config = DeploymentConfig.from_environment({"KNOWVIA_GENERATION_PROVIDER": "groq",
        "KNOWVIA_EVIDENCE_EVALUATOR": "groq", "GROQ_API_KEY": "fictional"})
    assert config.router_provider == "deterministic"
    assert "fictional" not in repr(config)


def test_invalid_evidence_uses_only_existing_bounded_repair_and_never_answers():
    from knowvia.answering import AnsweringService
    from knowvia.evidence import LLMEvidenceEvaluator
    from knowvia.models import Chunk, RetrievalResult, EvidenceStatus
    p, calls = provider(response("not JSON"))
    results = [RetrievalResult(Chunk("one", "policy.md", "Policy", "Procedure", "Use the portal."), .8)]
    def unexpected(*args):
        pytest.fail("Answer generation must not run")
    answer = AnsweringService(SimpleNamespace(search=lambda *args: results),
        SimpleNamespace(generate=unexpected), LLMEvidenceEvaluator(p)).answer("How?")
    assert answer.evidence_decision.status is EvidenceStatus.INSUFFICIENT
    assert len(calls) == 2


def test_provider_failure_does_not_trigger_semantic_repair_or_answer():
    from knowvia.evidence import LLMEvidenceEvaluator, evaluate_evidence_safely
    from knowvia.models import Chunk, RetrievalResult, EvidenceStatus
    p, calls = provider(error=TimeoutError())
    results = [RetrievalResult(Chunk("one", "policy.md", "Policy", "Procedure", "Use the portal."), .8)]
    assert evaluate_evidence_safely(LLMEvidenceEvaluator(p), "How?", results).status is EvidenceStatus.INSUFFICIENT
    assert len(calls) == 1


def test_http_transport_blocks_redirects_and_bounds_response_without_network(monkeypatch):
    from knowvia.generation import _groq_post_json
    captured = []
    class Body(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
    class Opener:
        def open(self, request, timeout):
            captured.append((request, timeout))
            return Body(json.dumps(response()).encode())
    def build(handler):
        assert handler.redirect_request(None, None, 302, "", {}, "https://example.invalid") is None
        return Opener()
    monkeypatch.setattr("urllib.request.build_opener", build)
    result = _groq_post_json("https://api.groq.com/openai/v1/chat/completions",
        {"messages": ["Unicode café"]}, 120, {"Authorization": "Bearer fictional"})
    assert result["choices"][0]["finish_reason"] == "stop"
    assert len(captured) == 1 and captured[0][1] == 120
    assert captured[0][0].get_header("User-agent") == "knowvia-agent/1.0"
    assert json.loads(captured[0][0].data)["messages"] == ["Unicode café"]
