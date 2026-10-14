"""Question-only scope constraints, never evidence sufficiency or eligibility facts."""
import re
from knowvia.models import EvidenceAnswerScope


def question_scope_constraint(question: str) -> EvidenceAnswerScope | None:
    """Recognize bounded grammatical intent; no policy/domain/category aliases.

    Explicit verification and personal qualifiers take precedence over quantity
    reporting. Unrecognized forms do not acquire a reporting constraint.
    """
    text = ' '.join(question.casefold().split())
    if re.search(r'\b(?:qualif(?:y|ies|ied|ication)|eligible|eligibility|entitled)\b', text):
        # Explicitly describing a rule about eligibility is not verifying a person.
        if (re.match(r'^(?:what (?:does|do) .{0,80}(?:policy|rules?) (?:say|state)|(?:describe|summarize|explain) .{0,50}(?:policy|rules?)|who is eligible|which .{0,60} are eligible)\b', text)
                and not re.search(r'\b(?:i|me|my|we|our)\b', text)):
            return EvidenceAnswerScope.POLICY_REPORTING
        return EvidenceAnswerScope.PERSONAL_ELIGIBILITY
    if (re.search(r'\b(?:given|based on)\s+(?:my|our)\b|\bmy (?:status|circumstances|contract|record|history|balance)\b', text)
            or re.search(r'\b(?:i|we)\b.{0,60}\bas (?:a|an)\b', text)
            or re.match(r'^(?:(?:am i|are we) (?:covered|included|qualified|a|an|part of)|(?:do|will|would) (?:i|we) (?:get(?!\s+to)|receive|qualify))\b', text)
            or (re.search(r'\b(?:i|my|we|our)\b', text) and re.search(r'\b(?:remaining|left|balance|accrued|already used)\b', text))):
        return EvidenceAnswerScope.PERSONAL_ELIGIBILITY
    if re.match(r'^how (?:many|much)\b.{0,100}\b(?:do|can) (?:i|we) (?:get|receive)\b', text):
        return EvidenceAnswerScope.POLICY_REPORTING
    if re.match(r'^(?:what (?:does|do) .{0,80}(?:policy|rules?) (?:say|state)|what (?:is|are) .{0,60}(?:policy|rules?)|(?:describe|summarize|explain) .{0,50}(?:policy|rules?))\b', text):
        return EvidenceAnswerScope.POLICY_REPORTING
    if (re.match(r'^what should (?:i|we) do\b', text)
            or re.match(r'^how (?:do|can|should)\b', text)
            or re.search(r'\bhow (?:can|do)\b.{0,40}\b(?:fix|resolve|troubleshoot)\b', text)):
        return EvidenceAnswerScope.GENERAL
    return None


def scope_constraint_instruction(question):
    scope = question_scope_constraint(question)
    if scope is None:
        return ''
    purpose = {
        EvidenceAnswerScope.POLICY_REPORTING: 'Personal qualification is outside this request. Judge support for reporting the documented rule with all conditions, never claim the employee qualifies.',
        EvidenceAnswerScope.PERSONAL_ELIGIBILITY: 'An applicable rule alone does not verify personal qualification. Do not assume unknown personal facts.',
        EvidenceAnswerScope.GENERAL: 'Answer the original problem or procedure; related policies alone do not establish its remedy.',
    }[scope]
    return (f'\n\nApplication question-intent constraint: answer_scope must be {scope.value}. '
            'This constrains the requested answer type, NOT evidence sufficiency. '
            'Return INSUFFICIENT if the evidence does not establish that answer. '
            + purpose)
