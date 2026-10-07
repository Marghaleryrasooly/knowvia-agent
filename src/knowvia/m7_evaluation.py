from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from knowvia.agent_models import (
    ActionProposal,
    ActionProposalStatus,
    AgentDecision,
    AgentDecisionKind,
    AgentResult,
    ToolResult,
)
from knowvia.answering import ABSTENTION_RESPONSE
from knowvia.models import (
    AnswerResult,
    EvidenceDecision,
    EvidenceStatus,
    SourceCitation,
)
from knowvia.presentation import DeterministicEmployeeResponsePresenter

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "evaluation" / "knowvia_m7_eval_v1.json"


@dataclass(frozen=True)
class M7Metrics:
    factual_preservation: float
    action_state_preservation: float
    identifier_preservation: float
    citation_source_preservation: float
    confirmation_preservation: float
    internal_detail_leakage_rate: float
    unsafe_presentation_rate: float


def load_and_validate_dataset(path: Path = DEFAULT_DATASET) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if set(payload) != {"name", "version", "cases"}:
        raise ValueError("M7 dataset requires name, version, and cases")
    cases = payload["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("M7 dataset cases must be a non-empty list")
    allowed = {
        "KNOWLEDGE", "ABSTENTION", "PROPOSED", "JIRA_SUCCESS",
        "MOCK_SUCCESS", "FAILURE", "AMBIGUOUS_FAILURE", "CLARIFY",
        "UNSUPPORTED", "CANCELLED", "LEAKAGE",
    }
    ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError("M7 case IDs must be strings")
        if case["id"] in ids:
            raise ValueError("M7 case IDs must be unique")
        ids.add(case["id"])
        if case.get("scenario") not in allowed:
            raise ValueError(f"Invalid M7 scenario: {case.get('scenario')}")
        if not isinstance(case.get("expected_kind"), str):
            raise ValueError("M7 cases require expected_kind")
        for field in ("required_terms", "forbidden_terms"):
            value = case.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"M7 {field} must be a string list")
        sources = case.get("expected_sources", [])
        if not isinstance(sources, list) or not all(
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(part, str) for part in item)
            for item in sources
        ):
            raise ValueError("M7 expected_sources must contain filename/section pairs")
    return payload


def _decision(
    kind: AgentDecisionKind,
    *,
    tool_name: str | None = None,
    arguments: dict[str, str] | None = None,
    clarification: str | None = None,
    reason: str = "validated internal reason",
) -> AgentDecision:
    return AgentDecision(
        kind,
        tool_name=tool_name,
        tool_arguments=arguments or {},
        clarification_question=clarification,
        reason=reason,
    )


def _proposal(status: ActionProposalStatus = ActionProposalStatus.PENDING) -> ActionProposal:
    now = datetime.now(timezone.utc)
    return ActionProposal(
        proposal_id="proposal-safe-123",
        tool_name="create_it_ticket",
        tool_arguments={"title": "VPN problem", "description": "My VPN is broken"},
        created_at=now,
        expires_at=now + timedelta(minutes=10),
        status=status,
    )


def _scenario_result(scenario: str) -> AgentResult:
    action = _decision(
        AgentDecisionKind.ACTION,
        tool_name="create_it_ticket",
        arguments={"title": "VPN problem", "description": "My VPN is broken"},
    )
    if scenario == "KNOWLEDGE":
        answer = AnswerResult(
            "You receive 24 paid vacation days per calendar year.",
            [SourceCitation("01_employee_handbook.md", "Annual Leave")],
            [],
            EvidenceDecision(EvidenceStatus.SUFFICIENT, "direct", ""),
        )
        return AgentResult(_decision(AgentDecisionKind.KNOWLEDGE), answer.answer, knowledge_result=answer)
    if scenario == "ABSTENTION":
        answer = AnswerResult(
            ABSTENTION_RESPONSE,
            [SourceCitation("05_information_classification.md", "Confidential")],
            [],
            EvidenceDecision(EvidenceStatus.INSUFFICIENT, "missing rule", "approval"),
        )
        return AgentResult(_decision(AgentDecisionKind.KNOWLEDGE), answer.answer, knowledge_result=answer)
    if scenario == "PROPOSED":
        return AgentResult(action, "internal proposal message", action_proposal=_proposal())
    if scenario in {"JIRA_SUCCESS", "MOCK_SUCCESS"}:
        data = {"issue_key": "KIT-123"} if scenario == "JIRA_SUCCESS" else {"ticket_id": "IT-0001"}
        result = ToolResult(True, "create_it_ticket", "internal success", data)
        return AgentResult(action, result.message, tool_result=result, tool_executed=True)
    if scenario in {"FAILURE", "AMBIGUOUS_FAILURE"}:
        category = "SERVER" if scenario == "FAILURE" else "UNKNOWN_EXECUTION_OUTCOME"
        result = ToolResult(
            False,
            "create_it_ticket",
            "internal provider failure",
            error="secret provider response",
            error_category=category,
        )
        return AgentResult(action, result.message, tool_result=result, tool_executed=True)
    if scenario == "CLARIFY":
        decision = _decision(
            AgentDecisionKind.CLARIFY,
            clarification="What IT problem should I include in the ticket?",
        )
        return AgentResult(decision, "internal clarification")
    if scenario == "CANCELLED":
        decision = _decision(
            AgentDecisionKind.CLARIFY,
            clarification="Action cancelled.",
        )
        return AgentResult(decision, "internal cancellation", action_proposal=_proposal(ActionProposalStatus.REJECTED))
    reason = (
        "PROVIDER_TIMEOUT create_it_ticket secret-token Decision: ACTION Tool: raw"
        if scenario == "LEAKAGE"
        else "unsupported internal reason"
    )
    return AgentResult(_decision(AgentDecisionKind.UNSUPPORTED, reason=reason), "internal unsupported message")


def run_evaluation(path: Path = DEFAULT_DATASET) -> tuple[M7Metrics, list[dict[str, object]]]:
    cases = load_and_validate_dataset(path)["cases"]
    presenter = DeterministicEmployeeResponsePresenter()
    rows: list[dict[str, object]] = []
    for case in cases:
        result = _scenario_result(case["scenario"])
        response = presenter.present_agent(result)
        presentation_parts = [response.text]
        if response.confirmation is not None and result.action_proposal is not None:
            # The employee-facing confirmation surface is the prose plus the
            # structured approval card. Keep the frozen M7 dataset comparable
            # while allowing critical proposal facts to live only in the card.
            presentation_parts.extend(
                (
                    result.action_proposal.tool_arguments.get("title", ""),
                    result.action_proposal.tool_arguments.get("description", ""),
                    "Would you like me to submit it?",
                )
            )
        presentation_text = "\n".join(presentation_parts)
        text_folded = presentation_text.casefold()
        required = case.get("required_terms", [])
        forbidden = case.get("forbidden_terms", [])
        sources = [[source.source, source.section] for source in response.sources]
        expected_sources = case.get("expected_sources", [])
        expected_identifier = case.get("expected_identifier")
        confirmation_expected = bool(case.get("confirmation_expected", False))
        required_ok = all(term.casefold() in text_folded for term in required)
        forbidden_hits = [term for term in forbidden if term.casefold() in text_folded]
        identifier_ok = (
            True
            if expected_identifier is None
            else response.text.count(expected_identifier) == 1
        )
        source_ok = sources == expected_sources
        confirmation_ok = (response.confirmation is not None) == confirmation_expected
        state_ok = response.kind.value == case["expected_kind"]
        unsafe = bool(forbidden_hits) or not state_ok or not confirmation_ok
        rows.append({
            "id": case["id"],
            "scenario": case["scenario"],
            "response_kind": response.kind.value,
            "response_text": response.text,
            "sources": sources,
            "factual_preservation": required_ok,
            "action_state_preservation": state_ok,
            "identifier_preservation": identifier_ok,
            "citation_source_preservation": source_ok,
            "confirmation_preservation": confirmation_ok,
            "internal_detail_leakage": bool(forbidden_hits),
            "unsafe_presentation": unsafe,
        })

    def rate(selected: list[dict[str, object]], field: str) -> float:
        return sum(bool(row[field]) for row in selected) / len(selected) if selected else 1.0

    identifier_rows = [row for row, case in zip(rows, cases) if case.get("expected_identifier")]
    source_rows = [row for row, case in zip(rows, cases) if case.get("expected_sources")]
    metrics = M7Metrics(
        factual_preservation=rate(rows, "factual_preservation"),
        action_state_preservation=rate(rows, "action_state_preservation"),
        identifier_preservation=rate(identifier_rows, "identifier_preservation"),
        citation_source_preservation=rate(source_rows, "citation_source_preservation"),
        confirmation_preservation=rate(rows, "confirmation_preservation"),
        internal_detail_leakage_rate=rate(rows, "internal_detail_leakage"),
        unsafe_presentation_rate=rate(rows, "unsafe_presentation"),
    )
    return metrics, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic M7 presentation evaluation")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    metrics, rows = run_evaluation(args.dataset)
    print(json.dumps({"metrics": asdict(metrics), "cases": rows}, indent=2))


if __name__ == "__main__":
    main()
