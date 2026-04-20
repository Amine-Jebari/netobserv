# Service: simulator

## Responsibility

Generates synthetic network telemetry and publishes it to the Kafka topic `network.telemetry`. Simulates a fleet of routers exchanging BGP UPDATE messages and reporting SNMP interface metrics (bytes in/out, error counts, link state).

## What it produces

Each message on `network.telemetry` is a JSON object with this shape:

```json
{
  "timestamp": "2026-04-18T12:00:00Z",
  "node_id": "router-07",
  "event_type": "bgp_update | snmp_metric",
  "payload": { ... }
}
```

## Planned tech

- Python 3.11 + `aiokafka` (async Kafka producer)
- `pydantic` models for every message schema
- `structlog` for structured JSON logging
- Configurable via env vars: emit rate, number of nodes, anomaly injection rate

## Key env vars

| Variable | Purpose |
|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | Redpanda / Kinesis endpoint |
| `SIMULATOR_EMIT_INTERVAL_MS` | Milliseconds between messages |
| `SIMULATOR_NUM_NODES` | Number of simulated routers |
| `SIMULATOR_ANOMALY_RATE` | Fraction of messages intentionally anomalous |

## Health check

`GET /health` → `{"status": "ok", "messages_sent": <count>}`
