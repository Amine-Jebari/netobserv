"""Prometheus metrics for the agent service.

Exposed via ``GET /metrics`` and required by the project mandate to track
LLM cost. ``claude_tokens_used_total`` keeps the legacy metric name (the
project's monitoring config already references it); we emit the same metric
regardless of which LLM provider is wired in.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class AgentMetrics:
    """Process-local registry — one instance per agent service."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        # Cost / token accounting. `type` ∈ {"input", "output", "total"}.
        # The metric name intentionally keeps the ``claude_`` prefix specified
        # in the phase prompt so existing dashboards keep working when the
        # underlying LLM swaps providers.
        self.tokens_used = Counter(
            "claude_tokens_used_total",
            "LLM tokens consumed by the agent (input/output/total).",
            labelnames=("type", "model"),
            registry=self.registry,
        )

        self.investigations_started = Counter(
            "agent_investigations_started_total",
            "Investigations the agent has started.",
            labelnames=("severity", "anomaly_type"),
            registry=self.registry,
        )
        self.investigations_completed = Counter(
            "agent_investigations_completed_total",
            "Investigations that finished, by outcome.",
            labelnames=("outcome",),  # success | timeout | error
            registry=self.registry,
        )
        self.tool_calls = Counter(
            "agent_tool_calls_total",
            "Tool invocations made during investigations.",
            labelnames=("tool", "outcome"),  # outcome ∈ {ok, error}
            registry=self.registry,
        )
        self.investigation_seconds = Histogram(
            "agent_investigation_duration_seconds",
            "Wall-clock duration of an investigation.",
            buckets=(1, 2, 5, 10, 20, 30, 60, 120, 300),
            registry=self.registry,
        )

        self.anomalies_consumed = Counter(
            "agent_anomalies_consumed_total",
            "Anomaly events read from Kafka, by routing decision.",
            labelnames=("severity", "decision"),  # decision ∈ {investigate, skip_info, skip_unknown}
            registry=self.registry,
        )

        self.kafka_consumer_connected = Gauge(
            "agent_kafka_consumer_connected",
            "1 = consumer connected to broker, 0 = disconnected.",
            registry=self.registry,
        )

    def render_prometheus(self) -> bytes:
        return generate_latest(self.registry)

    @property
    def content_type(self) -> str:
        return CONTENT_TYPE_LATEST
