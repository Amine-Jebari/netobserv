"""In-memory NetworkX graph kept in sync with the DynamoDB tables.

The graph is the hot read path for path/affected/subgraph queries. Every
mutation is written to DynamoDB **and** applied to the in-memory graph. On
startup the graph is rehydrated from a full DynamoDB scan so crashes do not
lose state.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Iterable

import networkx as nx

from .dynamodb_store import TopologyStore
from .logging_setup import get_logger

log = get_logger(__name__)


class TopologyGraph:
    """Thread/async-unsafe wrapper around a NetworkX DiGraph.

    Safe to use from a single asyncio event loop: every mutation method is
    synchronous and does not yield control, so handlers on the same loop see
    a consistent snapshot between awaits.
    """

    def __init__(self) -> None:
        self._g: nx.DiGraph = nx.DiGraph()

    # ── loading ──────────────────────────────────────────────────────────────

    async def rehydrate(self, store: TopologyStore) -> None:
        """Replace the in-memory graph with a fresh scan of DynamoDB."""

        nodes = await store.scan_nodes()
        links = await store.scan_links()
        new_g: nx.DiGraph = nx.DiGraph()
        for node in nodes:
            node_id = node.get("node_id")
            if not node_id:
                continue
            new_g.add_node(node_id, **{k: v for k, v in node.items() if k != "node_id"})
        for link in links:
            src = link.get("source_node")
            tgt = link.get("target_node")
            if not src or not tgt:
                continue
            new_g.add_edge(
                src,
                tgt,
                **{
                    k: v
                    for k, v in link.items()
                    if k not in {"source_node", "target_node"}
                },
            )
        self._g = new_g
        log.info(
            "topology.rehydrated",
            nodes=self._g.number_of_nodes(),
            edges=self._g.number_of_edges(),
        )

    # ── mutations ────────────────────────────────────────────────────────────

    def upsert_node(
        self,
        *,
        node_id: str,
        node_type: str,
        metadata: dict[str, Any] | None = None,
        last_seen: str | None = None,
        health_status: str | None = None,
    ) -> dict[str, Any]:
        attrs: dict[str, Any] = self._g.nodes[node_id] if node_id in self._g else {}
        attrs = dict(attrs)
        attrs["node_type"] = node_type
        if metadata is not None:
            existing_meta = attrs.get("metadata") or {}
            merged = {**existing_meta, **metadata}
            attrs["metadata"] = merged
        if last_seen is not None:
            attrs["last_seen"] = last_seen
        if health_status is not None:
            attrs["health_status"] = health_status
        self._g.add_node(node_id, **attrs)
        return {"node_id": node_id, **attrs}

    def upsert_link(
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
        attrs: dict[str, Any] = (
            dict(self._g.edges[source_node, target_node])
            if self._g.has_edge(source_node, target_node)
            else {}
        )
        attrs["link_type"] = link_type
        attrs["status"] = status
        if bandwidth is not None:
            attrs["bandwidth"] = bandwidth
        if utilization is not None:
            attrs["utilization"] = utilization
        if last_bgp_update is not None:
            attrs["last_bgp_update"] = last_bgp_update
        if as_path is not None:
            attrs["as_path"] = as_path
        if prefix is not None:
            attrs["prefix"] = prefix
        self._g.add_edge(source_node, target_node, **attrs)
        return {
            "source_node": source_node,
            "target_node": target_node,
            **attrs,
        }

    def mark_link_status(
        self,
        *,
        source_node: str,
        target_node: str,
        status: str,
        last_bgp_update: str | None = None,
    ) -> bool:
        if not self._g.has_edge(source_node, target_node):
            return False
        data = self._g.edges[source_node, target_node]
        data["status"] = status
        if last_bgp_update is not None:
            data["last_bgp_update"] = last_bgp_update
        return True

    def increment_traffic(
        self,
        *,
        source_node: str,
        target_node: str,
        bytes_delta: int,
        packets_delta: int,
        ewma_alpha: float,
    ) -> dict[str, Any]:
        """Add flow traffic to an edge; create a traffic edge if one is new."""

        if not self._g.has_edge(source_node, target_node):
            self._g.add_edge(
                source_node,
                target_node,
                link_type="traffic",
                status="active",
                traffic_bytes=0,
                traffic_packets=0,
            )
        edge = self._g.edges[source_node, target_node]
        edge["traffic_bytes"] = int(edge.get("traffic_bytes", 0)) + int(bytes_delta)
        edge["traffic_packets"] = (
            int(edge.get("traffic_packets", 0)) + int(packets_delta)
        )
        # Rolling utilisation estimate: EWMA of bytes/s relative to bandwidth.
        if "bandwidth" in edge and edge["bandwidth"]:
            instantaneous = min(1.0, float(bytes_delta * 8) / float(edge["bandwidth"]))
            prev = float(edge.get("utilization", instantaneous))
            edge["utilization"] = prev + ewma_alpha * (instantaneous - prev)
        return dict(edge)

    def update_node_health(
        self,
        *,
        node_id: str,
        health_status: str,
        last_seen: str,
        metric_updates: dict[str, float | int] | None = None,
    ) -> bool:
        if node_id not in self._g:
            return False
        attrs = self._g.nodes[node_id]
        attrs["health_status"] = health_status
        attrs["last_seen"] = last_seen
        if metric_updates:
            meta = attrs.setdefault("metadata", {})
            meta.update(metric_updates)
        return True

    # ── introspection ────────────────────────────────────────────────────────

    @property
    def node_count(self) -> int:
        return self._g.number_of_nodes()

    @property
    def edge_count(self) -> int:
        return self._g.number_of_edges()

    def has_node(self, node_id: str) -> bool:
        return node_id in self._g

    def to_json(self) -> dict[str, Any]:
        nodes = [
            {"node_id": n, **self._g.nodes[n]} for n in self._g.nodes
        ]
        edges = [
            {"source_node": s, "target_node": t, **self._g.edges[s, t]}
            for s, t in self._g.edges
        ]
        return {"nodes": nodes, "edges": edges}

    def node_with_neighbors(self, node_id: str) -> dict[str, Any] | None:
        if node_id not in self._g:
            return None
        outbound = [
            {"target_node": t, **self._g.edges[node_id, t]}
            for t in self._g.successors(node_id)
        ]
        inbound = [
            {"source_node": s, **self._g.edges[s, node_id]}
            for s in self._g.predecessors(node_id)
        ]
        return {
            "node_id": node_id,
            **self._g.nodes[node_id],
            "outbound": outbound,
            "inbound": inbound,
        }

    def find_node_by_ip(self, ip: str) -> str | None:
        """Locate the node that owns ``ip`` — by explicit IP list or subnet CIDR."""

        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        # 1. Exact IP match on any node's metadata.ips list.
        for node_id, data in self._g.nodes(data=True):
            meta = data.get("metadata") or {}
            for owned in meta.get("ips", []) or []:
                try:
                    if ipaddress.ip_address(str(owned)) == addr:
                        return node_id
                except ValueError:
                    continue
        # 2. Subnet-type nodes whose CIDR contains the IP.
        for node_id, data in self._g.nodes(data=True):
            if data.get("node_type") != "subnet":
                continue
            meta = data.get("metadata") or {}
            cidr = meta.get("cidr")
            if not cidr:
                continue
            try:
                if addr in ipaddress.ip_network(str(cidr), strict=False):
                    return node_id
            except ValueError:
                continue
        return None

    def shortest_path(self, src_id: str, dst_id: str) -> list[str] | None:
        if src_id not in self._g or dst_id not in self._g:
            return None
        try:
            return nx.shortest_path(
                self._g, source=src_id, target=dst_id
            )  # type: ignore[return-value]
        except nx.NetworkXNoPath:
            return None
        except nx.NodeNotFound:
            return None

    def affected_by_prefix(self, prefix: str) -> dict[str, Any]:
        """Return nodes/links directly tied to a BGP prefix announcement."""

        try:
            target_net = ipaddress.ip_network(prefix, strict=False)
        except ValueError:
            target_net = None

        matching_edges: list[dict[str, Any]] = []
        affected_asns: set[int] = set()
        endpoints: set[str] = set()
        for s, t, data in self._g.edges(data=True):
            edge_prefix = data.get("prefix")
            hit = False
            if edge_prefix == prefix:
                hit = True
            elif target_net is not None and edge_prefix:
                try:
                    other = ipaddress.ip_network(str(edge_prefix), strict=False)
                    # Edge's prefix equals, contains, or is contained in target.
                    if other.overlaps(target_net):
                        hit = True
                except ValueError:
                    pass
            if not hit:
                continue
            matching_edges.append(
                {"source_node": s, "target_node": t, **data}
            )
            endpoints.add(s)
            endpoints.add(t)
            for asn in data.get("as_path", []) or []:
                try:
                    affected_asns.add(int(asn))
                except (TypeError, ValueError):
                    continue

        nodes = [
            {"node_id": n, **self._g.nodes[n]}
            for n in endpoints
            if n in self._g
        ]
        return {
            "prefix": prefix,
            "nodes": nodes,
            "links": matching_edges,
            "as_path_impact": sorted(affected_asns),
        }

    def subgraph(self, node_id: str, depth: int) -> dict[str, Any] | None:
        if node_id not in self._g:
            return None
        depth = max(0, int(depth))
        # BFS on the undirected view so we capture both inbound and outbound.
        visited: dict[str, int] = {node_id: 0}
        frontier: list[str] = [node_id]
        undirected = self._g.to_undirected(as_view=True)
        for _ in range(depth):
            next_frontier: list[str] = []
            for n in frontier:
                for nbr in undirected.neighbors(n):
                    if nbr not in visited:
                        visited[nbr] = visited[n] + 1
                        next_frontier.append(nbr)
            frontier = next_frontier
            if not frontier:
                break

        selected = set(visited)
        nodes = [
            {"node_id": n, "hops": visited[n], **self._g.nodes[n]}
            for n in selected
        ]
        edges = [
            {"source_node": s, "target_node": t, **self._g.edges[s, t]}
            for s, t in self._g.edges
            if s in selected and t in selected
        ]
        return {
            "root": node_id,
            "depth": depth,
            "nodes": nodes,
            "edges": edges,
        }

    def iter_node_ids(self) -> Iterable[str]:
        return iter(self._g.nodes)
