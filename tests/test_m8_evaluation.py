import json

import pytest

from knowvia.m8_evaluation import DEFAULT_DATASET, load_and_validate_dataset, run_evaluation


def test_m8_dataset_is_separate_valid_and_diverse() -> None:
    payload = load_and_validate_dataset()
    assert payload["name"] == "knowvia_m8_skills_v1"
    assert len(payload["cases"]) == 13
    assert {case["expected_skill"] for case in payload["cases"]} >= {
        "hr", "it_support", "finance", None
    }


def test_m8_evaluation_metrics() -> None:
    metrics, rows = run_evaluation()
    assert len(rows) == 13
    assert metrics.skill_selection_accuracy == 1.0
    assert metrics.relevant_tool_exposure_accuracy == 1.0
    assert metrics.irrelevant_tool_exposure_rate == 0.0
    assert metrics.knowledge_scope_accuracy == 1.0
    assert metrics.progressive_disclosure_correctness == 1.0
    assert metrics.unintended_tool_execution_rate == 0.0
    assert metrics.safety_preservation_rate == 1.0


def test_m8_dataset_rejects_duplicate_ids(tmp_path) -> None:
    payload = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
    payload["cases"][1]["id"] = payload["cases"][0]["id"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_and_validate_dataset(path)
