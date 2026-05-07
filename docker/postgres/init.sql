-- Runs once when the Postgres container is first initialized.
-- Creates the pgvector extension and the runbook_chunks table that backs the
-- agent service's RAG retrieval (Phase 4.1).
--
-- The IVFFlat index is intentionally NOT created here: ivfflat clustering is
-- trained from existing rows, so building it before data is loaded yields a
-- degenerate index. scripts/seed_runbooks.py creates the index after the
-- corpus has been ingested.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Runbook chunk embeddings, populated by scripts/seed_runbooks.py.
-- Each row is one paragraph-aware chunk (~300 tokens) of a markdown runbook
-- in data/runbooks/. The embedding column is sized for sentence-transformers
-- all-MiniLM-L6-v2 (384 dimensions).
CREATE TABLE IF NOT EXISTS runbook_chunks (
    id             UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_file    TEXT        NOT NULL,
    section_title  TEXT        NOT NULL,
    content        TEXT        NOT NULL,
    embedding      vector(384) NOT NULL,
    metadata       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_file, section_title, content)
);

-- Cheap btree indexes for metadata filtering. The IVFFlat similarity index is
-- created by the seed script post-load (see file header).
CREATE INDEX IF NOT EXISTS runbook_chunks_anomaly_type_idx
    ON runbook_chunks ((metadata->>'anomaly_type'));

CREATE INDEX IF NOT EXISTS runbook_chunks_source_file_idx
    ON runbook_chunks (source_file);
