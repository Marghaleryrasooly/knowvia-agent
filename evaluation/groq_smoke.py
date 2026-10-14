"""Single authenticated VPN smoke test. Requires explicit --live authorization.

Uses temporary state and mock-only actions. No providers are called on import.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

from evaluation import local_acceptance

OUTPUT = Path("artifacts/evaluation/knowvia-groq-vpn-smoke.json")


def sanitized_report(payload):
    rows = payload.get("cases", [])
    row = rows[0] if rows else {}
    calls = [{"purpose": c["purpose"], **c.get("diagnostics", {})}
             for c in row.get("provider_calls", [])]
    errors = [c["error_category"] for c in calls if c.get("error_category")]
    return {
        "status": "PROVIDER_FAILURE" if errors else "COMPLETED" if rows else "RUNNING",
        "request_completed": bool(rows), "runtime": payload.get("runtime", {}),
        "http_status": row.get("http_status"), "route": row.get("route"),
        "question": row.get("question"), "evidence_decision": row.get("evidence_decision"),
        "supporting_ids": row.get("supporting_ids", []),
        "supporting_chunk_ids": [r["chunk_id"] for r in row.get("supporting_results", []) if "chunk_id" in r],
        "presentation": row.get("kind"), "final_answer": row.get("text"),
        "sources": row.get("sources", []), "acceptance_checks": row.get("grade", {}),
        "groq_inference_call_count": len(calls), "calls": calls,
        "error_category": errors[0] if errors else None,
        "groq_inference_latency_seconds": round(sum(c.get("elapsed_seconds", 0) for c in calls), 3),
        "latency_seconds": row.get("latency_seconds"), "tool_executions": row.get("executions", 0),
        "index_unchanged": payload.get("index_unchanged"),
        "limitations": ["Inference counts exclude unchanged routing calls.",
                         "Provider failure is not a knowledge-quality result.",
                         "Free-plan status must be checked in the Groq console; this harness never changes billing."]}


def run_smoke(output=OUTPUT):
    from dotenv import load_dotenv
    load_dotenv(".env")
    output = Path(output)
    local_acceptance.validate_candidate_output(output, "groq")
    if output.exists():
        raise ValueError("Refusing to overwrite an existing diagnostic artifact")
    matrix, scenarios = local_acceptance.load_matrix()
    selected = [s for s in scenarios if s["id"] == "natural-vpn"]
    if len(selected) != 1 or len(selected[0]["turns"]) != 1:
        raise ValueError("Expected exactly one VPN acceptance turn")
    serializer = local_acceptance.safe_artifact
    settings = {"KNOWVIA_GENERATION_PROVIDER": "groq", "KNOWVIA_EVIDENCE_EVALUATOR": "groq",
                "GROQ_GENERATION_MODEL": "openai/gpt-oss-120b", "GROQ_EVIDENCE_MODEL": "openai/gpt-oss-120b",
                "GROQ_TIMEOUT_SECONDS": "120", "GROQ_EVIDENCE_REASONING_EFFORT": "medium",
                "GROQ_GENERATION_REASONING_EFFORT": "low", "GROQ_EVIDENCE_MAX_COMPLETION_TOKENS": "2048",
                "GROQ_GENERATION_MAX_COMPLETION_TOKENS": "1024"}
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with patch.dict(os.environ, settings), \
             patch.object(local_acceptance, "load_matrix", lambda *a, **k: (matrix, selected)), \
             patch.object(local_acceptance, "safe_artifact", lambda p, s: serializer(sanitized_report(p), s)):
            payload = local_acceptance.run_live(output=output, candidate="groq", minute_token_budget=6200)
        return sanitized_report(payload)
    finally:
        logging.disable(previous_logging)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(json.dumps(run_smoke(args.output), indent=2))
