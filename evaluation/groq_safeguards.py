"""Offline-testable evaluation safeguards; never imported by application code."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
import time
from contextlib import contextmanager
from collections import Counter


class EvaluationStop(RuntimeError):
    """Ordinary Exception, safe to propagate through the HTTP worker."""


def atomic_write(path, serialized):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            name = stream.name
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


@contextmanager
def evaluation_lock(output):
    """Prevent concurrent evaluation processes from spending against one journal."""
    path = Path(str(output) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError("Evaluation checkpoint is locked; verify no runner is active before manual recovery") from None
    os.close(descriptor)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def protected_hashes(root):
    root = Path(root)
    paths = [root / "evaluation/knowvia_local_acceptance_v1.json",
             root / "evaluation/knowvia_knowledge_paraphrase_eval_v1.json",
             root / "artifacts/evaluation/knowvia-local-acceptance-baseline.json",
             root / "data/knowvia_index.json"]
    paths += list((root / "artifacts/evaluation").glob("knowvia-groq-*.json"))
    paths += list((root / "data").rglob("*.md"))
    paths += list((root / "knowvia_m1_dataset").rglob("*.md"))
    return {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.is_file()}


class InferenceBudget:
    """Serial rolling-window guard. Reservations survive crashes and resumes.

    Prompt reservation uses a conservative estimate (UTF-8 bytes / 3 + 512),
    not an exact GPT-OSS tokenizer. Unknown usage retains the full reservation.
    Daily means a conservative rolling 24 hours, not a guessed provider reset.
    """

    def __init__(self, interval=65, minute=6000, daily=150000, state=None,
                 clock=None, sleeper=None):
        if not math.isfinite(interval) or interval < 0 or type(minute) is not int or type(daily) is not int or minute < 1 or daily < minute:
            raise ValueError("Invalid evaluation pacing/budget configuration")
        self.interval, self.minute, self.daily = interval, minute, daily
        self.clock, self.sleeper = clock or time.time, sleeper or time.sleep
        self.state = state if state is not None else {"entries": [], "last_start": None, "stopped": None}
        self.wait_seconds = 0.0

    def stop(self, category):
        self.state["stopped"] = category

    def before(self, system, user, max_completion):
        if self.state.get("stopped"):
            raise EvaluationStop(self.state["stopped"])
        reservation = math.ceil(len((system + user).encode("utf-8")) / 3) + 512 + max_completion
        if reservation > self.minute:
            self.stop("request_exceeds_minute_budget")
            raise EvaluationStop(self.state["stopped"])
        while True:
            now = self.clock()
            day = [e for e in self.state["entries"] if now - e.get("charged_at", e["start"]) < 86400]
            if sum(e["tokens"] for e in day) + reservation > self.daily:
                self.stop("daily_budget")
                raise EvaluationStop("daily_budget")
            recent = [e for e in day if now - e.get("charged_at", e["start"]) < 60]
            last = self.state.get("last_start")
            delay = max(0, (last if last is not None else -1e20) + self.interval - now)
            if sum(e["tokens"] for e in recent) + reservation > self.minute:
                delay = max(delay, min(e.get("charged_at", e["start"]) for e in recent) + 60.01 - now)
            if delay <= 0:
                break
            self.sleeper(min(delay, 30))
            self.wait_seconds += min(delay, 30)
        entry = {"start": self.clock(), "tokens": reservation, "reservation": reservation,
                 "usage_known": False}
        self.state["entries"].append(entry)
        self.state["last_start"] = entry["start"]
        return entry

    def after(self, entry, diagnostics):
        entry["charged_at"] = self.clock()  # Conservative: completion tokens may be charged late.
        usage = diagnostics.get("token_usage", {})
        total = usage.get("total_tokens")
        if type(total) is not int:
            p, c = usage.get("prompt_tokens"), usage.get("completion_tokens")
            total = p + c if type(p) is int and type(c) is int else None
        if type(total) is int and total >= 0:
            entry.update(tokens=total, usage_known=True)
            if total > entry["reservation"]:
                self.stop("reservation_exceeded")
        error = diagnostics.get("error_category")
        if error:
            self.stop(error)  # All provider failures stop; no new calls after it.


def visible_fields(raw, evidence_count):
    from knowvia.evidence import parse_evidence_decision
    decision = parse_evidence_decision(raw, evidence_count)
    value = json.loads(raw)
    return decision.status.value, {key: value[key] for key in (
        "requested_claim", "directly_supported_facts", "supporting_evidence_ids", "missing_fact",
        "answer_scope", "policy_support", "personal_eligibility", "conditions") if key in value}


def unvalidated_visible_fields(raw):
    """Bounded visible JSON diagnostics only; never grants evidence authority.

    Artifact serialization still redacts configured secrets. No provider envelope,
    prompts, extra fields or hidden reasoning are retained.
    """
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    result = {"validated": False}
    for key in ("requested_claim", "missing_fact", "decision", "answer_scope",
                "policy_support", "personal_eligibility"):
        if isinstance(value.get(key), str):
            result[key] = value[key][:2000]
    for key in ("directly_supported_facts", "conditions"):
        items = value.get(key)
        if isinstance(items, list) and all(isinstance(item, str) for item in items):
            result[key] = [item[:2000] for item in items[:20]]
    ids = value.get("supporting_evidence_ids")
    if isinstance(ids, list) and all(type(item) is int for item in ids):
        result["supporting_evidence_ids"] = ids[:20]
    return result


def sanitized_diagnostics(value):
    usage = value.get("token_usage", {})
    errors = {"configuration", "authentication", "quota", "billing", "invalid_request", "api_access",
              "server", "http", "timeout", "transport", "invalid_response", "incomplete_output",
              "blocked", "empty_response"}
    finish = value.get("finish_reason")
    status = value.get("http_status")
    elapsed = value.get("elapsed_seconds")
    error = value.get("error_category")
    return {"http_status": status if type(status) is int and 100 <= status <= 599 else None,
            "finish_reason": finish if finish in {"stop", "length", "content_filter", "tool_calls", "function_call"} else None,
            "error_category": error if error in errors else "invalid_response" if error else None,
            "elapsed_seconds": elapsed if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0 else None,
            "token_usage": {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")
                            if isinstance(usage, dict) and type(usage.get(k)) is int and usage[k] >= 0}}


def extended_metrics(rows):
    calls = [c for r in rows for c in r.get("provider_calls", []) if c.get("attempted", True)]
    usages = [c.get("diagnostics", {}).get("token_usage", {}) for c in calls]
    totals = {k: sum(u.get(k, 0) for u in usages) for k in (
        "prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")}
    latencies = [c.get("latency_seconds", 0) for c in calls]
    http = [r["latency_seconds"] for r in rows]
    return {"groq_api_calls": len(calls), "token_usage": totals,
            "calls_with_unknown_usage": sum(not u for u in usages),
            "mean_inference_latency_seconds": statistics.mean(latencies) if latencies else None,
            "median_inference_latency_seconds": statistics.median(latencies) if latencies else None,
            "median_http_latency_seconds": statistics.median(http) if http else None,
            "provider_errors": sum(bool(c.get("diagnostics", {}).get("error_category")) for c in calls),
            "transport_errors": sum(c.get("diagnostics", {}).get("error_category") in {"timeout", "transport"} for c in calls),
            "provider_error_categories": dict(Counter(c["diagnostics"]["error_category"] for c in calls
                                                       if c.get("diagnostics", {}).get("error_category"))),
            "evidence_false_negatives": sum(r["expected"]["kind"] == "KNOWLEDGE"
                and r.get("evidence_decision") == "INSUFFICIENT"
                and any(c.get("parsed_decision") == "INSUFFICIENT" for c in r.get("provider_calls", []))
                and not any(c.get("transport_error_type") or c.get("diagnostics", {}).get("error_category")
                            for c in r.get("provider_calls", [])) for r in rows)}


def paired_comparison(rows, reference):
    by_id = {r["id"]: r for r in reference["cases"]}
    return [{"id": r["id"], "reference_pass": by_id[r["id"]]["grade"]["pass"],
             "candidate_pass": r["grade"]["pass"], "reference_kind": by_id[r["id"]]["kind"],
             "candidate_kind": r["kind"], "reference_evidence": by_id[r["id"]].get("evidence_decision"),
             "candidate_evidence": r.get("evidence_decision"),
             "reference_http_seconds": by_id[r["id"]]["latency_seconds"],
             "candidate_http_seconds": r["latency_seconds"],
             "infrastructure_failure": any(c.get("transport_error_type") or c.get("diagnostics", {}).get("error_category")
                                           for c in r.get("provider_calls", []))}
            for r in rows if r["id"] in by_id]


def resume_payload(payload, scenarios, hashes, approve_replay=False):
    if payload.get("protected_hashes") != hashes:
        raise ValueError("Protected inputs/reference artifacts changed; refusing resume")
    if payload.get("scenario_order") != [s["id"] for s in scenarios]:
        raise ValueError("Scenario ordering changed; refusing resume")
    completed = payload.get("completed_scenarios", [])
    if completed != [s["id"] for s in scenarios[:len(completed)]]:
        raise ValueError("Checkpoint is not a completed-scenario prefix")
    for scenario in scenarios[:len(completed)]:
        expected_ids = [f"{scenario['id']}/{i}" for i in range(1, len(scenario["turns"]) + 1)]
        actual_ids = [r["id"] for r in payload["cases"] if r["id"].rsplit("/", 1)[0] == scenario["id"]]
        if actual_ids != expected_ids:
            raise ValueError("Completed scenario has missing or duplicate turns")
    active = payload.get("active_scenario")
    if active and (len(completed) == len(scenarios) or active != scenarios[len(completed)]["id"]):
        raise ValueError("Incomplete scenario is not the next ordered scenario")
    if active:
        if not approve_replay:
            raise ValueError("Incomplete scenario requires explicit --approve-replay-incomplete; no calls replayed")
        partial = [r for r in payload["cases"] if r["id"].rsplit("/", 1)[0] == active]
        payload.setdefault("interrupted_attempts", []).append({"scenario": active, "cases": partial,
            "in_flight": payload.get("in_flight"), "stop_reason": payload.get("stop_reason"),
            "replay_explicitly_approved": True})
        payload["cases"] = [r for r in payload["cases"] if r not in partial]
        payload["active_scenario"] = None
        payload["in_flight"] = None
        payload["budget_state"]["stopped"] = None
    payload["status"] = "RUNNING"
    payload["measurement_status"] = "RUNNING"
    # Archived interruptions remain historical; they are not the resumed run's stop.
    payload["stop_reason"] = None
    payload["stopped_after_provider_failure"] = False
    return scenarios[len(completed):]
