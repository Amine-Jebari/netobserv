---
title: Interface Utilization and Capacity Planning
anomaly_type: snmp
severity: warn
tags: [capacity, utilization, planning, congestion]
---

# Interface Utilization and Capacity Planning

## Summary

A utilization anomaly fires when an interface's offered load approaches its
configured speed for sustained windows. While brief peaks are normal,
sustained saturation drives queue buildup, packet loss, and downstream
latency. This runbook helps distinguish a transient burst from a genuine
capacity problem and prescribes near-term and long-term remediation.

## Symptoms

- SNMP anomaly with `top_features` showing high z-scores on
  `if_utilization_pct_max` or `if_utilization_pct_mean`.
- 5 minute average utilization above 80 percent on a non-redundant link.
- Output queue depth growing linearly during peak windows.
- Downstream applications report intermittent timeouts coincident with
  business-hour peaks.
- Aggregation links where one member of an LAG runs hot while peers idle,
  indicating hash polarization rather than overall capacity exhaustion.

## Diagnostic Commands

```bash
# Per-interface utilization snapshot from InfluxDB
influx query 'from(bucket:"metrics")
  |> range(start:-1h)
  |> filter(fn:(r)=>r._measurement=="snmp" and r.metric=="ifUtilizationPct")
  |> group(columns:["device_id","interface"])
  |> aggregateWindow(every:1m, fn:max)'

# Device-level interface stats
show interfaces <if> | include "rate|utilization|drops"
show interface counters rate
show port-channel <id> traffic    # LAG balance

# Confirm which flows dominate
curl -s "http://api:8000/flows/top?interface=<if>&since=15m"
```

## Common Root Causes

1. **Genuine traffic growth** outstripping the original capacity plan -
   the link is sized for last year's volume.
2. **Hash polarization on a LAG** so the offered load concentrates on one
   member even though aggregate capacity is sufficient.
3. **Backup or replication job** scheduled during business hours,
   competing for capacity with production traffic.
4. **Misrouted traffic** that bypasses a higher-capacity path because of
   an IGP cost mistake.
5. **DDoS or scraping activity** masquerading as legitimate growth.
6. **Asymmetric peering** where outbound has plenty of headroom but
   inbound is exhausted on a paid-transit link.

## Remediation Steps

1. Confirm the trend is structural rather than a single bad day. Compare
   the trailing 7 day p95 utilization to the 30 day p95. If both are above
   75 percent, the link is genuinely full.
2. For LAG hash polarization, change the hash algorithm to include
   layer-4 ports
   (`port-channel load-balance src-dst-port` or platform equivalent) and
   verify member-link distribution improves.
3. For backup or replication jobs, reschedule them outside business hours
   or rate-limit them at the application layer.
4. For misrouted traffic, audit recent IGP and BGP changes. Restore the
   intended path or shift traffic with policy-based routing while you
   investigate.
5. For genuine growth, file a capacity ticket with concrete numbers:
   peak utilization, growth rate over the last 90 days, projected date of
   100 percent saturation. Request capacity expansion well before the
   projected crunch.
6. Until capacity expands, deploy QoS to protect business-critical
   classes. Mark and police bulk traffic so it absorbs the impact during
   peaks.
7. Add a forward-looking dashboard that projects utilization 90 days out
   based on the current growth slope, so future capacity decisions are
   triggered earlier than the alert threshold.
