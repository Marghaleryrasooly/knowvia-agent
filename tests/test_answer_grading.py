from knowvia.answer_grading import DeterministicAnswerGrader
from knowvia.eval_dataset import EvaluationCase, ExpectedSource


def case(**overrides) -> EvaluationCase:
    values = dict(
        id="case", question="How much?", expected_behavior="ANSWER",
        expected_sources=(ExpectedSource("policy.md", "Leave"),),
        reference_answer="24 days", answer_criteria=({"type": "number", "values": ["24"]},),
        forbidden_terms=(), forbidden_claims=(), tags=(), notes="",
    )
    values.update(overrides)
    return EvaluationCase(**values)


def test_deterministic_grader_supports_general_criteria() -> None:
    item = case(answer_criteria=(
        {"type": "contains_all", "values": ["manager", "approval"]},
        {"type": "contains_any", "values": ["five days", "5 days"]},
        {"type": "ordered_terms", "values": ["restart", "contact service desk"]},
    ))
    grade = DeterministicAnswerGrader().grade(
        item, "Get manager approval for 5 days. Restart, then contact Service Desk."
    )
    assert grade.passed


def test_forbidden_claim_fails_even_when_expected_fact_is_present() -> None:
    item = case(forbidden_claims=("unused vacation always carries over automatically",))
    grade = DeterministicAnswerGrader().grade(
        item, "Employees get 24 days; unused vacation always carries over automatically."
    )
    assert grade.criteria_passed
    assert not grade.forbidden_passed
    assert not grade.passed
    assert grade.matched_forbidden == ("unused vacation always carries over automatically",)
