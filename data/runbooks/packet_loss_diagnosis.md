---
title: Packet Loss Diagnosis
anomaly_type: snmp
severity: warn
tags: [packet-loss, errors, drops, congestion]
---

# Packet Loss Diagnosis

## Summary

Packet loss anomalies surface when SNMP-reported drop counters or interface
errors climb above their per-device baseline. Untreated loss degrades TCP
throughput, breaks UDP-based real-time applications, and amplifies tail
latency. This runbook isolates the layer responsible (physical, link, queue,
routing, host) and prescribes the first remediation step.

## Symptoms

- SNMP anomaly with `top_features` such as `if_in_errors_sum`,
  `if_out_discards_sum`, or `if_in_drops_sum` showing high z-scores.
- TCP retransmits visible in `ss -ti` exceed 1 percent of segments sent.
- Voice / video application reports increased MOS degradation.
- Layer-4 monitors show timeouts that vanish on retry.
- Flow records show truncated flows ending abruptly without a graceful FIN.

## Diagnostic Commands

```bash
# Interface-level counters
show interfaces <if> counters errors
show interfaces <if> | include "input errors|output errors|drops"
ethtool -S <if> | grep -E "drop|err|crc|frame"

# Drill down on the drop reason on Linux
tc -s qdisc show dev <if>
nstat -as | grep -i drop
dropwatch -l kas

# End-to-end loss rate
ping -c 200 -i 0.2 <dst>
mtr --report --report-cycles 200 --packet-size 1400 <dst>

# Optical health
show controllers <transceiver> phy
show interface <if> transceiver detail
```

## Common Root Causes

1. **Physical layer faults** - bad transceiver, dirty fiber, faulty patch
   cable, or a partially failing line card.
2. **Speed / duplex mismatch** between the device and an attached host or
   switch, common after a cable swap.
3. **Tail drops from sustained congestion** on an interface that runs hot
   even though average utilization looks acceptable.
4. **MTU mismatch** on tunnels causing fragmentation drops when DF is set.
5. **CPU drops on the receive side** when interrupt processing or kernel
   queues are saturated, often accompanied by `if_in_drops` rather than CRC
   errors.
6. **Faulty NAT or firewall state-table evictions** dropping mid-flow
   packets.
7. **Routing instability** sending part of a flow over a path that does not
   actually reach the destination.

## Remediation Steps

1. Distinguish the layer first. CRC, frame, and FCS errors are physical.
   Discards and drops with no error counter movement are usually queue or
   software issues. Use the diagnostic commands above to bin the symptom.
2. For physical issues, swap the suspect transceiver into a known-good port
   on a different device. If errors follow the transceiver, replace it. If
   they stay with the port, raise an RMA against the line card.
3. For congestion-based drops, look at the queue scheduler configuration and
   either reshape traffic, raise the queue limit, or enable ECN on the path.
   Adding capacity is the durable answer when offered load is genuinely
   higher than baseline.
4. For MTU mismatches, set TCP MSS clamping on the tunnel ingress and
   confirm path MTU discovery works end to end.
5. For host-side drops, scale the receive ring buffer
   (`ethtool -G <if> rx 4096`) and pin interrupt affinity to dedicated cores.
6. After remediating, watch the anomaly score and `if_in_errors` counter for
   60 minutes. The error rate should fall to near zero. If it does not,
   re-open the investigation.
7. Capture the device serial, firmware version, and remediation type in
   your asset inventory so you can spot recurring failure patterns.
