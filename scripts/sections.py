"""Canonical Markdown chapter parsing shared by TTS and HTML rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    index: int
    title: str | None
    body: str


def parse_markdown_sections(markdown: str) -> list[Section]:
    """Parse preamble plus ``##`` chapters without losing chapter body text."""
    text = str(markdown or "").strip()
    if not text:
        return []
    parts = re.split(r"\n(?=## )", text)
    sections: list[Section] = []
    chapter_index = 0
    before_first_heading = True
    for raw_part in parts:
        part = raw_part.strip()
        if not part:
            continue
        if part.startswith("## "):
            before_first_heading = False
            heading, separator, body = part.partition("\n")
            sections.append(Section(
                index=chapter_index,
                title=heading[3:].strip(),
                body=body.strip() if separator else "",
            ))
            chapter_index += 1
        elif before_first_heading:
            sections.append(Section(index=-1, title=None, body=part))
        elif sections:
            previous = sections[-1]
            sections[-1] = Section(
                index=previous.index,
                title=previous.title,
                body=f"{previous.body}\n{part}".strip(),
            )
    return sections


def chapter_sections(markdown: str) -> list[Section]:
    """Return only titled chapters from the canonical parse."""
    return [section for section in parse_markdown_sections(markdown) if section.title is not None]


def preamble_text(markdown: str) -> str:
    """Return the canonical pre-heading prose, if present."""
    return "\n\n".join(
        section.body for section in parse_markdown_sections(markdown)
        if section.title is None and section.body
    )


def chapter_body_map(markdown: str) -> dict[str, str]:
    """Map chapter titles to bodies using the same parser as TTS and HTML."""
    return {
        section.title: section.body
        for section in chapter_sections(markdown)
    }
