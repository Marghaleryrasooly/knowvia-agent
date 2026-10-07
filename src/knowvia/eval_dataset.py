from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from knowvia.parsing import load_markdown_documents


@dataclass(frozen=True)
class ExpectedSource:
    source: str
    section: str


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    question: str
    expected_behavior: str
    expected_sources: tuple[ExpectedSource, ...]
    reference_answer: str | None
    answer_criteria: tuple[dict[str, Any], ...]
    forbidden_terms: tuple[str, ...]
    forbidden_claims: tuple[str, ...]
    tags: tuple[str, ...]
    notes: str


@dataclass(frozen=True)
class EvaluationDataset:
    name: str
    version: str
    corpus: str
    cases: tuple[EvaluationCase, ...]


ALLOWED_CRITERIA = {"contains_all", "contains_any", "number", "ordered_terms"}


def load_evaluation_dataset(path: Path, documents: Path | None = None) -> EvaluationDataset:
    raw = json.loads(path.read_text(encoding="utf-8"))
    dataset = _parse_dataset(raw)
    validate_dataset(dataset, documents)
    return dataset


def _parse_dataset(raw: object) -> EvaluationDataset:
    if not isinstance(raw, dict):
        raise ValueError("Evaluation dataset must be a JSON object")
    required = {"name", "version", "corpus", "cases"}
    if set(raw) != required:
        raise ValueError(f"Dataset must contain exactly: {', '.join(sorted(required))}")
    if not isinstance(raw["cases"], list):
        raise ValueError("cases must be a list")
    cases: list[EvaluationCase] = []
    for item in raw["cases"]:
        if not isinstance(item, dict):
            raise ValueError("Each evaluation case must be an object")
        sources = tuple(ExpectedSource(**source) for source in item.get("expected_sources", []))
        cases.append(
            EvaluationCase(
                id=str(item.get("id", "")),
                question=str(item.get("question", "")),
                expected_behavior=str(item.get("expected_behavior", "")),
                expected_sources=sources,
                reference_answer=item.get("reference_answer"),
                answer_criteria=tuple(item.get("answer_criteria", [])),
                forbidden_terms=tuple(item.get("forbidden_terms", [])),
                forbidden_claims=tuple(item.get("forbidden_claims", [])),
                tags=tuple(item.get("tags", [])),
                notes=str(item.get("notes", "")),
            )
        )
    return EvaluationDataset(str(raw["name"]), str(raw["version"]), str(raw["corpus"]), tuple(cases))


def validate_dataset(dataset: EvaluationDataset, documents: Path | None = None) -> None:
    if not dataset.name.strip() or not dataset.version.strip() or not dataset.corpus.strip():
        raise ValueError("Dataset name, version, and corpus are required")
    if not dataset.cases:
        raise ValueError("Dataset must contain at least one case")
    ids = [case.id for case in dataset.cases]
    if any(not value.strip() for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("Case IDs must be non-empty and unique")

    known_sources: set[tuple[str, str]] | None = None
    if documents is not None:
        known_sources = {
            (section.source, section.heading)
            for section in load_markdown_documents(documents)
        }

    for case in dataset.cases:
        if not case.question.strip():
            raise ValueError(f"{case.id}: question is required")
        if case.expected_behavior not in {"ANSWER", "ABSTAIN"}:
            raise ValueError(f"{case.id}: expected_behavior must be ANSWER or ABSTAIN")
        if case.expected_behavior == "ANSWER":
            if not case.expected_sources or not case.reference_answer or not case.answer_criteria:
                raise ValueError(f"{case.id}: ANSWER cases need sources, reference_answer, and criteria")
        elif case.expected_sources or case.reference_answer is not None or case.answer_criteria:
            raise ValueError(f"{case.id}: ABSTAIN cases cannot define answer expectations")
        for source in case.expected_sources:
            if not source.source.strip() or not source.section.strip():
                raise ValueError(f"{case.id}: expected source fields cannot be empty")
            if known_sources is not None and (source.source, source.section) not in known_sources:
                raise ValueError(f"{case.id}: unknown source/section {source.source} -> {source.section}")
        for criterion in case.answer_criteria:
            if not isinstance(criterion, dict) or criterion.get("type") not in ALLOWED_CRITERIA:
                raise ValueError(f"{case.id}: unsupported answer criterion")
            values = criterion.get("values")
            if not isinstance(values, list) or not values or not all(isinstance(v, str) and v.strip() for v in values):
                raise ValueError(f"{case.id}: criterion values must be non-empty strings")
        for label, values in (("forbidden_terms", case.forbidden_terms), ("forbidden_claims", case.forbidden_claims)):
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{case.id}: {label} must contain non-empty strings")
