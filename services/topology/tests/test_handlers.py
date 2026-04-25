"""Unit tests for BGP / flow / SNMP event handlers."""

from __future__ import annotations

import pytest

from netobserv_topology.graph import TopologyGraph
from netobserv_topology.handlers import (
    LOCAL_GATEWAY_ID,
    BgpHandler,
    FlowHandler,
    SnmpHandler,
)
from netobserv_topology.metrics import TopologyMetrics

from .conftest import FakeBroadcaster, FakeStore


@pytest.fixture
def bgp_handler(
    graph: TopologyGraph,
    store: FakeStore,
    broadcaster: FakeBroadcaster,
    metrics: TopologyMetrics,
) -> BgpHandler:
    return BgpHandler(
        topic="topology.updates",
        graph=graph,
        store=store,
        broadcaster=broadcaster,
        metrics=metrics,
    )


@pytest.fixture
def flow_handler(
    graph: TopologyGraph,
    store: FakeStore,
    broadcaster: FakeBroadcaster,
    metrics: TopologyMetrics,
) -> FlowHandler:
    return FlowHandler(
        topic="network.flows",
        graph=graph,
        store=store,
        broadcaster=broadcaster,
        metrics=metrics,
        ewma_alpha=0.5,
    )


@pytest.fixture
def snmp_handler(
    graph: TopologyGraph,
    store: FakeStore,
    broadcaster: FakeBroadcaster,
    metrics: TopologyMetrics,
) -> SnmpHandler:
    return SnmpHandler(
        topic="network.snmp",
        graph=graph,
        store=store,
        broadcaster=broadcaster,
        metrics=metrics,
    )


async def test_bgp_announce_creates_peer_and_link(
    bgp_handler: BgpHandler,
    graph: TopologyGraph,
    store: FakeStore,
    broadcaster: FakeBroadcaster,
) -> None:
    await bgp_handler.handle(
        {
            "timestamp": "2026-04-23T12:00:00+00:00",
            "event_type": "ANNOUNCE",
            "peer_ip": "203.0.113.1",
            "prefix": "203.0.113.0/24",
            "as_path": [64501, 65000],
            "next_hop": "203.0.113.1",
        }
    )
    # Local anchor auto-created.
    assert graph.has_node(LOCAL_GATEWAY_ID)
    # Peer node upserted with IP and ASN.
    details = graph.node_with_neighbors("peer-203.0.113.1")
    assert details is not None
    assert details["metadata"]["ips"] == ["203.0.113.1"]
    assert details["metadata"]["asn"] == 64501
    # Link persisted in DynamoDB stand-in.
    link = store.links[(LOCAL_GATEWAY_ID, "peer-203.0.113.1")]
    assert link["link_type"] == "bgp_peer"
    assert link["status"] == "active"
    assert link["prefix"] == "203.0.113.0/24"
    assert link["as_path"] == [64501, 65000]
    # Broadcast fired.
    assert broadcaster.events[-1]["type"] == "link_announced"


async def test_bgp_withdraw_marks_link_inactive(
    bgp_handler: BgpHandler,
    graph: TopologyGraph,
    store: FakeStore,
    broadcaster: FakeBroadcaster,
) -> None:
    base = {
        "timestamp": "2026-04-23T12:00:00+00:00",
        "peer_ip": "203.0.113.1",
        "prefix": "203.0.113.0/24",
        "as_path": [64501, 65000],
        "next_hop": "203.0.113.1",
    }
    await bgp_handler.handle({**base, "event_type": "ANNOUNCE"})
    await bgp_handler.handle({**base, "event_type": "WITHDRAW"})
    link = store.links[(LOCAL_GATEWAY_ID, "peer-203.0.113.1")]
    assert link["status"] == "inactive"
    details = graph.node_with_neighbors("peer-203.0.113.1")
    assert details is not None
    inbound = details["inbound"][0]
    assert inbound["status"] == "inactive"
    # Withdrawal emits a distinct event type.
    assert broadcaster.events[-1]["type"] == "link_withdrawn"


async def test_bgp_invalid_event_type_counts_as_error(
    bgp_handler: BgpHandler,
    metrics: TopologyMetrics,
) -> None:
    await bgp_handler.handle(
        {
            "event_type": "NOPE",
            "peer_ip": "1.2.3.4",
            "prefix": "1.2.3.0/24",
        }
    )
    assert metrics.topics["topology.updates"].errors == 1


async def test_flow_handler_increments_traffic_between_known_nodes(
    flow_handler: FlowHandler,
    graph: TopologyGraph,
    store: FakeStore,
) -> None:
    graph.upsert_node(
        node_id="subnet-a1", node_type="subnet", metadata={"cidr": "10.10.1.0/24"}
    )
    graph.upsert_node(
        node_id="subnet-b1", node_type="subnet", metadata={"cidr": "10.20.1.0/24"}
    )
    graph.upsert_link(
        source_node="subnet-a1",
        target_node="subnet-b1",
        link_type="physical",
        bandwidth=1_000_000_000.0,
    )
    await flow_handler.handle(
        {
            "srcaddr": "10.10.1.5",
            "dstaddr": "10.20.1.99",
            "bytes": 4096,
            "packets": 8,
            "end_ts": 1_700_000_000,
        }
    )
    link = store.links[("subnet-a1", "subnet-b1")]
    assert link["traffic_bytes"] == 4096
    assert link["traffic_packets"] == 8
    assert 0.0 <= float(link["utilization"]) <= 1.0


async def test_flow_handler_skips_unknown_ips(
    flow_handler: FlowHandler,
    graph: TopologyGraph,
    store: FakeStore,
    metrics: TopologyMetrics,
) -> None:
    graph.upsert_node(
        node_id="subnet-a1", node_type="subnet", metadata={"cidr": "10.10.1.0/24"}
    )
    await flow_handler.handle(
        {
            "srcaddr": "10.10.1.5",
            "dstaddr": "8.8.8.8",
            "bytes": 500,
            "packets": 1,
            "end_ts": 1_700_000_000,
        }
    )
    # No known node on the destination side → no graph edge, no DynamoDB write.
    assert store.links == {}
    # Still counts as consumed.
    assert metrics.topics["network.flows"].consumed == 1
    assert metrics.topics["network.flows"].applied == 0


async def test_snmp_handler_creates_node_and_sets_critical(
    snmp_handler: SnmpHandler,
    graph: TopologyGraph,
    store: FakeStore,
    broadcaster: FakeBroadcaster,
) -> None:
    await snmp_handler.handle(
        {
            "timestamp": "2026-04-23T12:00:00+00:00",
            "device_id": "router-9",
            "interface": "eth0",
            "metric": "cpuLoad",
            "value": 97.5,
        }
    )
    details = graph.node_with_neighbors("router-9")
    assert details is not None
    assert details["health_status"] == "CRITICAL"
    assert details["metadata"]["snmp_cpuLoad"] == 97.5
    assert store.nodes["router-9"]["health_status"] == "CRITICAL"
    assert broadcaster.events[-1]["type"] == "node_health"


async def test_snmp_handler_derives_health_bands(
    snmp_handler: SnmpHandler,
    graph: TopologyGraph,
) -> None:
    await snmp_handler.handle(
        {
            "timestamp": "2026-04-23T12:00:00+00:00",
            "device_id": "router-ok",
            "interface": "eth0",
            "metric": "cpuLoad",
            "value": 20.0,
        }
    )
    assert graph.node_with_neighbors("router-ok")["health_status"] == "OK"  # type: ignore[index]

    await snmp_handler.handle(
        {
            "timestamp": "2026-04-23T12:00:00+00:00",
            "device_id": "router-warn",
            "interface": "eth0",
            "metric": "ifUtilizationPct",
            "value": 82.0,
        }
    )
    assert graph.node_with_neighbors("router-warn")["health_status"] == "WARN"  # type: ignore[index]


async def test_snmp_handler_rejects_non_numeric(
    snmp_handler: SnmpHandler,
    metrics: TopologyMetrics,
) -> None:
    await snmp_handler.handle(
        {
            "timestamp": "2026-04-23T12:00:00+00:00",
            "device_id": "router-1",
            "interface": "eth0",
            "metric": "cpuLoad",
            "value": "high",
        }
    )
    assert metrics.topics["network.snmp"].errors == 1
    assert metrics.topics["network.snmp"].applied == 0
