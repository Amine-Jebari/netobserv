---
title: High Latency Investigation
anomaly_type: snmp
severity: warn
tags: [latency, performance, snmp, end-to-end]
---

# High Latency Investigation

## Summary

A latency anomaly fires when round-trip-time or device-reported latency
exceeds the per-link baseline. Latency is a noisy signal because it lumps
together queuing, transmission, propagation, and serialization delays, so
this runbook prescribes a layered approach that confirms the anomaly is real
before chasing exotic causes.

## Symptoms

- SNMP anomaly events with `top_features` showing high z-scores on
  `latency_p95_ms` or `cpu_load_max`.
- End-user latency probes (RUM, synthetic) showing inflated p95 RTT for a
  particular geography or backbone link.
- Application-level timeouts that disappear when the call is retried after a
  short delay.
- Steady-state interface utilization below 70 percent yet queue depth growing.
- Asymmetric forward / return path latencies on a `mtr` probe.

## Diagnostic Commands

```bash
# Per-link latency from the topology service
curl -s "http://topology:8080/topology/path?src=10.0.1.5&dst=10.0.4.10"

# End-to-end probe with full hop visibility
mtr --report --report-cycles 60 --no-dns <dst_ip>
hping3 -S -p 443 --flood-rate 10 <dst_ip>

# Device queueing
show interfaces <if> queue
show platform hardware fed switch active qos queue stats
ethtool -S <if> | grep -E "drop|err"

# OS-level scheduling delays on the host
ss -ti dst <dst_ip>:443
chrony sources -v
```

## Common Root Causes

1. **Microbursts on a backbone link** - average utilization low but 1 ms
   bursts saturate the queue and tail-drop.
2. **Buffer bloat** - oversized buffers on a slow link inflate latency under
   load even when no packets drop.
3. **Routing detour** - an IGP cost change or tunnel reroute pushes traffic
   through an unintended path.
4. **CPU saturation on a control-plane device** - high `cpu_load` SNMP value
   correlates with elevated forwarding latency on slow path traffic.
5. **Underlying optical issue** - a partially failing transceiver introducing
   FEC corrections that add fixed latency per packet.
6. **Cross-AZ traffic pattern** that suddenly serialized through a single AZ
   instead of staying local, doubling RTT.

## Remediation Steps

1. Confirm latency really increased, not that the baseline shifted. Compare
   the current 5 minute p95 with the same window from yesterday and last week.
2. Pull `/topology/path` and `mtr` results to identify the hop where latency
   grows. If the inflation appears at one specific hop, focus there. If it
   accumulates across many hops, suspect a host-level problem.
3. If the anomaly is correlated with high CPU load on a router, schedule a
   maintenance window for that device, redistribute its workload to a peer,
   or move BGP sessions to a less loaded route reflector.
4. For microburst suspicion, enable a sub-second sampling profile on the
   affected interface and look for queue buildup correlated with anomaly
   timestamps.
5. If the path traversed an unintended AZ, audit recent IGP and tunnel
   metric changes. Revert the change that caused the rerouting if it was
   accidental.
6. After applying a fix, run a 30 minute soak test with synthetic probes
   from multiple regions and confirm anomaly events stop firing.
7. Capture the resolution in the post-incident note and link it to the
   anomaly event ID for future correlation.
