import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation import local_acceptance
from evaluation.groq_safeguards import (
    EvaluationStop, InferenceBudget, atomic_write, extended_metrics,
    evaluation_lock, protected_hashes, resume_payload, sanitized_diagnostics, visible_fields,
)


class Clock:
    def __init__(self): self.now = 1000000.0
    def __call__(self): return self.now
    def sleep(self, seconds): self.now += seconds


def guard(clock, **kw):
    return InferenceBudget(clock=clock, sleeper=clock.sleep, **kw)


def test_start_interval_includes_evidence_answer_and_repair():
    c = Clock(); b = guard(c)
    starts = []
    for purpose in ("evidence", "answer", "repair"):
        e = b.before("system", purpose, 100)
        starts.append(e["start"])
        b.after(e, {"token_usage": {"total_tokens": 40}})
    assert starts == [1000000, 1000065, 1000130]
    assert b.wait_seconds == 130


def test_rolling_minute_reservations_wait_and_reasoning_is_not_double_counted():
    c = Clock(); b = guard(c, interval=0, minute=1500, daily=4000)
    e = b.before("", "", 400)
    b.after(e, {"token_usage": {"prompt_tokens": 800, "completion_tokens": 100,
                               "reasoning_tokens": 90, "total_tokens": 900}})
    assert e["tokens"] == 900
    e2 = b.before("", "", 400)
    assert e2["start"] >= e["start"] + 60
    assert e2["tokens"] == 912  # Unknown usage reserves full allowance.


def test_daily_budget_and_oversized_request_do_not_send():
    c = Clock(); b = guard(c, minute=1000, daily=1100)
    e = b.before("", "", 100)
    with pytest.raises(EvaluationStop, match="daily_budget"): b.before("", "", 100)
    assert len(b.state["entries"]) == 1
    b2 = guard(c, minute=1000)
    with pytest.raises(EvaluationStop, match="minute_budget"): b2.before("x" * 3000, "", 100)
    assert not b2.state["entries"]


def test_long_inference_keeps_completion_usage_in_rolling_window():
    c = Clock(); b = guard(c, interval=0, minute=1500)
    e = b.before("", "", 400)
    c.sleep(120)
    b.after(e, {"token_usage": {"total_tokens": 900}})
    next_entry = b.before("", "", 400)
    assert next_entry["start"] >= e["charged_at"] + 60


@pytest.mark.parametrize("category", ["authentication", "api_access", "billing", "quota", "timeout", "transport", "server", "invalid_response"])
def test_provider_failure_latches_before_any_following_call(category):
    c = Clock(); b = guard(c); e = b.before("s", "u", 100)
    b.after(e, {"error_category": category})
    with pytest.raises(EvaluationStop, match=category): b.before("s", "u", 100)
    assert len(b.state["entries"]) == 1 and not e["usage_known"]


def test_reservation_overrun_stops_and_resume_retains_budget():
    c = Clock(); b = guard(c); e = b.before("", "", 10)
    b.after(e, {"token_usage": {"total_tokens": 1000}})
    resumed = guard(c, state=json.loads(json.dumps(b.state)))
    with pytest.raises(EvaluationStop, match="reservation_exceeded"): resumed.before("", "", 10)


def test_atomic_write_failure_keeps_previous_checkpoint(tmp_path, monkeypatch):
    p = tmp_path / "checkpoint.json"; atomic_write(p, '{"old":true}')
    def fail(*a): raise OSError("offline replace failure")
    monkeypatch.setattr("evaluation.groq_safeguards.os.replace", fail)
    with pytest.raises(OSError): atomic_write(p, '{"new":true}')
    assert json.loads(p.read_text()) == {"old": True}
    assert list(tmp_path.iterdir()) == [p]


def test_concurrent_checkpoint_runner_is_refused(tmp_path):
    p = tmp_path / "candidate.json"
    with evaluation_lock(p):
        with pytest.raises(ValueError, match="locked"):
            with evaluation_lock(p): pass
    assert not Path(str(p) + ".lock").exists()


def test_resume_only_completed_prefix_and_explicit_partial_replay():
    scenarios = [{"id": "a", "turns": [{}]}, {"id": "b", "turns": [{}, {}]}, {"id": "c", "turns": [{}]}]
    p = {"protected_hashes": {"f": "h"}, "scenario_order": ["a", "b", "c"],
         "completed_scenarios": ["a"], "active_scenario": "b", "cases": [{"id": "a/1"}, {"id": "b/1"}],
         "in_flight": {"turn": "b/2"}, "budget_state": {"stopped": "transport"}}
    with pytest.raises(ValueError, match="explicit"): resume_payload(p, scenarios, {"f": "h"})
    assert len(p["cases"]) == 2
    pending = resume_payload(p, scenarios, {"f": "h"}, approve_replay=True)
    assert pending == scenarios[1:]
    assert p["cases"] == [{"id": "a/1"}]
    assert p["interrupted_attempts"][0]["cases"] == [{"id": "b/1"}]
    assert p["interrupted_attempts"][0]["in_flight"]["turn"] == "b/2"
    with pytest.raises(ValueError, match="changed"): resume_payload(p, scenarios, {"f": "other"})


def test_visible_fields_are_validated_and_exclude_extra_reasoning():
    value = {"requested_claim": "Vacation allowance", "directly_supported_facts": ["24 days for full-time employees"],
             "supporting_evidence_ids": [1], "missing_fact": "", "decision": "SUFFICIENT"}
    status, fields = visible_fields(json.dumps(value), 3)
    assert status == "SUFFICIENT" and fields["supporting_evidence_ids"] == [1]
    with pytest.raises(ValueError): visible_fields(json.dumps({**value, "reasoning": "hidden"}), 3)


def test_diagnostics_are_allowlisted_not_arbitrary_provider_metadata():
    result = sanitized_diagnostics({"http_status": 429, "error_category": "quota", "elapsed_seconds": 1.5,
        "authorization": "private", "reasoning": "private", "raw_body": "private",
        "token_usage": {"total_tokens": 100, "reasoning_tokens": 30, "extra": "private"}})
    assert "private" not in json.dumps(result)
    assert result["http_status"] == 429 and result["token_usage"]["total_tokens"] == 100


def test_metrics_exclude_provider_error_abstention_from_false_negatives():
    rows = [{"expected": {"kind": "KNOWLEDGE"}, "evidence_decision": "INSUFFICIENT", "latency_seconds": 2,
             "provider_calls": [{"parsed_decision": "INSUFFICIENT", "latency_seconds": 1, "diagnostics": {"token_usage": {"total_tokens": 30, "completion_tokens": 20, "reasoning_tokens": 10}}}]},
            {"expected": {"kind": "KNOWLEDGE"}, "evidence_decision": "INSUFFICIENT", "latency_seconds": 4,
             "provider_calls": [{"latency_seconds": 3, "diagnostics": {"error_category": "transport"}}]}]
    m = extended_metrics(rows)
    assert m["evidence_false_negatives"] == 1 and m["provider_errors"] == 1
    assert m["transport_errors"] == 1 and m["provider_error_categories"] == {"transport": 1}
    assert m["token_usage"]["total_tokens"] == 30 and m["median_http_latency_seconds"] == 3


@pytest.fixture
def offline_http(monkeypatch):
    import knowvia.cli as cli
    import knowvia.web as web
    from knowvia.agent_routing import KnowledgeOnlyAgentRouter
    monkeypatch.setattr(web, "DeterministicAgentRouter", KnowledgeOnlyAgentRouter)
    from knowvia.generation import GroqGenerationProvider
    matrix, scenarios = local_acceptance.load_matrix()
    selected = [s for s in scenarios if s["id"] in {"natural-vpn", "vacation-exact"}]
    monkeypatch.setattr(local_acceptance, "load_matrix", lambda *a: (matrix, selected))
    for k, v in {"KNOWVIA_ENVIRONMENT": "test", "KNOWVIA_AUTH_COOKIE_SECURE": "false",
        "KNOWVIA_AGENT_ROUTER": "deterministic", "KNOWVIA_AGENT_TOOL": "mock",
        "KNOWVIA_MCP_TICKET_BACKEND": "mock", "KNOWVIA_ENABLE_REAL_JIRA_MUTATIONS": "false",
        "KNOWVIA_EMBEDDING_PROVIDER": "local", "KNOWVIA_GENERATION_PROVIDER": "groq",
        "KNOWVIA_EVIDENCE_EVALUATOR": "groq", "GROQ_API_KEY": "fictional",
        "KNOWVIA_ALLOW_DEVELOPMENT_IDENTITY_OVERRIDE": "false",
        "KNOWVIA_ALLOWED_HOSTS": "testserver,localhost,127.0.0.1"}.items(): monkeypatch.setenv(k, v)
    monkeypatch.setattr(cli, "create_embedder", lambda *a: SimpleNamespace(model_name="sentence-transformers/all-MiniLM-L6-v2",
        embed=lambda texts: [[1.] + [0.] * 383 for _ in texts]))
    calls = []
    def transport(*a):
        calls.append(1)
        scope = 'POLICY_REPORTING' if 'answer_scope must be POLICY_REPORTING' in a[1]['messages'][-1]['content'] else 'GENERAL'
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({
            "requested_claim": "Requested company procedure", "directly_supported_facts": [],
            "supporting_evidence_ids": [], "missing_fact": "Procedure not established", "decision": "INSUFFICIENT",
            "answer_scope": scope, "policy_support": "UNSUPPORTED" if scope == 'POLICY_REPORTING' else "NOT_APPLICABLE",
            "personal_eligibility": "UNKNOWN", "conditions": []})}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
    monkeypatch.setattr(cli, "_create_groq_generator", lambda **kw: GroqGenerationProvider("fictional", transport=transport))
    c = Clock()
    monkeypatch.setattr(local_acceptance, "InferenceBudget", lambda *a, **kw: InferenceBudget(*a, **kw, clock=c, sleeper=c.sleep))
    return calls, selected


def test_authenticated_batches_resume_without_repeated_calls(tmp_path, offline_http):
    calls, selected = offline_http
    before = protected_hashes(local_acceptance.ROOT)
    output = tmp_path / "candidate.json"
    first = local_acceptance.run_live(output, candidate="groq", batch_scenarios=1)
    assert len(calls) == 1 and first["completed_scenarios"] == [selected[0]["id"]]
    assert first["status"] == "BATCH_COMPLETE" and first["provider_acceptance"] == "NOT_EVALUATED"
    second = local_acceptance.run_live(output, candidate="groq", batch_scenarios=1, resume=True)
    assert len(calls) == 2 and len(second["cases"]) == 2 and second["status"] == "COMPLETE"
    assert second["cases"][0]["id"] == first["cases"][0]["id"]
    assert second["cases"][0]["provider_calls"][0]["visible_fields"]["missing_fact"] == "Procedure not established"
    assert "raw_output" not in output.read_text() and "fictional" not in output.read_text()
    third = local_acceptance.run_live(output, candidate="groq", resume=True)
    assert len(calls) == 2 and third["status"] == "COMPLETE"
    assert protected_hashes(local_acceptance.ROOT) == before


def test_changed_matrix_cannot_start_candidate(tmp_path, offline_http):
    calls, _ = offline_http
    changed = tmp_path / "changed-matrix.json"
    changed.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="saved Llama reference"):
        local_acceptance.run_live(tmp_path / "candidate.json", matrix_path=changed, candidate="groq")
    assert not calls


def test_interrupted_http_scenario_is_not_automatically_replayed(tmp_path, offline_http, monkeypatch):
    calls, selected = offline_http
    scenario = {**selected[0], "turns": selected[0]["turns"] * 2}
    matrix, _ = local_acceptance.load_matrix()
    monkeypatch.setattr(local_acceptance, "load_matrix", lambda *a: (matrix, [scenario]))
    import knowvia.generation as generation
    import knowvia.cli as cli
    def transport(*a):
        calls.append(1)
        if len(calls) == 2: raise TimeoutError()
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({
            "requested_claim": "Procedure", "directly_supported_facts": [], "supporting_evidence_ids": [],
            "missing_fact": "Missing procedure", "decision": "INSUFFICIENT",
            "answer_scope": "GENERAL", "policy_support": "NOT_APPLICABLE",
            "personal_eligibility": "UNKNOWN", "conditions": []})}}]}
    monkeypatch.setattr(cli, "_create_groq_generator", lambda **kw: generation.GroqGenerationProvider("fictional", transport=transport))
    p = tmp_path / "candidate.json"
    result = local_acceptance.run_live(p, candidate="groq")
    assert result["measurement_status"] == "STOPPED" and result["active_scenario"] == scenario["id"]
    assert len(result["cases"]) == 2 and not result["completed_scenarios"]
    with pytest.raises(ValueError, match="explicit"): local_acceptance.run_live(p, candidate="groq", resume=True)
    assert len(calls) == 2


def test_default_batch_keeps_five_whole_scenarios(tmp_path, offline_http, monkeypatch):
    calls, selected = offline_http
    matrix, _ = local_acceptance.load_matrix()
    scenarios = [{**selected[0], "id": f"offline-{i}"} for i in range(6)]
    scenarios[0] = {**scenarios[0], "turns": selected[0]["turns"] * 2}
    monkeypatch.setattr(local_acceptance, "load_matrix", lambda *a: (matrix, scenarios))
    result = local_acceptance.run_live(tmp_path / "batch.json", candidate="groq")
    assert result["completed_scenarios"] == [s["id"] for s in scenarios[:5]]
    assert len(result["cases"]) == len(calls) == 6
    assert result["status"] == "BATCH_COMPLETE"


def test_completed_scenario_with_missing_turn_refuses_resume():
    p = {"protected_hashes": {}, "scenario_order": ["a"], "completed_scenarios": ["a"], "cases": []}
    with pytest.raises(ValueError, match="missing or duplicate"):
        resume_payload(p, [{"id": "a", "turns": [{"input": "one"}]}], {})
