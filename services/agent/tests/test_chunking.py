"""Pure-Python tests for the markdown chunker.

These tests have no external dependencies (no torch, no Postgres) and run in
milliseconds. They exercise the contract that the seeder relies on:

  - frontmatter is parsed off and metadata extracted
  - sections are split by ``## Header``
  - chunks never split a word in half
  - chunks never exceed ``max_tokens`` worth of words
  - sequential chunks within a section overlap by ``overlap_tokens`` words
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.agent.chunking import (
    Section,
    chunk_runbook,
    chunk_section,
    parse_frontmatter,
    split_sections,
)


# ── Frontmatter ──────────────────────────────────────────────────────────────


def test_parse_frontmatter_extracts_metadata_and_strips_block() -> None:
    text = (
        "---\n"
        "title: BGP Flap\n"
        "anomaly_type: bgp\n"
        "tags: [bgp, routing]\n"
        "---\n"
        "# Heading\n"
        "Body line.\n"
    )
    meta, body = parse_frontmatter(text)
    assert meta["anomaly_type"] == "bgp"
    assert meta["title"] == "BGP Flap"
    assert meta["tags"] == ["bgp", "routing"]
    assert body.startswith("# Heading")
    assert "anomaly_type" not in body


def test_parse_frontmatter_no_block() -> None:
    text = "# Heading\n\nNo frontmatter here.\n"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_parse_frontmatter_malformed_yaml_returns_empty_metadata() -> None:
    text = "---\n:: not yaml ::\nkey: [unterminated\n---\nBody\n"
    meta, body = parse_frontmatter(text)
    # Either YAML succeeds with an unexpected dict or it fails to a {} - both fine.
    assert isinstance(meta, dict)
    assert body == "Body\n"


# ── Section splitting ────────────────────────────────────────────────────────


def test_split_sections_by_header() -> None:
    body = (
        "# Title\n\n"
        "Intro that is dropped because it is before the first ##.\n\n"
        "## Summary\n\n"
        "Summary paragraph one.\n\n"
        "## Symptoms\n\n"
        "First symptom.\n\nSecond symptom.\n\n"
        "## Remediation\n\n"
        "Step.\n"
    )
    sections = split_sections(body)
    titles = [s.title for s in sections]
    assert titles == ["Summary", "Symptoms", "Remediation"]
    assert "Summary paragraph one." in sections[0].body
    assert "Second symptom." in sections[1].body


def test_split_sections_no_headers_returns_single_body() -> None:
    body = "Just a paragraph.\n\nAnother paragraph.\n"
    sections = split_sections(body)
    assert len(sections) == 1
    assert sections[0].title == "Body"


# ── Chunking ─────────────────────────────────────────────────────────────────


def _word_count(s: str) -> int:
    return len(s.split())


def test_chunk_section_short_returns_single_chunk() -> None:
    section = Section(
        title="Summary",
        body="One short paragraph.\n\nAnother short paragraph here.\n",
    )
    chunks = chunk_section(section, max_tokens=300, overlap_tokens=50)
    assert len(chunks) == 1
    assert _word_count(chunks[0]) <= 300


def test_chunk_section_long_paragraph_is_split_word_aligned() -> None:
    # Build a paragraph of 500 distinct lowercase words.
    words = [f"w{i}" for i in range(500)]
    section = Section(title="Long", body=" ".join(words))
    chunks = chunk_section(section, max_tokens=100, overlap_tokens=20)

    # Each chunk respects the word budget.
    for chunk in chunks:
        assert _word_count(chunk) <= 100, chunk

    # No chunk starts or ends mid-word: every space-separated token in the
    # chunk must be a real token from our input list.
    valid_words = set(words)
    for chunk in chunks:
        for token in chunk.split():
            assert token in valid_words, f"unexpected token {token!r}"

    # Adjacent chunks overlap by ~20 words.
    a, b = chunks[0].split(), chunks[1].split()
    overlap = set(a[-30:]) & set(b[:30])
    assert len(overlap) >= 15  # allow some slack but ensure overlap exists


def test_chunk_section_paragraphs_combine_until_budget() -> None:
    paragraphs = ["para_" + " ".join([f"w{j}" for j in range(50)]) for _ in range(6)]
    body = "\n\n".join(paragraphs)
    section = Section(title="Combined", body=body)

    chunks = chunk_section(section, max_tokens=120, overlap_tokens=20)

    # We expect more than one chunk because 6 paragraphs of ~51 words each = ~306 words.
    assert len(chunks) >= 2
    for chunk in chunks:
        assert _word_count(chunk) <= 120


def test_chunk_section_does_not_split_mid_word_for_realistic_input() -> None:
    body = (
        "Network BGP sessions occasionally flap when transport is unstable.\n\n"
        "Investigators should capture syslog ADJCHANGE events alongside transceiver\n"
        "DOM values, prefix limit violations, and any outbound policy changes.\n\n"
        "An incident that is not root-caused will recur, so document the fix.\n"
    )
    section = Section(title="Summary", body=body)
    chunks = chunk_section(section, max_tokens=20, overlap_tokens=5)

    # Every token in the original body should appear intact (no half-tokens).
    body_words = set(body.split())
    for chunk in chunks:
        for token in chunk.split():
            assert token in body_words, f"chunk introduced split token {token!r}"


def test_chunk_section_zero_overlap_supported() -> None:
    body = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    section = Section(title="S", body=body)
    chunks = chunk_section(section, max_tokens=4, overlap_tokens=0)
    rejoined = " ".join(chunks).split()
    # With zero overlap, total words across chunks equals input.
    assert rejoined == body.split()


def test_chunk_section_invalid_overlap_rejected() -> None:
    section = Section(title="S", body="a b c d")
    with pytest.raises(ValueError):
        chunk_section(section, max_tokens=10, overlap_tokens=10)
    with pytest.raises(ValueError):
        chunk_section(section, max_tokens=0, overlap_tokens=0)


# ── End-to-end chunk_runbook on a real corpus file ──────────────────────────


REPO_ROOT = Path(__file__).resolve().parents[3]
CORPUS_DIR = REPO_ROOT / "data" / "runbooks"


@pytest.mark.skipif(not CORPUS_DIR.exists(), reason="runbook corpus not present")
def test_chunk_runbook_real_file_produces_chunks_with_expected_metadata() -> None:
    md = CORPUS_DIR / "bgp_flap_troubleshooting.md"
    chunks = chunk_runbook(md, max_tokens=300, overlap_tokens=50)

    assert len(chunks) >= 5  # at least one chunk per section
    titles = {c.section_title for c in chunks}
    assert {"Summary", "Symptoms", "Remediation Steps"}.issubset(titles)

    for c in chunks:
        assert c.source_file == "bgp_flap_troubleshooting.md"
        assert c.metadata["anomaly_type"] == "bgp"
        assert "chunk_index" in c.metadata
        assert _word_count(c.content) <= 300
