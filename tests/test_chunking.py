from knowvia.chunking import chunk_sections
from knowvia.models import DocumentSection


def test_one_short_section_becomes_one_chunk() -> None:
    section = DocumentSection("policy.md", "Policy", "Leave", "Take 24 days.")

    chunks = chunk_sections([section])

    assert len(chunks) == 1
    assert chunks[0].source == "policy.md"
    assert chunks[0].section == "Leave"
    assert chunks[0].text == "Take 24 days."
    assert "Document: Policy" in chunks[0].embedding_text


def test_long_section_has_minimal_fallback_without_crossing_sections() -> None:
    section = DocumentSection(
        "policy.md", "Policy", "Long section", "First paragraph.\n\nSecond paragraph."
    )

    chunks = chunk_sections([section], max_chars=18)

    assert [chunk.section for chunk in chunks] == [
        "Long section (part 1)",
        "Long section (part 2)",
    ]
    assert [chunk.text for chunk in chunks] == ["First paragraph.", "Second paragraph."]

