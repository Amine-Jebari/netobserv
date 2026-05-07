"""One-shot ingestion script for the runbook RAG corpus.

Pipeline:
  1. Read every ``*.md`` under ``RUNBOOKS_DIR``.
  2. Strip YAML frontmatter, split by ``## Header`` markdown sections,
     paragraph-aware chunk into ~CHUNK_MAX_TOKENS-word chunks with
     CHUNK_OVERLAP_TOKENS overlap. Word-aligned, never mid-token.
  3. Batch-embed every chunk with the configured sentence-transformers
     model (default ``all-MiniLM-L6-v2`` -> 384-dim).
  4. UPSERT each chunk into Postgres' ``runbook_chunks`` table
     (uniqueness on ``(source_file, section_title, content)``).
  5. (Re)create the IVFFlat similarity index with ``lists`` tuned to
     row count - the index is dropped if present so the next REINDEX
     runs against fresh data and clusters appropriately.

Run:
    python -m scripts.seed_runbooks
    python -m scripts.seed_runbooks --runbooks-dir ./data/runbooks --recreate-index
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Iterable

import structlog
from psycopg.types.json import Jsonb

from services.agent.chunking import Chunk, chunk_runbook
from services.agent.config import Settings, get_settings
from services.agent.db import connect
from services.agent.embeddings import embed_batch, get_embedding_dim


# ── Logging ──────────────────────────────────────────────────────────────────


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        stream=sys.stdout,
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )


log = structlog.get_logger("seed_runbooks")


# ── Pipeline ─────────────────────────────────────────────────────────────────


def discover_runbooks(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise FileNotFoundError(f"runbooks directory does not exist: {directory}")
    files = sorted(p for p in directory.glob("*.md") if p.is_file())
    if not files:
        raise FileNotFoundError(f"no .md runbooks found in {directory}")
    return files


def chunk_corpus(files: Iterable[Path], settings: Settings) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in files:
        file_chunks = chunk_runbook(
            path,
            max_tokens=settings.chunk_max_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
        )
        log.info(
            "runbook.chunked",
            file=path.name,
            chunks=len(file_chunks),
            anomaly_type=(file_chunks[0].metadata.get("anomaly_type") if file_chunks else None),
        )
        chunks.extend(file_chunks)
    return chunks


def upsert_chunks(chunks: list[Chunk], settings: Settings) -> int:
    """Embed all chunks, then UPSERT into runbook_chunks. Returns rows written."""
    if not chunks:
        log.warning("seed.no_chunks_to_write")
        return 0

    # Validate model dimension matches the column before doing the heavy work.
    model_dim = get_embedding_dim(settings.embedding_model_name)
    if model_dim != settings.embedding_dim:
        raise RuntimeError(
            f"embedding model {settings.embedding_model_name} returned dim={model_dim} "
            f"but EMBEDDING_DIM={settings.embedding_dim}. Update one or rebuild the table."
        )

    log.info("seed.embedding", n=len(chunks), model=settings.embedding_model_name, dim=model_dim)
    embeddings = embed_batch(
        [c.content for c in chunks],
        model_name=settings.embedding_model_name,
        batch_size=32,
    )

    table = settings.runbook_table
    sql = f"""
        INSERT INTO {table} (source_file, section_title, content, embedding, metadata)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (source_file, section_title, content)
        DO UPDATE SET
            embedding = EXCLUDED.embedding,
            metadata  = EXCLUDED.metadata,
            created_at = NOW()
    """  # noqa: S608 - table name is from trusted Settings.

    rows_written = 0
    with connect(settings) as conn:
        with conn.cursor() as cur:
            for chunk, vec in zip(chunks, embeddings):
                cur.execute(
                    sql,
                    (
                        chunk.source_file,
                        chunk.section_title,
                        chunk.content,
                        vec.tolist(),
                        Jsonb(chunk.metadata),
                    ),
                )
                rows_written += 1
        conn.commit()
    log.info("seed.upsert_complete", rows=rows_written, table=table)
    return rows_written


def rebuild_ivfflat_index(settings: Settings, recreate: bool = True) -> None:
    """Drop and recreate the IVFFlat cosine index using a row-count-aware ``lists``.

    Per pgvector guidance: ``lists ~= rows / 1000`` for large corpora, but a
    minimum of ``sqrt(rows)`` works well for small ones. We clamp to
    ``[1, 1024]`` to keep the index sane for tiny / huge inputs.
    """
    table = settings.runbook_table
    index_name = f"{table}_embedding_ivfflat_idx"

    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            (rows,) = cur.fetchone() or (0,)
            log.info("index.row_count", rows=rows, table=table)

            if rows == 0:
                log.warning("index.skip_empty_table")
                return

            lists = max(1, min(1024, int(math.sqrt(max(rows, 1)))))
            if recreate:
                cur.execute(f"DROP INDEX IF EXISTS {index_name}")
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON {table} USING ivfflat (embedding vector_cosine_ops) "
                f"WITH (lists = {lists})"
            )
            cur.execute(f"ANALYZE {table}")  # noqa: S608
        conn.commit()
    log.info("index.created", name=index_name, lists=lists, table=table)


def truncate_corpus(settings: Settings) -> None:
    """Wipe runbook_chunks. Used by ``--truncate`` to repopulate from scratch."""
    table = settings.runbook_table
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {table}")  # noqa: S608
        conn.commit()
    log.warning("seed.truncated", table=table)


# ── CLI ──────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the runbook RAG corpus into Postgres.")
    parser.add_argument(
        "--runbooks-dir",
        type=Path,
        default=None,
        help="Override the directory containing .md runbooks (defaults to RUNBOOKS_DIR env).",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="TRUNCATE the runbook_chunks table before inserting (idempotent fresh load).",
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="Skip rebuilding the IVFFlat similarity index after load.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Chunk and embed but do not write to Postgres. Useful for CI smoke tests.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    _configure_logging(settings.log_level)

    runbooks_dir = args.runbooks_dir or Path(settings.runbooks_dir)
    log.info(
        "seed.start",
        runbooks_dir=str(runbooks_dir),
        embedding_model=settings.embedding_model_name,
        embedding_dim=settings.embedding_dim,
        chunk_max_tokens=settings.chunk_max_tokens,
        chunk_overlap_tokens=settings.chunk_overlap_tokens,
        truncate=args.truncate,
        dry_run=args.dry_run,
    )

    files = discover_runbooks(runbooks_dir)
    log.info("seed.files_discovered", n=len(files), files=[f.name for f in files])

    chunks = chunk_corpus(files, settings)
    log.info("seed.chunks_total", n=len(chunks))

    if args.dry_run:
        log.info("seed.dry_run_complete", chunks=len(chunks))
        # Print a small preview so a human running this locally can sanity-check.
        if chunks:
            preview = chunks[0]
            print(json.dumps({
                "preview": {
                    "source_file": preview.source_file,
                    "section_title": preview.section_title,
                    "metadata": preview.metadata,
                    "content_words": len(preview.content.split()),
                    "content_head": preview.content[:120],
                },
            }, indent=2))
        return 0

    if args.truncate:
        truncate_corpus(settings)

    rows = upsert_chunks(chunks, settings)
    if rows == 0:
        log.error("seed.no_rows_written")
        return 1

    if not args.no_index:
        rebuild_ivfflat_index(settings, recreate=True)

    log.info("seed.complete", rows_written=rows, files=len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
