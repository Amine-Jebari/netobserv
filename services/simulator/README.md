# services/simulator

Synthetic network telemetry generator. Since there is no live AWS VPC yet, this
service **is** the data source for the local stack — every downstream service
ultimately consumes records produced here.

## What it emits

| Kafka topic | Record type | Default rate |
|---|---|---|
| `network.flows` | VPC Flow Log v2 (`version, account_id, interface_id, srcaddr, dstaddr, srcport, dstport, protocol, packets, bytes, start_ts, end_ts, action, log_status`) | ~75 records/s |
| `network.bgp` | BGP events: `ANNOUNCE` / `WITHDRAW` / `UPDATE` with `peer_ip, prefix, as_path, next_hop, local_pref, med` | 1–5 events/min |
| `network.snmp` | SNMP samples for `ifInOctets, ifOutOctets, ifInErrors, ifOutErrors, sysUpTime, cpuLoad, ifUtilizationPct, latencyMs` | every 30 s per device |

The topology is fixed and deterministic:
- 20 source IPs across three internal subnets
- 50 destinations (public services, CDN ranges, upstream peers)
- 10 simulated routers with 3 interfaces each
- 4 BGP peers announcing 6 prefixes

## Scenarios

The `SCENARIO` (alias `SCENARIO_MODE` + `SCENARIO`) environment controls anomaly
injection. Every `ANOMALY_INTERVAL_MINUTES` (default 15 min) the scheduler
injects a new scenario that runs for a fixed duration, then returns to normal.

| Name | Duration | What it does |
|---|---|---|
| `normal` | — | Only baseline traffic; nothing injected. |
| `bgp_flap` | 2 min | One prefix repeatedly `ANNOUNCE` / `WITHDRAW` on one peer, 2s cadence. |
| `traffic_spike` | 5 min | One source IP emits 10× baseline flow volume. |
| `route_leak` | 90 s | A bogus AS path (starting `65999`) appears announcing a known prefix. |
| `link_saturation` | 3 min | Target device reports `ifUtilizationPct` ≥ 95% and 5× octet counters. |
| `latency_spike` | 3 min | Target device emits extra `latencyMs` samples with elevated values. |

Every injection is logged at `WARNING` level with event name
`scenario.injected`, the parameters, and the remaining duration, so detections
can be correlated with ground truth later.

## Scenario modes

`SCENARIO_MODE` selects how the scheduler picks the next scenario:

- `scheduled` (default) — round-robin through `bgp_flap → traffic_spike → link_saturation → latency_spike → route_leak`.
- `random` — pick uniformly at random from the rotation.
- `single` — always inject the scenario named by `SCENARIO` (useful for targeted demos).

## Environment variables

| Name | Default | Purpose |
|---|---|---|
| `KAFKA_BOOTSTRAP_SERVERS` | `redpanda:9092` | Broker endpoint. |
| `KAFKA_TOPIC_FLOWS` | `network.flows` | Flow topic. |
| `KAFKA_TOPIC_BGP` | `network.bgp` | BGP topic. |
| `KAFKA_TOPIC_SNMP` | `network.snmp` | SNMP topic. |
| `PRODUCER_RATE` | `75` | Target flows/second in normal mode. |
| `SCENARIO_MODE` | `scheduled` | `scheduled`, `random`, or `single`. |
| `SCENARIO` | `normal` | Scenario to use when `SCENARIO_MODE=single`. |
| `ANOMALY_INTERVAL_MINUTES` | `15` | Minutes between scenario injections. |
| `SNMP_INTERVAL_SECONDS` | `30` | Polling interval per device. |
| `HEALTH_PORT` | `8080` | HTTP health endpoint port. |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `RANDOM_SEED` | — | Set for reproducible traffic mixes. |

## Health check

```
GET /health → 200 {"status":"ok","messages_sent":<n>,"active_scenario":<name|null>}
```

Returns `503` if the Kafka producer has failed.

## Run as part of the stack

```bash
cd ..
docker compose up -d simulator
docker compose logs -f simulator
```

## Run standalone (against a broker on localhost)

```bash
cd services/simulator
pip install -e .[dev]
KAFKA_BOOTSTRAP_SERVERS=localhost:9092 \
  SCENARIO_MODE=scheduled \
  ANOMALY_INTERVAL_MINUTES=2 \
  LOG_LEVEL=INFO \
  netobserv-simulator
```

## Run tests

```bash
cd services/simulator
pip install -e .[dev]
pytest
```

Tests cover schema conformance for all three generators and the full scenario
scheduler (injection timing, rotation, per-scenario parameters, expiry).

## Force a single scenario for a demo

```bash
SCENARIO_MODE=single \
  SCENARIO=bgp_flap \
  ANOMALY_INTERVAL_MINUTES=1 \
  docker compose up -d simulator
```
