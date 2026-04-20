# Service: collector

## Responsibility

Consumes the `network.telemetry` Kafka topic and fans each message out to four storage backends simultaneously. Acts as the single write path into the storage layer so upstream services never need to know about individual store clients.

## Fanout targets

| Target | Purpose |
|---|---|
| InfluxDB | Time-series metrics for Grafana and the anomaly service |
| OpenSearch | Full-text event log; searchable by node, severity, timestamp |
| DynamoDB | Current topology state (node/edge adjacency updated in real time) |
| MinIO (S3) | Raw message archive in NDJSON format, partitioned by date |

## Planned tech

- Python 3.11 + `aiokafka` consumer
- `influxdb-client`, `opensearch-py`, `boto3`, `aiobotocore`
- Async fanout with `asyncio.gather` — a failure to one backend is logged and retried, not fatal
- `structlog` + dead-letter logic: messages that fail all retries go to `network.telemetry.dlq`

## Key env vars

`KAFKA_BOOTSTRAP_SERVERS`, `INFLUXDB_URL`, `OPENSEARCH_HOST`, `DYNAMODB_ENDPOINT_URL`, `S3_ENDPOINT_URL`

## Health check

`GET /health` → `{"status": "ok", "lag": <consumer_lag>, "backends": {...}}`
