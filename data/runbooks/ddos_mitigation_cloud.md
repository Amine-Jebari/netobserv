---
title: DDoS Mitigation in Cloud Environments
anomaly_type: traffic
severity: critical
tags: [ddos, traffic, mitigation, cloud, edge]
---

# DDoS Mitigation in Cloud Environments

## Summary

A volumetric or application-layer DDoS attack drives anomalous traffic
patterns into a cloud-hosted edge. The platform's anomaly engine surfaces
these as a `traffic` anomaly with elevated total bytes, fan-in concentration,
and suppressed source diversity. This runbook walks an on-call engineer
through containment, scrubbing handoff, and verification without taking the
target service fully offline.

## Symptoms

- `anomaly_type=traffic` events with `severity=CRITICAL` and `top_features`
  showing high z-scores on `total_bytes`, `total_packets`, and
  `top_pair_byte_share`.
- A single destination IP or load balancer suddenly receives the majority of
  ingress traffic.
- Health checks on backend instances fail with timeouts despite low
  application CPU.
- Edge router CPU climbs above 80 percent and packet drops appear in the
  ingress queue counters.
- External monitoring shows synthetic probes from unaffected regions
  succeeding while affected-region probes fail.

## Diagnostic Commands

```bash
# Snapshot top talkers from the last 60 seconds via InfluxDB
influx query 'from(bucket:"metrics")
  |> range(start:-60s)
  |> filter(fn:(r)=>r._measurement=="flows")
  |> group(columns:["srcaddr"])
  |> sum(column:"_value")
  |> top(n:20)'

# Inspect the flow record sample on the anomaly event
jq '.raw_metrics_sample' /tmp/last_anomaly.json

# Cloud edge - list active filters
aws wafv2 list-web-acls --scope CLOUDFRONT
aws shield list-protections

# Confirm scrubber upstream is in path
mtr --report --no-dns -c 30 <target_ip>
dig +short <target_fqdn>
```

## Common Root Causes

1. **Reflection / amplification attack** - DNS, NTP, or memcached reflection
   targeting a single anycast address.
2. **HTTP flood** - layer 7 GET or POST flood overwhelming application
   workers without saturating the link.
3. **Spoofed SYN flood** - high packets-per-second with low byte volume,
   exhausting connection tracking.
4. **Misbehaving customer integration** with retry storms - looks similar to
   an attack but the source is internal.
5. **DNS cache poisoning** redirecting legitimate clients to a single backend.

## Remediation Steps

1. Confirm the anomaly is real by cross-checking the affected destination on
   at least two telemetry sources (flows + load balancer metrics). Internal
   retry storms often look identical and require a different fix.
2. Activate the upstream scrubbing service for the target prefix or
   FQDN. For AWS, ensure Shield Advanced is engaged and that the resource has
   a Shield protection. For Cloudflare or Akamai, escalate to a "Under Attack"
   posture for the relevant zone.
3. Apply a temporary edge ACL to drop the highest-confidence sources from the
   anomaly's `top_features` and `raw_metrics_sample`. Use anycast / scrubber
   capacity rather than your origin link wherever possible.
4. Increase backend autoscaling targets so the surviving traffic does not
   trigger a secondary outage from underprovisioning.
5. Watch the anomaly score and the load balancer success rate every minute.
   Once both have recovered for 15 consecutive minutes, begin walking back
   the most aggressive ACLs.
6. After mitigation, rotate any keys, tokens, or session secrets that may
   have been observable from the attacker's perspective.
7. File a post-incident report including peak pps, peak bps, scrubber
   handoff time, and customer-visible impact. Update the playbook with any
   gaps the on-call team encountered.
