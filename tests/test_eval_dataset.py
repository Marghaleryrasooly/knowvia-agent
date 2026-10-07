from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowvia.eval_dataset import load_evaluation_dataset


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_m4_dataset_is_valid_diverse_and_has_exactly_50_cases() -> None:
    dataset = load_evaluation_dataset(
        PROJECT_ROOT / "evaluation" / "knowvia_m4_eval_v1.json",
        PROJECT_ROOT / "knowvia_m1_dataset",
    )
    assert len(dataset.cases) == 50
    assert {case.expected_behavior for case in dataset.cases} == {"ANSWER", "ABSTAIN"}
    covered = {source.source for case in dataset.cases for source in case.expected_sources}
    assert covered == {f"{number:02d}_{name}.md" for number, name in [
        (1, "employee_handbook"), (2, "remote_work_policy"),
        (3, "it_access_security_policy"), (4, "employee_benefits_guide"),
        (5, "it_setup_guide"), (6, "engineering_practices"),
        (7, "information_classification_policy"), (8, "expense_policy"),
    ]}
    tags = {tag for case in dataset.cases for tag in case.tags}
    assert {"multi_source", "procedure", "unsupported_permission", "distractor_heavy"} <= tags


def test_dataset_rejects_unknown_source_section(tmp_path: Path) -> None:
    raw = {
        "name": "bad", "version": "1", "corpus": "x",
        "cases": [{
            "id": "x", "question": "q", "expected_behavior": "ANSWER",
            "expected_sources": [{"source": "missing.md", "section": "Nope"}],
            "reference_answer": "a", "answer_criteria": [{"type": "contains_all", "values": ["a"]}],
        }],
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown source"):
        load_evaluation_dataset(path, PROJECT_ROOT / "knowvia_m1_dataset")


def test_abstention_case_cannot_define_answer_criteria(tmp_path: Path) -> None:
    raw = {
        "name": "bad", "version": "1", "corpus": "x",
        "cases": [{
            "id": "x", "question": "q", "expected_behavior": "ABSTAIN",
            "expected_sources": [], "reference_answer": None,
            "answer_criteria": [{"type": "contains_all", "values": ["invented"]}],
        }],
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="ABSTAIN"):
        load_evaluation_dataset(path)
