from __future__ import annotations

import io
import json
import traceback
from urllib.error import HTTPError, URLError

import pytest

from knowvia.generation import CloudflareGenerationProvider, CloudflareGenerationError
from knowvia.cli import create_generator, create_evidence_evaluator
from knowvia.deployment import DeploymentConfig, DeploymentConfigurationError
from knowvia.evidence import LLMEvidenceEvaluator, evaluate_evidence_safely
from knowvia.models import EvidenceStatus, Chunk, RetrievalResult


ACCOUNT = "0" * 32
TOKEN = "fake-test-only-secret"


def provider(transport):
    return CloudflareGenerationProvider(ACCOUNT, TOKEN, transport=transport)


def test_success_preserves_prompts_and_explicit_settings():
    captured = []
    def transport(*args):
        captured.append(args)
        return {"success": True, "errors": [], "result": {"response": " answer "}}
    value = provider(transport)
    assert value.generate("system", "user") == "answer"
    url, body, timeout, headers = captured[0]
    assert url.endswith("/ai/run/@cf/meta/llama-3.2-3b-instruct")
    assert body == {"messages": [{"role": "system", "content": "system"},
                                 {"role": "user", "content": "user"}],
                    "stream": False, "temperature": 0.1, "max_tokens": 220}
    assert timeout == 120
    assert headers["Authorization"] == "Bearer " + TOKEN
    assert TOKEN not in repr(value)


def test_native_rest_materialized_json_is_preserved_as_text():
    generated = {"decision": "INSUFFICIENT", "missing_fact": "Documented policy"}
    value = provider(lambda *args: {"success": True, "result": {"response": generated}})
    assert json.loads(value.generate("system", "user")) == generated


@pytest.mark.parametrize("response", [None, [], {}, {"success": False, "errors": [TOKEN]},
    {"success": True, "result": None}, {"success": True, "result": {}},
    {"success": True, "result": {"response": " "}},
    {"success": True, "result": {"response": []}},
    {"success": True, "result": {"response": "answer", "tool_calls": [{}]}},
    {"success": True, "errors": [{"code": 3042, "message": TOKEN}], "result": {}},
])
def test_bad_envelopes_are_sanitized(response, caplog):
    with pytest.raises(CloudflareGenerationError) as error:
        provider(lambda *args: response).generate("private prompt", "private evidence")
    assert TOKEN not in str(error.value) + caplog.text
    assert "private" not in str(error.value)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503])
def test_http_errors_hide_headers_bodies_and_traceback_context(status, caplog):
    calls = []
    def transport(*args):
        calls.append(1)
        raise HTTPError("https://invalid/" + TOKEN, status, TOKEN,
                        {"Authorization": TOKEN}, io.BytesIO(TOKEN.encode()))
    with pytest.raises(CloudflareGenerationError) as error:
        provider(transport).generate("system", "user")
    rendered = "".join(traceback.format_exception(error.value))
    assert str(status) in str(error.value)
    assert TOKEN not in rendered + caplog.text
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [TimeoutError(TOKEN), URLError(TOKEN),
    URLError(TimeoutError(TOKEN)), OSError(TOKEN),
    json.JSONDecodeError(TOKEN, TOKEN, 0), UnicodeDecodeError("utf8", b"x", 0, 1, TOKEN),
    ValueError(TOKEN)])
def test_transport_failures_are_sanitized(failure, caplog):
    def transport(*args):
        raise failure
    with pytest.raises(CloudflareGenerationError) as error:
        provider(transport).generate("system", "user")
    assert TOKEN not in "".join(traceback.format_exception(error.value)) + caplog.text


def test_factories_use_correct_settings_and_do_not_change_ollama_default(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", TOKEN)
    monkeypatch.setenv("CLOUDFLARE_TIMEOUT_SECONDS", "60")
    generation = create_generator("cloudflare")
    evidence = create_evidence_evaluator("cloudflare")
    assert (generation._temperature, generation._max_output_tokens, generation._timeout) == (0.1, 220, 60)
    assert (evidence.generator._temperature, evidence.generator._max_output_tokens) == (0.0, 320)
    assert create_generator("ollama").model_name == "llama3.2:latest"


def test_configuration_fails_closed_without_secrets():
    with pytest.raises(DeploymentConfigurationError, match="not configured"):
        DeploymentConfig.from_environment({"KNOWVIA_GENERATION_PROVIDER": "cloudflare"})
    config = DeploymentConfig.from_environment({"KNOWVIA_GENERATION_PROVIDER": "cloudflare", "KNOWVIA_EVIDENCE_EVALUATOR": "cloudflare",
        "CLOUDFLARE_ACCOUNT_ID": ACCOUNT, "CLOUDFLARE_API_TOKEN": TOKEN})
    assert TOKEN not in repr(config)
    assert config.cloudflare_credentials_configured


def test_bad_configuration_does_not_echo_input():
    with pytest.raises(CloudflareGenerationError) as error:
        CloudflareGenerationProvider(TOKEN, TOKEN)
    assert TOKEN not in str(error.value)


def test_evidence_transport_failure_abstains_without_generation():
    def transport(*args):
        raise HTTPError("https://invalid", 429, TOKEN, {}, io.BytesIO(TOKEN.encode()))
    candidate = RetrievalResult(Chunk("leave", "policy.md", "Policy", "Leave", "24 days"), 0.9)
    decision = evaluate_evidence_safely(LLMEvidenceEvaluator(provider(transport)), "Leave?", [candidate])
    assert decision.status is EvidenceStatus.INSUFFICIENT
    assert decision.supporting_evidence_ids == ()
    assert TOKEN not in decision.reason


def test_existing_evidence_repair_is_bounded_and_unchanged():
    calls = []
    def transport(*args):
        calls.append(args[1])
        text = "invalid JSON" if len(calls) == 1 else json.dumps({
            "requested_claim": "Leave entitlement", "directly_supported_facts": ["24 days"],
            "supporting_evidence_ids": [1], "missing_fact": "", "decision": "SUFFICIENT"})
        return {"success": True, "result": {"response": text}}
    candidate = RetrievalResult(Chunk("leave", "policy.md", "Policy", "Leave", "24 days"), 0.9)
    evaluator = LLMEvidenceEvaluator(provider(transport))
    decision = evaluate_evidence_safely(evaluator, "Leave?", [candidate])
    assert decision.status is EvidenceStatus.SUFFICIENT
    assert decision.supporting_evidence_ids == (1,)
    assert len(calls) == 2


def test_cloudflare_health_and_public_config_are_lightweight_and_secret_free(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from knowvia.web import create_web_app
    from knowvia.runtime_paths import RuntimePaths
    from knowvia.deployment import ApplicationEnvironment
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", TOKEN)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setattr("knowvia.generation._cloudflare_post_json", lambda *args: pytest.fail("Unexpected remote call"))
    config = DeploymentConfig(environment=ApplicationEnvironment.TEST,
        generation_provider="cloudflare", evidence_provider="cloudflare",
        cloudflare_credentials_configured=True)
    client = TestClient(create_web_app(deployment_config=config, runtime_paths=RuntimePaths(tmp_path)))
    assert client.get("/health").json() == {"status": "ok"}
    response = client.get("/api/config")
    assert response.status_code == 200
    assert TOKEN not in response.text
    assert "CLOUDFLARE_API_TOKEN" not in response.text


def test_answer_failure_does_not_fabricate_a_response():
    from knowvia.answering import AnsweringService
    from knowvia.models import EvidenceDecision
    candidate = RetrievalResult(Chunk("leave", "policy.md", "Policy", "Leave", "24 days"), 0.9)
    class Retriever:
        def search(self, question, top_k):
            return [candidate]
    class Evaluator:
        def evaluate(self, question, results):
            return EvidenceDecision(EvidenceStatus.SUFFICIENT, "Documented", "", (1,))
    def transport(*args):
        raise TimeoutError(TOKEN)
    service = AnsweringService(Retriever(), provider(transport), Evaluator())
    with pytest.raises(CloudflareGenerationError, match="timeout"):
        service.answer("Leave?")


def test_real_transport_uses_nonredirecting_opener_and_parses_json(monkeypatch):
    from knowvia.generation import _cloudflare_post_json
    from urllib.request import HTTPRedirectHandler
    captured = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self): return b'{"success":true,"result":{"response":"answer"}}'
    class Opener:
        def open(self, request, timeout):
            captured.append(request)
            return Response()
    def build(handler):
        assert isinstance(handler, HTTPRedirectHandler)
        assert handler.redirect_request(None, None, 302, "redirect", {}, "https://untrusted") is None
        return Opener()
    monkeypatch.setattr("urllib.request.build_opener", build)
    response = _cloudflare_post_json("https://api.cloudflare.com/test", {"stream": False}, 120,
                                    {"Authorization": "Bearer " + TOKEN})
    assert response["result"]["response"] == "answer"
    assert captured[0].get_method() == "POST"
