# Architecture

## Dual-Target Design

Every service in this platform is written once and deployed twice: locally with Docker Compose, and on AWS with Terraform. The application code never changes between targets — only environment variables do. This is enforced by:

- `pydantic-settings` in every Python service, which reads all config from env vars with explicit types and defaults
- A single `.env` file for local development (gitignored), mirrored by `.env.example` with safe placeholder values
- AWS Secrets Manager and ECS task definition environment injection for production

---

## The Five Layers

### 1. Ingestion Layer
Telemetry is produced by the **simulator** service, which emits synthetic BGP UPDATE messages and SNMP interface metrics at a configurable rate. Messages are published to a Kafka topic (`network.telemetry`). Locally this is Redpanda; on AWS it is Kinesis Data Streams with the same producer/consumer API via the Kafka compatibility layer.

### 2. Storage Layer
The **collector** service consumes the Kafka topic and fans data out to four stores, each optimized for a different query pattern:

| Store | Local | AWS | Purpose |
|---|---|---|---|
| Time-series | InfluxDB 2.x | Timestream | Metric graphs, rate-of-change queries |
| Log / full-text | OpenSearch | OpenSearch Service | Event search, anomaly log archive |
| Topology graph | DynamoDB Local | DynamoDB | Node/edge adjacency for the live graph |
| Object storage | MinIO | S3 | Raw telemetry archive, runbook PDFs |

A fifth store — Postgres + pgvector — holds vector embeddings of historical runbooks for the RAG pipeline.

### 3. Anomaly Detection Layer
The **anomaly** service reads time-windowed metrics from InfluxDB, scores each window with an Isolation Forest model (PyOD), and publishes anomaly events to a second Kafka topic (`network.anomalies`) when the score exceeds the trained threshold. The model is retrained on a rolling 7-day baseline via the `scripts/train_baseline.py` job. On AWS, inference moves to a SageMaker Serverless endpoint, but the same scikit-learn/PyOD model artifact is used.

### 4. Agent Layer
The **agent** service subscribes to `network.anomalies`. On each event it:

1. Retrieves the affected topology context from DynamoDB
2. Embeds the anomaly description and queries pgvector for the top-k most similar past incidents
3. Calls the Claude API with the anomaly context + retrieved runbooks as few-shot examples
4. Writes the generated runbook to MinIO and indexes it in OpenSearch
5. Publishes a `network.runbooks` event for the API to surface

### 5. Frontend Layer
The **api** service (FastAPI) exposes a REST + WebSocket API consumed by the **frontend** (React + D3.js). The dashboard shows:

- Live topology graph with highlighted anomalous edges
- Metric sparklines per node (InfluxDB → API → frontend)
- Anomaly event feed with severity badges
- Runbook viewer with the Claude-generated remediation steps

---

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          INGESTION LAYER                                 │
│                                                                          │
│   ┌───────────┐   BGP/SNMP    ┌─────────────────────────────────────┐  │
│   │ simulator │──────────────▶│  Redpanda  (topic: network.telemetry)│  │
│   └───────────┘               └──────────────────┬──────────────────┘  │
└──────────────────────────────────────────────────│─────────────────────┘
                                                    │ consume
┌──────────────────────────────────────────────────▼─────────────────────┐
│                           STORAGE LAYER                                  │
│                                                                          │
│   ┌───────────┐   fanout     ┌──────────┐  ┌───────────┐  ┌─────────┐  │
│   │ collector │─────────────▶│ InfluxDB │  │OpenSearch │  │  MinIO  │  │
│   └───────────┘              └──────────┘  └───────────┘  └─────────┘  │
│                                                    │                     │
│                              ┌─────────────┐  ┌───▼──────┐             │
│                              │ DynamoDB    │  │ Postgres  │             │
│                              │ (topology)  │  │ +pgvector │             │
│                              └──────┬──────┘  └──────────┘             │
└─────────────────────────────────────│──────────────────────────────────┘
                                      │
┌─────────────────────────────────────▼──────────────────────────────────┐
│                        ANOMALY DETECTION LAYER                           │
│                                                                          │
│   ┌───────────────────┐  anomaly event  ┌──────────────────────────┐   │
│   │  anomaly service  │────────────────▶│  Redpanda                │   │
│   │  (PyOD Isolation  │                 │  (topic: network.anomalies)│  │
│   │   Forest)         │                 └────────────┬─────────────┘   │
└───┴───────────────────┴─────────────────────────────│──────────────────┘
                                                       │ consume
┌──────────────────────────────────────────────────────▼─────────────────┐
│                           AGENT LAYER                                    │
│                                                                          │
│   ┌────────────────────────────────────────────┐                        │
│   │  agent service                             │                        │
│   │  1. DynamoDB  → topology context           │                        │
│   │  2. pgvector  → similar past runbooks      │                        │
│   │  3. Claude API → generate new runbook      │                        │
│   │  4. MinIO     ← store runbook artifact     │                        │
│   │  5. Redpanda  → publish network.runbooks   │                        │
│   └────────────────────────────────────────────┘                        │
└─────────────────────────────────────────────────────────────────────────┘
                                      │
┌─────────────────────────────────────▼──────────────────────────────────┐
│                          FRONTEND LAYER                                  │
│                                                                          │
│   ┌────────────┐  REST+WS   ┌──────────────┐  HTTP   ┌──────────────┐  │
│   │  React +   │◀──────────▶│  FastAPI     │◀───────▶│  all stores  │  │
│   │  D3.js     │            │  (api svc)   │         │  above       │  │
│   └────────────┘            └──────────────┘         └──────────────┘  │
│                                                                          │
│   Grafana (separate container) reads directly from InfluxDB              │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Local ↔ AWS Mapping

| Component | Docker Compose | AWS |
|---|---|---|
| Message broker | Redpanda | Kinesis Data Streams |
| Time-series DB | InfluxDB 2.x | Timestream |
| Search / logs | OpenSearch (Docker) | OpenSearch Service |
| Graph / KV store | DynamoDB Local | DynamoDB |
| Object storage | MinIO | S3 |
| Vector DB | Postgres + pgvector | RDS Postgres + pgvector |
| ML inference | In-process PyOD | SageMaker Serverless |
| LLM | Claude API | Claude API (unchanged) |
| Backend | FastAPI container | ECS Fargate or Lambda |
| Frontend | Nginx container | S3 + CloudFront |
| Metrics viz | Grafana container | Grafana on ECS |
| Secrets | `.env` file | AWS Secrets Manager |
| Networking | Docker bridge | VPC + private subnets |
