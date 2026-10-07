from __future__ import annotations

import re
from pathlib import Path

from knowvia.models import DocumentSection

HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def parse_markdown(path: Path) -> list[DocumentSection]:
    """Parse a Markdown file into its level-two sections.

    The NovaTech dataset uses one ``#`` document title followed by ``##``
    sections. Deeper headings, if added later, remain part of their parent
    section's text rather than creating unrelated chunks.
    """
    title: str | None = None
    current_heading: str | None = None
    current_lines: list[str] = []
    sections: list[DocumentSection] = []

    def finish_section() -> None:
        if current_heading is None:
            return
        text = "\n".join(current_lines).strip()
        if text:
            sections.append(
                DocumentSection(
                    source=path.name,
                    title=title or path.stem,
                    heading=current_heading,
                    text=text,
                )
            )

    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = HEADING_PATTERN.match(line)
        if match and len(match.group(1)) == 1 and title is None:
            title = match.group(2)
        elif match and len(match.group(1)) == 2:
            finish_section()
            current_heading = match.group(2)
            current_lines = []
        elif current_heading is not None:
            current_lines.append(line)

    finish_section()

    if title is None:
        raise ValueError(f"Missing '# ' document title in {path}")
    return sections


def load_markdown_documents(directory: Path) -> list[DocumentSection]:
    """Load all policy Markdown files in stable filename order."""
    sections: list[DocumentSection] = []
    for path in sorted(directory.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        sections.extend(parse_markdown(path))
    return sections

