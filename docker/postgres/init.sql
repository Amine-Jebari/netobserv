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

-- ─────────────────────────────────────────────────────────────────────────────
-- Phase 4.2: investigations table.
--
-- Persists the result of every agent run: the originating anomaly, the agent's
-- reasoning trace (one JSON entry per think-step + tool call), the final
-- structured verdict + remediation plan (rendered runbook), and cost
-- accounting for the LLM call. The dashboard reads from this table; the agent
-- writes one row per anomaly it investigates.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS investigations (
    id                   UUID         PRIMARY KEY DEFAULT uuid_generate_v4(),
    anomaly_id           TEXT         NOT NULL,
    anomaly_type         TEXT         NOT NULL,
    severity             TEXT         NOT NULL,
    started_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at         TIMESTAMPTZ,
    duration_seconds     DOUBLE PRECISION,
    status               TEXT         NOT NULL DEFAULT 'running',
        -- running | completed | timed_out | error
    model                TEXT         NOT NULL,
    tool_call_count      INTEGER      NOT NULL DEFAULT 0,
    input_tokens         INTEGER      NOT NULL DEFAULT 0,
    output_tokens        INTEGER      NOT NULL DEFAULT 0,
    -- Originating anomaly event payload, verbatim.
    anomaly_event        JSONB        NOT NULL,
    -- Per-step reasoning trace; ordered list of {type, name, args, result, ts}.
    reasoning_trace      JSONB        NOT NULL DEFAULT '[]'::jsonb,
    -- Final structured verdict the agent emitted, including hypothesis,
    -- root_cause, confidence, citations[].
    verdict              JSONB,
    -- Rendered remediation runbook (markdown).
    runbook_markdown     TEXT,
    -- Optional error text when status='error'.
    error                TEXT
);

CREATE INDEX IF NOT EXISTS investigations_anomaly_id_idx
    ON investigations (anomaly_id);
CREATE INDEX IF NOT EXISTS investigations_started_at_idx
    ON investigations (started_at DESC);
CREATE INDEX IF NOT EXISTS investigations_severity_idx
    ON investigations (severity);
CREATE INDEX IF NOT EXISTS investigations_status_idx
    ON investigations (status);
