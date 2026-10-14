from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from knowvia.generation import GenerationProvider
from knowvia.models import EvidenceAnswerScope, EvidenceDecision, EvidenceStatus, RetrievalResult
from knowvia.evidence_scope_intent import question_scope_constraint, scope_constraint_instruction

# Native decoder constraints supplement, never replace, the semantic validator.
EVIDENCE_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["requested_claim", "directly_supported_facts", "supporting_evidence_ids", "missing_fact", "decision"],
    "properties": {
        "requested_claim": {"type": "string", "minLength": 1},
        "directly_supported_facts": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "supporting_evidence_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "missing_fact": {"type": "string"},
        "decision": {"type": "string", "enum": ["SUFFICIENT", "INSUFFICIENT"]},
    },
}

_ANSWERABILITY_CONTRACT = """PROBLEM RELEVANCE AND CONDITIONAL POLICY rules:
- For a reported problem, keep the employee's original problem as the answerability target, even when it is framed as an informational question. Restate that problem in requested_claim; do not substitute a broader request for related guidance.
- A fact must establish an answer to that problem, not merely concern the same topic. Usage restrictions, prerequisites, and neighboring procedures do not establish how to resolve a different failure.
- A troubleshooting procedure supports only the failure it documents. Do not assume an unstated cause or triggering condition in order to apply it to another problem. Documented reporting or escalation requirements can answer what to do when they directly cover the reported event.
- Distinguish reporting a conditional policy from verifying personal eligibility. An ordinary question about an allowance or rule can be answered by reporting the documented rule with all its conditions, without claiming the employee personally satisfies them. Preserve those conditions in directly_supported_facts.
- If the question specifically asks whether the employee qualifies, meets a condition, or belongs to an eligible group, that material fact must be established. Do not infer eligibility or ineligibility for an undocumented group. Unknown eligibility does not justify stripping conditions from a reported policy, and reporting a conditional policy does not verify personal eligibility.
- When only related facts exist, return INSUFFICIENT with the missing answer or applicability fact. Never turn a valid-looking selection of related sources into proof of answerability.

Paired example: Evidence: Overnight-duty staff may claim meals.
"What meals can I claim?" => SUFFICIENT to report that conditional rule, not personal eligibility.
"Am I eligible?" => INSUFFICIENT if overnight-duty status is unknown; that status is the missing fact.
"""

EVIDENCE_SYSTEM_PROMPT = """You are a strict evidence-sufficiency classifier for company-policy questions.
Decide whether the supplied evidence DIRECTLY SUPPORTS the employee's exact requested claim.

Required reasoning process:
1. Restate the exact claim that must be established to answer the question.
2. List only facts stated in, or ordinary close paraphrases of, the supplied evidence.
3. Decide whether those facts directly establish the requested claim without adding an unstated rule, exception, exclusion, permission, or assumption.
4. If not, name the specific missing fact.

Decision discipline:
- SUFFICIENT is required when the requested claim is directly established. Do not abstain merely because distractors are also present or because the employee used informal, abbreviated, or imperfect wording.
- INSUFFICIENT is required when a material fact is actually missing. It is not a default caution label when direct support exists.
- Resolve ordinary paraphrases using the closest directly matching evidence heading and fact. Do not broaden a request about one category into a total across different sibling categories.
- If wording is genuinely ambiguous and no evidence item is a clear direct semantic match, use INSUFFICIENT and state what meaning must be clarified. Do not select a loosely associated fact just because it is available.

A related fact is NOT automatically sufficient evidence for the requested claim. Ignore irrelevant distractor chunks when another chunk directly answers the question; distractors neither add support nor cancel direct support.

DIRECT SUPPORT rules:
- Ordinary paraphrases and workplace equivalents count when they preserve meaning. For example, a coworker is another employee, "roll over" can paraphrase "carry over," and "push straight to" can paraphrase "direct push to."
- Explicit "may" or "permitted" language directly supports permission. Explicit "prohibited," "must not," or "not allowed" language directly supports a negative permission answer.
- Explicit quantities, amounts, and durations answer matching factual questions. "At least N" establishes a minimum of N. "Up to N" establishes a maximum of N.
- Apply simple stated conditions. Six is longer than five; four is more than three; twelve is more than ten. This is direct application of the policy, not an unsupported inference.
- A documented procedure supports what to do. If the question says an earlier step is already complete, the remaining later steps are directly supported.
- A stated requirement, entitlement, definition, or eligibility rule directly supports a question about the same subject and same group.
- Do not demand identical wording, every imaginable exception, or unrelated details when the requested claim is already directly established.

MINIMAL SUPPORT rules:
- Select the smallest set of evidence IDs that fully establishes the answer.
- For every proposed supporting ID, ask: would the answer lose a required fact if this item were removed? If no, omit that ID.
- Do not select evidence merely because it shares a broad topic, employee group, number, timeframe, or neighboring policy category.
- directly_supported_facts must contain only the facts needed for the answer and must come from the selected evidence IDs. Do not list distractor facts.

UNSUPPORTED INFERENCE rules:
- A rule about one group does not establish eligibility OR ineligibility for a different group. "Permanent employees receive a benefit" says nothing about contractors unless the evidence explicitly includes contractors, excludes them, uses exclusive wording such as "only," or provides an exhaustive rule.
- A classification, sensitivity label, or general handling restriction does not establish whether a named product, service, recipient, device, or action is specifically approved or prohibited.
- A general rule does not create an unstated exception. An exception in one policy does not create an exception in another.
- Do not infer an unstated numeric limit, company fact, benefit detail, approval status, or exclusion.
- Do not use general knowledge, common practice, safety advice, or what a policy ought to say.

Return one JSON object containing these base fields AND the four answer-scope fields below:
{"requested_claim":"non-empty string","directly_supported_facts":["one or more non-empty strings"],"supporting_evidence_ids":[1],"missing_fact":"string","decision":"SUFFICIENT or INSUFFICIENT"}
Evidence IDs are the one-based numbers in [Evidence N]. Select only evidence that directly supports the requested claim; do not select merely related or distracting evidence.
For SUFFICIENT, directly_supported_facts and supporting_evidence_ids must be non-empty and missing_fact must be empty.
For INSUFFICIENT, supporting_evidence_ids must be empty and missing_fact must be non-empty; directly_supported_facts may contain related facts or may be empty when no relevant fact exists.
Before returning, silently verify all three consistency checks:
1. SUFFICIENT => one or more minimal supporting IDs AND an empty missing_fact.
2. INSUFFICIENT => zero supporting IDs AND a specific non-empty missing_fact.
3. Every selected ID is necessary for at least one listed directly supported fact.
Do not use Markdown fences. Do not include text before or after the JSON.""" + "\n\n" + _ANSWERABILITY_CONTRACT

EVIDENCE_REPAIR_SYSTEM_PROMPT = """You are the final consistency validator for a company-evidence decision.
Re-evaluate the employee question against only the supplied company evidence. The earlier
decision was rejected as structurally contradictory or malformed; do not copy its decision.

Return one strict JSON object with these base fields AND the four answer-scope fields below:
{"requested_claim":"non-empty string","directly_supported_facts":["non-empty strings"],"supporting_evidence_ids":[1],"missing_fact":"string","decision":"SUFFICIENT or INSUFFICIENT"}

Choose SUFFICIENT only when the evidence directly establishes the requested claim. Then
include the smallest necessary set of one-based evidence IDs and leave missing_fact empty.
Choose INSUFFICIENT whenever a material fact is absent or the meaning is genuinely ambiguous.
Then use no supporting evidence IDs and name a specific non-empty missing_fact. Related facts
may be listed but cannot substitute for the missing fact. Ignore distractors. Never use general
knowledge or infer an unstated company rule.

Apply these general direct-support rules:
- Explicit "may" or "permitted" language directly answers whether the same subject is allowed
  to do something. Include any stated approval, frequency, location, or equipment conditions in
  the grounded answer; their presence does not make the documented permission insufficient.
- A request for a procedure requires documented steps, channel, or submission method. A rule
  that only says when approval is required does not establish an otherwise unstated procedure.
- Do not demand unrelated details or every imaginable exception once the exact requested claim
  and its stated conditions are established.

Do not use Markdown or any surrounding text.""" + "\n\n" + _ANSWERABILITY_CONTRACT

# Versioned extension: original five-field records remain readable as GENERAL
# for reference fixtures. New model responses must express scope explicitly.
_SCOPE_CONTRACT = """
ANSWER SCOPE CONTRACT (requires answer_scope; policy-specific fields depend on scope):
answer_scope: GENERAL, POLICY_REPORTING, or PERSONAL_ELIGIBILITY.
policy_support: SUPPORTED, UNSUPPORTED, or NOT_APPLICABLE.
personal_eligibility: UNKNOWN, VERIFIED, or NOT_APPLICABLE.
conditions: array of exact condition text preserved in directly_supported_facts.
For ordinary questions about a documented allowance or rule, use POLICY_REPORTING:
judge whether the conditional rule can be reported, not whether the employee qualifies.
SUFFICIENT requires SUPPORTED policy, supporting IDs, and facts retaining every condition.
UNKNOWN personal eligibility does not prevent reporting that conditional rule.
For explicit verification of personal/group eligibility use PERSONAL_ELIGIBILITY.
No verified personal facts are supplied in this request: eligibility must be UNKNOWN
and PERSONAL_ELIGIBILITY must be INSUFFICIENT. Never infer employment status from role.
For problem solving use GENERAL and the unchanged direct-relevance rules.
GENERAL can report directly supported procedural guidance and its documented
triggering conditions. policy_support may be SUPPORTED for that guidance or
NOT_APPLICABLE; personal_eligibility must be UNKNOWN or NOT_APPLICABLE, never VERIFIED.
Preserve any procedural conditions in facts and cite their supporting evidence.
Do not convert an INSUFFICIENT decision by dropping its missing fact.
For GENERAL, the original five fields plus answer_scope are enough; do not require
irrelevant eligibility metadata. If documenting procedural conditions, return all
nine fields with UNKNOWN or NOT_APPLICABLE eligibility and condition-preserving facts.
POLICY_REPORTING and PERSONAL_ELIGIBILITY must return all nine fields.
No other keys are permitted.
"""
EVIDENCE_SYSTEM_PROMPT += _SCOPE_CONTRACT
EVIDENCE_REPAIR_SYSTEM_PROMPT += _SCOPE_CONTRACT
EVIDENCE_RESPONSE_SCHEMA["required"] += ["answer_scope"]
EVIDENCE_RESPONSE_SCHEMA["properties"].update({
    "answer_scope": {"enum": [s.value for s in EvidenceAnswerScope]},
    "policy_support": {"enum": ["SUPPORTED", "UNSUPPORTED", "NOT_APPLICABLE"]},
    "personal_eligibility": {"enum": ["UNKNOWN", "VERIFIED", "NOT_APPLICABLE"]},
    "conditions": {"type": "array", "items": {"type": "string", "minLength": 1}},
})
EVIDENCE_RESPONSE_SCHEMA["oneOf"] = [
    {"properties": {"answer_scope": {"const": "GENERAL"}},
     "not": {"anyOf": [{"required": [key]} for key in ("policy_support", "personal_eligibility", "conditions")]}},
    {"required": ["policy_support", "personal_eligibility", "conditions"]},
]


class EvidenceEvaluationError(ValueError):
    """Raised when an evaluator result cannot be trusted for routing."""


def rejected_evidence_fields(raw: str) -> dict:
    """Non-authoritative diagnostic shape only: no free text or raw output."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return {"json_object": False}
    if not isinstance(value, dict):
        return {"json_object": False}
    result = {"json_object": True, "validated": False}
    for key, allowed in {
        "answer_scope": {s.value for s in EvidenceAnswerScope},
        "policy_support": {"SUPPORTED", "UNSUPPORTED", "NOT_APPLICABLE"},
        "personal_eligibility": {"UNKNOWN", "VERIFIED", "NOT_APPLICABLE"},
        "decision": {"SUFFICIENT", "INSUFFICIENT"},
    }.items():
        item = value.get(key)
        result[key] = item if isinstance(item, str) and item in allowed else "INVALID_OR_MISSING"
    for key in ("conditions", "directly_supported_facts", "supporting_evidence_ids"):
        item = value.get(key)
        result[key + "_count"] = min(len(item), 1000) if isinstance(item, list) else None
    return result


@dataclass(frozen=True)
class EvidenceEvaluationDiagnostics:
    """Evaluation-only details; never included in employee-facing answers."""

    raw_output: str | None
    exception_type: str | None
    validation_error_message: str | None
    provider_request_failed: bool
    final_decision: EvidenceStatus


class EvidenceEvaluator(Protocol):
    """Decides whether retrieved evidence is sufficient for an exact question."""

    @property
    def evaluator_name(self) -> str: ...

    def evaluate(
        self, question: str, results: list[RetrievalResult]
    ) -> EvidenceDecision: ...


def build_evidence_user_prompt(
    question: str, results: list[RetrievalResult]
) -> str:
    contexts: list[str] = []
    for number, result in enumerate(results, start=1):
        chunk = result.chunk
        contexts.append(
            f"[Evidence {number}]\n"
            f"Source: {chunk.source}\n"
            f"Document: {chunk.title}\n"
            f"Section: {chunk.section}\n"
            "<<<BEGIN COMPANY EVIDENCE>>>\n"
            f"{chunk.text}\n"
            "<<<END COMPANY EVIDENCE>>>"
        )
    evidence = "\n\n".join(contexts) or "(No company evidence was retrieved.)"
    return (
        f"Employee question:\n{question.strip()}\n\n"
        f"Retrieved company evidence:\n\n{evidence}\n\n"
        "Classify whether this evidence is sufficient to answer the exact question."
    )


def build_evidence_repair_prompt(
    question: str,
    results: list[RetrievalResult],
    validation_error: EvidenceEvaluationError,
) -> str:
    """Request one bounded contract correction using the same trusted inputs."""
    return (
        build_evidence_user_prompt(question, results)
        + "\n\nReturn a fresh, internally consistent decision. Validation issue in the "
        "earlier decision: "
        + str(validation_error)
    )


def parse_evidence_decision(
    raw_output: str, evidence_count: int | None = None,
    *, verified_personal_facts: tuple[str, ...] = (), require_scope: bool = False,
    _defer_condition_wording: bool = False,
) -> EvidenceDecision:
    """Strictly validate a classifier response; ambiguity is never accepted."""
    if not isinstance(raw_output, str) or not raw_output.strip():
        raise EvidenceEvaluationError("Evidence evaluator returned empty output")
    try:
        value = json.loads(raw_output)
    except json.JSONDecodeError as error:
        raise EvidenceEvaluationError("Evidence evaluator did not return strict JSON") from error
    if not isinstance(value, dict):
        raise EvidenceEvaluationError("Evidence decision must be a JSON object")
    required_keys = {
        "requested_claim",
        "directly_supported_facts",
        "supporting_evidence_ids",
        "missing_fact",
        "decision",
    }
    scope_keys = {"answer_scope", "policy_support", "personal_eligibility", "conditions"}
    if require_scope and "answer_scope" not in value:
        raise EvidenceEvaluationError("Explicit answer scope is required")
    if set(value) not in (required_keys, required_keys | {"answer_scope"}, required_keys | scope_keys):
        raise EvidenceEvaluationError(
            "Evidence decision must contain exactly requested_claim, "
            "directly_supported_facts, supporting_evidence_ids, missing_fact, "
            "and decision"
        )
    if not isinstance(value["requested_claim"], str) or not value[
        "requested_claim"
    ].strip():
        raise EvidenceEvaluationError("requested_claim must be a non-empty string")
    facts = value["directly_supported_facts"]
    if not isinstance(facts, list) or not all(
        isinstance(fact, str) and fact.strip() for fact in facts
    ):
        raise EvidenceEvaluationError(
            "directly_supported_facts must be a list of non-empty strings"
        )
    supporting_ids = value["supporting_evidence_ids"]
    if not isinstance(supporting_ids, list) or not all(
        isinstance(evidence_id, int) and not isinstance(evidence_id, bool)
        for evidence_id in supporting_ids
    ):
        raise EvidenceEvaluationError(
            "supporting_evidence_ids must be a list of integers"
        )
    if len(set(supporting_ids)) != len(supporting_ids):
        raise EvidenceEvaluationError("supporting_evidence_ids cannot contain duplicates")
    if any(evidence_id < 1 for evidence_id in supporting_ids):
        raise EvidenceEvaluationError("supporting_evidence_ids must be one-based")
    if evidence_count is not None and any(
        evidence_id > evidence_count for evidence_id in supporting_ids
    ):
        raise EvidenceEvaluationError(
            "supporting_evidence_ids references evidence that was not retrieved"
        )
    if not isinstance(value["missing_fact"], str) or not isinstance(
        value["decision"], str
    ):
        raise EvidenceEvaluationError("missing_fact and decision must be strings")
    try:
        status = EvidenceStatus(value["decision"])
    except ValueError as error:
        raise EvidenceEvaluationError(
            "Evidence decision must be exactly SUFFICIENT or INSUFFICIENT"
        ) from error
    missing_fact = value["missing_fact"].strip()
    scope = EvidenceAnswerScope.GENERAL
    policy, eligibility, conditions = "NOT_APPLICABLE", "UNKNOWN", []
    if "answer_scope" in value:
        try:
            scope = EvidenceAnswerScope(value["answer_scope"])
        except (ValueError, TypeError) as error:
            raise EvidenceEvaluationError("Invalid answer scope") from error
        if not scope_keys <= set(value):
            if scope is not EvidenceAnswerScope.GENERAL:
                raise EvidenceEvaluationError("Policy and eligibility scopes require explicit support fields")
            eligibility = "NOT_APPLICABLE"
    if scope_keys <= set(value):
        try:
            scope = EvidenceAnswerScope(value["answer_scope"])
        except (ValueError, TypeError) as error:
            raise EvidenceEvaluationError("Invalid answer scope") from error
        policy, eligibility, conditions = value["policy_support"], value["personal_eligibility"], value["conditions"]
        if not isinstance(policy, str) or not isinstance(eligibility, str) or policy not in {"SUPPORTED", "UNSUPPORTED", "NOT_APPLICABLE"} or eligibility not in {"UNKNOWN", "VERIFIED", "NOT_APPLICABLE"}:
            raise EvidenceEvaluationError("Invalid policy or eligibility support")
        if not isinstance(conditions, list) or not all(isinstance(c, str) and c.strip() for c in conditions):
            raise EvidenceEvaluationError("Invalid policy conditions")
        if eligibility == "VERIFIED" and not verified_personal_facts:
            raise EvidenceEvaluationError("Personal eligibility lacks trusted verification")
        if scope is EvidenceAnswerScope.GENERAL and eligibility == "VERIFIED":
            raise EvidenceEvaluationError("General evidence cannot assert policy eligibility")
        if status is EvidenceStatus.SUFFICIENT and scope is EvidenceAnswerScope.GENERAL and policy == "UNSUPPORTED":
            raise EvidenceEvaluationError("Sufficient guidance cannot declare unsupported policy")
        if scope is EvidenceAnswerScope.PERSONAL_ELIGIBILITY and eligibility == "NOT_APPLICABLE":
            raise EvidenceEvaluationError("Personal eligibility cannot be not applicable")
        if status is EvidenceStatus.SUFFICIENT and scope is not EvidenceAnswerScope.GENERAL:
            if policy != "SUPPORTED":
                raise EvidenceEvaluationError("Sufficient policy requires supported policy")
            if scope is EvidenceAnswerScope.PERSONAL_ELIGIBILITY and eligibility != "VERIFIED":
                raise EvidenceEvaluationError("Personal eligibility remains unknown")
        if not _defer_condition_wording and status is EvidenceStatus.SUFFICIENT and any(not any(c.casefold() in fact.casefold() for fact in facts) for c in conditions):
            raise EvidenceEvaluationError("Supported facts omitted policy conditions")
    placeholders = {"string", "non-empty string", "SUFFICIENT or INSUFFICIENT"}
    if any(text.strip() in placeholders for text in [value["requested_claim"], missing_fact, *facts]):
        raise EvidenceEvaluationError("Evidence decision contains schema placeholders")
    if status is EvidenceStatus.SUFFICIENT:
        if not facts:
            raise EvidenceEvaluationError(
                "SUFFICIENT requires at least one directly supported fact"
            )
        if missing_fact:
            raise EvidenceEvaluationError(
                "SUFFICIENT cannot contain a missing fact"
            )
        if not supporting_ids:
            raise EvidenceEvaluationError(
                "SUFFICIENT requires at least one supporting evidence ID"
            )
        reason = "Directly supported: " + "; ".join(fact.strip() for fact in facts)
    else:
        if not missing_fact:
            raise EvidenceEvaluationError(
                "INSUFFICIENT requires a non-empty missing fact"
            )
        if supporting_ids:
            raise EvidenceEvaluationError(
                "INSUFFICIENT cannot contain supporting evidence IDs"
            )
        if facts:
            reason = (
                "Related evidence: "
                + "; ".join(fact.strip() for fact in facts)
                + f" Missing: {missing_fact}"
            )
        else:
            reason = f"Missing: {missing_fact}"
    return EvidenceDecision(
        status=status,
        reason=reason,
        missing_information=missing_fact,
        supporting_evidence_ids=tuple(supporting_ids),
        answer_scope=scope, policy_support=policy,
        personal_eligibility=eligibility, conditions=tuple(conditions),
    )


class LLMEvidenceEvaluator:
    def validate_conditions(self, question, answer, conditions, results):
        from knowvia.condition_preservation import validate_condition_preservation
        try:
            validate_condition_preservation(self.generator, question, answer, conditions, results)
        except ValueError:
            raise ValueError("Grounded policy answer omitted a validated condition or could not be validated") from None

    """Evidence classifier backed by a replaceable local generation provider."""

    def __init__(self, generator: GenerationProvider, *, require_scope: bool = False) -> None:
        self.generator = generator
        self.require_scope = require_scope

    def _parse(self, raw, results, question=None):
        # Only this source-aware path may replace literal containment with the
        # existing bounded semantic check. Standalone parsing stays strict.
        decision = parse_evidence_decision(raw, len(results), require_scope=self.require_scope,
                                           _defer_condition_wording=True)
        constraint = question_scope_constraint(question) if self.require_scope and question is not None else None
        if constraint is not None and decision.answer_scope is not constraint:
            raise EvidenceEvaluationError(f"Answer scope conflicts with question intent; expected {constraint.value}")
        if decision.status is EvidenceStatus.SUFFICIENT:
            selected = [results[i - 1].chunk.text.casefold() for i in decision.supporting_evidence_ids]
            if any(not any(condition.casefold() in text for text in selected) for condition in decision.conditions):
                raise EvidenceEvaluationError("Policy condition is not anchored in supporting evidence")
            facts = json.loads(raw)["directly_supported_facts"]
            if any(not any(condition.casefold() in fact.casefold() for fact in facts)
                   for condition in decision.conditions):
                from knowvia.condition_preservation import validate_condition_preservation
                try:
                    validate_condition_preservation(
                        self.generator, question or "", "\n".join(facts), decision.conditions,
                        [results[i - 1] for i in decision.supporting_evidence_ids],
                    )
                except ValueError:
                    raise EvidenceEvaluationError("Supported facts omitted or altered policy conditions") from None
        return decision

    @property
    def evaluator_name(self) -> str:
        return f"llm:{self.generator.model_name}"

    def evaluate(
        self, question: str, results: list[RetrievalResult]
    ) -> EvidenceDecision:
        constraint = scope_constraint_instruction(question) if self.require_scope else ''
        raw_output = self.generator.generate(
            EVIDENCE_SYSTEM_PROMPT,
            build_evidence_user_prompt(question, results) + constraint,
        )
        try:
            return self._parse(raw_output, results, question)
        except EvidenceEvaluationError as error:
            repaired_output = self.generator.generate(
                EVIDENCE_REPAIR_SYSTEM_PROMPT,
                build_evidence_repair_prompt(question, results, error) + constraint,
            )
            try:
                return self._parse(repaired_output, results, question)
            except EvidenceEvaluationError:
                recovered = recover_explicit_first_person_permission(question, results)
                if recovered is not None and not self.require_scope:
                    return recovered
                raise

    def evaluate_with_diagnostics(
        self, question: str, results: list[RetrievalResult]
    ) -> tuple[EvidenceDecision, EvidenceEvaluationDiagnostics]:
        """Evaluate while retaining failure details for offline evaluation only."""
        constraint = scope_constraint_instruction(question) if self.require_scope else ''
        if not results:
            decision = EvidenceDecision(
                status=EvidenceStatus.INSUFFICIENT,
                reason="No company evidence was retrieved.",
                missing_information="Company evidence that answers the question.",
            )
            return decision, EvidenceEvaluationDiagnostics(
                raw_output=None,
                exception_type=None,
                validation_error_message=None,
                provider_request_failed=False,
                final_decision=decision.status,
            )

        try:
            raw_output = self.generator.generate(
                EVIDENCE_SYSTEM_PROMPT,
                build_evidence_user_prompt(question, results) + constraint,
            )
        except Exception as error:
            decision = _invalid_evidence_decision()
            return decision, EvidenceEvaluationDiagnostics(
                raw_output=None,
                exception_type=type(error).__name__,
                validation_error_message=str(error),
                provider_request_failed=True,
                final_decision=decision.status,
            )

        try:
            decision = self._parse(raw_output, results, question)
            return decision, EvidenceEvaluationDiagnostics(
                raw_output=raw_output,
                exception_type=None,
                validation_error_message=None,
                provider_request_failed=False,
                final_decision=decision.status,
            )
        except EvidenceEvaluationError as error:
            try:
                raw_output = self.generator.generate(
                    EVIDENCE_REPAIR_SYSTEM_PROMPT,
                    build_evidence_repair_prompt(question, results, error) + constraint,
                )
            except Exception as provider_error:
                decision = _invalid_evidence_decision()
                return decision, EvidenceEvaluationDiagnostics(
                    raw_output=None,
                    exception_type=type(provider_error).__name__,
                    validation_error_message=str(provider_error),
                    provider_request_failed=True,
                    final_decision=decision.status,
                )
            try:
                decision = self._parse(raw_output, results, question)
                return decision, EvidenceEvaluationDiagnostics(
                    raw_output=raw_output,
                    exception_type=None,
                    validation_error_message=None,
                    provider_request_failed=False,
                    final_decision=decision.status,
                )
            except Exception as repair_error:
                error = repair_error
            recovered = recover_explicit_first_person_permission(question, results)
            if recovered is not None and not self.require_scope:
                return recovered, EvidenceEvaluationDiagnostics(
                    raw_output=raw_output,
                    exception_type=None,
                    validation_error_message=None,
                    provider_request_failed=False,
                    final_decision=recovered.status,
                )
            decision = _invalid_evidence_decision()
            return decision, EvidenceEvaluationDiagnostics(
                raw_output=raw_output,
                exception_type=type(error).__name__,
                validation_error_message=str(error),
                provider_request_failed=False,
                final_decision=decision.status,
            )


_FIRST_PERSON_PERMISSION = re.compile(
    r"^\s*(?:can|may|could)\s+(?:i|we)\s+(.+?)[?!.]*\s*$", re.I
)
_EXPLICIT_PERMISSION = re.compile(
    r"\b(?:employees?|staff|workers?)\b[^.!?]*\b"
    r"(?:may|are\s+allowed\s+to|are\s+permitted\s+to)\b[^.!?]*[.!?]?",
    re.I,
)
_PERMISSION_STOPWORDS = frozenset(
    {"a", "an", "the", "to", "for", "of", "in", "on", "at", "from", "my", "our"}
)


def _permission_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", text.casefold()):
        if token in _PERMISSION_STOPWORDS:
            continue
        if token.endswith("ly") and len(token) > 4:
            token = token[:-2]
        elif token.endswith("ing") and len(token) > 5:
            token = token[:-3]
        elif token.endswith("s") and len(token) > 4:
            token = token[:-1]
        terms.add(token)
    return terms


def recover_explicit_first_person_permission(
    question: str, results: list[RetrievalResult]
) -> EvidenceDecision | None:
    """Validate a narrow explicit permission claim without trusting malformed LLM state."""
    match = _FIRST_PERSON_PERMISSION.fullmatch(question)
    if match is None:
        return None
    requested_terms = _permission_terms(match.group(1))
    if not requested_terms:
        return None
    for evidence_id, result in enumerate(results, start=1):
        for permission_match in _EXPLICIT_PERMISSION.finditer(result.chunk.text):
            fact = permission_match.group(0).strip()
            if requested_terms.issubset(_permission_terms(fact)):
                return EvidenceDecision(
                    status=EvidenceStatus.SUFFICIENT,
                    reason=f"Directly supported: {fact}",
                    missing_information="",
                    supporting_evidence_ids=(evidence_id,),
                )
    return None


def _invalid_evidence_decision() -> EvidenceDecision:
    return EvidenceDecision(
        status=EvidenceStatus.INSUFFICIENT,
        reason="Evidence evaluation could not be validated.",
        missing_information="A validated evidence-sufficiency decision.",
    )


def evaluate_evidence_safely(
    evaluator: EvidenceEvaluator,
    question: str,
    results: list[RetrievalResult],
) -> EvidenceDecision:
    """Evaluate evidence with a fail-closed boundary around every provider."""
    if not results:
        return EvidenceDecision(
            status=EvidenceStatus.INSUFFICIENT,
            reason="No company evidence was retrieved.",
            missing_information="Company evidence that answers the question.",
        )
    try:
        decision = evaluator.evaluate(question, results)
        if not isinstance(decision, EvidenceDecision):
            raise EvidenceEvaluationError("Evaluator returned an invalid decision type")
        validate_evidence_selection(decision, len(results))
        return decision
    except Exception:
        return _invalid_evidence_decision()


def validate_evidence_selection(
    decision: EvidenceDecision, evidence_count: int
) -> None:
    """Validate supporting IDs from every evaluator before routing can answer."""
    ids = decision.supporting_evidence_ids
    if not isinstance(ids, tuple) or not all(
        isinstance(evidence_id, int) and not isinstance(evidence_id, bool)
        for evidence_id in ids
    ):
        raise EvidenceEvaluationError("Supporting evidence IDs must be integer tuples")
    if len(set(ids)) != len(ids):
        raise EvidenceEvaluationError("Supporting evidence IDs cannot contain duplicates")
    if any(evidence_id < 1 or evidence_id > evidence_count for evidence_id in ids):
        raise EvidenceEvaluationError("Supporting evidence ID is out of range")
    if decision.status is EvidenceStatus.SUFFICIENT and not ids:
        raise EvidenceEvaluationError("SUFFICIENT requires supporting evidence")
    if decision.status is EvidenceStatus.INSUFFICIENT and ids:
        raise EvidenceEvaluationError("INSUFFICIENT cannot select supporting evidence")
