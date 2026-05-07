"""RAG retrieval tests.

Two flavours are exercised:

  - **Unit tests** (always run): patch the psycopg connection pool to capture
    the SQL the rag layer would execute and the parameters it would bind. We
    verify the cosine-distance operator, the metadata filter, and the LIMIT
    binding. These do not require Postgres or sentence-transformers.

  - **Integration test** (opt-in): when ``RUN_RAG_INTEGRATION=1`` is set and a
    Postgres + sentence-transformers environment is available, the test seeds
    a tiny corpus and verifies that semantically-relevant queries return the
    expected chunks.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

pytest.importorskip("psycopg", reason="psycopg not installed — run `pip install -e .[dev]` first")

from services.agent.config import Settings
from services.agent.rag import (
    RunbookChunk,
    _row_to_chunk,
    get_full_runbook,
    search_runbooks,
    search_runbooks_filtered,
)


# ── Test doubles ─────────────────────────────────────────────────────────────


class _FakeCursor:
    """Captures the SQL/params and returns a canned row set."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed_sql: str | None = None
        self.executed_params: tuple[Any, ...] | None = None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed_sql = sql
        self.executed_params = tuple(params or ())

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self, row_factory: Any = None) -> _FakeCursor:  # noqa: ARG002
        return self._cursor

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


@contextmanager
def _fake_pooled_connection(cursor: _FakeCursor):
    yield _FakeConnection(cursor)


# ── Unit: row dataclass conversion ───────────────────────────────────────────


def test_row_to_chunk_score_is_one_minus_distance() -> None:
    row = {
        "id": uuid.uuid4(),
        "source_file": "bgp_flap_troubleshooting.md",
        "section_title": "Summary",
        "content": "A BGP flap occurs when ...",
        "metadata": {"anomaly_type": "bgp"},
        "cosine_distance": 0.2,
    }
    chunk = _row_to_chunk(row)
    assert isinstance(chunk, RunbookChunk)
    assert chunk.source_file == row["source_file"]
    assert chunk.section_title == "Summary"
    assert chunk.metadata["anomaly_type"] == "bgp"
    assert chunk.score == pytest.approx(0.8)
    assert chunk.cosine_distance == pytest.approx(0.2)


def test_row_to_chunk_handles_null_metadata() -> None:
    row = {
        "id": uuid.uuid4(),
        "source_file": "x.md",
        "section_title": "S",
        "content": "c",
        "metadata": None,
        "cosine_distance": 0.0,
    }
    chunk = _row_to_chunk(row)
    assert chunk.metadata == {}


# ── Unit: search_runbooks SQL shape ──────────────────────────────────────────


@patch("services.agent.rag.embed_text")
@patch("services.agent.rag.pooled_connection")
def test_search_runbooks_uses_cosine_distance_operator(mock_pool, mock_embed) -> None:
    mock_embed.return_value = np.zeros(384, dtype=np.float32)
    cursor = _FakeCursor(rows=[
        {
            "id": uuid.uuid4(),
            "source_file": "ddos_mitigation_cloud.md",
            "section_title": "Remediation Steps",
            "content": "Activate scrubbing...",
            "metadata": {"anomaly_type": "traffic"},
            "cosine_distance": 0.15,
        },
    ])
    mock_pool.side_effect = lambda *a, **kw: _fake_pooled_connection(cursor)

    settings = Settings(embedding_model_name="m", embedding_dim=384, runbook_table="runbook_chunks")
    results = search_runbooks("DDoS attack on edge", k=3, settings=settings)

    assert cursor.executed_sql is not None
    sql = cursor.executed_sql
    assert "<=>" in sql, "must use pgvector cosine distance operator"
    assert "ORDER BY embedding <=>" in sql
    assert "FROM runbook_chunks" in sql
    assert "LIMIT" in sql

    # Embedding bound twice (once in projection, once in ORDER BY) plus LIMIT.
    assert cursor.executed_params is not None
    assert cursor.executed_params[-1] == 3
    assert len(results) == 1
    assert results[0].source_file == "ddos_mitigation_cloud.md"


@patch("services.agent.rag.embed_text")
@patch("services.agent.rag.pooled_connection")
def test_search_runbooks_filtered_includes_metadata_predicate(mock_pool, mock_embed) -> None:
    mock_embed.return_value = np.zeros(384, dtype=np.float32)
    cursor = _FakeCursor(rows=[])
    mock_pool.side_effect = lambda *a, **kw: _fake_pooled_connection(cursor)

    settings = Settings(embedding_model_name="m", embedding_dim=384)
    search_runbooks_filtered("BGP flap", anomaly_type="bgp", k=4, settings=settings)

    sql = cursor.executed_sql or ""
    assert "metadata->>'anomaly_type'" in sql
    # The anomaly_type filter is the second bound parameter.
    assert cursor.executed_params is not None
    assert "bgp" in cursor.executed_params
    assert cursor.executed_params[-1] == 4


def test_search_runbooks_rejects_bad_inputs() -> None:
    settings = Settings(embedding_model_name="m", embedding_dim=384)
    with pytest.raises(ValueError):
        search_runbooks("", k=5, settings=settings)
    with pytest.raises(ValueError):
        search_runbooks("query", k=0, settings=settings)


def test_search_runbooks_filtered_rejects_blank_anomaly_type() -> None:
    settings = Settings(embedding_model_name="m", embedding_dim=384)
    with pytest.raises(ValueError):
        search_runbooks_filtered("query", anomaly_type="", k=5, settings=settings)


# ── Unit: get_full_runbook prefers disk over DB ──────────────────────────────


def test_get_full_runbook_reads_from_disk_when_present(tmp_path) -> None:
    rb = tmp_path / "test_runbook.md"
    rb.write_text("# Test\n\nHello world.\n", encoding="utf-8")

    settings = Settings(runbooks_dir=str(tmp_path))
    out = get_full_runbook("test_runbook.md", settings=settings)
    assert "Hello world." in out


@patch("services.agent.rag.pooled_connection")
def test_get_full_runbook_falls_back_to_postgres(mock_pool, tmp_path) -> None:
    settings = Settings(runbooks_dir=str(tmp_path))  # empty dir → DB fallback

    rows = [
        {
            "section_title": "Summary",
            "content": "Summary chunk one.",
            "metadata": {"chunk_index": 0},
        },
        {
            "section_title": "Summary",
            "content": "Summary chunk two.",
            "metadata": {"chunk_index": 1},
        },
        {
            "section_title": "Symptoms",
            "content": "Symptom one.",
            "metadata": {"chunk_index": 2},
        },
    ]
    cursor = _FakeCursor(rows=rows)
    mock_pool.side_effect = lambda *a, **kw: _fake_pooled_connection(cursor)

    out = get_full_runbook("missing_on_disk.md", settings=settings)
    assert "## Summary" in out
    assert "## Symptoms" in out
    assert "Summary chunk one." in out
    assert out.index("## Summary") < out.index("## Symptoms")


# ── Optional integration test ───────────────────────────────────────────────


REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(
    os.environ.get("RUN_RAG_INTEGRATION") != "1",
    reason="set RUN_RAG_INTEGRATION=1 to run live Postgres tests",
)
def test_integration_search_returns_relevant_runbook() -> None:  # pragma: no cover
    """End-to-end: embed a query and assert the top hit is the expected runbook.

    Requires:
      - Postgres at POSTGRES_HOST/PORT with the runbook_chunks table
      - The seed script to have already been run against that DB
      - sentence-transformers installed and the embedding model cached
    """
    results = search_runbooks("BGP session flap on edge router", k=3)
    assert len(results) >= 1
    top_files = {r.source_file for r in results}
    assert "bgp_flap_troubleshooting.md" in top_files
