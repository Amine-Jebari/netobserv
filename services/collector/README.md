# Service: collector

Kafka → InfluxDB / OpenSearch / S3 fan-out layer. One Python process consumes
the three telemetry topics on **independent consumer groups** (so they scale
separately) and dispatches each message to the destinations appropriate for its
shape. A failure in one destination never blocks writes to another.

## Fan-out matrix

| Topic          | InfluxDB                              | OpenSearch                           | S3 / MinIO                           | Kafka (out)        |
| -------------- | ------------------------------------- | ------------------------------------ | ------------------------------------ | ------------------ |
| `network.flows`| `measurement=flows`, tags=srcaddr+dstaddr+protocol, fields=packets+bytes | `flows-YYYY.MM.DD`                   | `flows/YYYY/MM/DD/HH/<uuid>.json`    | —                  |
| `network.bgp`  | —                                     | `bgp-events-YYYY.MM.DD`              | —                                    | `topology.updates` |
| `network.snmp` | `measurement=snmp`, tags=device_id+interface+metric, fields=value | —                                    | —                                    | —                  |

## Batching

Each destination flushes on whichever threshold fires first:

| Destination | Size trigger              | Time trigger  |
| ----------- | ------------------------- | ------------- |
| InfluxDB    | 500 points per write      | 5 s           |
| OpenSearch  | 100 docs per `_bulk`      | 5 s           |
| S3 / MinIO  | effectively unbounded     | 60 s (NDJSON) |
| Kafka (BGP) | publish-per-event         | —             |

Flushers are independent `asyncio.Task`s: a slow or failing destination applies
back-pressure only to its own queue, never to the consumer or to the other
destinations.

## Consumer groups

Topics are consumed on independent groups (`KAFKA_CONSUMER_GROUP_PREFIX` — default
`netobserv-collector`):

- `netobserv-collector-flows`
- `netobserv-collector-bgp`
- `netobserv-collector-snmp`

## Endpoints

| Method | Path       | Purpose                                                           |
| ------ | ---------- | ----------------------------------------------------------------- |
| GET    | `/health`  | 200 iff every client (influx/opensearch/archive/kafka) is connected |
| GET    | `/metrics` | Prometheus text exposition (`collector_messages_consumed_total`, `collector_messages_written_total`, `collector_errors_total`, `collector_lag_seconds`) |
| GET    | `/stats`   | JSON snapshot: per-topic throughput, errors, lag                   |

## Environment variables

| Var                              | Purpose                                              |
| -------------------------------- | ---------------------------------------------------- |
| `KAFKA_BOOTSTRAP_SERVERS`        | Redpanda/Kafka bootstrap                             |
| `KAFKA_TOPIC_FLOWS/_BGP/_SNMP`   | Input topic names                                    |
| `KAFKA_TOPIC_TOPOLOGY_UPDATES`   | Output topic for topology updates (`topology.updates`) |
| `KAFKA_CONSUMER_GROUP_PREFIX`    | Prefix for the three per-topic consumer groups       |
| `KAFKA_AUTO_OFFSET_RESET`        | `latest` (default) or `earliest`                     |
| `INFLUXDB_URL`                   | InfluxDB 2.x base URL                                |
| `INFLUXDB_TOKEN`                 | Write token                                          |
| `INFLUXDB_ORG`, `INFLUXDB_BUCKET`| Target org + bucket                                  |
| `OPENSEARCH_URL`                 | OpenSearch base URL (accepts `OPENSEARCH_HOST` too)  |
| `DYNAMODB_ENDPOINT`              | Reserved for topology service (passed through)       |
| `AWS_ACCESS_KEY_ID`              | S3/MinIO credentials                                 |
| `AWS_SECRET_ACCESS_KEY`          | S3/MinIO credentials                                 |
| `S3_ENDPOINT`                    | MinIO endpoint (blank → real AWS S3)                 |
| `S3_BUCKET`                      | Archive bucket name                                  |
| `S3_REGION`                      | Region (real AWS)                                    |
| `HTTP_PORT`                      | HTTP port for `/health`, `/metrics`, `/stats`        |
| `LOG_LEVEL`                      | `DEBUG` / `INFO` / `WARNING` / `ERROR`               |

Locally these are populated from the repo-wide `.env`. In production the same
names are set by Terraform outputs / Secrets Manager — **no code changes**.

## Run it

**With the stack:**

```bash
docker compose up -d collector
curl http://localhost:8091/health
curl http://localhost:8091/stats | jq
curl http://localhost:8091/metrics
```

**Tests:**

```bash
cd services/collector
pip install -e '.[dev]'
pytest
```
