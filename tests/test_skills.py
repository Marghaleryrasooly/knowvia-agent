import pytest

from knowvia.skills import (
    DeterministicSkillSelector,
    SkillDefinition,
    SkillRegistry,
    SkillRegistryError,
    SkillSelectionStatus,
    SkillSummary,
    create_default_skill_registry,
)


def test_skill_definition_and_registry_separate_discovery_from_loading() -> None:
    registry = create_default_skill_registry()

    summaries = registry.discover()

    assert len(summaries) == 4
    assert all(isinstance(summary, SkillSummary) for summary in summaries)
    assert not hasattr(summaries[0], "instructions")
    assert not hasattr(summaries[0], "allowed_tools")
    assert not hasattr(summaries[0], "knowledge_sources")
    loaded = registry.load("it_support")
    assert isinstance(loaded, SkillDefinition)
    assert loaded.allowed_tools == ("create_it_ticket",)
    general = registry.load("general_company_knowledge")
    assert general.allowed_tools == ()


def test_registry_rejects_unknown_and_duplicate_skills() -> None:
    registry = create_default_skill_registry()
    with pytest.raises(SkillRegistryError, match="Unknown"):
        registry.load("unknown")
    definition = registry.load("hr")
    with pytest.raises(SkillRegistryError, match="Duplicate"):
        SkillRegistry([definition, definition])


@pytest.mark.parametrize(
    ("employee_text", "expected_status", "expected_skill"),
    [
        ("How many vacation days do I get?", SkillSelectionStatus.SELECTED, "hr"),
        ("My VPN is broken.", SkillSelectionStatus.SELECTED, "it_support"),
        ("Create an IT ticket because my VPN is broken.", SkillSelectionStatus.SELECTED, "it_support"),
        ("Can I claim a meal during a business trip?", SkillSelectionStatus.SELECTED, "finance"),
        ("What are the vacation and meal rules?", SkillSelectionStatus.AMBIGUOUS, None),
        ("Write a marketing campaign.", SkillSelectionStatus.UNSUPPORTED, None),
    ],
)
def test_deterministic_skill_selection(employee_text, expected_status, expected_skill) -> None:
    selection = DeterministicSkillSelector().select(
        employee_text, create_default_skill_registry().discover()
    )
    assert selection.status is expected_status
    assert selection.skill_id == expected_skill


def test_selector_rejects_full_definitions_at_discovery_boundary() -> None:
    with pytest.raises(TypeError, match="SkillSummary"):
        DeterministicSkillSelector().select(
            "VPN help", tuple(create_default_skill_registry().load("it_support") for _ in range(1))
        )
