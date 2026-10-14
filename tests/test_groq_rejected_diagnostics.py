import json
from evaluation.groq_safeguards import unvalidated_visible_fields
from evaluation.local_acceptance import safe_artifact


def test_rejected_visible_fields_are_non_authoritative_and_allowlisted():
    raw = json.dumps({"requested_claim": "Documented procedure", "conditions": ["If it continues"],
                      "directly_supported_facts": ["Contact the desk"], "supporting_evidence_ids": [1],
                      "decision": "SUFFICIENT", "reasoning": "hidden", "headers": "private"})
    result = unvalidated_visible_fields(raw)
    assert result["validated"] is False
    assert result["conditions"] == ["If it continues"]
    assert "hidden" not in json.dumps(result) and "private" not in json.dumps(result)


def test_visible_diagnostics_are_bounded_and_secret_redaction_still_applies():
    secret = "fictional-diagnostic-credential"
    result = unvalidated_visible_fields(json.dumps({"requested_claim": secret,
        "conditions": ["x" * 3000] * 30, "supporting_evidence_ids": [True]}))
    assert len(result["conditions"]) == 20 and len(result["conditions"][0]) == 2000
    assert "supporting_evidence_ids" not in result
    assert secret not in safe_artifact(result, [secret])
    assert unvalidated_visible_fields("not JSON") == {}


def test_resume_clears_stale_stop_metadata_but_preserves_archived_reason():
    from evaluation.groq_safeguards import resume_payload
    payload = {"protected_hashes": {}, "scenario_order": ["a"], "completed_scenarios": [],
               "active_scenario": "a", "cases": [{"id": "a/1"}], "in_flight": None,
               "stop_reason": "daily_budget", "stopped_after_provider_failure": True,
               "budget_state": {"stopped": "daily_budget"}}
    resume_payload(payload, [{"id": "a", "turns": [{}]}], {}, approve_replay=True)
    assert payload["stop_reason"] is None and not payload["stopped_after_provider_failure"]
    assert payload["interrupted_attempts"][0]["stop_reason"] == "daily_budget"
