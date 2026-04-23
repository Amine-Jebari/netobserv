"""Flow handler: fan-out to Influx + OpenSearch + archive with isolation on failure."""

from __future__ import annotations

import asyncio

from netobserv_collector.handlers.flows import FlowsHandler


def _flow() -> dict[str, object]:
    return {
        "version": 2,
        "account_id": "123456789012",
        "interface_id": "eni-abc",
        "srcaddr": "10.0.0.1",
        "dstaddr": "8.8.8.8",
        "srcport": 54321,
        "dstport": 443,
        "protocol": 6,
        "packets": 10,
        "bytes": 1234,
        "start_ts": 1_710_000_000,
        "end_ts": 1_710_000_005,
        "action": "ACCEPT",
        "log_status": "OK",
    }


def _build(fake_influx, fake_opensearch, fake_archive, metrics) -> FlowsHandler:
    return FlowsHandler(
        topic="network.flows",
        influx=fake_influx,
        opensearch=fake_opensearch,
        archive=fake_archive,
        metrics=metrics,
        influx_batch_size=2,
        influx_flush_interval=0.05,
        opensearch_bulk_size=2,
        opensearch_flush_interval=0.05,
        archive_flush_interval=0.05,
    )


async def test_flows_fan_out_to_all_three(fake_influx, fake_opensearch, fake_archive, metrics):
    handler = _build(fake_influx, fake_opensearch, fake_archive, metrics)
    handler.start()
    try:
        await handler.handle(_flow())
        await handler.handle(_flow())
        # Size-triggered flushes already happened; give the time-based archive a tick.
        await asyncio.sleep(0.15)
    finally:
        await handler.stop()

    influx_points = [p for b in fake_influx.batches for p in b]
    os_docs = [d for b in fake_opensearch.batches for d in b]
    archived = [r for b in fake_archive.puts for r in b]

    assert len(influx_points) == 2
    assert all(p.measurement == "flows" for p in influx_points)
    assert influx_points[0].tags == {"srcaddr": "10.0.0.1", "dstaddr": "8.8.8.8", "protocol": "6"}
    assert influx_points[0].fields == {"packets": 10, "bytes": 1234}

    assert len(os_docs) == 2
    assert all(d.index_prefix == "flows" for d in os_docs)

    assert len(archived) == 2

    snapshot = metrics.snapshot_stats()
    assert snapshot["topics"]["network.flows"]["consumed"] == 2
    assert snapshot["destinations"]["influxdb"]["written"] == 2
    assert snapshot["destinations"]["opensearch"]["written"] == 2
    assert snapshot["destinations"]["s3"]["written"] == 2


async def test_flows_opensearch_failure_does_not_block_influx(
    fake_influx, fake_archive, metrics
):
    from tests.conftest import FakeOpenSearch

    broken_os = FakeOpenSearch(fail=True)
    handler = _build(fake_influx, broken_os, fake_archive, metrics)
    handler.start()
    try:
        await handler.handle(_flow())
        await handler.handle(_flow())
        await asyncio.sleep(0.15)
    finally:
        await handler.stop()

    # Influx batch still flushed; archive still flushed; OpenSearch errors recorded.
    assert sum(len(b) for b in fake_influx.batches) == 2
    assert sum(len(b) for b in fake_archive.puts) == 2
    assert metrics.snapshot_stats()["destinations"]["opensearch"]["errors"] >= 1


async def test_flows_bad_record_skips_influx(fake_influx, fake_opensearch, fake_archive, metrics):
    handler = _build(fake_influx, fake_opensearch, fake_archive, metrics)
    handler.start()
    try:
        await handler.handle({"srcaddr": "10.0.0.1"})  # missing required fields
        await asyncio.sleep(0.15)
    finally:
        await handler.stop()

    assert sum(len(b) for b in fake_influx.batches) == 0
    # OpenSearch and archive accept the raw record regardless.
    assert sum(len(b) for b in fake_opensearch.batches) == 1
    assert sum(len(b) for b in fake_archive.puts) == 1
    assert metrics.snapshot_stats()["destinations"]["influxdb"]["errors"] >= 1
