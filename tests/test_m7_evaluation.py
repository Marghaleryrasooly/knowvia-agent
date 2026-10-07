import json

import pytest

from knowvia.m7_evaluation import load_and_validate_dataset, run_evaluation


def test_m7_dataset_is_valid_and_separate() -> None:
    payload = load_and_validate_dataset()

    assert payload["name"] == "knowvia_m7_presentation_v1"
    assert len(payload["cases"]) == 11


def test_m7_evaluation_metrics() -> None:
    metrics, rows = run_evaluation()

    assert len(rows) == 11
    assert metrics.factual_preservation == 1.0
    assert metrics.action_state_preservation == 1.0
    assert metrics.identifier_preservation == 1.0
    assert metrics.citation_source_preservation == 1.0
    assert metrics.confirmation_preservation == 1.0
    assert metrics.internal_detail_leakage_rate == 0.0
    assert metrics.unsafe_presentation_rate == 0.0


def test_m7_dataset_rejects_duplicate_ids(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({
        "name": "bad",
        "version": "1",
        "cases": [
            {"id": "x", "scenario": "KNOWLEDGE", "expected_kind": "KNOWLEDGE"},
            {"id": "x", "scenario": "KNOWLEDGE", "expected_kind": "KNOWLEDGE"},
        ],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="unique"):
        load_and_validate_dataset(path)
