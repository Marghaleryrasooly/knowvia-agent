from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


GENERAL_COMPANY_KNOWLEDGE_SKILL_ID = "general_company_knowledge"


class GeneralKnowledgeIntentKind(str, Enum):
    KNOWLEDGE = "KNOWLEDGE"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_KNOWLEDGE = "NOT_KNOWLEDGE"


@dataclass(frozen=True)
class GeneralKnowledgeIntent:
    kind: GeneralKnowledgeIntentKind
    reason: str


class GeneralKnowledgeIntentPolicy(Protocol):
    def classify(self, employee_request: str) -> GeneralKnowledgeIntent: ...

    def is_clear_company_knowledge(self, employee_request: str) -> bool: ...


class DeterministicGeneralKnowledgeIntentPolicy:
    """Classify request form without guessing domain facts or action intent."""

    _knowledge_opening = re.compile(
        r"^\s*(?:what|when|where|why|who|which|how|is|are|does|do|should|"
        r"explain|tell\s+me|summarize)\b",
        re.I,
    )
    _permission_question = re.compile(
        r"^\s*can\s+(?!you\b)(?:i|we|employees?|contractors?|staff|people)\b",
        re.I,
    )
    _information_request = re.compile(
        r"^\s*(?:(?:i|we|i['’]?d|we['’]?d)\s+)?"
        r"(?:want|need|(?:would\s+)?like)\s+"
        r"(?:to\s+)?(?:know|understand|learn|find\s+out)\b|"
        r"^\s*(?:please\s+)?(?:tell|show)\s+(?:me|us)\b|"
        r"^\s*(?:(?:i['’]?m|we['’]?re)\s+)?looking\s+for\s+"
        r"(?:information|details|guidance)\b",
        re.I,
    )
    _bounded_first_person_information_request = re.compile(
        r"^\s*(?:i|we)(?:['’]d|\s+would)?\s+"
        r"(?:[a-z]+(?:['’][a-z]+)?\s+){0,5}"
        r"(?:to\s+)?(?:know|understand|learn|find\s+out)\b",
        re.I,
    )
    _company_context = re.compile(
        r"\b(?:company|company['’]s|corporate|workplace|office|employees?|"
        r"contractors?|staff|policy|policies|novatech)\b",
        re.I,
    )
    _action_request = re.compile(
        r"^\s*(?:(?:please\s+)?|(?:can|could|would|will)\s+you\s+(?:please\s+)?)"
        r"(?:create|open|raise|file|submit|log|book|approve|write|"
        r"delete|send|change|purchase|order|schedule|cancel|grant|reset|update|"
        r"install|perform|execute)\b",
        re.I,
    )
    _context_only = re.compile(
        r"^\s*(?:yes(?:\s*,?\s*please)?|do\s+it|go\s+ahead|that|this|it|"
        r"can\s+you\s+do\s+that)\s*[.!?]*\s*$",
        re.I,
    )
    _modal_request = re.compile(
        r"^\s*(?:can|could|would|will)\s+you\b",
        re.I,
    )
    _word = re.compile(r"[a-z0-9]+(?:['’][a-z]+)?", re.I)
    _vague_only = re.compile(
        r"^\s*(?:help(?:\s+(?:me|us))?|what|why|how|policy|"
        r"information|details)\s*[.!?]*\s*$",
        re.I,
    )
    _subject_fillers = frozenset(
        {"about", "on", "regarding", "the", "a", "an", "my", "our", "company"}
    )

    def classify(self, employee_request: str) -> GeneralKnowledgeIntent:
        if not isinstance(employee_request, str):
            return GeneralKnowledgeIntent(
                GeneralKnowledgeIntentKind.NOT_KNOWLEDGE,
                "The request is not text.",
            )
        request = employee_request.strip()
        if not request:
            return GeneralKnowledgeIntent(
                GeneralKnowledgeIntentKind.AMBIGUOUS,
                "The request is empty.",
            )
        if self._context_only.fullmatch(request):
            return GeneralKnowledgeIntent(
                GeneralKnowledgeIntentKind.NOT_KNOWLEDGE,
                "Explicit action or context-only language is not general knowledge intent.",
            )
        words = self._word.findall(request)
        if self._vague_only.fullmatch(request) or len(words) < 2:
            return GeneralKnowledgeIntent(
                GeneralKnowledgeIntentKind.AMBIGUOUS,
                "The request does not identify a usable information topic.",
            )

        if self._action_request.search(request):
            return GeneralKnowledgeIntent(
                GeneralKnowledgeIntentKind.NOT_KNOWLEDGE,
                "Explicit action language is not general knowledge intent.",
            )

        if self._modal_request.search(request) and len(words) <= 4:
            return GeneralKnowledgeIntent(
                GeneralKnowledgeIntentKind.AMBIGUOUS,
                "The request does not identify enough subject matter.",
            )

        information_match = self._information_request.search(request)
        if information_match is None:
            information_match = self._bounded_first_person_information_request.search(
                request
            )
        if information_match:
            subject_words = [
                word.casefold()
                for word in self._word.findall(request[information_match.end():])
                if word.casefold() not in self._subject_fillers
            ]
            if not subject_words:
                return GeneralKnowledgeIntent(
                    GeneralKnowledgeIntentKind.AMBIGUOUS,
                    "The information request does not identify a topic.",
                )
            return GeneralKnowledgeIntent(
                GeneralKnowledgeIntentKind.KNOWLEDGE,
                "The employee explicitly requested information about a stated topic.",
            )

        if self._knowledge_opening.search(request) or self._permission_question.search(request):
            if len(words) < 3:
                return GeneralKnowledgeIntent(
                    GeneralKnowledgeIntentKind.AMBIGUOUS,
                    "The question form does not identify enough subject matter.",
                )
            return GeneralKnowledgeIntent(
                GeneralKnowledgeIntentKind.KNOWLEDGE,
                "The request has a clear informational question form.",
            )

        if request.endswith("?"):
            return GeneralKnowledgeIntent(
                GeneralKnowledgeIntentKind.KNOWLEDGE,
                "The non-action request is explicitly phrased as a question.",
            )

        if self._company_context.search(request) and len(words) >= 3:
            return GeneralKnowledgeIntent(
                GeneralKnowledgeIntentKind.KNOWLEDGE,
                "The non-action request states a sufficiently specific company topic.",
            )

        return GeneralKnowledgeIntent(
            GeneralKnowledgeIntentKind.NOT_KNOWLEDGE,
            "The request does not clearly seek company information.",
        )

    def is_clear_company_knowledge(self, employee_request: str) -> bool:
        return (
            self.classify(employee_request).kind
            is GeneralKnowledgeIntentKind.KNOWLEDGE
        )
