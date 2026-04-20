-- Runs once when the Postgres container is first initialized.
-- Creates the pgvector extension required by the agent service for RAG.

CREATE EXTENSION IF NOT EXISTS vector;

-- Runbook embeddings table (populated by scripts/seed_runbooks.py)
CREATE TABLE IF NOT EXISTS runbook_embeddings (
    id          SERIAL PRIMARY KEY,
    runbook_id  TEXT        NOT NULL UNIQUE,
    title       TEXT        NOT NULL,
    content     TEXT        NOT NULL,
    embedding   vector(1536),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS runbook_embeddings_ivfflat_idx
    ON runbook_embeddings USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
