---
title: Asymmetric Routing Investigation
anomaly_type: traffic
severity: warn
tags: [routing, asymmetric, stateful-firewall, ecmp]
---

# Asymmetric Routing Investigation

## Summary

Asymmetric routing happens when traffic in one direction follows a different
path than the return traffic. It is normal at internet scale but becomes a
problem when stateful middleboxes (firewalls, NAT, IDS) sit on only one
direction, causing connections to silently break. The platform flags
asymmetry indirectly: as truncated flows, uneven byte counts on paired
flow records, or stateful firewall drop counters climbing.

## Symptoms

- TCP connections to a service work for the first few packets and then
  freeze or reset after a few seconds.
- Flow records show forward-direction bytes greatly exceeding return bytes,
  or vice versa, beyond what application protocol would explain.
- Stateful firewall logs show `no-state` or `out-of-window` drops for
  established sessions.
- `tcpdump` on the server shows the SYN arriving but the SYN-ACK never
  reaches the client end.
- Traceroute forward path differs structurally from a reverse traceroute.

## Diagnostic Commands

```bash
# Confirm asymmetric path with reverse traceroute
mtr --report --report-cycles 30 --no-dns <client_ip>
ssh <client_host> mtr --report --report-cycles 30 --no-dns <server_ip>

# Inspect flow record pairing
curl -s http://api:8000/flows?src=<client>&dst=<server>&since=15m

# Stateful firewall stats
show conn protocol tcp address <client_ip>
show conn count failed
fw stat | grep -i drop

# ECMP hash distribution snapshot
show ip cef <prefix> internal
show platform hardware fed switch active forward-asic resource statistics
```

## Common Root Causes

1. **ECMP hash skew** sending forward traffic over one link and return over
   another. Harmless until a stateful device sits on only one link.
2. **Recent IGP cost change** that altered the topology asymmetrically.
3. **Internet asymmetric peering** when the downstream provider has a
   different best path back than your outbound choice.
4. **VPN tunnel preference** that differs between two redundant tunnels at
   each end.
5. **Anycast misconfiguration** where the request lands on one POP and the
   response is sourced from another.
6. **NAT applied on only one direction** of a flow, breaking the address
   pair the stateful device tracks.

## Remediation Steps

1. Confirm asymmetry from both ends using paired traceroutes. Do not
   diagnose from one end only - ECMP can produce different per-flow paths
   that look asymmetric in a single-ended capture.
2. Identify the stateful device or function that loses the state. If it is
   a firewall on only one path, the durable fix is to either route both
   directions through the same device or move to a stateless
   anti-spoofing posture.
3. For ECMP-induced asymmetry where flows must traverse the same
   middlebox, enable per-flow stickiness (consistent hashing) on the
   forwarding plane.
4. For internet-facing asymmetry, work with your peer or transit provider
   to align outbound and inbound preferences. BGP local-pref and
   communities tagged for "no-export" can shape the inbound path.
5. If the cause is a recent IGP change, revert it during a maintenance
   window or reshape traffic with policy-based routing until the topology
   can be corrected globally.
6. Validate the fix by re-running paired traceroutes and observing the
   stateful firewall drop counters fall to zero.
7. Document the asymmetric path topology so future changes are evaluated
   against the constraint that stateful devices need both directions.
