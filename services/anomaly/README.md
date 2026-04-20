# Service: anomaly

## Responsibility

Reads time-windowed metrics from InfluxDB, extracts features per node, and scores each window with a trained Isolation Forest model (PyOD). When the anomaly score exceeds the configured threshold, publishes an anomaly event to the `network.anomalies` Kafka topic.

## Detection pipeline

```
InfluxDB (sliding window query)
    → feature extraction (delta rates, packet loss %, BGP flap count)
    → PyOD IsolationForest.score_samples()
    → threshold comparison
    → if anomaly: publish to network.anomalies
```

## Anomaly event schema

```json
{
  "timestamp": "2026-04-18T12:00:00Z",
  "node_id": "router-07",
  "score": 0.83,
  "features": { "bytes_delta": 1e9, "loss_pct": 12.5, "bgp_flaps": 3 },
  "severity": "high | medium | low"
}
```

## Planned tech

- Python 3.11 + `pyod`, `scikit-learn`, `influxdb-client`
- Model artifact stored in MinIO; loaded at startup and hot-reloaded on retrain
- `scripts/train_baseline.py` retrains from 7-day InfluxDB history

## Key env vars

`INFLUXDB_URL`, `ANOMALY_WINDOW_SECONDS`, `ANOMALY_SCORE_THRESHOLD`, `ANOMALY_MODEL_PATH`, `KAFKA_BOOTSTRAP_SERVERS`

## Health check

`GET /health` → `{"status": "ok", "model_version": "<sha>", "last_scored": "<ts>"}`
