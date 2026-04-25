"""Seed DynamoDB Local with a realistic network topology for demos.

Populates ``network-topology`` (nodes) and ``network-links`` (edges) with:

    • 4 VPCs spread across 3 regions
    • 10 subnets distributed across those VPCs
    • 3 transit gateways (one per region) with inter-region peering
    • 1 local BGP speaker peering with 4 external ASNs

Run once after ``docker compose up -d`` before starting the topology service:

    python scripts/init_topology.py

It is idempotent: re-running refreshes the seed data without duplicating rows,
and creates the tables first if they do not already exist.

Configured via the same env vars the topology service uses
(``DYNAMODB_ENDPOINT_URL``, ``DYNAMODB_REGION``, ``DYNAMODB_TABLE_TOPOLOGY``,
``DYNAMODB_TABLE_LINKS``, ``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

ISO_NOW = datetime.now(tz=timezone.utc).isoformat()


def _to_dynamo(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


# ─── seed topology ───────────────────────────────────────────────────────────

NODES: list[dict[str, Any]] = [
    # Local BGP speaker (the anchor that BGP peer edges attach to).
    {
        "node_id": "local-gateway",
        "node_type": "router",
        "metadata": {"role": "bgp-speaker", "asn": 65000, "region": "us-east-1"},
        "last_seen": ISO_NOW,
        "health_status": "OK",
    },
    # ── VPCs ─────────────────────────────────────────────────────────────────
    {
        "node_id": "vpc-a",
        "node_type": "vpc",
        "metadata": {"cidr": "10.10.0.0/16", "region": "us-east-1"},
        "last_seen": ISO_NOW,
        "health_status": "OK",
    },
    {
        "node_id": "vpc-b",
        "node_type": "vpc",
        "metadata": {"cidr": "10.20.0.0/16", "region": "us-east-1"},
        "last_seen": ISO_NOW,
        "health_status": "OK",
    },
    {
        "node_id": "vpc-c",
        "node_type": "vpc",
        "metadata": {"cidr": "10.30.0.0/16", "region": "us-west-2"},
        "last_seen": ISO_NOW,
        "health_status": "OK",
    },
    {
        "node_id": "vpc-d",
        "node_type": "vpc",
        "metadata": {"cidr": "10.40.0.0/16", "region": "eu-west-1"},
        "last_seen": ISO_NOW,
        "health_status": "OK",
    },
    # ── Subnets (10 total) ───────────────────────────────────────────────────
    {"node_id": "subnet-a1", "node_type": "subnet",
     "metadata": {"cidr": "10.10.1.0/24", "vpc": "vpc-a", "zone": "us-east-1a"},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "subnet-a2", "node_type": "subnet",
     "metadata": {"cidr": "10.10.2.0/24", "vpc": "vpc-a", "zone": "us-east-1b"},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "subnet-a3", "node_type": "subnet",
     "metadata": {"cidr": "10.10.3.0/24", "vpc": "vpc-a", "zone": "us-east-1c"},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "subnet-b1", "node_type": "subnet",
     "metadata": {"cidr": "10.20.1.0/24", "vpc": "vpc-b", "zone": "us-east-1a"},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "subnet-b2", "node_type": "subnet",
     "metadata": {"cidr": "10.20.2.0/24", "vpc": "vpc-b", "zone": "us-east-1b"},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "subnet-c1", "node_type": "subnet",
     "metadata": {"cidr": "10.30.1.0/24", "vpc": "vpc-c", "zone": "us-west-2a"},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "subnet-c2", "node_type": "subnet",
     "metadata": {"cidr": "10.30.2.0/24", "vpc": "vpc-c", "zone": "us-west-2b"},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "subnet-c3", "node_type": "subnet",
     "metadata": {"cidr": "10.30.3.0/24", "vpc": "vpc-c", "zone": "us-west-2c"},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "subnet-d1", "node_type": "subnet",
     "metadata": {"cidr": "10.40.1.0/24", "vpc": "vpc-d", "zone": "eu-west-1a"},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "subnet-d2", "node_type": "subnet",
     "metadata": {"cidr": "10.40.2.0/24", "vpc": "vpc-d", "zone": "eu-west-1b"},
     "last_seen": ISO_NOW, "health_status": "OK"},
    # ── Transit Gateways (3) ─────────────────────────────────────────────────
    {"node_id": "tgw-1", "node_type": "router",
     "metadata": {"role": "transit-gateway", "region": "us-east-1", "ips": ["10.10.0.1", "10.20.0.1"]},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "tgw-2", "node_type": "router",
     "metadata": {"role": "transit-gateway", "region": "us-west-2", "ips": ["10.30.0.1"]},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "tgw-3", "node_type": "router",
     "metadata": {"role": "transit-gateway", "region": "eu-west-1", "ips": ["10.40.0.1"]},
     "last_seen": ISO_NOW, "health_status": "OK"},
    # ── External BGP peers (4) ───────────────────────────────────────────────
    {"node_id": "peer-203.0.113.1", "node_type": "router",
     "metadata": {"role": "ebgp-peer", "asn": 64501, "ips": ["203.0.113.1"]},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "peer-198.51.100.1", "node_type": "router",
     "metadata": {"role": "ebgp-peer", "asn": 64502, "ips": ["198.51.100.1"]},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "peer-192.0.2.1", "node_type": "router",
     "metadata": {"role": "ebgp-peer", "asn": 64503, "ips": ["192.0.2.1"]},
     "last_seen": ISO_NOW, "health_status": "OK"},
    {"node_id": "peer-172.16.0.1", "node_type": "router",
     "metadata": {"role": "ebgp-peer", "asn": 64504, "ips": ["172.16.0.1"]},
     "last_seen": ISO_NOW, "health_status": "OK"},
    # ── Autonomous System stubs ──────────────────────────────────────────────
    {"node_id": "asn-65000", "node_type": "asn",
     "metadata": {"asn": 65000, "name": "netobserv-internal"},
     "last_seen": ISO_NOW, "health_status": "OK"},
]


def _physical(
    src: str, dst: str, bandwidth: float, bidirectional: bool = True
) -> list[dict[str, Any]]:
    edge = {
        "source_node": src,
        "target_node": dst,
        "link_type": "physical",
        "status": "active",
        "bandwidth": bandwidth,
        "utilization": 0.0,
        "traffic_bytes": 0,
        "traffic_packets": 0,
    }
    edges = [edge]
    if bidirectional:
        edges.append({**edge, "source_node": dst, "target_node": src})
    return edges


def _bgp(
    peer_id: str, prefix: str, as_path: list[int]
) -> list[dict[str, Any]]:
    return [
        {
            "source_node": "local-gateway",
            "target_node": peer_id,
            "link_type": "bgp_peer",
            "status": "active",
            "bandwidth": 1_000_000_000.0,
            "utilization": 0.0,
            "prefix": prefix,
            "as_path": as_path,
            "last_bgp_update": ISO_NOW,
        }
    ]


LINKS: list[dict[str, Any]] = [
    # Subnets → VPCs (both directions for traffic routing).
    *_physical("subnet-a1", "vpc-a", 10_000_000_000.0),
    *_physical("subnet-a2", "vpc-a", 10_000_000_000.0),
    *_physical("subnet-a3", "vpc-a", 10_000_000_000.0),
    *_physical("subnet-b1", "vpc-b", 10_000_000_000.0),
    *_physical("subnet-b2", "vpc-b", 10_000_000_000.0),
    *_physical("subnet-c1", "vpc-c", 10_000_000_000.0),
    *_physical("subnet-c2", "vpc-c", 10_000_000_000.0),
    *_physical("subnet-c3", "vpc-c", 10_000_000_000.0),
    *_physical("subnet-d1", "vpc-d", 10_000_000_000.0),
    *_physical("subnet-d2", "vpc-d", 10_000_000_000.0),
    # VPCs → Transit gateways.
    *_physical("vpc-a", "tgw-1", 40_000_000_000.0),
    *_physical("vpc-b", "tgw-1", 40_000_000_000.0),
    *_physical("vpc-c", "tgw-2", 40_000_000_000.0),
    *_physical("vpc-d", "tgw-3", 40_000_000_000.0),
    # Inter-region TGW peering (full mesh).
    *_physical("tgw-1", "tgw-2", 100_000_000_000.0),
    *_physical("tgw-1", "tgw-3", 100_000_000_000.0),
    *_physical("tgw-2", "tgw-3", 100_000_000_000.0),
    # Local BGP speaker uplink to TGW-1.
    *_physical("local-gateway", "tgw-1", 20_000_000_000.0),
    # BGP peering.
    *_bgp("peer-203.0.113.1", "203.0.113.0/24", [64501, 65000]),
    *_bgp("peer-198.51.100.1", "198.51.100.0/24", [64502, 65000]),
    *_bgp("peer-192.0.2.1", "192.0.2.0/24", [64503, 64510, 65000]),
    *_bgp("peer-172.16.0.1", "172.16.0.0/16", [64504, 65000]),
]


# ─── table bootstrap ─────────────────────────────────────────────────────────

def _create_tables(dynamodb: Any, nodes_table: str, links_table: str) -> None:
    existing = {t.name for t in dynamodb.tables.all()}

    if nodes_table not in existing:
        print(f"Creating table {nodes_table}...")
        table = dynamodb.create_table(
            TableName=nodes_table,
            KeySchema=[{"AttributeName": "node_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "node_id", "AttributeType": "S"},
                {"AttributeName": "node_type", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "node-type-lookup",
                    "KeySchema": [
                        {"AttributeName": "node_type", "KeyType": "HASH"}
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()

    if links_table not in existing:
        print(f"Creating table {links_table}...")
        table = dynamodb.create_table(
            TableName=links_table,
            KeySchema=[
                {"AttributeName": "source_node", "KeyType": "HASH"},
                {"AttributeName": "target_node", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "source_node", "AttributeType": "S"},
                {"AttributeName": "target_node", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "target-lookup",
                    "KeySchema": [
                        {"AttributeName": "target_node", "KeyType": "HASH"},
                        {"AttributeName": "source_node", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()


def _seed(dynamodb: Any, nodes_table: str, links_table: str) -> None:
    nodes = dynamodb.Table(nodes_table)
    links = dynamodb.Table(links_table)

    with nodes.batch_writer() as batch:
        for item in NODES:
            batch.put_item(Item=_to_dynamo(item))

    with links.batch_writer() as batch:
        for item in LINKS:
            batch.put_item(Item=_to_dynamo(item))


def main() -> int:
    endpoint = os.getenv("DYNAMODB_ENDPOINT_URL", "http://localhost:8000")
    region = os.getenv("DYNAMODB_REGION") or os.getenv("AWS_REGION", "us-east-1")
    access_key = os.getenv("AWS_ACCESS_KEY_ID", "fakekey")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "fakesecret")
    nodes_table = os.getenv("DYNAMODB_TABLE_TOPOLOGY", "network-topology")
    links_table = os.getenv("DYNAMODB_TABLE_LINKS", "network-links")

    kwargs: dict[str, Any] = {"region_name": region}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key

    print(f"Target: {endpoint or 'real AWS'} / region={region}")
    dynamodb = boto3.resource("dynamodb", **kwargs)

    try:
        _create_tables(dynamodb, nodes_table, links_table)
        _seed(dynamodb, nodes_table, links_table)
    except ClientError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Seeded {len(NODES)} nodes into {nodes_table}.")
    print(f"Seeded {len(LINKS)} directed edges into {links_table}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
