# Service: api

## Responsibility

FastAPI backend that the React frontend consumes. Exposes a REST API for topology, metrics, anomaly events, and runbooks. Also serves a WebSocket endpoint that pushes real-time anomaly and runbook events to connected clients.

## Endpoints (planned)

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/topology` | Current network graph (nodes + edges) |
| `GET` | `/metrics/{node_id}` | Time-series metrics for a node |
| `GET` | `/anomalies` | Paginated anomaly event list |
| `GET` | `/runbooks` | Paginated runbook list |
| `GET` | `/runbooks/{id}` | Full runbook Markdown |
| `WS` | `/ws` | Real-time anomaly + runbook push stream |

## Planned tech

- Python 3.11 + FastAPI + Uvicorn
- `influxdb-client` for metric queries
- `opensearch-py` for runbook/event queries
- `boto3` for DynamoDB topology reads
- JWT-based auth (optional, configurable off for demo)

## Key env vars

`API_HOST`, `API_PORT`, `API_CORS_ORIGINS`, `INFLUXDB_URL`, `OPENSEARCH_HOST`, `DYNAMODB_ENDPOINT_URL`

## Health check

`GET /health` → `{"status": "ok", "version": "0.1.0"}`
