from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class KnowledgeNormalizationKind(str, Enum):
    UNCHANGED = "UNCHANGED"
    DECLARATIVE_PROBLEM = "DECLARATIVE_PROBLEM"


@dataclass(frozen=True)
class NormalizedKnowledgeRequest:
    """A bounded informational request derived only from the current turn."""

    original_request: str
    normalized_request: str
    normalization_applied: bool
    normalization_kind: KnowledgeNormalizationKind


@dataclass(frozen=True)
class AmbiguousDeclarativeStatus:
    """A non-action status statement that needs polarity or purpose clarified."""

    subject: str
    clarification_question: str


class KnowledgeRequestNormalizer(Protocol):
    def normalize(self, employee_request: str) -> NormalizedKnowledgeRequest: ...


class DeterministicKnowledgeRequestNormalizer:
    """Conservatively frames clear problem reports as knowledge questions."""

    _explicit_question = re.compile(
        r"^\s*(?:how|what|when|where|why|who|which|is|are|can|could|should|"
        r"does|do|explain|tell me|summarize)\b",
        re.I,
    )
    _employee_context = re.compile(
        r"^\s*(?:i\b|i['’]?m\b|i am\b|my\b|we\b|we['’]?re\b|our\b)",
        re.I,
    )
    _problem_signal = re.compile(
        r"\b(?:isn['’]?t|is not|aren['’]?t|are not|won['’]?t|will not|"
        r"can['’]?t|cannot|couldn['’]?t|could not|unable to|not working|"
        r"broken|fails?|failed|having trouble|problems? with|issues? with|"
        r"giving (?:an )?error|reports? (?:an )?error)\b",
        re.I,
    )
    _owned_subject_negative = re.compile(
        r"^\s*(?:my|our)\s+.+?\b(?:does|do|did)(?:n['’]?t|\s+not)\s+\w+",
        re.I,
    )
    _explicit_ticket_action = re.compile(
        r"\b(?:create|make|open|raise|file|submit|log)\b.{0,50}"
        r"\b(?:(?:it|support|helpdesk|jira)\s+)?(?:ticket|issue)\b",
        re.I,
    )

    @staticmethod
    def _is_one_edit_or_transposition(left: str, right: str) -> bool:
        """Bound typo tolerance to one short grammar token, never employee facts."""
        if left == right:
            return True
        if len(left) == len(right):
            differences = [index for index, pair in enumerate(zip(left, right)) if pair[0] != pair[1]]
            if len(differences) == 1:
                return True
            if len(differences) == 2:
                first, second = differences
                return (
                    second == first + 1
                    and left[first] == right[second]
                    and left[second] == right[first]
                )
            return False
        if abs(len(left) - len(right)) != 1:
            return False
        shorter, longer = (left, right) if len(left) < len(right) else (right, left)
        for index in range(len(longer)):
            if longer[:index] + longer[index + 1 :] == shorter:
                return True
        return False

    @classmethod
    def _has_owned_subject_negative(cls, request: str) -> bool:
        tokens = re.findall(r"[a-z]+", request.casefold())
        if not tokens or tokens[0] not in {"my", "our"}:
            return False
        for index, token in enumerate(tokens[1:-1], start=1):
            if tokens[index + 1] != "not":
                continue
            if any(
                cls._is_one_edit_or_transposition(token, auxiliary)
                for auxiliary in ("do", "does", "did")
            ):
                return True
        return False

    def is_declarative_problem(
        self, employee_request: str, *, allow_terse_fragment: bool = False
    ) -> bool:
        """Recognize a problem report without inferring any action intent."""
        request = employee_request.strip()
        if not request or self._explicit_question.search(request):
            return False
        if self._explicit_ticket_action.search(request):
            return False
        if self._employee_context.search(request) and (
            self._problem_signal.search(request)
            or self._owned_subject_negative.search(request)
            or self._has_owned_subject_negative(request)
        ):
            return True
        return bool(
            allow_terse_fragment
            and len(re.findall(r"\w+", request)) <= 10
            and self._problem_signal.search(request)
        )

    def normalize(self, employee_request: str) -> NormalizedKnowledgeRequest:
        if not isinstance(employee_request, str):
            raise TypeError("Knowledge request must be a string")
        request = employee_request.strip()
        if not request:
            return self._unchanged(request)
        if (
            request.endswith("?")
            or self._explicit_question.search(request)
            or self._explicit_ticket_action.search(request)
        ):
            return self._unchanged(request)
        if not self.is_declarative_problem(request):
            return self._unchanged(request)

        normalized = (
            "What documented company guidance directly addresses this "
            f"reported problem: {request}"
        )
        return NormalizedKnowledgeRequest(
            original_request=request,
            normalized_request=normalized,
            normalization_applied=True,
            normalization_kind=KnowledgeNormalizationKind.DECLARATIVE_PROBLEM,
        )

    @staticmethod
    def _unchanged(request: str) -> NormalizedKnowledgeRequest:
        return NormalizedKnowledgeRequest(
            original_request=request,
            normalized_request=request,
            normalization_applied=False,
            normalization_kind=KnowledgeNormalizationKind.UNCHANGED,
        )


class DeclarativeStatusAmbiguityPolicy:
    """Preserve polarity for short owned-subject status declarations."""

    _positive_state_terms = frozenset(
        {
            "work",
            "works",
            "working",
            "connect",
            "connects",
            "connected",
            "start",
            "starts",
            "starting",
            "run",
            "runs",
            "running",
            "open",
            "opens",
            "opening",
            "load",
            "loads",
            "loading",
            "function",
            "functions",
            "functioning",
        }
    )
    _exact_auxiliaries = frozenset({"do", "does", "did", "is", "are", "was", "were"})

    def __init__(
        self, normalizer: DeterministicKnowledgeRequestNormalizer | None = None
    ) -> None:
        self.normalizer = normalizer or DeterministicKnowledgeRequestNormalizer()

    def classify(self, employee_request: str) -> AmbiguousDeclarativeStatus | None:
        request = employee_request.strip()
        if (
            not request
            or request.endswith("?")
            or self.normalizer._explicit_ticket_action.search(request)
            or self.normalizer.is_declarative_problem(
                request, allow_terse_fragment=True
            )
        ):
            return None

        tokens = re.findall(r"[a-z0-9]+", request.casefold())
        if len(tokens) < 3 or tokens[0] not in {"my", "our"}:
            return None
        if tokens[-1] not in self._positive_state_terms or "not" in tokens[1:-1]:
            return None

        subject_tokens = tokens[1:-1]
        malformed_auxiliary = False
        if subject_tokens:
            auxiliary = subject_tokens[-1]
            if auxiliary in self._exact_auxiliaries:
                subject_tokens = subject_tokens[:-1]
            elif any(
                self.normalizer._is_one_edit_or_transposition(auxiliary, expected)
                for expected in ("do", "does", "did")
            ):
                malformed_auxiliary = True
                subject_tokens = subject_tokens[:-1]
        if not subject_tokens:
            return None

        subject = " ".join(subject_tokens)
        if malformed_auxiliary:
            question = (
                f"Did you mean that your {subject} is working, or that it is not "
                "working and you need help or an IT ticket created?"
            )
        else:
            question = f"What help or company information do you need about your {subject}?"
        return AmbiguousDeclarativeStatus(subject, question)

    def reconstruct_problem(
        self, earlier_request: str, clarification_answer: str
    ) -> str | None:
        status = self.classify(earlier_request)
        if status is None:
            return None
        match = re.match(r"^\s*(?:it|that|this)\b(.*)$", clarification_answer, re.I)
        if match is None or not match.group(1).strip():
            return None
        return f"{status.subject}{match.group(1)}".strip(" .?!")


def validate_normalized_knowledge_request(
    result: NormalizedKnowledgeRequest, original_request: str
) -> NormalizedKnowledgeRequest:
    if not isinstance(result, NormalizedKnowledgeRequest):
        raise TypeError("Normalizer returned an invalid result")
    original = original_request.strip()
    if result.original_request != original:
        raise ValueError("Normalizer changed the recorded original request")
    if not result.normalized_request.strip():
        raise ValueError("Normalized knowledge request cannot be empty")
    if result.normalization_applied != (
        result.normalization_kind is not KnowledgeNormalizationKind.UNCHANGED
    ):
        raise ValueError("Normalization status is contradictory")
    return result
