"""Internal in-process metrics for the collector.

Exposed at ``GET /metrics`` in Prometheus text-exposition format and at
``GET /stats`` as a JSON summary. We deliberately avoid the ``prometheus_client``
dependency — a few counters and a render helper are enough.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class TopicStats:
    """Per-topic counters and the freshest observed producer-to-consume lag."""

    consumed: int = 0
    written: int = 0
    errors: int = 0
    lag_seconds: float = 0.0
    last_message_ts: float = 0.0


@dataclass
class DestinationStats:
    """Per-destination write counters (InfluxDB, OpenSearch, S3, Kafka)."""

    written: int = 0
    errors: int = 0


@dataclass
class CollectorMetrics:
    """Thread/async-safe metrics registry for the collector."""

    started_at: float = field(default_factory=time.time)
    topics: dict[str, TopicStats] = field(default_factory=dict)
    destinations: dict[str, DestinationStats] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ── recording ────────────────────────────────────────────────────────────

    def _topic(self, topic: str) -> TopicStats:
        stats = self.topics.get(topic)
        if stats is None:
            stats = TopicStats()
            self.topics[topic] = stats
        return stats

    def _dest(self, destination: str) -> DestinationStats:
        stats = self.destinations.get(destination)
        if stats is None:
            stats = DestinationStats()
            self.destinations[destination] = stats
        return stats

    def record_consumed(self, topic: str, message_ts: float | None = None) -> None:
        with self._lock:
            stats = self._topic(topic)
            stats.consumed += 1
            if message_ts is not None:
                stats.last_message_ts = message_ts
                lag = time.time() - message_ts
                if lag >= 0:
                    stats.lag_seconds = lag

    def record_written(self, topic: str, destination: str, count: int = 1) -> None:
        with self._lock:
            self._topic(topic).written += count
            self._dest(destination).written += count

    def record_error(self, topic: str, destination: str, count: int = 1) -> None:
        with self._lock:
            self._topic(topic).errors += count
            self._dest(destination).errors += count

    # ── rendering ────────────────────────────────────────────────────────────

    def snapshot_stats(self) -> dict[str, object]:
        with self._lock:
            uptime = time.time() - self.started_at
            topics: dict[str, dict[str, float]] = {}
            for name, s in self.topics.items():
                rate = (s.consumed / uptime) if uptime > 0 else 0.0
                topics[name] = {
                    "consumed": s.consumed,
                    "written": s.written,
                    "errors": s.errors,
                    "lag_seconds": round(s.lag_seconds, 3),
                    "throughput_per_second": round(rate, 2),
                }
            destinations = {
                name: {"written": d.written, "errors": d.errors}
                for name, d in self.destinations.items()
            }
            return {
                "uptime_seconds": round(uptime, 2),
                "topics": topics,
                "destinations": destinations,
            }

    def render_prometheus(self) -> str:
        """Render Prometheus 0.0.4 text exposition format."""

        with self._lock:
            lines: list[str] = []

            lines.append("# HELP collector_messages_consumed_total Messages consumed from Kafka.")
            lines.append("# TYPE collector_messages_consumed_total counter")
            for topic, s in self.topics.items():
                lines.append(
                    f'collector_messages_consumed_total{{topic="{topic}"}} {s.consumed}'
                )

            lines.append("# HELP collector_messages_written_total Messages successfully written downstream.")
            lines.append("# TYPE collector_messages_written_total counter")
            for topic, s in self.topics.items():
                lines.append(
                    f'collector_messages_written_total{{topic="{topic}"}} {s.written}'
                )
            for dest, d in self.destinations.items():
                lines.append(
                    f'collector_messages_written_total{{destination="{dest}"}} {d.written}'
                )

            lines.append("# HELP collector_errors_total Total write or parse errors by topic/destination.")
            lines.append("# TYPE collector_errors_total counter")
            for topic, s in self.topics.items():
                lines.append(
                    f'collector_errors_total{{topic="{topic}"}} {s.errors}'
                )
            for dest, d in self.destinations.items():
                lines.append(
                    f'collector_errors_total{{destination="{dest}"}} {d.errors}'
                )

            lines.append("# HELP collector_lag_seconds Seconds between message timestamp and consumption.")
            lines.append("# TYPE collector_lag_seconds gauge")
            for topic, s in self.topics.items():
                lines.append(
                    f'collector_lag_seconds{{topic="{topic}"}} {s.lag_seconds:.3f}'
                )

            lines.append("# HELP collector_uptime_seconds Process uptime in seconds.")
            lines.append("# TYPE collector_uptime_seconds gauge")
            lines.append(f"collector_uptime_seconds {time.time() - self.started_at:.2f}")

            return "\n".join(lines) + "\n"
