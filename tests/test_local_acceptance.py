from pathlib import Path

from evaluation.local_acceptance import grade, junit_summary, load_matrix, metrics, safe_artifact


def row(kind="KNOWLEDGE", sources=None, text="24 paid days"):
    sources = sources or []
    return {"kind": kind, "text": text, "sources": sources, "supporting_results": sources,
            "executions": 0, "expected_executions": 0, "retrieved": [], "provider_calls": [],
            "latency_seconds": 1, "regression": True}


def test_matrix_covers_exact_cases_and_regressions_without_changing_original_dataset():
    _, scenarios = load_matrix()
    inputs = {t.get("input") for s in scenarios for t in s["turns"]}
    assert {"My VPN isn't working. How can I fix it?", "What documented steps should I follow when the VPN connection fails?",
            "What should I do if I lose my company laptop?", "How many vacation days do I get?",
            "What should I do if I forget my password?", "my vpn dose work", "It is not working.", "my vpn dose not work"} <= inputs
    assert any(s["id"] == "identity-isolation" for s in scenarios)
    assert any(t.get("operation") == "confirm" for s in scenarios for t in s["turns"])


def test_extra_sources_fail_strict_acceptance_even_with_correct_fact():
    expected = {"kind": "KNOWLEDGE", "sources": [["handbook", "Leave"]], "facts": [["24"]]}
    result = row(sources=[{"source": "handbook", "section": "Leave"}, {"source": "it", "section": "Passwords"}])
    value = grade(expected, result)
    assert value["answer_or_behavior_pass"]
    assert value["source_precision"] == .5
    assert not value["pass"]


def test_negative_answer_is_measured_as_unsafe_not_hidden_by_valid_json():
    expected = {"kind": "ABSTENTION", "sources": []}
    result = {**row(), "expected": expected}
    result["grade"] = grade(expected, result)
    value = metrics([result])
    assert value["unsafe_answers"] == 1
    assert value["unsupported_abstention_precision"] == 0


def test_missing_required_fact_and_unvalidated_citation_fail():
    expected = {"kind": "KNOWLEDGE", "sources": [["policy", "Devices"]], "facts": [["immediately"]]}
    result = row(sources=[{"source": "policy", "section": "Devices"}], text="Report the device.")
    result["supporting_results"] = []
    value = grade(expected, result)
    assert not value["answer_or_behavior_pass"]
    assert not value["citations_match_validated_support"]


def test_scope_violation_or_unapproved_execution_fails():
    result = row(kind="CLARIFICATION")
    result.update(executions=1, retrieved=[{"source": "restricted"}], source_allowlist=["public"])
    value = grade({"kind": "CLARIFICATION", "sources": []}, result)
    assert not value["action_execution_pass"] and not value["scope_pass"]


def test_secret_redaction_never_serializes_values():
    fake = "not-a-real-secret-example"
    encoded = safe_artifact({"raw_output": fake}, [fake])
    assert fake not in encoded
    assert "[REDACTED]" in encoded


def test_junit_summary_handles_relative_report_paths_without_exposing_local_paths(tmp_path, monkeypatch):
    (tmp_path / "report.xml").write_text('<testsuites><testsuite><testcase name="pass"/><testcase name="skip"><skipped/></testcase></testsuite></testsuites>', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert junit_summary(Path("report.xml")) == {"passed": 1, "failed": 0, "skipped": 1, "report": "report.xml"}
