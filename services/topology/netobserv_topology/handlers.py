"""Event handlers that translate Kafka records into graph + DynamoDB mutations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .dynamodb_store import TopologyStore
from .graph import TopologyGraph
from .logging_setup import get_logger
from .metrics import TopologyMetrics
from .websocket import TopologyBroadcaster

log = get_logger(__name__)

# Fixed node representing the local BGP speaker. BGP events attach peer edges
# here. Created on demand if the seed script has not run yet.
LOCAL_GATEWAY_ID = "local-gateway"


def _parse_timestamp(value: Any) -> str:
    """Normalise a timestamp field to an ISO-8601 string in UTC."""

    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(tz=timezone.utc).isoformat()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    return datetime.now(tz=timezone.utc).isoformat()


def _message_ts(record: dict[str, Any]) -> float | None:
    ts = record.get("timestamp")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    if isinstance(ts, (int, float)):
        return float(ts)
    return None


class BgpHandler:
    """Applies BGP ANNOUNCE / WITHDRAW / UPDATE events to the graph."""

    def __init__(
        self,
        *,
        topic: str,
        graph: TopologyGraph,
        store: TopologyStore,
        broadcaster: TopologyBroadcaster,
        metrics: TopologyMetrics,
    ) -> None:
        self._topic = topic
        self._graph = graph
        self._store = store
        self._broadcaster = broadcaster
        self._metrics = metrics

    async def handle(self, record: dict[str, Any]) -> None:
        self._metrics.record_consumed(self._topic, _message_ts(record))

        event_type = str(record.get("event_type") or "").upper()
        peer_ip = record.get("peer_ip")
        prefix = record.get("prefix")
        if not peer_ip or not prefix or event_type not in {"ANNOUNCE", "WITHDRAW", "UPDATE"}:
            log.warning("bgp.invalid", event_type=event_type, peer_ip=peer_ip, prefix=prefix)
            self._metrics.record_error(self._topic)
            return

        ts = _parse_timestamp(record.get("timestamp"))
        peer_id = f"peer-{peer_ip}"
        as_path = [int(a) for a in (record.get("as_path") or []) if _is_int_like(a)]
        peer_asn = as_path[0] if as_path else None

        # Ensure the local anchor exists.
        if not self._graph.has_node(LOCAL_GATEWAY_ID):
            meta = {"role": "bgp-speaker"}
            self._graph.upsert_node(
                node_id=LOCAL_GATEWAY_ID,
                node_type="router",
                metadata=meta,
                last_seen=ts,
                health_status="OK",
            )
            await self._store.put_node(
                node_id=LOCAL_GATEWAY_ID,
                node_type="router",
                metadata=meta,
                last_seen=ts,
                health_status="OK",
            )

        # Upsert the peer node (idempotent for all three event types).
        peer_meta: dict[str, Any] = {"ips": [peer_ip]}
        if peer_asn is not None:
            peer_meta["asn"] = peer_asn
        self._graph.upsert_node(
            node_id=peer_id,
            node_type="router",
            metadata=peer_meta,
            last_seen=ts,
            health_status="OK",
        )
        await self._store.put_node(
            node_id=peer_id,
            node_type="router",
            metadata=peer_meta,
            last_seen=ts,
            health_status="OK",
        )

        if event_type in {"ANNOUNCE", "UPDATE"}:
            self._graph.upsert_link(
                source_node=LOCAL_GATEWAY_ID,
                target_node=peer_id,
                link_type="bgp_peer",
                status="active",
                as_path=as_path,
                prefix=str(prefix),
                last_bgp_update=ts,
            )
            await self._store.put_link(
                source_node=LOCAL_GATEWAY_ID,
                target_node=peer_id,
                link_type="bgp_peer",
                status="active",
                as_path=as_path,
                prefix=str(prefix),
                last_bgp_update=ts,
            )
            diff_kind = "link_announced"
        else:  # WITHDRAW
            self._graph.mark_link_status(
                source_node=LOCAL_GATEWAY_ID,
                target_node=peer_id,
                status="inactive",
                last_bgp_update=ts,
            )
            await self._store.mark_link_status(
                source_node=LOCAL_GATEWAY_ID,
                target_node=peer_id,
                status="inactive",
                last_bgp_update=ts,
            )
            diff_kind = "link_withdrawn"

        self._metrics.record_applied(self._topic)
        self._metrics.set_graph_size(self._graph.node_count, self._graph.edge_count)
        await self._broadcaster.broadcast(
            {
                "type": diff_kind,
                "source_node": LOCAL_GATEWAY_ID,
                "target_node": peer_id,
                "prefix": str(prefix),
                "as_path": as_path,
                "timestamp": ts,
            }
        )


class FlowHandler:
    """Applies VPC-flow records to traffic edges between known subnets/routers."""

    def __init__(
        self,
        *,
        topic: str,
        graph: TopologyGraph,
        store: TopologyStore,
        broadcaster: TopologyBroadcaster,
        metrics: TopologyMetrics,
        ewma_alpha: float,
    ) -> None:
        self._topic = topic
        self._graph = graph
        self._store = store
        self._broadcaster = broadcaster
        self._metrics = metrics
        self._ewma_alpha = ewma_alpha

    async def handle(self, record: dict[str, Any]) -> None:
        self._metrics.record_consumed(self._topic, _message_ts(record))

        srcaddr = record.get("srcaddr")
        dstaddr = record.get("dstaddr")
        if not srcaddr or not dstaddr:
            self._metrics.record_error(self._topic)
            return

        src_node = self._graph.find_node_by_ip(str(srcaddr))
        dst_node = self._graph.find_node_by_ip(str(dstaddr))
        # Only record flows between nodes we have in-graph. Everything else
        # (random internet destinations) is out of scope for the topology view.
        if src_node is None or dst_node is None or src_node == dst_node:
            return

        bytes_delta = int(record.get("bytes") or 0)
        packets_delta = int(record.get("packets") or 0)
        updated = self._graph.increment_traffic(
            source_node=src_node,
            target_node=dst_node,
            bytes_delta=bytes_delta,
            packets_delta=packets_delta,
            ewma_alpha=self._ewma_alpha,
        )
        await self._store.increment_traffic(
            source_node=src_node,
            target_node=dst_node,
            bytes_delta=bytes_delta,
            packets_delta=packets_delta,
            utilization=updated.get("utilization"),
        )
        self._metrics.record_applied(self._topic)
        self._metrics.set_graph_size(self._graph.node_count, self._graph.edge_count)
        # Traffic updates are high-rate; broadcast a lightweight diff only.
        await self._broadcaster.broadcast(
            {
                "type": "traffic_delta",
                "source_node": src_node,
                "target_node": dst_node,
                "bytes": bytes_delta,
                "packets": packets_delta,
                "utilization": updated.get("utilization"),
            }
        )


class SnmpHandler:
    """Applies SNMP samples to node health + per-metric metadata."""

    # Per-metric thresholds for deriving health status from a sample.
    _THRESHOLDS: dict[str, tuple[float, float]] = {
        "cpuLoad": (70.0, 90.0),
        "latencyMs": (100.0, 500.0),
        "ifUtilizationPct": (75.0, 90.0),
        "ifInErrors": (1.0, 10.0),
        "ifOutErrors": (1.0, 10.0),
    }

    def __init__(
        self,
        *,
        topic: str,
        graph: TopologyGraph,
        store: TopologyStore,
        broadcaster: TopologyBroadcaster,
        metrics: TopologyMetrics,
    ) -> None:
        self._topic = topic
        self._graph = graph
        self._store = store
        self._broadcaster = broadcaster
        self._metrics = metrics

    @classmethod
    def _health_from_metric(cls, metric: str, value: float) -> str:
        thresholds = cls._THRESHOLDS.get(metric)
        if thresholds is None:
            return "OK"
        warn, critical = thresholds
        if value >= critical:
            return "CRITICAL"
        if value >= warn:
            return "WARN"
        return "OK"

    async def handle(self, record: dict[str, Any]) -> None:
        self._metrics.record_consumed(self._topic, _message_ts(record))

        device_id = record.get("device_id")
        metric = record.get("metric")
        value = record.get("value")
        if not device_id or not metric or value is None:
            self._metrics.record_error(self._topic)
            return

        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            self._metrics.record_error(self._topic)
            return

        ts = _parse_timestamp(record.get("timestamp"))
        health = self._health_from_metric(str(metric), numeric_value)
        metric_key = f"snmp_{metric}"

        if not self._graph.has_node(device_id):
            meta = {"discovered_from": "snmp"}
            self._graph.upsert_node(
                node_id=device_id,
                node_type="router",
                metadata=meta,
                last_seen=ts,
                health_status=health,
            )
            await self._store.put_node(
                node_id=device_id,
                node_type="router",
                metadata=meta,
                last_seen=ts,
                health_status=health,
            )

        self._graph.update_node_health(
            node_id=device_id,
            health_status=health,
            last_seen=ts,
            metric_updates={metric_key: numeric_value},
        )
        await self._store.update_node_health(
            node_id=device_id,
            health_status=health,
            last_seen=ts,
            metric_updates={metric_key: numeric_value},
        )

        self._metrics.record_applied(self._topic)
        self._metrics.set_graph_size(self._graph.node_count, self._graph.edge_count)
        await self._broadcaster.broadcast(
            {
                "type": "node_health",
                "node_id": device_id,
                "health_status": health,
                "metric": metric,
                "value": numeric_value,
                "timestamp": ts,
            }
        )


def _is_int_like(value: Any) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False
