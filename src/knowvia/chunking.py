from __future__ import annotations

import hashlib

from knowvia.models import Chunk, DocumentSection


def _chunk_id(section: DocumentSection, part: int) -> str:
    key = f"{section.source}\0{section.heading}\0{part}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def chunk_sections(
    sections: list[DocumentSection], max_chars: int = 4_000
) -> list[Chunk]:
    """Create one chunk per section, with a small paragraph fallback.

    Every current NovaTech section fits in one chunk. If a future section is
    unusually long, adjacent paragraphs are grouped without crossing the
    section boundary.
    """
    chunks: list[Chunk] = []
    for section in sections:
        parts = _split_long_section(section.text, max_chars)
        for index, text in enumerate(parts, start=1):
            section_name = section.heading
            if len(parts) > 1:
                section_name = f"{section.heading} (part {index})"
            chunks.append(
                Chunk(
                    id=_chunk_id(section, index),
                    source=section.source,
                    title=section.title,
                    section=section_name,
                    text=text,
                )
            )
    return chunks


def _split_long_section(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if len(text) <= max_chars:
        return [text]

    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    parts: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                parts.append(current)
                current = ""
            parts.extend(
                paragraph[start : start + max_chars]
                for start in range(0, len(paragraph), max_chars)
            )
        elif not current:
            current = paragraph
        elif len(current) + 2 + len(paragraph) <= max_chars:
            current += f"\n\n{paragraph}"
        else:
            parts.append(current)
            current = paragraph
    if current:
        parts.append(current)
    return parts

