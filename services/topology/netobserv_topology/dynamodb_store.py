"""DynamoDB-backed persistence for nodes and links.

Uses the aioboto3 resource API. Transparently targets DynamoDB Local (non-empty
``endpoint_url``) or real AWS DynamoDB (empty ``endpoint_url``) — the code path
is identical.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, AsyncIterator

import aioboto3
from botocore.exceptions import ClientError

from .logging_setup import get_logger

log = get_logger(__name__)

NODE_TYPES = {"router", "subnet", "vpc", "asn"}
LINK_TYPES = {"bgp_peer", "traffic", "physical"}


def _to_dynamo(value: Any) -> Any:
    """Coerce Python floats into Decimal (DynamoDB does not store float)."""

    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    if isinstance(value, tuple):
        return [_to_dynamo(v) for v in value]
    return value


def _from_dynamo(value: Any) -> Any:
    """Coerce DynamoDB Decimals back into native int/float."""

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    return value


class TopologyStore:
    """Thin async wrapper around the two DynamoDB topology tables."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        region: str,
        access_key: str,
        secret_key: str,
        table_nodes: str,
        table_links: str,
    ) -> None:
        self._endpoint_url = endpoint_url or None
        self._region = region
        self._access_key = access_key or None
        self._secret_key = secret_key or None
        self._table_nodes = table_nodes
        self._table_links = table_links
        self._session = aioboto3.Session()
        self._connected = False
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def nodes_table_name(self) -> str:
        return self._table_nodes

    @property
    def links_table_name(self) -> str:
        return self._table_links

    @asynccontextmanager
    async def _resource(self) -> AsyncIterator[Any]:
        kwargs: dict[str, Any] = {"region_name": self._region}
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url
        if self._access_key:
            kwargs["aws_access_key_id"] = self._access_key
        if self._secret_key:
            kwargs["aws_secret_access_key"] = self._secret_key
        async with self._session.resource("dynamodb", **kwargs) as resource:
            yield resource

    async def start(self) -> None:
        """Verify that both tables exist and are reachable."""

        async with self._resource() as resource:
            for name in (self._table_nodes, self._table_links):
                table = await resource.Table(name)
                try:
                    await table.load()
                except ClientError as exc:
                    self._connected = False
                    log.error("dynamodb.table_missing", table=name, error=str(exc))
                    raise
        self._connected = True
        log.info(
            "dynamodb.connected",
            endpoint=self._endpoint_url or "aws",
            nodes_table=self._table_nodes,
            links_table=self._table_links,
        )

    async def stop(self) -> None:
        self._connected = False

    # ── nodes ────────────────────────────────────────────────────────────────

    async def put_node(
        self,
        *,
        node_id: str,
        node_type: str,
        metadata: dict[str, Any] | None = None,
        last_seen: str | None = None,
        health_status: str | None = None,
    ) -> dict[str, Any]:
        item = {
            "node_id": node_id,
            "node_type": node_type,
            "metadata": metadata or {},
        }
        if last_seen is not None:
            item["last_seen"] = last_seen
        if health_status is not None:
            item["health_status"] = health_status
        async with self._resource() as resource:
            table = await resource.Table(self._table_nodes)
            await table.put_item(Item=_to_dynamo(item))
        return _from_dynamo(item)

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        async with self._resource() as resource:
            table = await resource.Table(self._table_nodes)
            resp = await table.get_item(Key={"node_id": node_id})
        item = resp.get("Item")
        return _from_dynamo(item) if item is not None else None

    async def update_node_health(
        self,
        *,
        node_id: str,
        health_status: str,
        last_seen: str,
        metric_updates: dict[str, float | int] | None = None,
    ) -> None:
        """Merge SNMP-derived fields onto a node without overwriting metadata."""

        update_expr = ["SET health_status = :hs, last_seen = :ts"]
        expr_values: dict[str, Any] = {
            ":hs": health_status,
            ":ts": last_seen,
        }
        if metric_updates:
            for idx, (metric, value) in enumerate(metric_updates.items()):
                placeholder = f":m{idx}"
                name_placeholder = f"#m{idx}"
                update_expr.append(
                    f"metadata.{name_placeholder} = {placeholder}"
                )
                expr_values[placeholder] = _to_dynamo(value)
            expr_names = {
                f"#m{idx}": metric
                for idx, metric in enumerate(metric_updates.keys())
            }
        else:
            expr_names = {}

        kwargs: dict[str, Any] = {
            "Key": {"node_id": node_id},
            "UpdateExpression": ", ".join(update_expr),
            "ExpressionAttributeValues": expr_values,
        }
        if expr_names:
            kwargs["ExpressionAttributeNames"] = expr_names

        async with self._resource() as resource:
            table = await resource.Table(self._table_nodes)
            await table.update_item(**kwargs)

    async def scan_nodes(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        async with self._resource() as resource:
            table = await resource.Table(self._table_nodes)
            kwargs: dict[str, Any] = {}
            while True:
                resp = await table.scan(**kwargs)
                items.extend(resp.get("Items", []))
                last = resp.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last
        return [_from_dynamo(i) for i in items]

    # ── links ────────────────────────────────────────────────────────────────

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
            item["as_path"] = as_path
        if prefix is not None:
            item["prefix"] = prefix
        async with self._resource() as resource:
            table = await resource.Table(self._table_links)
            await table.put_item(Item=_to_dynamo(item))
        return _from_dynamo(item)

    async def mark_link_status(
        self,
        *,
        source_node: str,
        target_node: str,
        status: str,
        last_bgp_update: str | None = None,
    ) -> None:
        expr = "SET #st = :status"
        values: dict[str, Any] = {":status": status}
        names = {"#st": "status"}
        if last_bgp_update is not None:
            expr += ", last_bgp_update = :ts"
            values[":ts"] = last_bgp_update
        async with self._resource() as resource:
            table = await resource.Table(self._table_links)
            await table.update_item(
                Key={"source_node": source_node, "target_node": target_node},
                UpdateExpression=expr,
                ExpressionAttributeValues=values,
                ExpressionAttributeNames=names,
            )

    async def increment_traffic(
        self,
        *,
        source_node: str,
        target_node: str,
        bytes_delta: int,
        packets_delta: int,
        utilization: float | None = None,
    ) -> None:
        """Add flow-record deltas to a link's running counters."""

        expr = (
            "ADD traffic_bytes :b, traffic_packets :p "
            "SET link_type = if_not_exists(link_type, :lt), "
            "    #st = if_not_exists(#st, :active)"
        )
        values: dict[str, Any] = {
            ":b": bytes_delta,
            ":p": packets_delta,
            ":lt": "traffic",
            ":active": "active",
        }
        names = {"#st": "status"}
        if utilization is not None:
            expr += ", utilization = :u"
            values[":u"] = _to_dynamo(utilization)
        async with self._resource() as resource:
            table = await resource.Table(self._table_links)
            await table.update_item(
                Key={"source_node": source_node, "target_node": target_node},
                UpdateExpression=expr,
                ExpressionAttributeValues=values,
                ExpressionAttributeNames=names,
            )

    async def scan_links(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        async with self._resource() as resource:
            table = await resource.Table(self._table_links)
            kwargs: dict[str, Any] = {}
            while True:
                resp = await table.scan(**kwargs)
                items.extend(resp.get("Items", []))
                last = resp.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last
        return [_from_dynamo(i) for i in items]
