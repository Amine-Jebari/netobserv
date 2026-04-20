# Service: frontend

## Responsibility

React + D3.js single-page application. Provides a real-time network observability dashboard with a live topology graph, metric sparklines, anomaly event feed, and an AI-generated runbook viewer.

## Key views

| View | Description |
|---|---|
| **Topology Graph** | D3.js force-directed graph; anomalous edges pulse red in real time |
| **Node Detail** | Click a router → metrics panel with InfluxDB sparklines |
| **Anomaly Feed** | Real-time list of detected anomalies with severity badges |
| **Runbook Viewer** | Rendered Markdown of Claude-generated remediation steps |
| **Grafana Embed** | Iframe embed of the Grafana dashboard for deep metric drill-down |

## Planned tech

- React 18 + TypeScript
- D3.js v7 for topology rendering
- WebSocket hook for real-time updates from the API service
- `react-markdown` for runbook rendering
- `recharts` for sparklines
- Nginx for static file serving in Docker

## Key env vars

| Variable | Purpose |
|---|---|
| `REACT_APP_API_URL` | FastAPI base URL |
| `REACT_APP_WS_URL` | WebSocket endpoint |
| `REACT_APP_ENV` | Environment label displayed in the UI |

## Health check

Nginx serves `GET /health` → `200 OK` from a static file.
