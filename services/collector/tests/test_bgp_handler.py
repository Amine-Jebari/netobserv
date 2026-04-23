"""BGP handler: indexes to OpenSearch + publishes to topology.updates."""

from __future__ import annotations

import asyncio

from netobserv_collector.handlers.bgp import BgpHandler


def _event() -> dict[str, object]:
    return {
        "timestamp": "2026-04-21T12:00:00+00:00",
        "event_type": "ANNOUNCE",
        "peer_ip": "192.0.2.1",
        "prefix": "10.10.0.0/16",
        "as_path": [64500, 64501],
        "next_hop": "192.0.2.1",
        "local_pref": 100,
        "med": 0,
    }


async def test_bgp_fans_to_opensearch_and_topology_topic(
    fake_opensearch, fake_kafka_producer, metrics
):
    handler = BgpHandler(
        topic="network.bgp",
        topology_topic="topology.updates",
        opensearch=fake_opensearch,
        kafka_producer=fake_kafka_producer,
        metrics=metrics,
        opensearch_bulk_size=2,
        opensearch_flush_interval=0.05,
    )
    handler.start()
    try:
        await handler.handle(_event())
        await handler.handle(_event())
        await asyncio.sleep(0.15)
    finally:
        await handler.stop()

    os_docs = [d for b in fake_opensearch.batches for d in b]
    assert len(os_docs) == 2
    assert all(d.index_prefix == "bgp-events" for d in os_docs)

    assert len(fake_kafka_producer.sent) == 2
    topic, payload, key = fake_kafka_producer.sent[0]
    assert topic == "topology.updates"
    assert payload["prefix"] == "10.10.0.0/16"
    assert key == "10.10.0.0/16"

    snap = metrics.snapshot_stats()
    assert snap["topics"]["network.bgp"]["consumed"] == 2
    assert snap["destinations"]["opensearch"]["written"] == 2
    assert snap["destinations"]["kafka-topology"]["written"] == 2


async def test_bgp_kafka_failure_records_error(fake_opensearch, metrics):
    from tests.conftest import FakeKafkaProducer

    broken = FakeKafkaProducer(fail=True)
    handler = BgpHandler(
        topic="network.bgp",
        topology_topic="topology.updates",
        opensearch=fake_opensearch,
        kafka_producer=broken,
        metrics=metrics,
        opensearch_bulk_size=1,
        opensearch_flush_interval=0.05,
    )
    handler.start()
    try:
        await handler.handle(_event())
        await asyncio.sleep(0.1)
    finally:
        await handler.stop()

    # OpenSearch still got the event; the topology producer error is accounted for.
    assert sum(len(b) for b in fake_opensearch.batches) == 1
    assert metrics.snapshot_stats()["destinations"]["kafka-topology"]["errors"] == 1
