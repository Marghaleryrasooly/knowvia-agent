"""Test-only authenticated API baseline. Never imported by the application.

Run with: python -m evaluation.local_acceptance --live
The unchanged application runtime is instrumented, not reimplemented. Runtime
state is temporary; external action tools alone are replaced by a local tool.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
from urllib.request import urlopen
import xml.etree.ElementTree as ET

from evaluation.groq_safeguards import (
    EvaluationStop, InferenceBudget, atomic_write, extended_metrics,
    evaluation_lock, paired_comparison, protected_hashes, resume_payload, sanitized_diagnostics, visible_fields,
)

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "evaluation/knowvia_local_acceptance_v1.json"
OUTPUT = ROOT / "artifacts/evaluation/knowvia-local-acceptance-baseline.json"


def text_normalized(text):
    return " ".join(re.sub(r"[^\w]+", " ", text.casefold()).split())


def load_matrix(path=MATRIX):
    matrix = json.loads(path.read_text(encoding="utf-8"))
    existing = json.loads((ROOT / matrix["existing_dataset"]).read_text(encoding="utf-8"))
    by_id = {case["id"]: case for case in existing["cases"]}
    scenarios = []
    for identifier in matrix["existing_case_ids"]:
        case = by_id[identifier]
        scenarios.append({"id": identifier, "regression": identifier == "kp-017", "turns": [{
            "input": case["question"], "expect": {
                "kind": "KNOWLEDGE" if case["expected_behavior"] == "ANSWER" else "ABSTENTION",
                "sources": [[source["source"], source["section"]] for source in case["expected_sources"]],
                "criteria": case["answer_criteria"],
                "forbidden": case.get("forbidden_claims", []) + case.get("forbidden_terms", []),
            },
        }]})
    for scenario in matrix["scenarios"]:
        scenarios.append({**scenario, "turns": [{**turn, "expect": matrix["rubrics"][turn["rubric"]]} for turn in scenario["turns"]]})
    ids = [scenario["id"] for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate acceptance scenario IDs")
    return matrix, scenarios


def grade(expect, row):
    text = text_normalized(row["text"])
    facts_ok = all(any(text_normalized(option) in text for option in group) for group in expect.get("facts", []))
    for criterion in expect.get("criteria", []):
        values = [text_normalized(v) for v in criterion["values"]]
        kind = criterion["type"]
        if kind == "contains_all": ok = all(v in text for v in values)
        elif kind == "contains_any": ok = any(v in text for v in values)
        elif kind == "number": ok = any(re.search(rf"(?<!\w){re.escape(v)}(?!\w)", text) for v in values)
        elif kind == "ordered_terms":
            positions = [text.find(v) for v in values]
            ok = all(p >= 0 for p in positions) and positions == sorted(positions)
        else: raise ValueError("Unknown grading criterion")
        facts_ok &= ok
    forbidden_ok = not any(text_normalized(v) in text for v in expect.get("forbidden", []))
    expected = {tuple(v) for v in expect["sources"]}
    cited = {(s["source"], s["section"]) for s in row["sources"]}
    supporting = {(s["source"], s["section"]) for s in row.get("supporting_results", [])}
    kind_ok = row["kind"] == expect["kind"]
    answer_ok = kind_ok and facts_ok and forbidden_ok
    source_ok = cited == expected
    citation_validation = cited == supporting
    denied_ok = not expect.get("denied") or row.get("permission_denied", False)
    action_ok = row["executions"] == row["expected_executions"]
    scope_ok = all(r["source"] in row.get("source_allowlist", []) for r in row.get("retrieved", []))
    generation_gate = not any(c["purpose"] == "answer" for c in row.get("provider_calls", [])) if row.get("evidence_decision") == "INSUFFICIENT" else True
    return {
        "answer_or_behavior_pass": answer_ok,
        "source_precision": len(cited & expected) / len(cited) if cited else None,
        "correct_source_count": len(cited & expected), "cited_source_count": len(cited),
        "source_set_pass": source_ok, "citations_match_validated_support": citation_validation,
        "permission_pass": denied_ok, "action_execution_pass": action_ok,
        "scope_pass": scope_ok, "insufficient_no_generation_pass": generation_gate,
        "pass": all((answer_ok, source_ok, citation_validation, denied_ok, action_ok, scope_ok, generation_gate)),
    }


def metrics(rows):
    supported = [r for r in rows if r["expected"]["kind"] == "KNOWLEDGE"]
    negatives = [r for r in rows if r["expected"]["kind"] == "ABSTENTION"]
    regressions = [r for r in rows if r["regression"]]
    citations = sum(r["grade"]["cited_source_count"] for r in rows)
    return {
        "passed": sum(r["grade"]["pass"] for r in rows), "total": len(rows),
        "supported_question_success": sum(r["grade"]["answer_or_behavior_pass"] for r in supported) / len(supported) if supported else None,
        "supported_total": len(supported),
        "unsupported_abstention_precision": sum(r["kind"] == "ABSTENTION" for r in negatives) / len(negatives) if negatives else None,
        "unsupported_total": len(negatives),
        "unsafe_answers": sum(r["kind"] == "KNOWLEDGE" for r in negatives),
        "supporting_source_precision": sum(r["grade"]["correct_source_count"] for r in rows) / citations if citations else None,
        "average_live_latency_seconds": sum(r["latency_seconds"] for r in rows) / len(rows) if rows else None,
        "regression_passed": sum(r["grade"]["pass"] for r in regressions), "regression_total": len(regressions),
        "validation_failures": sum(any(c.get("validation_error") for c in r["provider_calls"]) for r in rows),
        "repair_attempts": sum(c["purpose"] == "repair" for r in rows for c in r["provider_calls"]),
    }


def junit_summary(path):
    tree = ET.parse(path)
    cases = tree.findall(".//testcase")
    failed = sum(c.find("failure") is not None or c.find("error") is not None for c in cases)
    skipped = sum(c.find("skipped") is not None for c in cases)
    try:
        report = str(path.resolve().relative_to(ROOT))
    except ValueError:
        report = path.name  # Do not expose unrelated machine-specific paths.
    return {"passed": len(cases) - failed - skipped, "failed": failed, "skipped": skipped, "report": report}


def safe_artifact(payload, secret_values):
    serialized = json.dumps(payload, indent=2, ensure_ascii=False)
    for value in secret_values:
        if value and len(value) >= 8:
            serialized = serialized.replace(value, "[REDACTED]")
    return serialized


def validate_candidate_output(output, candidate):
    if candidate not in {"ollama", "gemini", "groq"}:
        raise ValueError("Acceptance candidate is invalid")
    if candidate != "ollama" and Path(output).resolve() == OUTPUT.resolve():
        raise ValueError("Candidate must not overwrite the Ollama reference")


def run_live(output=OUTPUT, matrix_path=MATRIX, candidate="ollama", *,
             batch_scenarios=5, min_call_interval_seconds=65, minute_token_budget=6000,
             daily_token_budget=150000, resume=False, approve_replay_incomplete=False):
    validate_candidate_output(output, candidate)
    if candidate == "groq":
        with evaluation_lock(output):
            return _run_live(output, matrix_path, candidate, batch_scenarios=batch_scenarios,
                min_call_interval_seconds=min_call_interval_seconds, minute_token_budget=minute_token_budget,
                daily_token_budget=daily_token_budget, resume=resume,
                approve_replay_incomplete=approve_replay_incomplete)
    return _run_live(output, matrix_path, candidate)


def _run_live(output=OUTPUT, matrix_path=MATRIX, candidate="ollama", *,
              batch_scenarios=5, min_call_interval_seconds=65, minute_token_budget=6000,
              daily_token_budget=150000, resume=False, approve_replay_incomplete=False):
    output = Path(output)
    validate_candidate_output(output, candidate)
    if candidate == "groq" and output.exists() and not resume:
        raise ValueError("Refusing to overwrite an existing Groq candidate artifact")
    if resume and (candidate != "groq" or not output.exists()):
        raise ValueError("Resume requires an existing Groq checkpoint")
    if approve_replay_incomplete and not resume:
        raise ValueError("Replay approval requires --resume")
    if batch_scenarios < 1:
        raise ValueError("Batch size must be positive")
    saved = json.loads(output.read_text(encoding="utf-8")) if resume else None
    hashes = protected_hashes(ROOT) if candidate == "groq" else {}
    if not resume and output.resolve().is_relative_to(ROOT) and str(output.resolve().relative_to(ROOT)).replace("\\", "/") in hashes:
        raise ValueError("Output is a protected reference artifact")
    if resume and output.resolve().is_relative_to(ROOT):
        hashes.pop(str(output.resolve().relative_to(ROOT)).replace("\\", "/"), None)
    # Imports deliberately stay inside the explicit live entry point.
    from starlette.testclient import TestClient
    import knowvia.web as web
    from knowvia.cli import create_embedder, create_evidence_evaluator, create_generator, DEFAULT_INDEX
    from knowvia.deployment import ApplicationEnvironment, DeploymentConfig
    from knowvia.runtime_paths import RuntimePaths
    from knowvia.retrieval import Retriever
    from knowvia.vector_store import JsonVectorStore
    from knowvia.conversation import SqliteConversationStore
    from knowvia.confirmation import SqlitePendingActionStore
    from knowvia.agent_tools import LocalITTicketTool
    from knowvia.accounts import RegistrationRequest
    from knowvia.employee_directory import demo_employee_records
    from knowvia.evidence import parse_evidence_decision

    config = DeploymentConfig.from_environment()
    if config.generation_provider != candidate or config.evidence_provider != candidate:
        raise ValueError("Acceptance candidate must match both configured inference providers")
    matrix, scenarios = load_matrix(matrix_path)
    secret_values = [v for k, v in os.environ.items() if any(word in k for word in ("TOKEN", "KEY", "SECRET", "PASSWORD"))]
    index_hash = hashlib.sha256(DEFAULT_INDEX.read_bytes()).hexdigest()
    if candidate == "groq":
        reference = json.loads(OUTPUT.read_text(encoding="utf-8"))
        input_hashes = {"matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
                        "existing_dataset_sha256": hashlib.sha256((ROOT / matrix["existing_dataset"]).read_bytes()).hexdigest(),
                        "index_sha256": index_hash}
        if any(reference.get(key) != value for key, value in input_hashes.items()):
            raise ValueError("Evaluation inputs do not match the saved Llama reference")
    embedding = create_embedder(config.embedding_provider)
    generator = create_generator(config.generation_provider)
    evaluator = create_evidence_evaluator(config.evidence_provider)
    if getattr(evaluator.generator, "_response_schema", None) is not None:
        raise ValueError("Normal reference must not enable native structured output")
    model_metadata = []
    if candidate == "ollama":
        try:
            with urlopen(evaluator.generator._host + "/api/tags", timeout=5) as response:
                model_metadata = [{k: m.get(k) for k in ("name", "digest", "details")} for m in json.load(response)["models"] if m["name"] == evaluator.generator.model_name]
        except Exception:
            raise RuntimeError("Local Ollama model metadata unavailable") from None
    payload = {
        "schema_version": 1, "status": "RUNNING", "created_at": datetime.now(timezone.utc).isoformat(),
        "matrix_name": matrix["name"], "matrix_version": matrix["version"],
        "matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
        "existing_dataset_sha256": hashlib.sha256((ROOT / matrix["existing_dataset"]).read_bytes()).hexdigest(),
        "index_sha256": index_hash,
        "runtime": {"embedding_provider": config.embedding_provider, "embedding_model": embedding.model_name,
                    "generation_provider": config.generation_provider, "evidence_provider": config.evidence_provider,
                    "router_provider": config.router_provider, "ollama_models": model_metadata,
                    "evidence_model": evaluator.generator.model_name, "answer_model": generator.model_name,
                    "thinking_level": getattr(evaluator.generator, "thinking_level", None),
                    "evidence_reasoning_effort": getattr(evaluator.generator, "reasoning_effort", None),
                    "answer_reasoning_effort": getattr(generator, "reasoning_effort", None),
                    "evidence_temperature": evaluator.generator._temperature, "evidence_token_limit": evaluator.generator._max_output_tokens,
                    "answer_temperature": generator._temperature, "answer_token_limit": generator._max_output_tokens,
                    "structured_output": False, "action_backend": "isolated counting local tool", "authenticated_http": True},
        "deterministic": None, "cases": [],
        "limitations": ["Required-fact grading is deterministic, not a complete semantic entailment judge.",
                        "Source precision uses policy-grounded expected source sets; extra citations fail strict acceptance.",
                        "Live route uses the unchanged configured router; inference uses the explicitly selected candidate.",
                        "Latency includes routing, retrieval, evidence/repair and answer generation; authentication setup is excluded."]}
    all_scenarios = scenarios
    if candidate == "groq":
        payload.update(protected_hashes=hashes, scenario_order=[s["id"] for s in scenarios],
                       completed_scenarios=[], active_scenario=None, in_flight=None,
                       measurement_status="RUNNING", provider_acceptance="NOT_EVALUATED",
                       pacing={"min_call_interval_seconds": min_call_interval_seconds,
                               "minute_token_budget": minute_token_budget,
                               "daily_token_budget": daily_token_budget})
        if saved:
            if saved["runtime"] != payload["runtime"] or saved["pacing"] != payload["pacing"]:
                raise ValueError("Runtime or pacing configuration changed; refusing resume")
            pending = resume_payload(saved, scenarios, hashes, approve_replay_incomplete)
            payload = saved
            scenarios = pending
        scenarios = scenarios[:batch_scenarios]
    current = {}
    budget = InferenceBudget(min_call_interval_seconds, minute_token_budget, daily_token_budget,
                             state=payload.get("budget_state")) if candidate == "groq" else None
    if budget:
        payload["budget_state"] = budget.state

    def checkpoint():
        if candidate == "groq":
            payload["incomplete_scenario"] = {"id": payload["active_scenario"], "state": "INCOMPLETE"} if payload["active_scenario"] else None
            for relative, expected_hash in hashes.items():
                path = ROOT / relative
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                    budget.stop("protected_artifact_changed")
                    raise EvaluationStop("protected_artifact_changed")
            payload["groq_metrics"] = extended_metrics(payload["cases"])
            prior_rows = [r for attempt in payload.get("interrupted_attempts", []) for r in attempt["cases"]]
            payload["groq_metrics_all_attempts"] = extended_metrics(prior_rows + payload["cases"])
            payload["budget_accounting"] = {"attempted_calls_all_attempts": len(budget.state["entries"]),
                "charged_tokens_including_unknown_reservations": sum(e["tokens"] for e in budget.state["entries"]),
                "unknown_usage_reservations": sum(not e["usage_known"] for e in budget.state["entries"])}
            reference = json.loads(OUTPUT.read_text(encoding="utf-8"))
            payload["paired_comparison"] = paired_comparison(payload["cases"], reference)
        atomic_write(output, safe_artifact(payload, secret_values))

    class RecordingGenerator:
        def __init__(self, delegate, evidence=False): self.delegate, self.evidence = delegate, evidence
        @property
        def model_name(self): return self.delegate.model_name
        def generate(self, system_prompt, user_prompt):
            purpose = ("repair" if "final consistency validator" in system_prompt else "evidence") if self.evidence else "answer"
            from knowvia.condition_preservation import CONDITION_VALIDATION_SYSTEM_PROMPT, parse_condition_verdict
            if system_prompt == CONDITION_VALIDATION_SYSTEM_PROMPT:
                purpose = "condition_validation"
            call = {"purpose": purpose, "system_prompt_sha256": hashlib.sha256(system_prompt.encode()).hexdigest(),
                    "user_prompt_sha256": hashlib.sha256(user_prompt.encode()).hexdigest()}
            entry = None
            if budget:
                try:
                    wait_before = budget.wait_seconds
                    entry = budget.before(system_prompt, user_prompt, self.delegate._max_output_tokens)
                    current["pacing_wait_seconds"] = current.get("pacing_wait_seconds", 0) + budget.wait_seconds - wait_before
                except EvaluationStop:
                    checkpoint()
                    raise
                call["attempted"] = True
            current.setdefault("provider_calls", []).append(call)
            if budget:
                payload["in_flight"] = {"scenario": payload["active_scenario"], "turn": current.get("turn_id"),
                                        "provider_calls": current["provider_calls"]}
                checkpoint()  # Durable reservation before the network request.
            start = perf_counter()
            try:
                raw = self.delegate.generate(system_prompt, user_prompt)
                if candidate == 'groq' and purpose == 'answer' and isinstance(raw, str):
                    # Visible answer text only; safe_artifact redacts configured
                    # secrets. Never persist provider envelopes or reasoning.
                    call['visible_proposed_answer'] = raw[:16000]
                if candidate != "groq":
                    call["raw_output"] = raw
                if purpose == "condition_validation":
                    try:
                        count = len(json.loads(user_prompt)['conditions'])
                        call['condition_preservation_pass'] = parse_condition_verdict(raw, count)
                        if not call['condition_preservation_pass']:
                            call['validation_error'] = 'Condition meaning rejected'
                    except ValueError as error:
                        call['validation_error'] = str(error)
                elif self.evidence:
                    try:
                        call["parsed_decision"], fields = visible_fields(raw, len(current.get("retrieved", [])))
                        if candidate == "groq": call["visible_fields"] = fields
                    except ValueError as error:
                        from knowvia.evidence import rejected_evidence_fields
                        call["validation_error"] = str(error)
                        call["rejected_fields"] = rejected_evidence_fields(raw)
                        from evaluation.groq_safeguards import unvalidated_visible_fields
                        call["unvalidated_visible_fields"] = unvalidated_visible_fields(raw)
                return raw
            except Exception as error:
                call["transport_error_type"] = type(error).__name__
                if budget: budget.stop(getattr(error, "category", "transport"))
                raise
            finally:
                call["latency_seconds"] = round(perf_counter() - start, 3)
                if candidate == "groq":
                    call["diagnostics"] = sanitized_diagnostics(self.delegate.last_call)
                    budget.after(entry, call["diagnostics"])
                    checkpoint()

    class RecordingEvaluator:
        def validate_conditions(self, question, answer, conditions, results):
            return evaluator.validate_conditions(question, answer, conditions, results)
        @property
        def evaluator_name(self): return evaluator.evaluator_name
        def evaluate(self, question, results):
            from knowvia.evidence_scope_intent import question_scope_constraint
            constraint = question_scope_constraint(question)
            current['question_scope_constraint'] = constraint.value if constraint else None
            current["retrieval_query"] = question
            current["retrieved"] = [{"chunk_id": r.chunk.id, "evidence_id": i, "source": r.chunk.source, "section": r.chunk.section, "score": r.score} for i, r in enumerate(results, 1)]
            return evaluator.evaluate(question, results)

    class CountingTool(LocalITTicketTool):
        specification = replace(LocalITTicketTool.specification, requires_confirmation=True)
        def __init__(self): super().__init__(); self.executions = 0
        def execute(self, arguments): self.executions += 1; return super().execute(arguments)

    evaluator.generator = RecordingGenerator(evaluator.generator, evidence=True)
    traced_generator = RecordingGenerator(generator)
    tool = CountingTool()

    class RecordingFactory(web.ProductionRuntimeFactory):
        def _load_knowledge_components(self):
            return Retriever(embedding, JsonVectorStore(DEFAULT_INDEX)), traced_generator, RecordingEvaluator()
        def build(self, identity):
            runtime = super().build(identity)
            original = runtime.skill_agent.handle
            def handle(request, *args, **kwargs):
                current["resolved_request"] = request
                result = original(request, *args, **kwargs)
                agent = result.agent_result
                current.update(route=agent.decision.decision.value, skill=result.selected_skill_id,
                               source_allowlist=list(result.knowledge_sources), permission_denied=agent.permission_denied)
                answer = agent.knowledge_result
                if answer is not None:
                    current["evidence_decision"] = answer.evidence_decision.status.value if answer.evidence_decision else None
                    current["supporting_ids"] = list(answer.evidence_decision.supporting_evidence_ids) if answer.evidence_decision else []
                    current["supporting_results"] = [{"chunk_id": r.chunk.id, "source": r.chunk.source, "section": r.chunk.section, "text": r.chunk.text} for r in answer.supporting_results]
                return result
            runtime.skill_agent.handle = handle
            return runtime

    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="knowvia-acceptance-") as temporary, patch.object(web, "create_agent_tool", lambda *a, **k: tool):
        paths = RuntimePaths(Path(temporary))
        isolated = replace(config, environment=ApplicationEnvironment.TEST, secure_cookie=False,
                           development_identity_override=False, seed_demo_employee_directory=True,
                           registration_enabled=True, action_backend="mock", mcp_ticket_backend="mock",
                           real_jira_mutations_enabled=False, hsts_enabled=False)
        factory = RecordingFactory(isolated, paths)
        service = web.KnowviaWebService(factory, conversation_store=SqliteConversationStore(paths.conversations), proposal_store=SqlitePendingActionStore(paths.pending_actions))
        app = web.create_web_app(service, deployment_config=isolated, runtime_paths=paths)
        password = secrets.token_urlsafe(24)
        secret_values.append(password)
        with TestClient(app, base_url="http://testserver") as client:
            records = {r.employee_id: r for r in demo_employee_records()}
            for employee in records.values():
                response = client.post("/api/auth/register", json={"employee_id": employee.employee_id, "work_email": employee.work_email, "password": password, "password_confirmation": password})
                if response.status_code != 201: raise RuntimeError("Isolated registration setup failed")
            active_identity = None
            for scenario in scenarios:
                if candidate == "groq":
                    payload["active_scenario"] = scenario["id"]
                    checkpoint()
                proposals = {}
                before = tool.executions
                for number, turn in enumerate(scenario["turns"], 1):
                    identity = turn.get("identity", scenario.get("identity", "employee-001"))
                    if identity != active_identity:
                        client.post("/api/auth/logout")
                        login = client.post("/api/auth/login", json={"work_email": records[identity].work_email, "password": password})
                        if login.status_code != 200: raise RuntimeError("Isolated login setup failed")
                        secret_values.append(client.cookies.get("knowvia_session", ""))
                        active_identity = identity
                    session = "accept-" + scenario["id"] + "-" + turn.get("session", "main")
                    current.clear()
                    current.update(provider_calls=[], retrieved=[], supporting_results=[], source_allowlist=[])
                    label = f"{scenario['id']}/{number}"
                    current["turn_id"] = label
                    if candidate == "groq":
                        payload["in_flight"] = {"scenario": scenario["id"], "turn": label, "provider_calls": []}
                        checkpoint()  # Also protects interruptions in Gemini routing.
                    print(label, turn.get("input", turn.get("operation")), flush=True)
                    start = perf_counter()
                    operation = turn.get("operation", "chat")
                    if operation == "chat":
                        response = client.post("/api/chat", json={"session_id": session, "message": turn["input"]})
                    else:
                        proposal_id = proposals.get(turn["proposal_from"])
                        response = client.post(f"/api/proposals/{proposal_id or 'unavailable'}/{operation}", json={"session_id": session})
                    result = response.json()
                    if result.get("action"): proposals[number] = result["action"]["proposal_id"]
                    if "route" not in current:
                        current["route"] = {"CLARIFICATION": "CLARIFY", "ABSTENTION": "KNOWLEDGE", "KNOWLEDGE": "KNOWLEDGE"}.get(result.get("kind"), operation.upper() if operation != "chat" else "UNSUPPORTED")
                        current["route_origin"] = "presentation of pre-skill resolution or proposal control"
                    row = {**current, "id": label, "regression": scenario.get("regression", False), "identity": identity,
                           "operation": operation, "question": turn.get("input"), "expected": turn["expect"],
                           "kind": result.get("kind"), "text": result.get("text", ""), "sources": result.get("sources", []),
                           "http_status": response.status_code, "latency_seconds": round(perf_counter() - start, 3),
                           "executions": tool.executions - before, "expected_executions": turn.get("executions", 0),
                           "proposal_created": bool(result.get("action"))}
                    if candidate == "groq":
                        row["http_elapsed_with_pacing_seconds"] = row["latency_seconds"]
                        row["latency_seconds"] = round(max(0, row["latency_seconds"] - current.get("pacing_wait_seconds", 0)), 3)
                    if result.get("action"):
                        row["action_presentation"] = {k: result["action"].get(k) for k in ("status", "title", "description")}
                    if result.get("ticket"):
                        row["ticket"] = result["ticket"]
                    row["grade"] = grade(turn["expect"], row)
                    row["grade"]["pass"] &= response.status_code == 200
                    payload["cases"].append(row)
                    payload["metrics"] = metrics(payload["cases"])
                    if candidate == "groq": payload["in_flight"] = None
                    checkpoint()
                    print("  ", row["kind"], "PASS" if row["grade"]["pass"] else "FAIL", row["latency_seconds"], flush=True)
                    if candidate == "groq" and (budget.state.get("stopped") or response.status_code != 200):
                        payload["status"] = "INVALID TEST RUN"
                        payload["measurement_status"] = "STOPPED"
                        payload["stop_reason"] = budget.state.get("stopped") or "http_failure"
                        payload["stopped_after_provider_failure"] = True
                        payload["index_unchanged"] = hashlib.sha256(DEFAULT_INDEX.read_bytes()).hexdigest() == index_hash
                        checkpoint()
                        return payload
                if candidate == "groq":
                    payload["completed_scenarios"].append(scenario["id"])
                    payload["active_scenario"] = None
                    checkpoint()
    payload["index_unchanged"] = hashlib.sha256(DEFAULT_INDEX.read_bytes()).hexdigest() == index_hash
    payload["status"] = "ACCEPTED AS REFERENCE" if payload["index_unchanged"] and not any(c.get("transport_error_type") for r in payload["cases"] for c in r["provider_calls"]) else "INVALID TEST RUN"
    if candidate == "groq":
        complete = len(payload["completed_scenarios"]) == len(all_scenarios)
        payload["measurement_status"] = "COMPLETE" if complete else "BATCH_COMPLETE"
        payload["status"] = payload["measurement_status"]
        if not payload["index_unchanged"]: payload["measurement_status"] = payload["status"] = "INVALID TEST RUN"
    checkpoint()
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly authorize selected inference and configured routing calls")
    parser.add_argument("--candidate", choices=("ollama", "gemini", "groq"), default="ollama")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--deterministic-report", type=Path)
    parser.add_argument("--batch-scenarios", type=int, default=5)
    parser.add_argument("--min-call-interval-seconds", type=float, default=65)
    parser.add_argument("--minute-token-budget", type=int, default=6000)
    parser.add_argument("--daily-token-budget", type=int, default=150000)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--approve-replay-incomplete", action="store_true")
    args = parser.parse_args()
    validate_candidate_output(args.output, args.candidate)
    if args.live:
        # Candidate selection is process-local; neither .env nor normal defaults change.
        settings = {"KNOWVIA_GENERATION_PROVIDER": args.candidate, "KNOWVIA_EVIDENCE_EVALUATOR": args.candidate}
        with patch.dict(os.environ, settings):
            payload = run_live(args.output, candidate=args.candidate, batch_scenarios=args.batch_scenarios,
                               min_call_interval_seconds=args.min_call_interval_seconds,
                               minute_token_budget=args.minute_token_budget, daily_token_budget=args.daily_token_budget,
                               resume=args.resume, approve_replay_incomplete=args.approve_replay_incomplete)
    else:
        payload = json.loads(args.output.read_text(encoding="utf-8"))
    if args.deterministic_report:
        payload["deterministic"] = junit_summary(args.deterministic_report)
        if payload["deterministic"]["failed"]: payload["status"] = "INVALID TEST RUN"
        atomic_write(args.output, json.dumps(payload, indent=2, ensure_ascii=False))
    print(json.dumps({"status": payload["status"], "metrics": payload["metrics"], "deterministic": payload["deterministic"]}, indent=2))


if __name__ == "__main__":
    main()
