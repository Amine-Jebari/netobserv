"""Unit tests for the in-memory NetworkX graph wrapper."""

from __future__ import annotations

from netobserv_topology.graph import TopologyGraph


def _build_seed(g: TopologyGraph) -> None:
    """Minimal seed: two VPCs, one subnet each, one transit gateway linking them."""

    g.upsert_node(
        node_id="subnet-a1",
        node_type="subnet",
        metadata={"cidr": "10.10.1.0/24"},
    )
    g.upsert_node(
        node_id="subnet-b1",
        node_type="subnet",
        metadata={"cidr": "10.20.1.0/24"},
    )
    g.upsert_node(
        node_id="vpc-a",
        node_type="vpc",
        metadata={"cidr": "10.10.0.0/16"},
    )
    g.upsert_node(
        node_id="vpc-b",
        node_type="vpc",
        metadata={"cidr": "10.20.0.0/16"},
    )
    g.upsert_node(
        node_id="tgw-1",
        node_type="router",
        metadata={"ips": ["10.10.0.1", "10.20.0.1"]},
    )
    for a, b in [
        ("subnet-a1", "vpc-a"),
        ("vpc-a", "subnet-a1"),
        ("subnet-b1", "vpc-b"),
        ("vpc-b", "subnet-b1"),
        ("vpc-a", "tgw-1"),
        ("tgw-1", "vpc-a"),
        ("vpc-b", "tgw-1"),
        ("tgw-1", "vpc-b"),
    ]:
        g.upsert_link(
            source_node=a,
            target_node=b,
            link_type="physical",
            bandwidth=10_000_000_000.0,
        )


def test_upsert_node_merges_metadata(graph: TopologyGraph) -> None:
    graph.upsert_node(
        node_id="router-1",
        node_type="router",
        metadata={"ips": ["10.0.0.1"]},
    )
    graph.upsert_node(
        node_id="router-1",
        node_type="router",
        metadata={"asn": 65001},
    )
    data = graph.node_with_neighbors("router-1")
    assert data is not None
    assert data["metadata"] == {"ips": ["10.0.0.1"], "asn": 65001}


def test_upsert_link_and_mark_inactive(graph: TopologyGraph) -> None:
    graph.upsert_node(node_id="a", node_type="router")
    graph.upsert_node(node_id="b", node_type="router")
    graph.upsert_link(
        source_node="a", target_node="b", link_type="bgp_peer", prefix="10.0.0.0/24"
    )
    assert graph.mark_link_status(
        source_node="a", target_node="b", status="inactive"
    )
    data = graph.node_with_neighbors("a")
    assert data is not None
    outbound = data["outbound"][0]
    assert outbound["status"] == "inactive"


def test_increment_traffic_creates_edge_and_updates_utilization(
    graph: TopologyGraph,
) -> None:
    graph.upsert_node(node_id="a", node_type="router")
    graph.upsert_node(node_id="b", node_type="router")
    graph.upsert_link(
        source_node="a",
        target_node="b",
        link_type="physical",
        bandwidth=1_000_000.0,  # 1 Mbps
    )
    # 125_000 bytes = 1_000_000 bits → exactly saturates 1 Mbps.
    edge = graph.increment_traffic(
        source_node="a",
        target_node="b",
        bytes_delta=125_000,
        packets_delta=100,
        ewma_alpha=1.0,
    )
    assert edge["traffic_bytes"] == 125_000
    assert edge["traffic_packets"] == 100
    # With alpha=1.0 utilisation takes on the instantaneous value (clipped to 1).
    assert edge["utilization"] == 1.0


def test_find_node_by_ip_exact_ip_match(graph: TopologyGraph) -> None:
    _build_seed(graph)
    assert graph.find_node_by_ip("10.10.0.1") == "tgw-1"


def test_find_node_by_ip_subnet_cidr(graph: TopologyGraph) -> None:
    _build_seed(graph)
    assert graph.find_node_by_ip("10.10.1.5") == "subnet-a1"
    assert graph.find_node_by_ip("10.20.1.200") == "subnet-b1"


def test_find_node_by_ip_no_match(graph: TopologyGraph) -> None:
    _build_seed(graph)
    assert graph.find_node_by_ip("8.8.8.8") is None
    assert graph.find_node_by_ip("not-an-ip") is None


def test_shortest_path_hops_through_tgw(graph: TopologyGraph) -> None:
    _build_seed(graph)
    path = graph.shortest_path("subnet-a1", "subnet-b1")
    assert path == ["subnet-a1", "vpc-a", "tgw-1", "vpc-b", "subnet-b1"]


def test_shortest_path_returns_none_on_no_path(graph: TopologyGraph) -> None:
    graph.upsert_node(node_id="isolated", node_type="router")
    _build_seed(graph)
    assert graph.shortest_path("isolated", "subnet-a1") is None


def test_affected_by_prefix_exact(graph: TopologyGraph) -> None:
    graph.upsert_node(node_id="local-gateway", node_type="router")
    graph.upsert_node(node_id="peer-1", node_type="router")
    graph.upsert_link(
        source_node="local-gateway",
        target_node="peer-1",
        link_type="bgp_peer",
        prefix="203.0.113.0/24",
        as_path=[64501, 65000],
    )
    result = graph.affected_by_prefix("203.0.113.0/24")
    assert result["prefix"] == "203.0.113.0/24"
    assert len(result["links"]) == 1
    assert {n["node_id"] for n in result["nodes"]} == {"local-gateway", "peer-1"}
    assert result["as_path_impact"] == [64501, 65000]


def test_affected_by_prefix_overlap(graph: TopologyGraph) -> None:
    """Queries for a supernet should also match the more-specific edge prefix."""

    graph.upsert_node(node_id="local-gateway", node_type="router")
    graph.upsert_node(node_id="peer-1", node_type="router")
    graph.upsert_link(
        source_node="local-gateway",
        target_node="peer-1",
        link_type="bgp_peer",
        prefix="10.1.2.0/24",
    )
    result = graph.affected_by_prefix("10.0.0.0/8")
    assert len(result["links"]) == 1


def test_subgraph_bfs_limits_depth(graph: TopologyGraph) -> None:
    _build_seed(graph)
    sub = graph.subgraph("subnet-a1", depth=1)
    assert sub is not None
    node_ids = {n["node_id"] for n in sub["nodes"]}
    # Depth 1 from subnet-a1 should reach vpc-a (undirected neighbour) but not tgw-1.
    assert "subnet-a1" in node_ids
    assert "vpc-a" in node_ids
    assert "tgw-1" not in node_ids

    sub2 = graph.subgraph("subnet-a1", depth=2)
    assert sub2 is not None
    assert "tgw-1" in {n["node_id"] for n in sub2["nodes"]}


def test_subgraph_unknown_node_returns_none(graph: TopologyGraph) -> None:
    assert graph.subgraph("nope", depth=2) is None


def test_to_json_round_trip(graph: TopologyGraph) -> None:
    _build_seed(graph)
    snapshot = graph.to_json()
    assert "nodes" in snapshot and "edges" in snapshot
    assert len(snapshot["nodes"]) == 5
    assert len(snapshot["edges"]) == 8
