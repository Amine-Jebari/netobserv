"""RAG retrieval surface for the network agent.

This module is the public boundary the LangChain agent calls into. It wraps
pgvector cosine-distance queries against the ``runbook_chunks`` table and
returns typed dataclasses so the agent never has to touch psycopg directly.

The three public functions:

- ``search_runbooks(query, k)``                     - top-k semantic search.
- ``search_runbooks_filtered(query, anomaly_type)`` - same, filtered by metadata.
- ``get_full_runbook(source_file)``                 - reassembled markdown text
                                                      for citation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from psycopg.rows import dict_row

from .config import Settings, get_settings
from .db import pooled_connection
from .embeddings import embed_text


# ── Public dataclass returned from search calls ──────────────────────────────


@dataclass
class RunbookChunk:
    """A single retrieved runbook chunk plus its similarity score."""

    id: str
    source_file: str
    section_title: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0  # cosine similarity in [-1, 1]; 1.0 = identical direction.

    @property
    def cosine_distance(self) -> float:
        """Distance in [0, 2]; lower = more similar."""
        return max(0.0, 1.0 - self.score)


# ── Internal helpers ─────────────────────────────────────────────────────────


def _embed_query(query: str, settings: Settings) -> np.ndarray:
    """Embed a search query and validate the dimension matches the column."""
    if not query or not query.strip():
        raise ValueError("query must be a non-empty string")
    vec = embed_text(query, settings.embedding_model_name)
    if vec.ndim != 1:
        raise ValueError(f"expected 1-D embedding, got shape {vec.shape}")
    if vec.shape[0] != settings.embedding_dim:
        raise ValueError(
            f"embedding dim mismatch: model returned {vec.shape[0]}, "
            f"expected {settings.embedding_dim}"
        )
    return vec


def _row_to_chunk(row: dict[str, Any]) -> RunbookChunk:
    """Convert a result row (with optional ``cosine_distance`` field) to a RunbookChunk."""
    metadata = row.get("metadata") or {}
    distance = row.get("cosine_distance")
    score = float(1.0 - distance) if distance is not None else 0.0
    return RunbookChunk(
        id=str(row["id"]),
        source_file=row["source_file"],
        section_title=row["section_title"],
        content=row["content"],
        metadata=metadata if isinstance(metadata, dict) else {},
        score=score,
    )


# ── Public API ───────────────────────────────────────────────────────────────


def search_runbooks(
    query: str,
    k: int = 5,
    settings: Settings | None = None,
) -> list[RunbookChunk]:
    """Return the top-``k`` runbook chunks ranked by cosine similarity to ``query``.

    Internally uses pgvector's ``<=>`` cosine-distance operator, so results are
    sorted ascending by distance (ascending order = most similar first).
    """
    if k <= 0:
        raise ValueError("k must be > 0")
    settings = settings or get_settings()

    embedding = _embed_query(query, settings)

    sql = f"""
        SELECT
            id,
            source_file,
            section_title,
            content,
            metadata,
            (embedding <=> %s) AS cosine_distance
        FROM {settings.runbook_table}
        ORDER BY embedding <=> %s
        LIMIT %s
    """  # noqa: S608 - table name comes from trusted settings, not user input.

    with pooled_connection(settings) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (embedding, embedding, k))
            rows = cur.fetchall()

    return [_row_to_chunk(r) for r in rows]


def search_runbooks_filtered(
    query: str,
    anomaly_type: str,
    k: int = 5,
    settings: Settings | None = None,
) -> list[RunbookChunk]:
    """Top-``k`` semantic search restricted to a specific ``anomaly_type``.

    The filter matches ``metadata->>'anomaly_type'`` and benefits from the
    ``runbook_chunks_anomaly_type_idx`` btree index created in init.sql. Pass
    one of: ``"traffic"``, ``"bgp"``, ``"snmp"``.
    """
    if not anomaly_type:
        raise ValueError("anomaly_type must be a non-empty string")
    if k <= 0:
        raise ValueError("k must be > 0")
    settings = settings or get_settings()

    embedding = _embed_query(query, settings)

    sql = f"""
        SELECT
            id,
            source_file,
            section_title,
            content,
            metadata,
            (embedding <=> %s) AS cosine_distance
        FROM {settings.runbook_table}
        WHERE metadata->>'anomaly_type' = %s
        ORDER BY embedding <=> %s
        LIMIT %s
    """  # noqa: S608 - table name comes from trusted settings, not user input.

    with pooled_connection(settings) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (embedding, anomaly_type, embedding, k))
            rows = cur.fetchall()

    return [_row_to_chunk(r) for r in rows]


def get_full_runbook(
    source_file: str,
    settings: Settings | None = None,
) -> str:
    """Return the full markdown text of a runbook for citation in the agent's output.

    Resolution order:
      1. If ``RUNBOOKS_DIR`` (env / settings.runbooks_dir) contains
         ``source_file`` on disk, return its contents verbatim. This is the
         preferred path - it preserves the original markdown including
         frontmatter and code blocks.
      2. Otherwise, reassemble the runbook from the chunks stored in Postgres,
         joined in chunk-index order. The reassembled text loses formatting
         that the chunker stripped (e.g. frontmatter) but is still useful for
         the agent's citation context.
    """
    if not source_file:
        raise ValueError("source_file must be a non-empty string")
    settings = settings or get_settings()

    # Disk lookup first.
    runbooks_dir = Path(os.environ.get("RUNBOOKS_DIR", settings.runbooks_dir))
    on_disk = runbooks_dir / source_file
    if on_disk.is_file():
        return on_disk.read_text(encoding="utf-8")

    # Fallback: reassemble from Postgres.
    sql = f"""
        SELECT section_title, content, metadata
        FROM {settings.runbook_table}
        WHERE source_file = %s
        ORDER BY (metadata->>'chunk_index')::int ASC
    """  # noqa: S608 - table name comes from trusted settings, not user input.

    with pooled_connection(settings) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (source_file,))
            rows = cur.fetchall()

    if not rows:
        raise FileNotFoundError(f"runbook not found: {source_file}")

    # Reassemble: emit each section header once, then its (overlapping) chunks.
    parts: list[str] = []
    last_section: str | None = None
    for row in rows:
        section = row["section_title"]
        if section != last_section:
            parts.append(f"## {section}\n")
            last_section = section
        parts.append(row["content"])
    return "\n\n".join(parts).strip() + "\n"
