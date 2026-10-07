from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from knowvia.eval_dataset import EvaluationCase


@dataclass(frozen=True)
class AnswerGrade:
    passed: bool
    criteria_passed: bool
    forbidden_passed: bool
    failed_criteria: tuple[str, ...]
    matched_forbidden: tuple[str, ...]


class AnswerGrader(Protocol):
    def grade(self, case: EvaluationCase, answer: str) -> AnswerGrade: ...


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[^\w$]+", " ", text)
    return " ".join(text.split())


class DeterministicAnswerGrader:
    """Grades explicit facts reproducibly; it is intentionally not an LLM judge."""

    def grade(self, case: EvaluationCase, answer: str) -> AnswerGrade:
        normalized = normalize_text(answer)
        failures: list[str] = []
        for criterion in case.answer_criteria:
            kind = criterion["type"]
            values = [normalize_text(value) for value in criterion["values"]]
            if kind == "contains_all":
                passed = all(value in normalized for value in values)
            elif kind == "contains_any":
                passed = any(value in normalized for value in values)
            elif kind == "number":
                passed = any(re.search(rf"(?<!\w){re.escape(value)}(?!\w)", normalized) for value in values)
            elif kind == "ordered_terms":
                positions = [normalized.find(value) for value in values]
                passed = all(position >= 0 for position in positions) and positions == sorted(positions)
            else:  # validation normally makes this unreachable
                passed = False
            if not passed:
                failures.append(f"{kind}: {criterion['values']}")

        forbidden = [
            phrase
            for phrase in (*case.forbidden_terms, *case.forbidden_claims)
            if normalize_text(phrase) in normalized
        ]
        criteria_passed = not failures
        forbidden_passed = not forbidden
        return AnswerGrade(
            passed=criteria_passed and forbidden_passed,
            criteria_passed=criteria_passed,
            forbidden_passed=forbidden_passed,
            failed_criteria=tuple(failures),
            matched_forbidden=tuple(forbidden),
        )
