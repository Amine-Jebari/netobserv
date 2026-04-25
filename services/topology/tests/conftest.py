"""Shared test fixtures and fakes for the topology service."""

from __future__ import annotations

from typing import Any

import pytest

from netobserv_topology.graph import TopologyGraph
from netobserv_topology.metrics import TopologyMetrics


class FakeStore:
    """In-memory stand-in for ``TopologyStore`` with the same write surface."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.links: dict[tuple[str, str], dict[str, Any]] = {}
        self.connected = True

    async def put_node(
        self,
        *,
        node_id: str,
        node_type: str,
        metadata: dict[str, Any] | None = None,
        last_seen: str | None = None,
        health_status: str | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "node_id": node_id,
            "node_type": node_type,
            "metadata": metadata or {},
        }
        if last_seen is not None:
            item["last_seen"] = last_seen
        if health_status is not None:
            item["health_status"] = health_status
        self.nodes[node_id] = item
        return item

    async def put_link(
        self,
        *,
        source_node: str,
        target_node: str,
        link_type: str,
        bandwidth: float | None = None,
        utilization: float | None = None,
        last_bgp_update: str | None = None,
        status: str = "active",
        as_path: list[int] | None = None,
        prefix: str | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "source_node": source_node,
            "target_node": target_node,
            "link_type": link_type,
            "status": status,
        }
        if bandwidth is not None:
            item["bandwidth"] = bandwidth
        if utilization is not None:
            item["utilization"] = utilization
        if last_bgp_update is not None:
            item["last_bgp_update"] = last_bgp_update
        if as_path is not None:
            item["as_path"] = list(as_path)
        if prefix is not None:
            item["prefix"] = prefix
        self.links[(source_node, target_node)] = item
        return item

    async def mark_link_status(
        self,
        *,
        source_node: str,
        target_node: str,
        status: str,
        last_bgp_update: str | None = None,
    ) -> None:
        key = (source_node, target_node)
        item = self.links.get(key)
        if item is None:
            item = {
                "source_node": source_node,
                "target_node": target_node,
                "link_type": "bgp_peer",
            }
            self.links[key] = item
        item["status"] = status
        if last_bgp_update is not None:
            item["last_bgp_update"] = last_bgp_update

    async def increment_traffic(
        self,
        *,
        source_node: str,
        target_node: str,
        bytes_delta: int,
        packets_delta: int,
        utilization: float | None = None,
    ) -> None:
        key = (source_node, target_node)
        item = self.links.get(key)
        if item is None:
            item = {
                "source_node": source_node,
                "target_node": target_node,
                "link_type": "traffic",
                "status": "active",
                "traffic_bytes": 0,
                "traffic_packets": 0,
            }
            self.links[key] = item
        item["traffic_bytes"] = int(item.get("traffic_bytes", 0)) + int(bytes_delta)
        item["traffic_packets"] = (
            int(item.get("traffic_packets", 0)) + int(packets_delta)
        )
        if utilization is not None:
            item["utilization"] = utilization

    async def update_node_health(
        self,
        *,
        node_id: str,
        health_status: str,
        last_seen: str,
        metric_updates: dict[str, float | int] | None = None,
    ) -> None:
        item = self.nodes.setdefault(
            node_id,
            {"node_id": node_id, "node_type": "router", "metadata": {}},
        )
        item["health_status"] = health_status
        item["last_seen"] = last_seen
        if metric_updates:
            item.setdefault("metadata", {}).update(metric_updates)


class FakeBroadcaster:
    """Captures broadcast events so tests can assert what was pushed."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.client_count = 0

    async def broadcast(self, event: dict[str, Any]) -> None:
        self.events.append(event)


@pytest.fixture
def graph() -> TopologyGraph:
    return TopologyGraph()


@pytest.fixture
def metrics() -> TopologyMetrics:
    return TopologyMetrics()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def broadcaster() -> FakeBroadcaster:
    return FakeBroadcaster()
