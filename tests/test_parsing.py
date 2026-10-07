from pathlib import Path

import pytest

from knowvia.parsing import load_markdown_documents, parse_markdown

DATASET = Path(__file__).parents[1] / "knowvia_m1_dataset"


def test_parses_title_section_and_markdown_list() -> None:
    sections = parse_markdown(DATASET / "05_it_setup_guide.md")

    vpn = next(section for section in sections if section.heading == "VPN")
    assert vpn.source == "05_it_setup_guide.md"
    assert vpn.title == "NovaTech IT Setup Guide"
    assert "1. Confirm that the internet connection is working." in vpn.text
    assert "contact the IT Service Desk" in vpn.text


def test_loads_policy_documents_but_not_dataset_readme() -> None:
    sections = load_markdown_documents(DATASET)

    assert len(sections) == 37
    assert {section.source for section in sections} == {
        f"{number:02d}_{name}.md"
        for number, name in [
            (1, "employee_handbook"),
            (2, "remote_work_policy"),
            (3, "it_access_security_policy"),
            (4, "employee_benefits_guide"),
            (5, "it_setup_guide"),
            (6, "engineering_practices"),
            (7, "information_classification_policy"),
            (8, "expense_policy"),
        ]
    }


def test_missing_document_title_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "untitled.md"
    path.write_text("## Section\nText", encoding="utf-8")

    with pytest.raises(ValueError, match="document title"):
        parse_markdown(path)
