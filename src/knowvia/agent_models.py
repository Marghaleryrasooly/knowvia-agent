from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TYPE_CHECKING
from datetime import datetime

from knowvia.models import AnswerResult
from knowvia.knowledge_normalization import NormalizedKnowledgeRequest

if TYPE_CHECKING:
    from knowvia.ticket_lifecycle import TicketStatusResult


class AgentDecisionKind(str, Enum):
    """The four safe next steps available to the M5 orchestrator."""

    KNOWLEDGE = "KNOWLEDGE"
    ACTION = "ACTION"
    CLARIFY = "CLARIFY"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class AgentDecision:
    """A router's structured, not-yet-executed decision."""

    decision: AgentDecisionKind
    tool_name: str | None = None
    tool_arguments: dict[str, str] = field(default_factory=dict)
    clarification_question: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class ToolResult:
    """Provider-neutral result returned by every enterprise tool implementation."""

    success: bool
    tool_name: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_category: str | None = None


class ActionProposalStatus(str, Enum):
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ActionProposal:
    """A validated real action waiting for an explicit human confirmation."""

    proposal_id: str
    tool_name: str
    tool_arguments: dict[str, str]
    created_at: datetime
    expires_at: datetime
    status: ActionProposalStatus = ActionProposalStatus.PENDING
    result: ToolResult | None = None
    subject_id: str | None = None


@dataclass(frozen=True)
class AgentResult:
    """The employee-facing outcome plus structured orchestration details."""

    decision: AgentDecision
    message: str
    knowledge_result: AnswerResult | None = None
    tool_result: ToolResult | None = None
    tool_executed: bool = False
    action_proposal: ActionProposal | None = None
    normalized_knowledge_request: NormalizedKnowledgeRequest | None = None
    permission_denied: bool = False
    ticket_status_result: "TicketStatusResult | None" = None
    confirmation_failed: bool = False
