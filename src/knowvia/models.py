from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class DocumentSection:
    """A section parsed from one Markdown document."""

    source: str
    title: str
    heading: str
    text: str


@dataclass(frozen=True)
class Chunk:
    """A searchable piece of a document and its metadata."""

    id: str
    source: str
    title: str
    section: str
    text: str
    embedding: list[float] | None = None

    @property
    def embedding_text(self) -> str:
        """Include metadata so short policy text has useful semantic context."""
        return f"Document: {self.title}\nSection: {self.section}\n\n{self.text}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Chunk:
        return cls(**value)


@dataclass(frozen=True)
class RetrievalResult:
    """One ranked match returned by a vector store."""

    chunk: Chunk
    score: float


@dataclass(frozen=True)
class SourceCitation:
    """A source derived from retrieved chunk metadata."""

    source: str
    section: str


class EvidenceStatus(str, Enum):
    """Validated evidence-routing outcomes."""

    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT = "INSUFFICIENT"


@dataclass(frozen=True)
class EvidenceDecision:
    """A structured decision about whether retrieved evidence answers a question."""

    status: EvidenceStatus
    reason: str
    missing_information: str
    supporting_evidence_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AnswerResult:
    """An answer with separate retrieved candidates and supporting evidence."""

    answer: str
    sources: list[SourceCitation]
    retrieval_results: list[RetrievalResult]
    evidence_decision: EvidenceDecision | None = None
    supporting_results: list[RetrievalResult] = field(default_factory=list)
