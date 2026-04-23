"""SNMP handler: writes InfluxDB points with device/interface/metric tags."""

from __future__ import annotations

import asyncio

from netobserv_collector.handlers.snmp import SnmpHandler


def _sample() -> dict[str, object]:
    return {
        "timestamp": "2026-04-21T12:00:00+00:00",
        "device_id": "rtr-01",
        "interface": "eth0",
        "metric": "ifUtilizationPct",
        "value": 42.5,
    }


async def test_snmp_writes_to_influx(fake_influx, metrics):
    handler = SnmpHandler(
        topic="network.snmp",
        influx=fake_influx,
        metrics=metrics,
        influx_batch_size=2,
        influx_flush_interval=0.05,
    )
    handler.start()
    try:
        await handler.handle(_sample())
        await handler.handle(_sample())
        await asyncio.sleep(0.15)
    finally:
        await handler.stop()

    points = [p for b in fake_influx.batches for p in b]
    assert len(points) == 2
    p = points[0]
    assert p.measurement == "snmp"
    assert p.tags == {"device_id": "rtr-01", "interface": "eth0", "metric": "ifUtilizationPct"}
    assert p.fields == {"value": 42.5}
    assert p.timestamp_ns is not None

    snap = metrics.snapshot_stats()
    assert snap["topics"]["network.snmp"]["consumed"] == 2
    assert snap["destinations"]["influxdb"]["written"] == 2


async def test_snmp_bad_record_records_error(fake_influx, metrics):
    handler = SnmpHandler(
        topic="network.snmp",
        influx=fake_influx,
        metrics=metrics,
        influx_batch_size=1,
        influx_flush_interval=0.05,
    )
    handler.start()
    try:
        await handler.handle({"device_id": "rtr-01"})
        await asyncio.sleep(0.1)
    finally:
        await handler.stop()

    assert sum(len(b) for b in fake_influx.batches) == 0
    assert metrics.snapshot_stats()["destinations"]["influxdb"]["errors"] >= 1
