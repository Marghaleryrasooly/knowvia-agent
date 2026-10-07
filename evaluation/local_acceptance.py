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


def run_live(output=OUTPUT, matrix_path=MATRIX):
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
    if config.generation_provider != "ollama" or config.evidence_provider != "ollama":
        raise ValueError("This LOCAL reference requires configured Ollama generation and evidence")
    matrix, scenarios = load_matrix(matrix_path)
    secret_values = [v for k, v in os.environ.items() if any(word in k for word in ("TOKEN", "KEY", "SECRET", "PASSWORD"))]
    index_hash = hashlib.sha256(DEFAULT_INDEX.read_bytes()).hexdigest()
    embedding = create_embedder(config.embedding_provider)
    generator = create_generator(config.generation_provider)
    evaluator = create_evidence_evaluator(config.evidence_provider)
    if evaluator.generator._response_schema is not None:
        raise ValueError("Normal reference must not enable native structured output")
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
                    "evidence_temperature": evaluator.generator._temperature, "evidence_token_limit": evaluator.generator._max_output_tokens,
                    "answer_temperature": generator._temperature, "answer_token_limit": generator._max_output_tokens,
                    "structured_output": False, "action_backend": "isolated counting local tool", "authenticated_http": True},
        "deterministic": None, "cases": [],
        "limitations": ["Required-fact grading is deterministic, not a complete semantic entailment judge.",
                        "Source precision uses policy-grounded expected source sets; extra citations fail strict acceptance.",
                        "Live route uses current configured router, including Gemini when selected; inference is local Ollama.",
                        "Latency includes routing, retrieval, evidence/repair and answer generation; authentication setup is excluded."]}
    current = {}

    class RecordingGenerator:
        def __init__(self, delegate, evidence=False): self.delegate, self.evidence = delegate, evidence
        @property
        def model_name(self): return self.delegate.model_name
        def generate(self, system_prompt, user_prompt):
            purpose = ("repair" if "final consistency validator" in system_prompt else "evidence") if self.evidence else "answer"
            call = {"purpose": purpose, "system_prompt_sha256": hashlib.sha256(system_prompt.encode()).hexdigest(),
                    "user_prompt_sha256": hashlib.sha256(user_prompt.encode()).hexdigest()}
            current.setdefault("provider_calls", []).append(call)
            start = perf_counter()
            try:
                raw = self.delegate.generate(system_prompt, user_prompt)
                call["raw_output"] = raw
                if self.evidence:
                    try: call["parsed_decision"] = parse_evidence_decision(raw, len(current.get("retrieved", []))).status.value
                    except ValueError as error: call["validation_error"] = str(error)
                return raw
            except Exception as error:
                call["transport_error_type"] = type(error).__name__
                raise
            finally: call["latency_seconds"] = round(perf_counter() - start, 3)

    class RecordingEvaluator:
        @property
        def evaluator_name(self): return evaluator.evaluator_name
        def evaluate(self, question, results):
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
                    if result.get("action"):
                        row["action_presentation"] = {k: result["action"].get(k) for k in ("status", "title", "description")}
                    if result.get("ticket"):
                        row["ticket"] = result["ticket"]
                    row["grade"] = grade(turn["expect"], row)
                    row["grade"]["pass"] &= response.status_code == 200
                    payload["cases"].append(row)
                    payload["metrics"] = metrics(payload["cases"])
                    output.write_text(safe_artifact(payload, secret_values), encoding="utf-8")
                    print("  ", row["kind"], "PASS" if row["grade"]["pass"] else "FAIL", row["latency_seconds"], flush=True)
    payload["index_unchanged"] = hashlib.sha256(DEFAULT_INDEX.read_bytes()).hexdigest() == index_hash
    payload["status"] = "ACCEPTED AS REFERENCE" if payload["index_unchanged"] and not any(c.get("transport_error_type") for r in payload["cases"] for c in r["provider_calls"]) else "INVALID TEST RUN"
    output.write_text(safe_artifact(payload, secret_values), encoding="utf-8")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly authorize local Ollama and configured routing calls")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--deterministic-report", type=Path)
    args = parser.parse_args()
    if args.live:
        payload = run_live(args.output)
    else:
        payload = json.loads(args.output.read_text(encoding="utf-8"))
    if args.deterministic_report:
        payload["deterministic"] = junit_summary(args.deterministic_report)
        if payload["deterministic"]["failed"]: payload["status"] = "INVALID TEST RUN"
        args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "metrics": payload["metrics"], "deterministic": payload["deterministic"]}, indent=2))


if __name__ == "__main__":
    main()
