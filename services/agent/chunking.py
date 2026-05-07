"""Paragraph-aware markdown chunking for the runbook corpus.

The chunker is deliberately dependency-free (no transformers, no nltk) so the
unit tests can run in milliseconds. Token counts are approximated by whitespace
word counts; for the all-MiniLM-L6-v2 vocabulary the ratio is close to 1.3
tokens per word, so 300 word chunks land near the model's 256-token effective
window without truncation.

Public API:
    parse_frontmatter(text)   -> (metadata: dict, body: str)
    split_sections(body)      -> list[Section]
    chunk_section(section, max_tokens, overlap_tokens) -> list[str]
    chunk_runbook(path, max_tokens, overlap_tokens)    -> list[Chunk]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # PyYAML is in the package's runtime deps; the bare-config tests do not need it.
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - tests skip the frontmatter path when absent.
    yaml = None  # type: ignore[assignment]


# ── Datatypes ────────────────────────────────────────────────────────────────


@dataclass
class Section:
    title: str
    body: str


@dataclass
class Chunk:
    source_file: str
    section_title: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Frontmatter ──────────────────────────────────────────────────────────────


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return ``(metadata_dict, body_without_frontmatter)``.

    Tolerates files with no frontmatter (returns ``({}, text)``). If PyYAML is
    unavailable we still strip the frontmatter block but return an empty dict.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw = match.group(1)
    body = text[match.end() :]
    if yaml is None:
        return {}, body
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, body


# ── Section splitting ────────────────────────────────────────────────────────


_SECTION_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def split_sections(body: str) -> list[Section]:
    """Split a markdown body into ``## Header`` sections.

    Anything before the first ``## `` header is dropped (we treat the H1 title
    as the runbook name, captured via filename / frontmatter). If the body has
    no ``##`` headers, the whole body is returned as a single ``Body`` section.
    """
    matches = list(_SECTION_HEADER_RE.finditer(body))
    if not matches:
        text = body.strip()
        return [Section(title="Body", body=text)] if text else []

    sections: list[Section] = []
    for i, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section_body = body[start:end].strip()
        if section_body:
            sections.append(Section(title=title, body=section_body))
    return sections


# ── Chunking ─────────────────────────────────────────────────────────────────


_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def _word_count(text: str) -> int:
    return len(text.split())


def _split_long_paragraph(words: list[str], max_tokens: int, overlap_tokens: int) -> list[str]:
    """Word-aligned greedy split of a paragraph that on its own exceeds the budget."""
    out: list[str] = []
    n = len(words)
    if n == 0:
        return out
    i = 0
    while i < n:
        end = min(i + max_tokens, n)
        out.append(" ".join(words[i:end]))
        if end >= n:
            break
        # Step forward, leaving overlap_tokens worth of trailing context.
        step = max(1, max_tokens - overlap_tokens)
        i += step
    return out


def chunk_section(
    section: Section,
    max_tokens: int = 300,
    overlap_tokens: int = 50,
) -> list[str]:
    """Greedy paragraph-aware chunking with word-aligned overlap.

    Paragraphs are joined into chunks until adding the next paragraph would
    exceed ``max_tokens``. Chunks then begin with the trailing ``overlap_tokens``
    words of the previous chunk so semantic context is preserved. A paragraph
    longer than ``max_tokens`` on its own is split on word boundaries.
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens must be > 0")
    if overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must be in [0, max_tokens)")

    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(section.body) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_count = 0

    def flush() -> None:
        nonlocal current, current_count
        if current:
            chunks.append("\n\n".join(current).strip())
            current = []
            current_count = 0

    for para in paragraphs:
        para_words = para.split()
        para_count = len(para_words)

        # Oversized single paragraph: flush whatever is buffered, split it
        # explicitly, do not try to merge with neighbours.
        if para_count > max_tokens:
            flush()
            for piece in _split_long_paragraph(para_words, max_tokens, overlap_tokens):
                chunks.append(piece)
            continue

        if current_count + para_count <= max_tokens:
            current.append(para)
            current_count += para_count
            continue

        # Doesn't fit: flush, then seed the next chunk with overlap context.
        flush()
        if overlap_tokens > 0 and chunks:
            tail_words = chunks[-1].split()[-overlap_tokens:]
            if tail_words:
                overlap_text = " ".join(tail_words)
                current.append(overlap_text)
                current_count += len(tail_words)
        current.append(para)
        current_count += para_count

    flush()
    return chunks


def chunk_runbook(
    path: Path,
    max_tokens: int = 300,
    overlap_tokens: int = 50,
) -> list[Chunk]:
    """Read a markdown runbook from disk and return its chunks.

    Each chunk inherits the file's frontmatter as metadata, plus the chunk
    index inside the file. Filename (without the .md extension) is folded into
    metadata as ``source_stem`` so the agent can cite human-readable names.
    """
    text = path.read_text(encoding="utf-8")
    metadata, body = parse_frontmatter(text)
    sections = split_sections(body)

    chunks: list[Chunk] = []
    chunk_idx = 0
    for section in sections:
        for content in chunk_section(section, max_tokens, overlap_tokens):
            meta = dict(metadata)
            meta["source_stem"] = path.stem
            meta["section_title"] = section.title
            meta["chunk_index"] = chunk_idx
            chunks.append(
                Chunk(
                    source_file=path.name,
                    section_title=section.title,
                    content=content,
                    metadata=meta,
                )
            )
            chunk_idx += 1
    return chunks
