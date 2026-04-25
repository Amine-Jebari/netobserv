"""HTTP + WebSocket smoke tests via FastAPI's TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from netobserv_topology.graph import TopologyGraph
from netobserv_topology.http_app import build_http_app
from netobserv_topology.metrics import TopologyMetrics
from netobserv_topology.websocket import TopologyBroadcaster


def _seed(g: TopologyGraph) -> None:
    g.upsert_node(
        node_id="subnet-a1", node_type="subnet", metadata={"cidr": "10.10.1.0/24"}
    )
    g.upsert_node(
        node_id="subnet-b1", node_type="subnet", metadata={"cidr": "10.20.1.0/24"}
    )
    g.upsert_node(
        node_id="tgw-1",
        node_type="router",
        metadata={"ips": ["10.10.0.1", "10.20.0.1"]},
    )
    g.upsert_link(source_node="subnet-a1", target_node="tgw-1", link_type="physical")
    g.upsert_link(source_node="tgw-1", target_node="subnet-a1", link_type="physical")
    g.upsert_link(source_node="subnet-b1", target_node="tgw-1", link_type="physical")
    g.upsert_link(source_node="tgw-1", target_node="subnet-b1", link_type="physical")
    g.upsert_node(node_id="local-gateway", node_type="router")
    g.upsert_node(node_id="peer-203.0.113.1", node_type="router")
    g.upsert_link(
        source_node="local-gateway",
        target_node="peer-203.0.113.1",
        link_type="bgp_peer",
        prefix="203.0.113.0/24",
        as_path=[64501, 65000],
    )


@pytest.fixture
def client() -> TestClient:
    graph = TopologyGraph()
    _seed(graph)
    broadcaster = TopologyBroadcaster()
    metrics = TopologyMetrics()
    app = build_http_app(
        graph=graph,
        broadcaster=broadcaster,
        metrics=metrics,
        health_probe=lambda: {"dynamodb": True, "consumer_topology.updates": True},
    )
    return TestClient(app)


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["node_count"] == 5
    assert body["edge_count"] == 5


def test_full_topology_payload(client: TestClient) -> None:
    resp = client.get("/topology")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["nodes"]) == 5
    assert len(payload["edges"]) == 5


def test_node_details_includes_neighbors(client: TestClient) -> None:
    resp = client.get("/topology/node/tgw-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["node_id"] == "tgw-1"
    assert {n["target_node"] for n in body["outbound"]} == {"subnet-a1", "subnet-b1"}


def test_node_details_404(client: TestClient) -> None:
    assert client.get("/topology/node/not-a-node").status_code == 404


def test_path_between_two_ips(client: TestClient) -> None:
    resp = client.get(
        "/topology/path", params={"src": "10.10.1.5", "dst": "10.20.1.42"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["src_node"] == "subnet-a1"
    assert body["dst_node"] == "subnet-b1"
    assert body["path"] == ["subnet-a1", "tgw-1", "subnet-b1"]
    assert body["hops"] == 2


def test_path_404_when_src_unknown(client: TestClient) -> None:
    resp = client.get("/topology/path", params={"src": "8.8.8.8", "dst": "10.20.1.1"})
    assert resp.status_code == 404


def test_affected_by_prefix(client: TestClient) -> None:
    resp = client.get("/topology/affected", params={"prefix": "203.0.113.0/24"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["prefix"] == "203.0.113.0/24"
    assert {n["node_id"] for n in body["nodes"]} == {
        "local-gateway",
        "peer-203.0.113.1",
    }
    assert body["as_path_impact"] == [64501, 65000]


def test_subgraph_respects_depth(client: TestClient) -> None:
    resp = client.get(
        "/topology/subgraph", params={"node_id": "subnet-a1", "depth": 1}
    )
    assert resp.status_code == 200
    body = resp.json()
    node_ids = {n["node_id"] for n in body["nodes"]}
    assert "subnet-a1" in node_ids
    assert "tgw-1" in node_ids
    assert "subnet-b1" not in node_ids


def test_subgraph_depth_two(client: TestClient) -> None:
    resp = client.get(
        "/topology/subgraph", params={"node_id": "subnet-a1", "depth": 2}
    )
    assert resp.status_code == 200
    node_ids = {n["node_id"] for n in resp.json()["nodes"]}
    assert {"subnet-a1", "tgw-1", "subnet-b1"}.issubset(node_ids)


def test_stats_snapshot(client: TestClient) -> None:
    resp = client.get("/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["node_count"] == 5
    assert body["edge_count"] == 5


def test_metrics_prometheus_format(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "topology_graph_nodes 5" in body
    assert "topology_graph_edges 5" in body


def test_websocket_sends_initial_snapshot(client: TestClient) -> None:
    with client.websocket_connect("/ws/topology") as ws:
        message = ws.receive_json()
        assert message["type"] == "snapshot"
        assert len(message["nodes"]) == 5
        assert len(message["edges"]) == 5
