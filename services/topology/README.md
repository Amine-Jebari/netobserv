# Service: topology

## Responsibility

Maintains a live graph of the network topology in DynamoDB. Subscribes to `network.telemetry`, extracts BGP adjacency changes, and upserts node/edge records. Exposes a REST API so the frontend and agent service can query current and historical topology state.

## Data model (DynamoDB)

Each item in `netobserv-topology`:

```
PK: NODE#<node_id>   SK: EDGE#<peer_id>
attrs: { asn, ip, weight, state, last_seen, bgp_state }
```

## Planned tech

- Python 3.11 + FastAPI
- `boto3` DynamoDB resource API with single-table design
- NetworkX for in-memory graph algorithms (shortest path, affected subgraph extraction)
- WebSocket endpoint broadcasting topology diffs to the frontend

## Key env vars

`DYNAMODB_ENDPOINT_URL`, `DYNAMODB_TABLE_TOPOLOGY`, `KAFKA_BOOTSTRAP_SERVERS`

## Health check

`GET /health` → `{"status": "ok", "node_count": <n>, "edge_count": <n>}`
