# Service: anomaly

Two responsibilities under one roof:

1. **Phase 3.1 — baseline trainer (this directory).** A one-shot pipeline that
   reads accumulated telemetry from InfluxDB + OpenSearch, engineers per-minute
   features, trains three PyOD models, and ships the bundle to MinIO/S3.
2. **Phase 3.2 — real-time scoring engine** (added in the next phase). It will
   load the same bundle from MinIO/S3 and score live telemetry pulled from
   Kafka.

Both phases import feature engineering from a single source — `features.py` —
so what's computed at training time matches what's computed at inference time
exactly.

---

## Module map

| File | Purpose |
| --- | --- |
| `baseline.py` | Training pipeline + `python -m services.anomaly.baseline` entry point. |
| `features.py` | Per-minute feature engineering shared by trainer and engine. |
| `config.py` | `pydantic-settings` schema for every env var the service reads. |
| `influx_query.py` | Synchronous Flux queries against InfluxDB 2.x. |
| `opensearch_query.py` | Paginated `_search` against the BGP indices. |
| `model_store.py` | Joblib serialization, tarball bundling, S3/MinIO upload. |
| `logging_setup.py` | structlog JSON-line configuration. |
| `Dockerfile` | One-shot training image (`docker compose --profile training up baseline`). |
| `tests/` | pytest suite — feature determinism, model round-trip. |

---

## Running

### Local one-shot

```bash
# From the repo root, with .env loaded into your shell:
python -m services.anomaly.baseline
```

### Docker (training profile)

```bash
docker compose --profile training up baseline
```

The training profile keeps `baseline` out of the default `docker compose up`
flow — it's a one-shot job that should run on demand, not as a long-lived
service.

---

## Environment variables

Required:

| Var | Purpose |
| --- | --- |
| `INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET` | InfluxDB query target (flows + SNMP). |
| `S3_ENDPOINT` | Blank → real AWS S3; populated → MinIO. |
| `S3_BUCKET` | Bucket where bundles are uploaded (default `netobserv-archive`). |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | S3/MinIO credentials. |
| `LOOKBACK_HOURS` | Training window in hours (default `24`, capped at 168). |

Optional:

| Var | Default | Purpose |
| --- | --- | --- |
| `OPENSEARCH_URL` (or `OPENSEARCH_HOST`) | `http://opensearch:9200` | BGP event index host. |
| `OPENSEARCH_USER`, `OPENSEARCH_PASSWORD` | empty | Set on real AWS OpenSearch. |
| `OPENSEARCH_BGP_INDEX_PATTERN` | `bgp-events-*` | Index pattern to query. |
| `S3_MODEL_PREFIX` | `models` | Key prefix for the bundles. |
| `CONTAMINATION_TRAFFIC` / `CONTAMINATION_BGP` / `CONTAMINATION_SNMP` | `0.05` | PyOD contamination per model. |
| `TEST_SPLIT` | `0.2` | Held-out fraction for AUC evaluation. |
| `SYNTHETIC_OUTLIERS_PER_EVAL` | `20` | Synthetic ±5σ outliers injected during eval. |
| `MIN_TRAINING_MINUTES` | `30` | Minimum per-minute rows required to train a model. |
| `RANDOM_SEED` | `42` | Reproducibility. |
| `LOG_LEVEL` | `INFO` | structlog JSON output level. |

If OpenSearch is unreachable, BGP training is skipped (logged at WARNING) and
the bundle still ships with the traffic + SNMP models.

---

## Model bundle format

The trainer uploads a single `.tar.gz` whose flat layout is:

```
baseline-vYYYYMMDDTHHMMSSZ.tar.gz
├── traffic_iforest.joblib   # PyOD IForest fitted on TRAFFIC_FEATURES
├── bgp_lof.joblib           # PyOD LOF (novelty=True) fitted on BGP_FEATURES
├── snmp_mcd.joblib          # PyOD MCD (Mahalanobis) fitted on SNMP_FEATURES
└── metadata.json
```

Two keys are always written to MinIO/S3:

* `models/baseline-vYYYYMMDDTHHMMSSZ.tar.gz` — immutable, timestamped.
* `models/baseline-latest.tar.gz` — copy alias the engine pulls on startup.

### `metadata.json` schema

```jsonc
{
  "training_started_at":  "2026-04-27T12:00:00+00:00",
  "training_finished_at": "2026-04-27T12:01:42+00:00",
  "training_window_hours": 24,
  "row_counts": { "traffic": 1432, "bgp": 78, "snmp": 1432 },
  "feature_schema": {
    "traffic": [ "total_bytes", "total_packets", ... ],
    "bgp":     [ "announce_count", "withdraw_count", ... ],
    "snmp":    [ "cpu_load_mean", "cpu_load_max", ... ]
  },
  "feature_stats": {
    "traffic": { "total_bytes": { "mean": 9.4e6, "std": 3.1e6 }, ... },
    "bgp":     { ... },
    "snmp":    { ... }
  },
  "model_metrics": {
    "traffic_iforest": {
      "auc": 0.998, "n_train": 1146, "n_test": 286, "n_synth": 20,
      "decision_p50": -0.043, "decision_p95":  0.011,
      "observed_contamination": 0.05, "contamination_target": 0.05
    },
    "bgp_lof":   { ... },
    "snmp_mcd":  { ... }
  },
  "library_versions": {
    "numpy": "1.26.4", "pandas": "2.2.2", "scikit-learn": "1.4.2",
    "pyod": "1.1.3",  "joblib": "1.4.0", "boto3": "1.34.85"
  },
  "bundle_format_version": 1
}
```

### Feature spaces (canonical column order)

Each list below **is** the order the model was fitted on. The real-time engine
must build feature vectors in exactly this order before calling
`model.decision_function(X)`.

#### Traffic — `traffic_iforest` (Isolation Forest)

| Column | Meaning |
| --- | --- |
| `total_bytes` | Sum of `bytes` across all flows in the minute. |
| `total_packets` | Sum of `packets`. |
| `num_flows` | Count of flow records in the minute. |
| `unique_src_count` | Distinct `srcaddr` values. |
| `unique_dst_count` | Distinct `dstaddr` values. |
| `top_pair_share` | Bytes of the noisiest src→dst pair / total bytes (∈ [0,1]). |
| `bytes_per_packet` | `total_bytes / total_packets` (mean packet size). |
| `hour_sin`, `hour_cos` | Cyclic encoding of hour-of-day. |
| `dow_sin`, `dow_cos`   | Cyclic encoding of day-of-week. |

#### BGP — `bgp_lof` (Local Outlier Factor, `novelty=True`)

| Column | Meaning |
| --- | --- |
| `announce_count` | Number of `event_type == ANNOUNCE` events. |
| `withdraw_count` | Number of `event_type == WITHDRAW` events. |
| `update_count`   | Number of `event_type == UPDATE` events. |
| `unique_prefixes` | Distinct prefixes touched. |
| `unique_peers`    | Distinct peer IPs active in the minute. |
| `as_path_mean`    | Mean of `len(as_path)` across events in the minute. |
| `as_path_std`     | Std-dev of `len(as_path)`. |
| `hour_sin`, `hour_cos`, `dow_sin`, `dow_cos` | Diurnal features. |

#### SNMP — `snmp_mcd` (Minimum Covariance Determinant / Mahalanobis)

| Column | Aggregation |
| --- | --- |
| `cpu_load_mean`         | Mean of `cpuLoad` samples. |
| `cpu_load_max`          | Max of `cpuLoad` samples. |
| `latency_ms_mean`       | Mean of `latencyMs`. |
| `latency_ms_p95`        | 95th-percentile of `latencyMs`. |
| `if_utilization_mean`   | Mean of `ifUtilizationPct`. |
| `if_utilization_max`    | Max of `ifUtilizationPct`. |
| `if_in_errors_total`    | Sum of `ifInErrors`. |
| `if_out_errors_total`   | Sum of `ifOutErrors`. |
| `hour_sin`, `hour_cos`, `dow_sin`, `dow_cos` | Diurnal features. |

Missing metric names are treated as zero rather than NaN, so a window where
e.g. SNMP polling glitched for one device degrades gracefully instead of
poisoning the row.

---

## Cross-validation

We don't have ground-truth anomaly labels, so `decision_function` AUC is
computed with an unsupervised trick:

1. Hold out `TEST_SPLIT` of the per-minute rows from training.
2. Synthesize `SYNTHETIC_OUTLIERS_PER_EVAL` clearly anomalous points by
   shifting each feature by a random ±5–8σ from the training mean.
3. Score the held-out real rows (label `0`) and the synthetics (label `1`),
   compute `roc_auc_score`.

This is a sanity check that the model separates obvious outliers from the
training distribution — it does **not** measure recall on real attacks. The
real evaluation happens once labels exist (Phase 7 evaluation harness).

---

## Tests

```bash
# From repo root
pytest services/anomaly/tests/

# Or from this directory
pytest
```
