from __future__ import annotations

import re
from typing import Protocol


class ActionIntentPolicy(Protocol):
    def is_explicit(self, employee_request: str, tool_name: str) -> bool: ...


class ExplicitCurrentTurnActionIntentPolicy:
    """Requires a tool-specific action request in the current employee turn."""

    _ticket_request = re.compile(
        r"\b(?:create|make|open|raise|file|submit|log)\b.{0,50}?"
        r"\b(?:(?:it|support|helpdesk|jira)\s+)?(?:ticket|issue)\b",
        re.I,
    )

    def is_explicit(self, employee_request: str, tool_name: str) -> bool:
        if not isinstance(employee_request, str):
            return False
        if tool_name == "create_it_ticket":
            return TicketRequestExtractor().action_match(employee_request) is not None
        # New tools must opt into an explicit current-turn policy before use.
        return False


class TicketRequestExtractor:
    """Bounded employee-authored details, shared by context and local routing.

    No model decisions, permissions, or confirmation authority live here.
    Uncertain supplied text is never treated as permission to borrow old details.
    """

    _quoted = re.compile(r'"[^"\n]*"|“[^”\n]*”|`[^`]*`|(?<!\w)\'[^\'\n]+\'(?!\w)')
    _negated = re.compile(r"\b(?:do\s+not|don['’]?t|never|not\s+to|without|avoid|cancel)\b", re.I)
    _hypothetical = re.compile(r"\b(?:if|unless|suppose|hypothetically|example|quote|says?|said|told)\b", re.I)
    _information = re.compile(r"^\s*(?:how|what|when|where|why|who|which|explain|summarize|tell\s+me)\b", re.I)
    _placeholder = re.compile(r"^(?:me|myself|that|this|it|it\s+support|please|thanks|thank\s+you|(?:that|this|the)\s+(?:problem|issue))$", re.I)
    _signal = re.compile(
        r"\b(?:isn['’]?t|aren['’]?t|doesn['’]?t|won['’]?t|can['’]?t|cannot|unable|"
        r"not\s+(?:work\w*|start\w*|connect\w*|boot\w*|respond\w*|available)|"
        r"broken|fails?|failed|failure\w*|error\w*|problems?|issues?|trouble|"
        r"lost|stolen|missing|theft|damaged|overheat\w*|crash\w*|freez\w*|disconnect\w*|"
        r"restart\w*|reboot\w*)\b", re.I)
    _owned = re.compile(r"^\s*(?:my|our|i|we|the)\b", re.I)
    _denied_problem = re.compile(r"\b(?:not|isn['’]?t|wasn['’]?t)\s+(?:lost|stolen|missing|broken|damaged)\b", re.I)
    technical_subject = re.compile(r"\b(?:hardware|computer|printer|monitor|keyboard|mouse|screen|headset|network|software|application|device|server|wi-fi)\b", re.I)

    @classmethod
    def action_match(cls, text):
        if not isinstance(text, str) or len(text) > 2000:
            return None
        plain = cls._quoted.sub(lambda m: ' ' * len(m.group()), text)
        if re.search(r'["“”`]|(?<!\w)\'|\'(?!\w)', plain):
            return None  # Unbalanced quoting cannot safely establish intent.
        matches = list(ExplicitCurrentTurnActionIntentPolicy._ticket_request.finditer(plain))
        if len(matches) != 1:
            return None
        match = matches[0]
        # Judge the instruction's clause, not a preceding employee problem.
        start = max(plain.rfind(c, 0, match.start()) for c in '.!?;\n') + 1
        prefix = plain[start:match.start()]
        if cls._negated.search(prefix.rsplit(',', 1)[-1]) or cls._hypothetical.search(prefix) or cls._information.search(prefix):
            return None
        if (re.search(r"\b(?:how\s+to|instructions\s+to|example\s+of)\s*$", prefix, re.I)
                or re.match(r"\s*(?:can|could|would)\s+you\s+(?:explain|describe|show|tell)\b", prefix, re.I)):
            return None
        tail = plain[match.end():]
        if re.search(r"\b(?:(?:do\s+not|don['’]?t|never)\s+(?:please\s+)?(?:create|make|open|raise|file|submit|log|execute|do)\b|cancel\s+(?:it|that|this|the\s+ticket)\b|instead\b)", tail, re.I):
            return None
        return match

    @classmethod
    def problem(cls, text):
        """Recognize a clear report for history/details answers, not a new action."""
        text = text.strip(' .!?;\n')
        if not text or len(text) > 500 or cls._quoted.search(text) or cls._denied_problem.search(text):
            return None
        if cls._information.search(text) or cls._hypothetical.search(text):
            return None
        if re.search(r"\b(?:create|make|open|raise|file|submit|log)\b.*\b(?:ticket|issue)\b", text, re.I):
            return None
        if cls._owned.search(text) and cls._signal.search(text) and not cls.competing(text):
            return text
        return None

    @classmethod
    def competing(cls, text):
        parts = re.split(r"[.;\n]+|\b(?:and|also|plus)\b", text, flags=re.I)
        return sum(bool(cls._signal.search(p)) for p in parts) > 1

    @classmethod
    def extract(cls, text):
        """Return (status, problem): NONE, MISSING, DETAILS or AMBIGUOUS."""
        match = cls.action_match(text)
        if match is None:
            return 'NONE', None
        if cls._quoted.search(text):
            return 'AMBIGUOUS', None
        before = text[:match.start()].strip(' .!?;,\n')
        before = re.sub(r"(?:[,;]\s*|\s+)(?:please|can\s+you|could\s+you)\s*$", '', before, flags=re.I)
        # Strip only request politeness; never swallow employee problem text.
        if re.fullmatch(r"(?:please\s*)?(?:(?:can|could|would)\s+you\s*)?(?:please)?", before, re.I):
            before = ''
        after = text[match.end():].strip(' .!?;,\n')
        after = re.sub(r"^for\s+(?:me|myself)\b[\s.!?,;]*", '', after, flags=re.I)
        after = re.sub(r"^please\s+", '', after, flags=re.I)
        after = re.sub(r"^(?:because|for|about|regarding)\s+|^:\s*", '', after, flags=re.I).strip(' .!?;,\n')
        after = re.sub(r"[,\s]+please$", '', after, flags=re.I)
        if cls._placeholder.fullmatch(after):
            after = ''
        candidates = [p for p in (before, after) if p]
        if not candidates:
            return 'MISSING', None
        if len(candidates) != 1 or len(candidates[0]) > 500 or cls.competing(candidates[0]):
            return 'AMBIGUOUS', None
        detail = candidates[0]
        if before and cls.problem(detail) is None:
            return 'AMBIGUOUS', None
        if cls._hypothetical.search(detail) or cls._denied_problem.search(detail) or re.search(r"\b(?:ignore|bypass|override)\b", detail, re.I):
            return 'AMBIGUOUS', None
        return 'DETAILS', detail
