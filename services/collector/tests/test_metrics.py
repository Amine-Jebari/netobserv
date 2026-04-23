"""CollectorMetrics — counters and Prometheus rendering."""

from __future__ import annotations

import time

from netobserv_collector.metrics import CollectorMetrics


def test_counters_and_prometheus_render():
    m = CollectorMetrics()
    m.record_consumed("network.flows", message_ts=time.time() - 1.0)
    m.record_written("network.flows", "influxdb", 1)
    m.record_error("network.flows", "opensearch", 1)

    snap = m.snapshot_stats()
    assert snap["topics"]["network.flows"]["consumed"] == 1
    assert snap["topics"]["network.flows"]["errors"] == 1
    assert snap["destinations"]["influxdb"]["written"] == 1
    assert snap["destinations"]["opensearch"]["errors"] == 1
    assert snap["topics"]["network.flows"]["lag_seconds"] >= 0.5

    prom = m.render_prometheus()
    assert "collector_messages_consumed_total" in prom
    assert "collector_messages_written_total" in prom
    assert "collector_errors_total" in prom
    assert "collector_lag_seconds" in prom
    assert 'topic="network.flows"' in prom
