# Changelog

All notable changes to the Network Observability Platform are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
