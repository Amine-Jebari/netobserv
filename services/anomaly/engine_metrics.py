"""Prometheus metrics for the real-time anomaly engine.

Hand-rolled to avoid adding ``prometheus_client`` as a dep — the surface is
small enough that we can render the text-exposition format ourselves and stay
consistent with the collector + topology services.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class EngineMetrics:
    """Mutable in-process counters/gauges + a Prometheus renderer.

    Methods are short and synchronous; we hold a single lock for simplicity
    because contention is bounded (≤ a handful of writes per scoring window).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._anomalies: dict[str, int] = defaultdict(int)
        self._messages: dict[str, int] = defaultdict(int)
        self._scoring_latency_seconds_sum: float = 0.0
        self._scoring_latency_count: int = 0
        self._scoring_latency_buckets: list[tuple[float, int]] = [
            (0.005, 0),
            (0.01, 0),
            (0.025, 0),
            (0.05, 0),
            (0.1, 0),
            (0.25, 0),
            (0.5, 0),
            (1.0, 0),
            (2.5, 0),
            (5.0, 0),
            (10.0, 0),
        ]
        self._model_reload_timestamp: float = 0.0
        self._model_loaded: bool = False
        self._slack_alerts_sent: int = 0
        self._slack_failures: int = 0

    # ── recording ────────────────────────────────────────────────────────────

    def record_anomaly(self, severity: str) -> None:
        with self._lock:
            self._anomalies[severity] += 1

    def record_message(self, stream: str, count: int = 1) -> None:
        with self._lock:
            self._messages[stream] += count

    def observe_scoring_latency(self, seconds: float) -> None:
        with self._lock:
            self._scoring_latency_seconds_sum += seconds
            self._scoring_latency_count += 1
            for i, (le, count) in enumerate(self._scoring_latency_buckets):
                if seconds <= le:
                    self._scoring_latency_buckets[i] = (le, count + 1)

    def mark_model_reloaded(self) -> None:
        with self._lock:
            self._model_reload_timestamp = time.time()
            self._model_loaded = True

    def mark_model_unloaded(self) -> None:
        with self._lock:
            self._model_loaded = False

    def record_slack_sent(self) -> None:
        with self._lock:
            self._slack_alerts_sent += 1

    def record_slack_failure(self) -> None:
        with self._lock:
            self._slack_failures += 1

    # ── snapshots ────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            avg_latency = (
                self._scoring_latency_seconds_sum / self._scoring_latency_count
                if self._scoring_latency_count
                else 0.0
            )
            return {
                "uptime_seconds": round(time.time() - self._started_at, 2),
                "model_loaded": self._model_loaded,
                "model_reload_timestamp": self._model_reload_timestamp,
                "anomalies_detected_total": dict(self._anomalies),
                "messages_consumed_total": dict(self._messages),
                "scoring_latency_seconds_avg": round(avg_latency, 6),
                "slack_alerts_sent": self._slack_alerts_sent,
                "slack_failures": self._slack_failures,
            }

    # ── Prometheus renderer ──────────────────────────────────────────────────

    def render_prometheus(self) -> str:
        with self._lock:
            lines: list[str] = []

            lines.append("# HELP anomalies_detected_total Number of anomalies emitted by severity.")
            lines.append("# TYPE anomalies_detected_total counter")
            for severity, count in self._anomalies.items():
                lines.append(
                    f'anomalies_detected_total{{severity="{severity}"}} {count}'
                )

            lines.append("# HELP messages_consumed_total Telemetry messages consumed by stream.")
            lines.append("# TYPE messages_consumed_total counter")
            for stream, count in self._messages.items():
                lines.append(
                    f'messages_consumed_total{{stream="{stream}"}} {count}'
                )

            lines.append("# HELP scoring_latency_seconds Histogram of per-window scoring latency.")
            lines.append("# TYPE scoring_latency_seconds histogram")
            for le, count in self._scoring_latency_buckets:
                lines.append(
                    f'scoring_latency_seconds_bucket{{le="{le}"}} {count}'
                )
            lines.append(
                f'scoring_latency_seconds_bucket{{le="+Inf"}} {self._scoring_latency_count}'
            )
            lines.append(
                f"scoring_latency_seconds_sum {self._scoring_latency_seconds_sum:.6f}"
            )
            lines.append(
                f"scoring_latency_seconds_count {self._scoring_latency_count}"
            )

            lines.append("# HELP model_reload_timestamp Unix timestamp of the most recent successful model load.")
            lines.append("# TYPE model_reload_timestamp gauge")
            lines.append(f"model_reload_timestamp {self._model_reload_timestamp:.3f}")

            lines.append("# HELP anomaly_engine_model_loaded Whether a model is currently loaded (1) or not (0).")
            lines.append("# TYPE anomaly_engine_model_loaded gauge")
            lines.append(
                f"anomaly_engine_model_loaded {1 if self._model_loaded else 0}"
            )

            lines.append("# HELP anomaly_engine_slack_alerts_total Slack webhook alerts sent / failed.")
            lines.append("# TYPE anomaly_engine_slack_alerts_total counter")
            lines.append(
                f'anomaly_engine_slack_alerts_total{{result="sent"}} {self._slack_alerts_sent}'
            )
            lines.append(
                f'anomaly_engine_slack_alerts_total{{result="failed"}} {self._slack_failures}'
            )

            return "\n".join(lines) + "\n"
