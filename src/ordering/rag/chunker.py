"""Heading-aware chunking.

Splits on Markdown headings so each chunk carries its section path (e.g. "Supplier Payment
Policy > Failed payments"), then packs paragraphs/list items into ~``max_tokens`` windows with
~15% overlap. Section paths are indexed with higher weight for keyword search and shown in
citations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def approx_tokens(text: str) -> int:
    return max(1, int(len(text.split()) * 1.3))


@dataclass
class Chunk:
    index: int
    section_path: str
    content: str
    token_count: int


def _sections(text: str) -> list[tuple[str, str]]:
    stack: list[tuple[int, str]] = []
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.split("\n"):
        m = _HEADING.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            stack = [s for s in stack if s[0] < level] + [(level, title)]
            sections.append((" > ".join(t for _, t in stack), []))
        else:
            sections[-1][1].append(line)
    return [(path, "\n".join(lines).strip()) for path, lines in sections if "\n".join(lines).strip()]


def _blocks(body: str) -> list[str]:
    blocks, current = [], []
    for line in body.split("\n"):
        starts_item = bool(re.match(r"^\s*([-*]|\d+\.)\s+", line))
        if not line.strip() or (starts_item and current):
            if current:
                blocks.append("\n".join(current).strip())
            current = [line] if line.strip() else []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


def chunk_text(text: str, max_tokens: int = 350, overlap_ratio: float = 0.15) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section_path, body in _sections(text):
        blocks = _blocks(body)
        window: list[str] = []
        for block in blocks:
            candidate = [*window, block]
            if window and approx_tokens("\n\n".join(candidate)) > max_tokens:
                content = "\n\n".join(window)
                chunks.append(Chunk(len(chunks), section_path, content, approx_tokens(content)))
                # overlap: carry trailing blocks worth ~overlap_ratio of the window
                carry, budget = [], int(max_tokens * overlap_ratio)
                for prev in reversed(window):
                    if approx_tokens("\n\n".join([prev, *carry])) > budget:
                        break
                    carry.insert(0, prev)
                window = [*carry, block]
            else:
                window = candidate
        if window:
            content = "\n\n".join(window)
            chunks.append(Chunk(len(chunks), section_path, content, approx_tokens(content)))
    return chunks
