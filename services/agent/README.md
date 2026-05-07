# Service: agent

The AI brain of the platform. Phase 4.1 lands the **RAG knowledge-base
layer**; Phase 4.2 will add the LangChain agent that turns anomaly events
into Claude-generated runbooks.

## Phase 4.1 surface (current)

- **`services/agent/rag.py`** - public retrieval API the agent calls into.
  Wraps pgvector cosine-distance queries against the `runbook_chunks`
  table.
  - `search_runbooks(query, k=5)` - top-k semantic search.
  - `search_runbooks_filtered(query, anomaly_type, k=5)` - filtered by
    `metadata.anomaly_type`.
  - `get_full_runbook(source_file)` - returns the full markdown text
    (disk first, Postgres reassembly as fallback) for the agent's
    citation context.
- **`services/agent/embeddings.py`** - cached `sentence-transformers`
  loader; `embed_text` and `embed_batch` return float32 numpy arrays.
- **`services/agent/chunking.py`** - paragraph-aware markdown chunker.
  Strips YAML frontmatter, splits by `## Header`, joins paragraphs into
  word-aligned chunks of `<= chunk_max_tokens` words with
  `chunk_overlap_tokens` overlap.
- **`services/agent/db.py`** - psycopg connection helpers (single-shot
  `connect()` for the seeder, `psycopg_pool.ConnectionPool` for the
  long-lived agent). Registers the pgvector adapter on every connection.
- **`services/agent/config.py`** - pydantic-settings schema. Aliases
  `EMBEDDING_MODEL_NAME`/`VECTOR_EMBEDDING_MODEL` and
  `EMBEDDING_DIM`/`VECTOR_DIMENSIONS`.
- **`scripts/seed_runbooks.py`** - one-shot ingestion script driven by
  the same `Settings` class. Reads markdown from `data/runbooks/`,
  chunks, embeds, upserts into `runbook_chunks`, and creates the
  IVFFlat index post-load with `lists` tuned to the row count.

## Knowledge base schema

`docker/postgres/init.sql` (Phase 2) and the seeder maintain:

```sql
CREATE TABLE runbook_chunks (
    id            UUID PRIMARY KEY,
    source_file   TEXT NOT NULL,
    section_title TEXT NOT NULL,
    content       TEXT NOT NULL,
    embedding     vector(384) NOT NULL,
    metadata      JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_file, section_title, content)
);
```

Embeddings are produced by `sentence-transformers/all-MiniLM-L6-v2`
(384 dimensions). Switching models requires recreating the table with
the new column type and re-running the seeder.

## Phase 4.2 surface (planned)

The same Phase 4.2 modules will sit alongside the Phase 4.1 code:

- Subscribe to `anomalies.scored`, query `topology` for affected nodes,
  call `search_runbooks_filtered(...)` for context, prompt Claude.
- Persist the generated runbook to MinIO/S3 as Markdown.
- Index runbook metadata in OpenSearch and publish an
  `anomalies.runbooks` event for the API + frontend.

## Key env vars

```
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=netobserv
POSTGRES_USER=netobserv
POSTGRES_PASSWORD=changeme_local
EMBEDDING_MODEL_NAME=sentence-transformers/all-MiniLM-L6-v2
EMBEDDING_DIM=384
RAG_TOP_K=5
RUNBOOKS_DIR=/data/runbooks
```

## Running the seeder locally

```bash
docker compose --profile seeding up seed-runbooks
# or, from the repo root with a venv:
python -m scripts.seed_runbooks --runbooks-dir data/runbooks
```

## Tests

```bash
cd services/agent
pip install -e ".[dev]"
pytest -q
```

`test_chunking.py` runs in milliseconds (no torch, no Postgres).
`test_embeddings.py` requires the model (cached in the Docker image).
`test_rag.py` runs in pure-mock mode; set `RUN_RAG_INTEGRATION=1` to
run the live Postgres + sentence-transformers integration check.
