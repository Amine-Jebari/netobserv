# Service: topology

Maintains the live network topology graph for the Network Observability
Platform. Consumes BGP / flow / SNMP events from Kafka, persists nodes and
links to DynamoDB, keeps an in-memory NetworkX graph for fast queries, and
exposes REST + WebSocket endpoints that the frontend and agent services use.

## Responsibilities

| Stream            | Effect on the graph                                    |
|-------------------|--------------------------------------------------------|
| `topology.updates`| BGP ANNOUNCE/UPDATE → upsert peer + `bgp_peer` link. WITHDRAW → mark link inactive (kept for history). |
| `network.flows`   | Increment `traffic_bytes` / `traffic_packets` on the edge between srcaddr's and dstaddr's nodes; update rolling utilisation. |
| `network.snmp`    | Update target device's `health_status` + `last_seen` from metric thresholds. |

## Data model

**Nodes — table `network-topology`**

| Attribute       | Type    | Notes                                       |
|-----------------|---------|---------------------------------------------|
| `node_id` (PK)  | String  | e.g. `vpc-a`, `subnet-a1`, `tgw-1`, `peer-10.0.0.1` |
| `node_type`     | String  | `router` \| `subnet` \| `vpc` \| `asn`      |
| `metadata`      | Map     | `{ips, cidr, asn, region, …}`                |
| `last_seen`     | String  | ISO-8601 timestamp                          |
| `health_status` | String  | `OK` \| `WARN` \| `CRITICAL`                 |

A GSI on `node_type` (defined in `terraform/dynamodb.tf`) lets consumers
filter the node population by category efficiently.

**Links — table `network-links`**

| Attribute           | Type    | Notes                                  |
|---------------------|---------|----------------------------------------|
| `source_node` (PK)  | String  | Upstream node id                        |
| `target_node` (SK)  | String  | Downstream node id                      |
| `link_type`         | String  | `bgp_peer` \| `traffic` \| `physical`   |
| `status`            | String  | `active` \| `inactive`                  |
| `bandwidth`         | Number  | Link capacity in bps (physical/bgp)     |
| `utilization`       | Number  | EWMA of bytes/s ÷ bandwidth (0–1)       |
| `as_path`           | List    | BGP AS path at time of announcement     |
| `prefix`            | String  | Announced prefix (CIDR)                 |
| `last_bgp_update`   | String  | ISO-8601 timestamp                      |
| `traffic_bytes`     | Number  | Running counter of bytes observed       |
| `traffic_packets`   | Number  | Running counter of packets observed     |

The `target-lookup` GSI lets callers query inbound edges for a given target.

## HTTP endpoints

| Method | Path                                                      | Purpose |
|--------|-----------------------------------------------------------|---------|
| GET    | `/health`                                                 | 200 iff DynamoDB + every Kafka consumer is connected. |
| GET    | `/metrics`                                                | Prometheus text exposition. |
| GET    | `/stats`                                                  | JSON per-topic throughput + graph size. |
| GET    | `/topology`                                               | Full graph `{nodes, edges}`. |
| GET    | `/topology/node/{node_id}`                                | Node details + inbound / outbound neighbours. |
| GET    | `/topology/path?src=<ip>&dst=<ip>`                        | Shortest path between two IPs. |
| GET    | `/topology/affected?prefix=<cidr>`                        | Nodes/links impacted by a BGP prefix change. |
| GET    | `/topology/subgraph?node_id=<id>&depth=<n>`               | BFS subgraph around a node (agent context). |
| WS     | `/ws/topology`                                            | Initial snapshot + live diffs for the frontend. |

## Key env vars

| Variable                           | Default                       | Notes |
|-----------------------------------|-------------------------------|-------|
| `KAFKA_BOOTSTRAP_SERVERS`          | `redpanda:9092`               | Brokers. |
| `KAFKA_TOPIC_TOPOLOGY_UPDATES`     | `topology.updates`            | BGP stream consumed from the collector. |
| `KAFKA_TOPIC_FLOWS`                | `network.flows`               | Flow records for traffic edges. |
| `KAFKA_TOPIC_SNMP`                 | `network.snmp`                | SNMP samples for node health. |
| `KAFKA_CONSUMER_GROUP_PREFIX`      | `netobserv-topology`          | Each topic gets `-bgp` / `-flows` / `-snmp`. |
| `DYNAMODB_ENDPOINT`                | *(empty → real AWS)*          | Set to `http://dynamodb-local:8000` locally. |
| `DYNAMODB_TABLE_TOPOLOGY`          | `network-topology`            | Node table. |
| `DYNAMODB_TABLE_LINKS`             | `network-links`               | Edge table. |
| `TOPOLOGY_REFRESH_INTERVAL`        | `300`                         | Seconds between full DynamoDB rehydrations (0 = disabled). |
| `TRAFFIC_EWMA_ALPHA`               | `0.2`                         | Utilisation smoothing factor. |
| `HTTP_PORT`                        | `8080`                        | In-container HTTP port. |
| `LOG_LEVEL`                        | `INFO`                        | Python log level. |

## Run locally

```bash
# Create and seed tables in DynamoDB Local:
python scripts/init_topology.py

# Bring up (redpanda + dynamodb-local are listed as dependencies):
docker compose up -d topology

# Tail logs:
docker compose logs -f topology

# Quick query:
curl -s http://localhost:8092/topology | jq '.nodes | length'
curl -s 'http://localhost:8092/topology/path?src=10.10.1.5&dst=10.20.1.5'
```

## Tests

```bash
cd services/topology
pip install -e '.[dev]'
pytest
```
