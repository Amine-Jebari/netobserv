"""In-process counters exposed at /metrics and /stats."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class TopicStats:
    consumed: int = 0
    applied: int = 0
    errors: int = 0
    last_message_ts: float = 0.0
    lag_seconds: float = 0.0


@dataclass
class TopologyMetrics:
    started_at: float = field(default_factory=time.time)
    topics: dict[str, TopicStats] = field(default_factory=dict)
    node_count: int = 0
    edge_count: int = 0
    ws_clients: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _topic(self, topic: str) -> TopicStats:
        stats = self.topics.get(topic)
        if stats is None:
            stats = TopicStats()
            self.topics[topic] = stats
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

    def record_applied(self, topic: str, count: int = 1) -> None:
        with self._lock:
            self._topic(topic).applied += count

    def record_error(self, topic: str, count: int = 1) -> None:
        with self._lock:
            self._topic(topic).errors += count

    def set_graph_size(self, nodes: int, edges: int) -> None:
        with self._lock:
            self.node_count = nodes
            self.edge_count = edges

    def set_ws_clients(self, count: int) -> None:
        with self._lock:
            self.ws_clients = count

    def snapshot_stats(self) -> dict[str, object]:
        with self._lock:
            uptime = time.time() - self.started_at
            topics: dict[str, dict[str, float]] = {}
            for name, s in self.topics.items():
                rate = (s.consumed / uptime) if uptime > 0 else 0.0
                topics[name] = {
                    "consumed": s.consumed,
                    "applied": s.applied,
                    "errors": s.errors,
                    "lag_seconds": round(s.lag_seconds, 3),
                    "throughput_per_second": round(rate, 2),
                }
            return {
                "uptime_seconds": round(uptime, 2),
                "node_count": self.node_count,
                "edge_count": self.edge_count,
                "ws_clients": self.ws_clients,
                "topics": topics,
            }

    def render_prometheus(self) -> str:
        with self._lock:
            lines: list[str] = []

            lines.append("# HELP topology_messages_consumed_total Messages consumed from Kafka.")
            lines.append("# TYPE topology_messages_consumed_total counter")
            for topic, s in self.topics.items():
                lines.append(
                    f'topology_messages_consumed_total{{topic="{topic}"}} {s.consumed}'
                )

            lines.append("# HELP topology_messages_applied_total Messages successfully applied to the graph.")
            lines.append("# TYPE topology_messages_applied_total counter")
            for topic, s in self.topics.items():
                lines.append(
                    f'topology_messages_applied_total{{topic="{topic}"}} {s.applied}'
                )

            lines.append("# HELP topology_errors_total Topology apply errors by topic.")
            lines.append("# TYPE topology_errors_total counter")
            for topic, s in self.topics.items():
                lines.append(
                    f'topology_errors_total{{topic="{topic}"}} {s.errors}'
                )

            lines.append("# HELP topology_lag_seconds Seconds between message timestamp and consumption.")
            lines.append("# TYPE topology_lag_seconds gauge")
            for topic, s in self.topics.items():
                lines.append(
                    f'topology_lag_seconds{{topic="{topic}"}} {s.lag_seconds:.3f}'
                )

            lines.append("# HELP topology_graph_nodes Current node count in the in-memory graph.")
            lines.append("# TYPE topology_graph_nodes gauge")
            lines.append(f"topology_graph_nodes {self.node_count}")

            lines.append("# HELP topology_graph_edges Current edge count in the in-memory graph.")
            lines.append("# TYPE topology_graph_edges gauge")
            lines.append(f"topology_graph_edges {self.edge_count}")

            lines.append("# HELP topology_ws_clients Active WebSocket subscribers.")
            lines.append("# TYPE topology_ws_clients gauge")
            lines.append(f"topology_ws_clients {self.ws_clients}")

            lines.append("# HELP topology_uptime_seconds Process uptime in seconds.")
            lines.append("# TYPE topology_uptime_seconds gauge")
            lines.append(f"topology_uptime_seconds {time.time() - self.started_at:.2f}")

            return "\n".join(lines) + "\n"
