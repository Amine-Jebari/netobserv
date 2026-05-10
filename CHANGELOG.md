# Changelog

All notable changes to the Network Observability Platform are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Phase 4.2] — 2026-05-07 — AI Investigation Agent

### Added

- **services/agent/** — extended the Phase 4.1 RAG package into a long-running
  investigation agent. The service consumes ``anomalies.scored`` from Kafka,
  routes WARN/CRITICAL events into a bounded LangChain function-calling loop
  against **Google Gemini** (``gemini-2.0-flash`` by default, swappable via
  ``GEMINI_MODEL``), and persists each investigation's reasoning trace +
  rendered runbook into a new Postgres ``investigations`` table for the
  dashboard to render.
  - **`tools.py`** — four LangChain ``StructuredTool`` adapters wired through
    a shared ``Toolbox`` (httpx client + RAG closure):
    - ``query_topology(node_id?, prefix?)`` — calls the topology service's
      ``/topology/node/{id}`` or ``/topology/affected`` endpoints and
      projects the verbose payload into a ≤20-node / ≤30-edge summary.
    - ``get_metrics(target, metric, lookback_minutes=30)`` — runs a Flux
      query against InfluxDB filtered against ``srcaddr|dstaddr|device_id|interface``
      and returns up to 50 ``(ts, value)`` points (uniform-stride
      downsampled when more are present). User-influenced strings are
      ASCII-sanitized before interpolation into Flux.
    - ``search_runbooks(query, k=3)`` — wraps the Phase 4.1 pgvector search
      and returns top-3 chunks with stable ``filename.md#Section`` citation
      handles the agent must echo in its verdict.
    - ``get_recent_events(prefix?, node_id?, lookback_minutes=30)`` — last
      20 BGP events / scored anomalies from OpenSearch; the bool query
      ANDs a time range and ORs ``prefix == X`` / multi-match on
      ``peer_ip|device_id|node_id|srcaddr|dstaddr``.
  - **`agent.py`** — the bounded reasoning loop. Drives a
    ``ChatGoogleGenerativeAI`` with the four tools bound for function-calling
    and enforces:
      * ``MAX_TOOL_CALLS`` (default 8) hard cap — once exceeded the loop
        injects a "tool budget exhausted" ToolMessage and prompts for the
        final verdict;
      * ``INVESTIGATION_TIMEOUT_SECONDS`` (default 120) wall-clock cap;
      * structured trace capture: every thought, tool call, tool result,
        and final answer is appended as a ``TraceStep`` and persisted in
        ``investigations.reasoning_trace`` (JSONB);
      * token accounting via ``message.usage_metadata`` with both
        the legacy ``response_metadata`` shape and the normalized
        ``usage_metadata`` shape supported, exposed as the Prometheus
        counter ``claude_tokens_used_total{type, model}`` (legacy name kept
        intentionally for dashboard continuity);
      * a strict system prompt with a 4-step protocol
        (UNDERSTAND → HYPOTHESIZE → VERIFY → CONCLUDE), a citation-required
        rule, and a fenced-JSON output contract that ``parse_verdict`` extracts.
  - **`runbook.py`** — deterministic markdown renderer. Takes the verdict
    + trace and emits a fixed-section runbook (Summary / Root cause /
    Blast radius / Evidence / Remediation steps / Citations / Investigation
    metadata). Uses a template instead of a second LLM call so output is
    reproducible and there's no per-investigation cost doubling.
  - **`investigations_store.py`** — Postgres CRUD for the ``investigations``
    table. ``insert_started()`` is called before the loop runs so the row
    exists for the duration of the investigation; ``update_completed()``
    fills in the trace/verdict/runbook + token counts when done.
  - **`metrics.py`** — Prometheus registry exposing
    ``claude_tokens_used_total``, ``agent_investigations_started_total``,
    ``agent_investigations_completed_total{outcome}``,
    ``agent_tool_calls_total{tool, outcome}``,
    ``agent_investigation_duration_seconds`` (histogram), and
    ``agent_anomalies_consumed_total{severity, decision}``.
  - **`main.py` / `__main__.py`** — service entry point. AIOKafkaConsumer
    on ``anomalies.scored``, severity-based routing (INFO is logged but
    not investigated; WARN/CRITICAL spawn an investigation off the loop
    via ``run_in_executor``). The same module hosts the FastAPI app:
      * ``GET /health`` — 200 once the consumer is connected, 503 otherwise;
      * ``GET /metrics`` — Prometheus exposition;
      * ``GET /investigations[?limit=&severity=]`` — recent investigations
        in compact summary form (id, severity, root_cause/confidence
        snippet);
      * ``GET /investigations/{id}`` — full row including reasoning trace
        and rendered runbook;
      * ``POST /investigate`` — manually trigger an investigation on a
        supplied anomaly event (replays from cache if a completed
        investigation already exists for that anomaly_id — useful for
        demos and debugging without re-spending LLM tokens).
  - **`config.py`** — extended with the Phase 4.2 surface: ``GOOGLE_API_KEY``,
    ``GEMINI_MODEL`` (``CLAUDE_MODEL`` kept as a legacy alias),
    ``KAFKA_*`` consumer knobs, ``TOPOLOGY_SERVICE_URL``, ``INFLUXDB_*``,
    ``OPENSEARCH_*`` patterns, and the agent-loop safety knobs
    ``MAX_TOOL_CALLS`` / ``INVESTIGATION_TIMEOUT_SECONDS`` /
    ``INVESTIGATE_SEVERITIES``.
  - **`tests/`** — three new pytest modules:
    - ``test_tools.py`` exercises each of the four tools using
      ``httpx.MockTransport`` (no real backends) — happy path, missing
      arguments, 4xx/5xx, and a sanitization test that confirms hostile
      characters in tool arguments don't bleed into the Flux query;
    - ``test_agent_loop.py`` plugs a ``ScriptedChatModel`` that emits a
      pre-recorded sequence of ``AIMessage`` responses (tool call, then
      fenced-JSON final). Validates trace shape, token accounting, the
      tool-call budget, and the no-verdict error path;
    - ``test_runbook.py`` covers the markdown renderer's section
      structure, step renumbering, and the missing-verdict short-circuit.
  - **`Dockerfile`** — extended to copy all new modules and expose 8080.
    The pre-warmed ``sentence-transformers`` model from Phase 4.1 is
    preserved so the agent's first ``search_runbooks`` call doesn't pay
    the load latency.
  - **`pyproject.toml`** — bumped to 0.2.0 and added ``langchain``,
    ``langchain-core``, ``langchain-google-genai``, ``google-generativeai``,
    ``fastapi``, ``uvicorn[standard]``, ``aiokafka``, ``httpx``, and
    ``prometheus-client``.

- **docker/postgres/init.sql** — new ``investigations`` table with btree
  indexes on ``anomaly_id``, ``started_at DESC``, ``severity``, and
  ``status``. JSONB columns for ``anomaly_event``, ``reasoning_trace``,
  and ``verdict`` so the dashboard can render the trace without a JOIN.

- **docker-compose.yml** — new ``agent`` service running the long-lived
  investigator on port ``8094`` (host) → ``8080`` (container).
  ``depends_on`` waits for redpanda, postgres, topology, influxdb, and
  opensearch all healthy.

- **.env.example** — replaced the ``ANTHROPIC_API_KEY`` / ``CLAUDE_MODEL``
  block with ``GOOGLE_API_KEY`` / ``GEMINI_MODEL``, and added the agent
  safety knobs (``AGENT_MAX_TOOL_CALLS``,
  ``AGENT_INVESTIGATION_TIMEOUT_SECONDS``,
  ``AGENT_INVESTIGATE_SEVERITIES``) plus the new OpenSearch index
  patterns.

- **terraform/outputs.tf** — new aggregate outputs for the agent's AWS
  deployment: ``topology_service_url`` (variable, populated when the
  topology Fargate service is provisioned in a later phase),
  ``influxdb_endpoint`` (Timestream regional query endpoint),
  ``influxdb_database``, plus the bundled ``agent_service_config`` map
  the deployment pipeline can pass straight to the agent's task
  definition / Helm chart. Per-resource outputs (``rds_endpoint``,
  ``opensearch_endpoint``, etc.) keep their existing locations to avoid
  declaration collisions.

### Notes

- **LLM provider**: this phase intentionally uses **Google Gemini** rather
  than Anthropic Claude. The ``claude_tokens_used_total`` Prometheus metric
  name is preserved so the platform's existing dashboards and the project's
  cost-tracking spec keep working when providers swap. The original
  scoping doc references Claude / Anthropic SDK; this implementation
  satisfies the same contract via ``langchain-google-genai``.
- **No new Terraform files** beyond ``outputs.tf`` (per the phase brief).
  The agent's compute (ECS Fargate task definition) lands in a later phase
  alongside the deployment pipeline; until then ``agent_service_config``
  is the single integration point.

---

## [Phase 4.1] — 2026-05-04 — RAG Knowledge Base

### Added

- **data/runbooks/** — 10 hand-authored synthetic runbooks (300-600 words
  each) covering the troubleshooting playbooks the agent will retrieve as
  few-shot context: `bgp_flap_troubleshooting.md`, `route_leak_detection.md`,
  `ddos_mitigation_cloud.md`, `high_latency_investigation.md`,
  `packet_loss_diagnosis.md`, `asymmetric_routing.md`, `mtu_blackhole.md`,
  `interface_utilization_capacity.md`, `dns_resolution_failures.md`,
  `vpc_peering_connectivity.md`. Each runbook has YAML frontmatter
  (`anomaly_type`, `severity`, `tags`) and the canonical sections
  `Summary` / `Symptoms` / `Diagnostic Commands` / `Common Root Causes` /
  `Remediation Steps` so the chunker maps cleanly to retrieval-time filters.

- **services/agent/** — `netobserv-agent` Python 3.11 package providing the
  Phase 4.1 RAG layer. Phase 4.2 will sit on top of these modules:
  - **`chunking.py`** — paragraph-aware markdown chunker. Strips YAML
    frontmatter, splits the body by `## Header`, then greedily joins
    paragraphs into word-aligned chunks of `<= chunk_max_tokens` words with
    `chunk_overlap_tokens` overlap. Pure-Python, dependency-free (PyYAML
    is the only optional import), millisecond-fast unit tests. Oversized
    paragraphs are split on word boundaries so the chunker never returns a
    half-word.
  - **`embeddings.py`** — cached `sentence-transformers` loader. Module-
    level singleton with a thread lock so the model is loaded once per
    process; `embed_text` / `embed_batch` return float32 numpy arrays.
  - **`db.py`** — psycopg connection helpers: `connect()` for the seeder's
    one-shot connection, `get_pool()` returning a process-level
    `psycopg_pool.ConnectionPool` for the long-lived agent. The pgvector
    adapter is registered on every connection (`register_vector`).
  - **`rag.py`** — public retrieval API. Three functions:
    - `search_runbooks(query, k=5)` — top-k semantic search using
      pgvector's `<=>` cosine distance.
    - `search_runbooks_filtered(query, anomaly_type, k=5)` — same plus a
      `metadata->>'anomaly_type'` predicate, backed by the btree index on
      that JSON path.
    - `get_full_runbook(source_file)` — returns the original markdown text
      from disk (`RUNBOOKS_DIR`) when present, otherwise reassembles the
      runbook from `runbook_chunks` rows ordered by `metadata.chunk_index`.
    All three return typed `RunbookChunk` dataclasses with both the cosine
    `score` (1 - distance) and the raw `cosine_distance`.
  - **`config.py`** — `pydantic-settings` schema covering Postgres
    connection, embedding model + dim (with `EMBEDDING_MODEL_NAME` /
    `VECTOR_EMBEDDING_MODEL` aliases), chunking knobs, RAG top-k, and the
    Phase 4.2 Claude knobs so the same Settings class will serve the
    agent without a refactor.
  - **`Dockerfile`** — multi-stage `python:3.11-slim` build, non-root
    `agent:10004` user, pre-downloads the embedding model into
    `/opt/models` so the seed and Phase 4.2 agent images do not fetch
    from Hugging Face at runtime. Default ENTRYPOINT is the long-running
    agent; the Phase 4.1 seed compose service overrides this.
  - **`tests/`** — pytest suite covering: (a) the chunker invariants
    (frontmatter parsing, section splitting, no mid-word splits, overlap
    correctness, oversized-paragraph splitting), (b) the embedding model
    contract (384-dim, finite vectors, semantic similarity sanity check),
    and (c) the rag layer behaviour with mocked psycopg (cosine operator
    used, anomaly-type filter applied, score = 1 - distance, disk-first
    `get_full_runbook` with Postgres fallback). An integration test gated
    on `RUN_RAG_INTEGRATION=1` runs end-to-end against a live Postgres.

- **scripts/seed_runbooks.py** — one-shot ingestion script. Discovers
  every `*.md` under `RUNBOOKS_DIR`, chunks with the agent package's
  chunker, batch-embeds with `sentence-transformers/all-MiniLM-L6-v2`,
  validates that the model dimension matches `EMBEDDING_DIM` before any
  writes, then UPSERTs into `runbook_chunks` keyed on
  `(source_file, section_title, content)`. After load, drops and
  recreates the IVFFlat cosine index with `lists` tuned to the row count
  (`max(1, min(1024, sqrt(rows)))`) and runs `ANALYZE` so the planner
  picks the index. CLI flags: `--runbooks-dir`, `--truncate`, `--no-index`,
  `--dry-run`. Also creates `scripts/__init__.py` so
  `python -m scripts.seed_runbooks` resolves both inside the container
  and from the repo root.

### Terraform

- **terraform/rds.tf** — AWS counterpart to the local pgvector container.
  - `aws_db_instance.pgvector` — Postgres 16.3, db.t3.micro by default
    (override `rds_instance_class`), gp3 storage, encrypted at rest,
    Performance Insights and Enhanced Monitoring (1-minute interval) on,
    backup retention 7 days, deletion protection on, multi-AZ off (flip
    for production), CloudWatch logs export for `postgresql` and
    `upgrade`.
  - `aws_db_parameter_group.pgvector` — custom `postgres16` family
    parameter group preloading `pg_stat_statements,vector` via
    `shared_preload_libraries`. pgvector is built-in from Postgres 15.2+
    so `CREATE EXTENSION vector` after provisioning is the only manual
    step (the `rds_pgvector_init_sql` output documents this).
  - `aws_db_subnet_group.main` — created when `rds_subnet_ids` is
    populated; needs ≥2 subnets across AZs.
  - `aws_security_group.rds` — created when `rds_vpc_id` is populated;
    ingress allows TCP 5432 only from `var.vpc_cidr` (no public exposure).
  - `aws_secretsmanager_secret.rds_master` + version — stores the
    randomly-generated 32-character master password and the connection
    metadata (username, dbname). The instance ignores `password` drift
    (managed via Secrets Manager rotation in production).
  - `aws_iam_role.rds_monitoring` — attaches the AWS-managed
    `AmazonRDSEnhancedMonitoringRole` policy.
  - Outputs: `rds_endpoint`, `rds_address`, `rds_port`,
    `rds_database_name`, `rds_master_secret_arn`,
    `rds_security_group_id`, `rds_pgvector_init_sql`.

### Changed

- **docker/postgres/init.sql** — schema updated to match the Phase 4.1
  contract. Replaces the placeholder `runbook_embeddings` table with
  `runbook_chunks (id UUID, source_file TEXT, section_title TEXT,
  content TEXT, embedding vector(384), metadata JSONB, created_at TIMESTAMPTZ)`.
  The `vector(1536)` column is now `vector(384)` (all-MiniLM-L6-v2). Adds
  the `uuid-ossp` extension, a uniqueness constraint on
  `(source_file, section_title, content)` for idempotent re-seeding, and
  cheap btree indexes on `metadata->>'anomaly_type'` and `source_file`.
  The IVFFlat cosine index is no longer created here; the seed script
  builds it post-load with row-count-tuned `lists` so cluster centroids
  train against real data.

- **docker-compose.yml** — adds `seed-runbooks` under the `seeding`
  profile. Builds the agent image, overrides ENTRYPOINT to
  `python -m scripts.seed_runbooks`, mounts `./data/runbooks` and
  `./scripts` read-only, depends on `postgres` healthy. Excluded from
  the default `docker compose up`; invoked with
  `docker compose --profile seeding up seed-runbooks` (or `make seed`).

- **Makefile** — `make seed` now drives the `seed-runbooks` compose
  service via `docker compose --profile seeding up --build
  --abort-on-container-exit`, replacing the orphaned `scripts` service
  invocation. `make train` is updated to the equivalent `--profile
  training` form for symmetry.

- **.env.example** — replaces `VECTOR_EMBEDDING_MODEL=text-embedding-3-small`
  / `VECTOR_DIMENSIONS=1536` (the OpenAI default carried over from Phase
  1) with `EMBEDDING_MODEL_NAME=sentence-transformers/all-MiniLM-L6-v2`
  and `EMBEDDING_DIM=384`. Adds `RUNBOOKS_DIR`, `CHUNK_MAX_TOKENS`,
  `CHUNK_OVERLAP_TOKENS`, `RAG_TOP_K`. The Phase 4.2 placeholder
  variables (`AGENT_MAX_TOKENS`, `AGENT_TEMPERATURE`,
  `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`) are kept for the next phase.

- **services/agent/README.md** — updated from a Phase 1 placeholder to
  document the Phase 4.1 surface (`rag.py` API, schema, env vars, run
  instructions, test layout).

---

## [Phase 3.2] — 2026-04-30 — Real-Time Anomaly Detection Engine

### Added

- **services/anomaly/engine.py** — long-lived asyncio service that consumes
  `network.flows` / `network.bgp` / `network.snmp` from Kafka, buffers each
  stream in fixed-duration windows (default 10 s, `WINDOW_SECONDS`), scores
  every closed window against the three PyOD models loaded from the latest
  bundle in MinIO/S3, and publishes enriched anomaly events to
  `anomalies.scored`. FastAPI surface exposes `GET /health` (returns 503
  until a model bundle is loaded), `GET /stats`, `GET /metrics` (Prometheus),
  and `POST /reload-model` for hot reloads without a restart.
- **services/anomaly/scorer.py** — model loader + unified scorer. Untars the
  bundle produced by Phase 3.1, deserializes the three `.joblib` models plus
  `metadata.json`, and turns a `WindowBatch` into zero-or-more
  `ScoredAnomaly` records. Raw decision-function scores are normalized onto a
  common 0-1 scale using the trainer-recorded `decision_p50`/`decision_p95`
  quantiles (linear interpolation: p50 → 0.0, p95 → 0.5, then linearly
  extrapolated and clamped to 1.0). Severity bands: drop below
  `INFO_THRESHOLD` (0.3), `INFO` ∈ [0.3, 0.6), `WARN` ∈ [0.6, 0.85),
  `CRITICAL` ≥ 0.85. Top-3 contributing features are computed per event
  using absolute z-scores against the trainer's per-feature mean/std.
- **services/anomaly/windowing.py** — `WindowBuffer` with one async lock and
  a background flusher task. Empty windows are skipped; callback failures
  are logged and the loop keeps running.
- **services/anomaly/kafka_io.py** — small `StreamConsumer` + `JsonProducer`
  wrappers around aiokafka, mirroring the collector's pattern.
- **services/anomaly/topology_client.py** — async httpx client for
  `GET /topology/affected?prefix=...`. Used to enrich BGP anomalies with the
  set of node IDs and links impacted by an announced/withdrawn prefix.
  Best-effort: errors are logged but do not block event publication.
- **services/anomaly/slack_alerter.py** — direct Slack webhook poster fired
  in-line for `CRITICAL` events (`SLACK_WEBHOOK_URL`). Provides a baseline of
  on-call value even if the AI agent is offline. Empty webhook URL = silent
  no-op for dev environments.
- **services/anomaly/engine_metrics.py** — Prometheus counters/gauges/histogram:
  `anomalies_detected_total{severity}`, `messages_consumed_total{stream}`,
  `scoring_latency_seconds` (histogram), `model_reload_timestamp`,
  `anomaly_engine_model_loaded`, `anomaly_engine_slack_alerts_total`. JSON
  snapshot also exposed at `/stats`.
- **services/anomaly/model_fetch.py** — `wait_for_bundle` polling helper
  (`MODEL_WAIT_POLL_SECONDS` between `head_object` checks; optional
  `MODEL_WAIT_MAX_SECONDS` deadline) and `download_bundle` for `/reload-model`.
- **services/anomaly/tests/test_windowing.py** — covers per-stream
  buffering, atomic drain, the background flusher cadence, callback-error
  resilience, window-boundary timestamping, and validation of bad inputs.
- **services/anomaly/tests/test_scoring.py** — covers severity threshold
  bands, score normalization (below p50, at p95, extrapolation, NaN,
  degenerate quantiles), top-feature ranking by absolute z-score, and the
  `Scorer.score_batch` end-to-end path with stub PyOD models.

### Anomaly event schema (`anomalies.scored` topic)

Each published JSON record contains: `anomaly_id`, `timestamp`, `severity`,
`score` (unified 0-1), `raw_score`, `anomaly_type` (`traffic` / `bgp` /
`snmp`), `affected_nodes`, `affected_prefixes`, `top_features` (list of
`{feature, value, zscore}`), `feature_vector`, `context_window_start`,
`context_window_end`, `raw_metrics_sample` (last N raw input messages).

### Changed

- **services/anomaly/config.py** — `Settings` now serves both the trainer
  and the engine. New fields: `model_key`, `kafka_*`, `window_seconds`,
  `info_threshold` / `warn_threshold` / `critical_threshold` (with cross-
  field validators enforcing strict ordering), `topology_service_url`,
  `slack_webhook_url`, `http_host`/`http_port`, `model_wait_*`,
  `raw_metrics_sample_size`.
- **services/anomaly/Dockerfile** — installs `curl` for the engine
  healthcheck, copies the new engine modules, switches the default
  ENTRYPOINT from `services.anomaly.baseline` to `services.anomaly.engine`.
  The trainer compose service overrides the entrypoint explicitly.
- **services/anomaly/pyproject.toml** — adds `aiokafka`, `fastapi`,
  `uvicorn`, and `pytest-asyncio`; bumps version to `0.2.0`; enables
  `asyncio_mode = "auto"` for pytest. Image renamed to
  `netobserv/anomaly:local` (was `netobserv/anomaly-baseline:local`).
- **docker-compose.yml** — new `anomaly-engine` service on host port 8093,
  depending on `redpanda`, `redpanda-init`, `minio`, `minio-init`, and
  `topology`. The existing `baseline` service now overrides the entrypoint
  and shares the same image.
- **.env.example** — adds `ANOMALY_MODEL_KEY`, `ANOMALY_WINDOW_SECONDS_RT`,
  `ANOMALY_INFO_THRESHOLD`, `ANOMALY_WARN_THRESHOLD`,
  `ANOMALY_CRITICAL_THRESHOLD`, `ANOMALY_MODEL_WAIT_*`,
  `KAFKA_ANOMALY_GROUP_PREFIX`, `TOPOLOGY_SERVICE_URL`, `SLACK_WEBHOOK_URL`.
  Removes the dead Phase-1 placeholders (`ANOMALY_MODEL_PATH`,
  `ANOMALY_RETRAIN_INTERVAL_HOURS`, `ANOMALY_SCORE_THRESHOLD`,
  `ANOMALY_WINDOW_SECONDS`).

### Terraform

- **terraform/sagemaker.tf** — replaces the placeholder image URI with a
  resolved ECR reference (`<account>.dkr.ecr.<region>.amazonaws.com/
  netobserv-anomaly:engine-v0.2.0`, override-able). New `ecr_repository_name`
  and `ecr_image_tag` variables. Container env now sets `MODEL_KEY` /
  `S3_BUCKET` so the engine reads the bundle from S3 directly. New output:
  `sagemaker_model_image_uri`.
- **terraform/lambda.tf** — new file. Defines an async Lambda wrapper
  (`netobserv-anomaly-scorer`) triggered by the three telemetry Kinesis
  streams. It forwards record batches to the SageMaker endpoint and publishes
  anomaly events to `anomalies-scored`. IAM role is least-privilege: read on
  the three source streams, write on `anomalies-scored`,
  `sagemaker:InvokeEndpoint` on the endpoint declared in `sagemaker.tf`, and
  `s3:GetObject` on the deployment artifact key. Gated by
  `lambda_enabled = false` until the deployment zip is built and uploaded.

---

## [Phase 3.1] — 2026-04-27 — Baseline Anomaly-Detection Trainer

### Added

- **services/anomaly/baseline.py** — one-shot training pipeline. Pulls the
  last `LOOKBACK_HOURS` of telemetry (flows + SNMP from InfluxDB, BGP events
  from OpenSearch), engineers per-minute features, fits three PyOD models on
  separate feature subsets, cross-validates each on a held-out slice plus
  injected synthetic outliers, bundles everything into a single `.tar.gz`,
  and uploads to MinIO/S3.

  - **Isolation Forest** on traffic-volume features (total bytes/packets,
    flow count, src/dst diversity, top-pair byte concentration, mean packet
    size, plus diurnal sine/cosine encodings of hour-of-day and day-of-week).
  - **Local Outlier Factor** (`novelty=True`) on per-minute BGP event
    frequency features (announce/withdraw/update counts, unique
    prefixes/peers, AS-path length mean/std, diurnal features).
  - **Minimum Covariance Determinant** (PyOD MCD — robust Mahalanobis) on
    per-minute SNMP aggregates (cpuLoad mean/max, latency mean/p95, interface
    utilization mean/max, error counter sums, diurnal features).

  Cross-validation is unsupervised: held-out real rows (label 0) are scored
  against synthesized ±5–8σ outliers (label 1) and the resulting
  `roc_auc_score` plus observed contamination quantiles are printed and
  recorded in the bundle metadata.

- **services/anomaly/features.py** — single source of truth for feature
  engineering. Exposes `make_traffic_features`, `make_bgp_features`,
  `make_snmp_features`, `add_diurnal`, and the canonical column-order
  constants (`TRAFFIC_FEATURES`, `BGP_FEATURES`, `SNMP_FEATURES`). Phase 3.2
  imports from this module so training and inference compute features
  identically. Pure-pandas, deterministic, no I/O.

- **services/anomaly/model_store.py** — joblib serialization, tarball
  bundling, S3/MinIO upload and `baseline-latest.tar.gz` alias maintenance.
  The bundle layout (`<name>.joblib` files + `metadata.json`) is the public
  contract Phase 3.2 will load.

- **services/anomaly/influx_query.py / opensearch_query.py** — synchronous
  Flux + OpenSearch query helpers. The OpenSearch helper degrades gracefully
  to an empty DataFrame on connection failure so a flaky search index never
  blocks the rest of the bundle.

- **services/anomaly/config.py / logging_setup.py** — `pydantic-settings`
  schema (with multi-source aliases for S3/AWS env vars) and structlog JSON
  logging configuration. Same patterns as collector and topology.

- **services/anomaly/Dockerfile** — multi-stage `python:3.11-slim` build,
  non-root `anomaly:10003` user, recreates the `services/anomaly/` package
  layout inside `/app` so the runtime entry point is exactly the same as the
  local dev command (`python -m services.anomaly.baseline`).

- **services/anomaly/tests/** — pytest suite covering deterministic
  feature-engineering output (same input → byte-identical DataFrame),
  aggregation correctness (top-pair share, AS-path mean/std, SNMP p95,
  missing-metric → zero), unit-circle invariance of diurnal encodings, and
  bundle round-trip (serialize → load gives equivalent models + metadata,
  including numpy / datetime values via the custom JSON encoder).

- **services/anomaly/README.md** — full module map, env-var reference,
  bundle format spec, canonical column order per model, and explanation of
  the unsupervised cross-validation methodology. The "Feature spaces"
  section is the contract Phase 3.2 binds against.

- **services/__init__.py / services/anomaly/__init__.py** — empty package
  markers so `python -m services.anomaly.baseline` resolves the
  `from .features import ...` style relative imports from the repo root.

- **terraform/sagemaker.tf** — AWS counterpart hosting the trained bundle as
  a SageMaker Serverless Inference endpoint:
  - `aws_iam_role` for SageMaker execution with S3 read access scoped to the
    archive bucket (`s3:GetObject`, `s3:ListBucket` on
    `aws_s3_bucket.archive`), plus CloudWatch Logs and ECR read-only managed
    policies.
  - `aws_sagemaker_model` pointing at a placeholder ECR image URI (Phase 3.3
    will containerize the inference handler) and the
    `models/baseline-latest.tar.gz` artifact key the trainer maintains.
  - `aws_sagemaker_endpoint_configuration` with a `serverless_config` block
    (memory + max-concurrency variables) — pay-per-request, scales to zero
    between demos.
  - `aws_sagemaker_endpoint` plus outputs for endpoint name/ARN, role ARN,
    and the S3 model URI.
  - Gated by `var.sagemaker_enabled` (default `false`) so `terraform plan`
    can run before the inference image exists.

### Changed

- **docker-compose.yml** — added the `baseline` service under the
  `training` profile. Excluded from the default `docker compose up`; invoked
  with `docker compose --profile training up baseline`. Depends on
  `influxdb`, `opensearch`, `minio` healthy and `minio-init` complete; uses
  the same env-var conventions as the collector for S3/MinIO selection.

- **.env.example** — added a Phase 3.1 block under `## Anomaly Detection`:
  `ANOMALY_LOOKBACK_HOURS`, `ANOMALY_BGP_INDEX_PATTERN`,
  `ANOMALY_MODEL_PREFIX`, `ANOMALY_CONTAMINATION_TRAFFIC`,
  `ANOMALY_CONTAMINATION_BGP`, `ANOMALY_CONTAMINATION_SNMP`,
  `ANOMALY_MIN_TRAINING_MINUTES`, `ANOMALY_RANDOM_SEED`. The Phase 3.2 vars
  (`ANOMALY_WINDOW_SECONDS`, `ANOMALY_SCORE_THRESHOLD`, etc.) are kept
  in place under the same heading.

---

## [Phase 2.3] — 2026-04-23 — Topology Service

### Added

- **services/topology/** — `netobserv_topology` Python 3.11 package that owns
  the live network-graph view of the platform:
  - Consumes three Kafka topics on independent consumer groups
    (`netobserv-topology-bgp` / `-flows` / `-snmp`):
    - `topology.updates` → BGP ANNOUNCE/UPDATE upserts peer nodes and
      `bgp_peer` edges (source=`local-gateway`); WITHDRAW marks the edge
      inactive (kept for historical visibility rather than deleted).
    - `network.flows` → increments `traffic_bytes`/`traffic_packets` counters
      and updates an EWMA-smoothed `utilization` value on the edge between
      the subnets/routers owning srcaddr and dstaddr.
    - `network.snmp` → updates a node's `health_status` (OK/WARN/CRITICAL)
      and `last_seen` based on per-metric thresholds (`cpuLoad`,
      `latencyMs`, `ifUtilizationPct`, interface error counters).
  - Persists everything to DynamoDB (`network-topology` nodes,
    `network-links` edges) via `aioboto3`; re-hydrates an in-memory
    `networkx.DiGraph` on startup by scanning both tables and re-hydrates
    periodically (`TOPOLOGY_REFRESH_INTERVAL`, default 300 s) to self-heal
    from dropped Kafka messages.

- **FastAPI surface** — `GET /health` (200 iff DynamoDB + every consumer is
  connected), `GET /metrics` (Prometheus text exposition with
  `topology_messages_consumed_total`, `topology_messages_applied_total`,
  `topology_errors_total`, `topology_graph_nodes`, `topology_graph_edges`,
  `topology_ws_clients`, `topology_lag_seconds`), `GET /stats` (JSON
  per-topic throughput), and the query endpoints called by the agent and
  frontend:
  - `GET /topology` — full graph as `{nodes, edges}`.
  - `GET /topology/node/{node_id}` — node details + inbound / outbound edges.
  - `GET /topology/path?src=<ip>&dst=<ip>` — shortest path (maps IPs → nodes
    via explicit `metadata.ips` matches or subnet CIDR containment, then
    `networkx.shortest_path`).
  - `GET /topology/affected?prefix=<cidr>` — nodes and edges whose stored
    prefix matches or overlaps the query CIDR, plus aggregated AS-path impact.
  - `GET /topology/subgraph?node_id=<id>&depth=<n>` — BFS subgraph up to
    10 hops (used to build agent context without dragging in the whole graph).
  - `WebSocket /ws/topology` — sends an initial `snapshot` payload on
    connect, then streams per-mutation diffs (`link_announced`,
    `link_withdrawn`, `traffic_delta`, `node_health`) to the React dashboard.

- **services/topology/Dockerfile** — multi-stage `python:3.11-slim` build,
  non-root `topology:10002` user, container `HEALTHCHECK` polling `/health`.

- **services/topology/tests/** — pytest suite (graph invariants, path/
  affected/subgraph queries, handler behaviour for ANNOUNCE/WITHDRAW/
  traffic/health, HTTP + WebSocket smoke via `TestClient`) with an
  in-memory `FakeStore` that mirrors the DynamoDB write surface.

- **scripts/init_topology.py** — one-shot, idempotent seeder. Creates both
  DynamoDB tables (with the `node-type-lookup` GSI on nodes and
  `target-lookup` GSI on links) if absent, then writes a realistic starting
  topology: 4 VPCs across 3 regions, 10 subnets, 3 transit gateways in a
  full mesh, 1 local BGP speaker peering with 4 external ASNs. Gives the
  `/topology` endpoint something to render before any Kafka events arrive.

### Changed

- **docker-compose.yml** — added `topology` service. Depends on `redpanda`,
  `redpanda-init`, and `dynamodb-local` healthy. Container port 8080
  published on host `8092` (keeps 8080/8090/8091 free).

- **terraform/dynamodb.tf** — added a `node-type-lookup` GSI to the
  `network-topology` table (hash_key=`node_type`, projection=ALL) so agent
  queries can filter nodes by type without a full scan. `AttributeDefinitions`
  now includes `node_type`.

- **.env.example** — added `DYNAMODB_TABLE_LINKS`, a new `## Topology`
  section (`KAFKA_TOPOLOGY_GROUP_PREFIX`, `TOPOLOGY_REFRESH_INTERVAL`,
  `TRAFFIC_EWMA_ALPHA`), and corrected `DYNAMODB_TABLE_TOPOLOGY` default
  to the canonical `network-topology` used by the Terraform files.

---

## [Phase 2.2] — 2026-04-21 — Collector Service

### Added

- **services/collector/** — `netobserv_collector` Python 3.11 package that
  consumes `network.flows`, `network.bgp`, and `network.snmp` on **three
  independent Kafka consumer groups** (prefix `netobserv-collector-*`) and
  fans each message out to the destinations appropriate for its shape:

  - **Flows** → InfluxDB (`measurement=flows`, tags=srcaddr+dstaddr+protocol,
    fields=packets+bytes) **and** OpenSearch (`flows-YYYY.MM.DD`) **and**
    MinIO/S3 (`flows/YYYY/MM/DD/HH/<uuid>.json`).
  - **BGP events** → OpenSearch (`bgp-events-YYYY.MM.DD`) **and** republished
    to the `topology.updates` Kafka topic for the topology service.
  - **SNMP samples** → InfluxDB (`measurement=snmp`, tags=device_id+interface+metric,
    fields=value).

- **Batching** — InfluxDB line-protocol 500-point batches; OpenSearch `_bulk`
  100-doc batches; MinIO/S3 1-minute NDJSON chunks. Each destination runs on
  its own `asyncio.Task` so a failing sink never blocks writes to any other
  sink.

- **HTTP endpoints** — `GET /health` (200 iff every client is connected),
  `GET /metrics` (Prometheus text exposition: `collector_messages_consumed_total`,
  `collector_messages_written_total`, `collector_errors_total`,
  `collector_lag_seconds`), `GET /stats` (JSON per-topic throughput).

- **Environment-agnostic S3/MinIO** — blank `S3_ENDPOINT` targets real AWS
  S3; a populated endpoint targets MinIO. Same code path in both envs.

- **services/collector/Dockerfile** — multi-stage `python:3.11-slim` build,
  non-root `collector:10001` user, container `HEALTHCHECK` polling `/health`.

- **services/collector/tests/** — pytest suite with mocked clients covering
  each fan-out handler (flows, bgp, snmp), isolation under destination
  failures, InfluxDB line-protocol escaping, and the metrics registry.

- **Terraform (AWS equivalents):**
  - `terraform/timestream.tf` — `netobserv` database with two tables
    (`metrics`, `flows`); split memory/magnetic retention tuned per table.
  - `terraform/opensearch.tf` — single-node `t3.small.search` domain with
    fine-grained access control (master user in Secrets Manager), at-rest +
    node-to-node encryption, HTTPS-only, CloudWatch application logs, and
    optional VPC placement (`opensearch_subnet_ids`).
  - `terraform/s3.tf` — versioned archive bucket with a random suffix,
    public-access block, AES256 SSE, lifecycle transition to Deep Archive
    after 30 days and expiration at 365 days (plus non-current cleanup and
    incomplete-multipart abort).
  - `terraform/dynamodb.tf` — `network-topology` (PK=node_id) and
    `network-links` (PK=source_node, SK=target_node) with a
    `target-lookup` GSI, PAY_PER_REQUEST, point-in-time recovery, SSE.

### Changed

- **docker-compose.yml** — added `collector` service (depends on `redpanda`,
  `influxdb`, `opensearch`, and `minio` healthy + the init one-shots
  completed). Container port 8080 mapped to host `8091`. Added
  `topology.updates` to the topics provisioned by `redpanda-init`.

- **.env.example** — added `KAFKA_TOPIC_TOPOLOGY_UPDATES=topology.updates`,
  `KAFKA_COLLECTOR_GROUP_PREFIX`, and a new `## Collector` section with
  tuning knobs for batch sizes and archive flush interval.

---

## [Phase 2.1] — 2026-04-20 — Simulator Service

### Added

- **services/simulator/** — `netobserv_simulator` Python 3.11 package that
  generates synthetic VPC Flow Log v2 records, BGP events, and SNMP metric
  samples, publishing each to its own Kafka topic. Async producer built on
  `aiokafka`, config via `pydantic-settings`, structured JSON logging via
  `structlog`, embedded FastAPI `/health` endpoint on port 8080.

- **Fixed topology for learnability** — 20 source IPs across three internal
  subnets, 50 destinations (mix of public services, CDN ranges, upstream peers),
  10 simulated routers with 3 interfaces each, 4 BGP peers announcing 6 prefixes.
  Traffic mix weighted: HTTPS 55%, HTTP 15%, DNS 15%, SSH 5%, BGP 4%, NTP/QUIC 6%.

- **Scenario engine** — injects one of six scenarios every
  `ANOMALY_INTERVAL_MINUTES` (default 15): `normal`, `bgp_flap`,
  `traffic_spike`, `route_leak`, `link_saturation`, `latency_spike`. Three
  scheduling modes: `scheduled` (round-robin), `random`, `single`. Every
  injection is logged at WARNING with `scenario.injected` so detections can
  be correlated with ground truth.

- **services/simulator/Dockerfile** — multi-stage build, non-root user
  `sim:10001`, minimal `python:3.11-slim` runtime, container `HEALTHCHECK`
  polling the HTTP endpoint.

- **services/simulator/tests/** — pytest suite covering schema conformance
  for all three generators, topology constants, and the full scenario
  scheduler (injection timing, rotation, per-scenario parameters, expiry).

- **services/simulator/README.md** — full scenario reference, env-var table,
  standalone and compose run instructions.

- **terraform/kinesis.tf** — four Kinesis Data Streams mirroring the Kafka
  topics (`network-flows`, `network-bgp`, `network-snmp`,
  `anomalies-scored`): KMS encryption, per-stream configurable shard counts,
  24–8760h retention variable, shard-level CloudWatch metrics, tagged with
  `LocalTopic` for traceability back to the local stack. Outputs stream
  ARNs and names as maps for downstream consumers.

### Changed

- **docker-compose.yml** — added `simulator` service (depends on
  `redpanda` healthy and `redpanda-init` completed). Container port 8080
  mapped to host `8090` to avoid colliding with Redpanda Console.

- **.env.example / .env** — replaced legacy simulator vars
  (`SIMULATOR_EMIT_INTERVAL_MS`, `SIMULATOR_NUM_NODES`,
  `SIMULATOR_ANOMALY_RATE`) with the new schema (`SIMULATOR_PRODUCER_RATE`,
  `SIMULATOR_SCENARIO_MODE`, `SIMULATOR_SCENARIO`,
  `SIMULATOR_ANOMALY_INTERVAL_MINUTES`, `SIMULATOR_SNMP_INTERVAL_SECONDS`).

---

## [Phase 2] — 2026-04-19 — Local Infrastructure Stack

### Added

- **docker-compose.yml** — full local stack with 11 services:
  Redpanda (+ init + console), InfluxDB 2.7, OpenSearch 2.13 (+ Dashboards),
  DynamoDB Local 2.4, MinIO (+ init), Postgres 16 + pgvector, Grafana 10.4.
  All services have health checks, named volumes, and `restart: unless-stopped`.

- **docker/postgres/init.sql** — runs on first boot; creates the `vector`
  extension and the `runbook_embeddings` table with an IVFFlat cosine index.

- **grafana/provisioning/datasources/influxdb.yaml** — auto-configures
  InfluxDB as the default Grafana datasource (Flux query language); credentials
  injected via environment variables at container start.

- **scripts/verify-stack.sh** — bash script that pings every service health
  endpoint and the Redpanda topic list, reports pass/fail summary, exits 1 if
  any check fails.

### Changed

- **.env.example** — updated Kafka topic names to the canonical set
  (`network.flows`, `network.bgp`, `network.snmp`, `anomalies.scored`,
  `anomalies.runbooks`); added `INFLUXDB_RETENTION`, `OPENSEARCH_JAVA_OPTS`,
  `S3_BUCKET_ARCHIVE`; renamed `S3_BUCKET_RAW` → `S3_BUCKET_ARCHIVE`.

---

## [Phase 1] — 2026-04-18 — Repo Skeleton

### Added

- **Repository structure** — full locked-in directory layout:
  `services/{simulator,collector,topology,anomaly,agent,api,frontend}`,
  `terraform/`, `grafana/`, `scripts/`, `docs/`

- **README.md** — project overview, "Why this project" rationale, quick-start
  guide, service endpoint table, project structure map, and "Deploy to AWS" section.
  Professional badge row for Python, Docker, Terraform, React, FastAPI.

- **ARCHITECTURE.md** — dual-target design rationale, description of all five
  layers (ingestion, storage, anomaly detection, agent, frontend), full ASCII
  data-flow diagram, and local ↔ AWS component mapping table.

- **.gitignore** — comprehensive ignore rules for Python, Node, Terraform,
  IDE files, OS files, Docker overrides, and all `.env` variants (`.env.example` is tracked).

- **.env.example** — every environment variable the platform will ever need,
  grouped by service, with safe placeholder values for local Docker Compose.
  Includes `ANTHROPIC_API_KEY`, all Kafka/InfluxDB/OpenSearch/DynamoDB/
  MinIO/Postgres/Grafana connection strings, and Terraform variable stubs.

- **Makefile** — targets: `up`, `down`, `logs`, `clean`, `ps`, `seed`, `train`,
  `test`, `lint`, `fmt`, `tf-init`, `tf-plan`, `tf-apply`, `tf-destroy`.
  Coloured help output when run with no arguments.

- **pyproject.toml** — shared dev tooling: `ruff`, `mypy`, `pytest`,
  `pre-commit`, plus `ruff` formatter and linter configuration.

- **.pre-commit-config.yaml** — hooks: `ruff` (lint + format), `mypy`,
  `terraform fmt`, standard file hygiene (trailing whitespace, end-of-file,
  large file guard).

- **terraform/providers.tf** — AWS provider skeleton with version pinning;
  region sourced from variable.

- **terraform/variables.tf** — variable stubs: `aws_region`, `project_name`,
  `environment`, `vpc_cidr`.

- **Service placeholder READMEs** — each service directory contains a
  `README.md` describing its responsibility, planned tech, and interface.
