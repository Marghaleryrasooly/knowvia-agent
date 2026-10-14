import json
from types import SimpleNamespace

import pytest

from evaluation import groq_smoke, local_acceptance


@pytest.fixture(autouse=True)
def simulated_pacing(monkeypatch):
    from evaluation.groq_safeguards import InferenceBudget
    import knowvia.web as web
    from knowvia.agent_routing import KnowledgeOnlyAgentRouter
    # Provider plumbing tests use an explicit offline knowledge route, not live Gemini.
    monkeypatch.setattr(web, "DeterministicAgentRouter", KnowledgeOnlyAgentRouter)
    now = [1000000.0]
    def sleep(seconds): now[0] += seconds
    monkeypatch.setattr(local_acceptance, "InferenceBudget", lambda *a, **kw: InferenceBudget(
        *a, **kw, clock=lambda: now[0], sleeper=sleep))


def test_groq_candidate_cannot_overwrite_reference_or_existing_artifact(tmp_path):
    with pytest.raises(ValueError, match="overwrite"):
        local_acceptance.validate_candidate_output(local_acceptance.OUTPUT, "groq")
    output = tmp_path / "candidate.json"
    output.write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError, match="overwrite"):
        local_acceptance.run_live(output=output, candidate="groq")
    assert output.read_text() == "existing"


def test_sanitized_report_records_metadata_not_raw_outputs_or_prompts():
    payload = {"runtime": {"generation_provider": "groq"}, "index_unchanged": True,
        "cases": [{"question": "My VPN isn't working. How can I fix it?", "route": "KNOWLEDGE",
        "text": "safe abstention", "provider_calls": [{"purpose": "evidence",
        "raw_output": "private response", "user_prompt": "private evidence",
        "diagnostics": {"error_category": "quota", "http_status": 429, "elapsed_seconds": 1,
                        "finish_reason": None, "token_usage": {}}}]}]}
    report = groq_smoke.sanitized_report(payload)
    assert report["status"] == "PROVIDER_FAILURE" and report["error_category"] == "quota"
    assert report["groq_inference_call_count"] == 1
    assert "private" not in json.dumps(report)


def test_smoke_selects_exactly_one_turn_without_modifying_matrix_or_environment(tmp_path, monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a: None)
    monkeypatch.setenv("KNOWVIA_GENERATION_PROVIDER", "ollama")
    monkeypatch.setenv("KNOWVIA_AGENT_ROUTER", "gemini")
    from os import environ
    original_load = local_acceptance.load_matrix
    original_matrix = local_acceptance.MATRIX.read_bytes()
    observed = []
    def fake_live(output, candidate, minute_token_budget):
        _, scenarios = local_acceptance.load_matrix()
        observed.append(scenarios)
        assert candidate == "groq" and environ["KNOWVIA_AGENT_ROUTER"] == "gemini"
        assert minute_token_budget == 6200
        assert environ["GROQ_EVIDENCE_REASONING_EFFORT"] == "medium"
        assert environ["GROQ_GENERATION_MAX_COMPLETION_TOKENS"] == "1024"
        return {"cases": []}
    monkeypatch.setattr(local_acceptance, "run_live", fake_live)
    groq_smoke.run_smoke(tmp_path / "smoke.json")
    assert len(observed[0]) == 1 and observed[0][0]["id"] == "natural-vpn"
    assert len(observed[0][0]["turns"]) == 1
    assert observed[0][0]["turns"][0]["input"] == "My VPN isn't working. How can I fix it?"
    assert local_acceptance.MATRIX.read_bytes() == original_matrix
    assert local_acceptance.load_matrix is original_load
    assert environ["KNOWVIA_GENERATION_PROVIDER"] == "ollama"


def test_authenticated_acceptance_stops_after_first_provider_failure(tmp_path, monkeypatch):
    # Real authenticated HTTP harness, real retrieval/index and validation;
    # only query embeddings and remote inference replaced with offline fakes.
    import knowvia.cli as cli
    from knowvia.generation import GroqGenerationProvider
    from urllib.error import HTTPError
    matrix, scenarios = local_acceptance.load_matrix()
    selected = [s for s in scenarios if s["id"] == "natural-vpn"]
    monkeypatch.setattr(local_acceptance, "load_matrix", lambda *a: (matrix, selected * 2))
    for key, value in {
        "KNOWVIA_ENVIRONMENT": "test", "KNOWVIA_AUTH_COOKIE_SECURE": "false",
        "KNOWVIA_ALLOW_DEVELOPMENT_IDENTITY_OVERRIDE": "false", "KNOWVIA_AGENT_ROUTER": "deterministic",
        "KNOWVIA_AGENT_TOOL": "mock", "KNOWVIA_MCP_TICKET_BACKEND": "mock",
        "KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS": "false", "KNOWVIA_EMBEDDING_PROVIDER": "local",
        "KNOWVIA_GENERATION_PROVIDER": "groq", "KNOWVIA_EVIDENCE_EVALUATOR": "groq",
        "GROQ_API_KEY": "fictional", "KNOWVIA_ALLOWED_HOSTS": "testserver,localhost,127.0.0.1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "create_embedder", lambda *a: SimpleNamespace(
        model_name="sentence-transformers/all-MiniLM-L6-v2", embed=lambda texts: [[1.0] + [0.0] * 383 for _ in texts]))
    calls = []
    def transport(*args):
        calls.append(1)
        raise HTTPError("https://example.invalid", 429, "private body", {}, None)
    monkeypatch.setattr(cli, "_create_groq_generator", lambda **kw: GroqGenerationProvider(
        "fictional", transport=transport))
    result = local_acceptance.run_live(output=tmp_path / "candidate.json", candidate="groq")
    assert len(calls) == 1 and len(result["cases"]) == 1
    assert result["status"] == "INVALID TEST RUN"
    assert result["stopped_after_provider_failure"]
    assert result["cases"][0]["kind"] == "ABSTENTION"
    assert result["cases"][0]["executions"] == 0
    assert result["cases"][0]["provider_calls"][0]["diagnostics"]["error_category"] == "quota"


def test_authenticated_groq_smoke_success_with_offline_response_fixtures(tmp_path, monkeypatch):
    # This verifies plumbing, not live Groq quality: real auth/API/retrieval/
    # evidence validation/citations, with explicitly constructed model outputs.
    import knowvia.cli as cli
    from knowvia.generation import GroqGenerationProvider
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a: None)
    for key, value in {
        "KNOWVIA_ENVIRONMENT": "test", "KNOWVIA_AUTH_COOKIE_SECURE": "false",
        "KNOWVIA_ALLOW_DEVELOPMENT_IDENTITY_OVERRIDE": "false", "KNOWVIA_AGENT_ROUTER": "deterministic",
        "KNOWVIA_AGENT_TOOL": "mock", "KNOWVIA_MCP_TICKET_BACKEND": "mock",
        "KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS": "false", "KNOWVIA_EMBEDDING_PROVIDER": "local",
        "GROQ_API_KEY": "fictional", "KNOWVIA_ALLOWED_HOSTS": "testserver,localhost,127.0.0.1",
    }.items():
        monkeypatch.setenv(key, value)
    index = json.loads((local_acceptance.ROOT / "data/knowvia_index.json").read_text(encoding="utf-8"))
    vpn = next(c for c in index["chunks"] if c["section"] == "VPN")
    monkeypatch.setattr(cli, "create_embedder", lambda *a: SimpleNamespace(
        model_name=index["embedding_model"], embed=lambda texts: [vpn["embedding"] for _ in texts]))
    calls = []
    def transport(url, payload, timeout, headers):
        calls.append(payload)
        text = json.dumps({"requested_claim": "Documented VPN troubleshooting steps",
            "directly_supported_facts": ["Confirm internet, restart VPN client, re-authenticate with MFA, restart computer, contact IT Service Desk if issue continues."],
            "supporting_evidence_ids": [1], "missing_fact": "", "decision": "SUFFICIENT",
            "answer_scope": "GENERAL", "policy_support": "NOT_APPLICABLE",
            "personal_eligibility": "UNKNOWN", "conditions": []}) if len(calls) == 1 else (
            "Confirm your internet connection, restart the VPN client, re-authenticate using multi-factor authentication, "
            "restart the computer, and contact the IT Service Desk if the issue continues.")
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
    monkeypatch.setattr("knowvia.generation._groq_post_json", transport)
    output = tmp_path / "smoke.json"
    report = groq_smoke.run_smoke(output)
    assert report["status"] == "COMPLETED" and report["evidence_decision"] == "SUFFICIENT"
    assert report["groq_inference_call_count"] == 2 and report["tool_executions"] == 0
    assert report["supporting_chunk_ids"] == [vpn["id"]]
    assert report["sources"][0]["section"] == "VPN"
    assert [c["reasoning_effort"] for c in calls] == ["medium", "low"]
    saved = output.read_text(encoding="utf-8")
    assert "fictional" not in saved and "raw_output" not in saved
    assert "directly_supported_facts" not in saved
