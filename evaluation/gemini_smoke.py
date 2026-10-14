"""One explicitly authorized authenticated VPN diagnostic; no raw SDK logging."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

from evaluation import local_acceptance

FINISH_REASONS = {"STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "LANGUAGE",
                  "OTHER", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
                  "MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "MALFORMED_RESPONSE"}
BLOCKED_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
OUTPUT = Path("artifacts/evaluation/knowvia-gemini-vpn-diagnostic.json")
EXPERIMENT_OUTPUT = Path("artifacts/evaluation/knowvia-gemini-vpn-low-1024-experiment.json")
PROFILES = {
    "high-reference": {"thinking_level": "high", "evidence_output_limit": 320, "answer_output_limit": 220},
    "experimental-low-1024": {"thinking_level": "low", "evidence_output_limit": 1024, "answer_output_limit": 1024},
}


def configure_profile(provider, *, evidence, profile):
    """Explicit test-instance configuration only; production defaults stay untouched."""
    settings = PROFILES[profile]
    provider.thinking_level = settings["thinking_level"]
    provider._max_output_tokens = settings["evidence_output_limit" if evidence else "answer_output_limit"]
    return provider


class DiagnosticModels:
    """Observe SDK metadata only; preserve the provider's ordinary exceptions."""

    def __init__(self, delegate, records, purpose):
        self.delegate, self.records, self.purpose = delegate, records, purpose

    def generate_content(self, **kwargs):
        # Never issue another call after a transport/response failure.
        if any(r.get("error_category") for r in self.records):
            raise RuntimeError("Diagnostic inference stopped after failure")
        record = {"purpose": self.purpose, "http_status": None, "finish_reason": None,
                  "token_usage": {}, "error_category": None}
        self.records.append(record)
        start = perf_counter()
        try:
            response = self.delegate.generate_content(**kwargs)
            candidates = getattr(response, "candidates", None)
            if candidates and len(candidates) == 1:
                finish = getattr(candidates[0], "finish_reason", None)
                finish = getattr(finish, "value", finish)
                record["finish_reason"] = finish if finish in FINISH_REASONS else None
            usage = getattr(response, "usage_metadata", None)
            for field in ("prompt_token_count", "candidates_token_count",
                          "thoughts_token_count", "total_token_count"):
                value = getattr(usage, field, None)
                if type(value) is int and value >= 0:
                    record["token_usage"][field] = value
            sdk_response = getattr(response, "sdk_http_response", None)
            status = getattr(sdk_response, "status_code", None)
            if type(status) is int and 100 <= status <= 599:
                record["http_status"] = status
            feedback = getattr(response, "prompt_feedback", None)
            block = getattr(feedback, "block_reason", None)
            block = getattr(block, "value", block)
            if block and block != "BLOCK_REASON_UNSPECIFIED":
                record["error_category"] = "blocked"
            elif record["finish_reason"] == "MAX_TOKENS":
                record["error_category"] = "incomplete_output"
            elif record["finish_reason"] in BLOCKED_REASONS:
                record["error_category"] = "blocked"
            elif not candidates or len(candidates) != 1 or record["finish_reason"] != "STOP":
                record["error_category"] = "invalid_response"
            else:
                parts = getattr(getattr(candidates[0], "content", None), "parts", None)
                if not parts:
                    record["error_category"] = "empty_response"
                elif any(any(getattr(p, f, None) is not None for f in (
                    "function_call", "function_response", "inline_data", "file_data",
                    "executable_code", "code_execution_result")) for p in parts):
                    record["error_category"] = "invalid_response"
                else:
                    visible = [p for p in parts if not getattr(p, "thought", False)]
                    if any(not isinstance(getattr(p, "text", None), str) for p in visible):
                        record["error_category"] = "invalid_response"
                    elif not any(p.text.strip() for p in visible):
                        record["error_category"] = "empty_response"
            return response
        except Exception as error:
            code = getattr(error, "code", None)
            if type(code) is int and 100 <= code <= 599:
                record["http_status"] = code
            record["error_category"] = (
                "quota" if code == 429 else "authentication" if code in (401, 403) else
                "billing" if code == 402 else "invalid_request" if code == 400 else
                "api_access" if code == 404 else
                "timeout" if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower() else
                "transport")
            raise
        finally:
            record["elapsed_seconds"] = round(perf_counter() - start, 3)


def sanitized_report(payload, records, profile="high-reference"):
    rows = payload.get("cases", [])
    row = rows[0] if rows else {}
    errors = [r["error_category"] for r in records if r.get("error_category")]
    return {
        "status": "PROVIDER_FAILURE" if errors else "COMPLETED" if rows else "RUNNING",
        "request_completed": bool(rows), "http_status": row.get("http_status"),
        "provider": "gemini", "model": "gemini-3.8-flash", "profile": profile,
        **PROFILES[profile], "timeout_seconds": 120,
        "evidence_temperature": 0.0, "answer_temperature": 0.1,
        "gemini_inference_call_count": len(records), "error_category": errors[0] if errors else None,
        "calls": records, "route": row.get("route"), "evidence_decision": row.get("evidence_decision"),
        "supporting_ids": row.get("supporting_ids", []), "presentation": row.get("kind"),
        "final_answer": row.get("text") if rows else None,
        "sources": row.get("sources", []), "latency_seconds": row.get("latency_seconds"),
        "gemini_inference_latency_seconds": round(sum(r.get("elapsed_seconds", 0) for r in records), 3),
        "supporting_chunk_ids": [r["chunk_id"] for r in row.get("supporting_results", []) if "chunk_id" in r],
        "acceptance_checks": row.get("grade", {}),
        "tool_executions": row.get("executions", 0), "index_unchanged": payload.get("index_unchanged"),
        "quota_status": "Project free quota/billing tier not exposed by model metadata",
        "limitations": ["Inference count excludes unchanged Gemini routing calls.",
                         "A safe HTTP abstention after provider failure is not a quality result."]}


def run_smoke(output=None, profile="high-reference"):
    if profile not in PROFILES:
        raise ValueError("Unknown diagnostic configuration profile")
    if output is None:
        output = EXPERIMENT_OUTPUT if profile == "experimental-low-1024" else OUTPUT
    from dotenv import load_dotenv
    load_dotenv(".env")
    from knowvia import cli
    if Path(output).exists():
        raise ValueError("Refusing to overwrite an existing diagnostic artifact")
    local_acceptance.validate_candidate_output(output, "gemini")
    matrix, scenarios = local_acceptance.load_matrix()
    selected = [s for s in scenarios if s["id"] == "natural-vpn"]
    assert len(selected) == 1 and len(selected[0]["turns"]) == 1
    records = []
    original_factory, original_serializer = cli._create_gemini_generator, local_acceptance.safe_artifact
    def factory(*, evidence):
        provider = configure_profile(original_factory(evidence=evidence), evidence=evidence, profile=profile)
        provider._client = SimpleNamespace(models=DiagnosticModels(
            provider._client.models, records, "evidence_or_repair" if evidence else "answer"),
            sdk_client=provider._client)
        return provider
    def serialize(payload, secret_values):
        return original_serializer(sanitized_report(payload, records, profile), secret_values)
    settings = {"KNOWVIA_GENERATION_PROVIDER": "gemini", "KNOWVIA_EVIDENCE_EVALUATOR": "gemini",
                "GEMINI_GENERATION_MODEL": "gemini-3.8-flash", "GEMINI_EVIDENCE_MODEL": "gemini-3.8-flash",
                "GEMINI_INFERENCE_TIMEOUT_SECONDS": "120"}
    # Only console logging is disabled; allowlisted diagnostic records are retained.
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with patch.dict(os.environ, settings), patch.object(cli, "_create_gemini_generator", factory), \
             patch.object(local_acceptance, "load_matrix", lambda *a, **k: (matrix, selected)), \
             patch.object(local_acceptance, "safe_artifact", serialize):
            payload = local_acceptance.run_live(output=Path(output), candidate="gemini")
        return sanitized_report(payload, records, profile)
    finally:
        logging.disable(previous_logging)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="high-reference")
    args = parser.parse_args()
    print(json.dumps(run_smoke(profile=args.profile), indent=2))
