---
title: BGP Flap Troubleshooting
anomaly_type: bgp
severity: high
tags: [bgp, routing, peering, flap]
---

# BGP Flap Troubleshooting

## Summary

A BGP flap occurs when a peering session repeatedly transitions between the
Established and a non-Established state, causing rapid route withdrawals and
re-announcements. Flaps propagate downstream as churn, blackhole short windows
of traffic, and trigger spurious anomaly alerts. This runbook covers detection,
isolation, and stabilization for BGP flaps observed on edge routers and
cloud-hosted route reflectors.

## Symptoms

- `bgp_state_changes_total` Prometheus counter increasing more than 5 times
  within a 10 minute window for a single neighbor.
- Anomaly engine emits `severity=CRITICAL` events with `anomaly_type=bgp` and
  `top_features` showing elevated `bgp_withdraw_count` and
  `bgp_unique_prefix_changes`.
- Operators notice intermittent reachability to specific destination prefixes.
- Sustained CPU spikes on route reflectors as the RIB recomputes.
- Downstream IGP convergence storms if redistribution is configured.

## Diagnostic Commands

```bash
# Cisco / IOS-XR
show bgp neighbor 10.0.0.1 detail
show bgp summary | include 10.0.0.1
show logging | include "BGP-5-ADJCHANGE"

# Juniper
show bgp neighbor 10.0.0.1 extensive
show log messages | match BGP_PREFIX_THRESH_EXCEEDED

# Linux / FRR
vtysh -c "show ip bgp neighbors 10.0.0.1"
vtysh -c "show ip bgp summary"
journalctl -u frr.service --since "10 min ago" | grep -i adjchange

# Platform-agnostic
ping -i 0.5 -W 1 -c 60 <neighbor_ip>
mtr --report --report-cycles 30 <neighbor_ip>
```

## Common Root Causes

1. **Underlying transport instability** - flapping sub-interface, optical
   transceiver errors, or a saturated link layer below BGP.
2. **Hold-timer mismatch** - asymmetric hold timers between peers, or a hold
   timer set lower than the steady-state keepalive RTT.
3. **MTU blackhole** - PMTUD broken on the path causes large UPDATE messages
   to silently drop, killing the session every time the RIB churns.
4. **Route flap damping** misconfiguration on a peer suppressing prefixes.
5. **TCP MD5 / TCP-AO** key mismatch after a credential rotation.
6. **Resource exhaustion** - peer router out of FIB capacity or memory,
   triggering protocol-level drops.
7. **Prefix-limit threshold exceeded** - peer dropping the session because
   announced prefix count crossed the configured maximum.

## Remediation Steps

1. Identify the flapping neighbor from anomaly metadata or `show bgp summary`
   and capture the most recent 30 minutes of `ADJCHANGE` syslog events.
2. If the link is shared with non-BGP traffic, check interface counters and
   transceiver DOM values for a physical-layer culprit before touching BGP.
3. Verify hold and keepalive timers match on both ends. Bump the hold timer to
   180 seconds temporarily if the path RTT is noisy.
4. Run a path MTU probe (`tracepath`, `mtr -s 1500`) to confirm 1500 byte
   packets reach the neighbor end to end. Lower the BGP TCP MSS to 1380 if a
   blackhole is suspected and you cannot fix the offending hop.
5. If the prefix-limit threshold is exceeded, raise it on the local side after
   confirming the increase is expected, or filter the offending peer.
6. After applying the fix, soft-clear the session
   (`clear bgp neighbor 10.0.0.1 soft`) and watch the anomaly stream for
   30 minutes to confirm the flap rate has dropped to zero.
7. Open a follow-up ticket if the root cause was a peer-side configuration
   so the change is tracked in the partner's change-management system.
