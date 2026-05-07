---
title: Route Leak Detection
anomaly_type: bgp
severity: critical
tags: [bgp, route-leak, hijack, policy]
---

# Route Leak Detection

## Summary

A route leak happens when a BGP speaker advertises prefixes to a peer in
violation of the intended routing policy, typically by re-announcing
provider-learned routes to another provider. Leaks redirect traffic through
unintended autonomous systems, increase latency, expose data to third parties,
and in the worst case fully blackhole prefixes. This runbook helps confirm a
leak in progress and contain it before downstream effects spread.

## Symptoms

- Anomaly engine reports `anomaly_type=bgp` with `top_features` highlighting
  `bgp_unique_peers` and `bgp_announce_count` outside their baseline ranges.
- `/topology/affected?prefix=...` returns a non-empty `affected_links` list
  pointing at peering edges that should not carry the prefix.
- External monitoring (RIPE RIS, BGPMon) flags new origin AS for an internal
  prefix.
- Customers report increased RTT or routing through unexpected ASNs.
- Looking-glass output from upstream providers shows the prefix learned via a
  peer rather than your customer cone.

## Diagnostic Commands

```bash
# Inspect what is actually being announced to each peer
show bgp neighbor 192.0.2.1 advertised-routes
show bgp ipv4 unicast neighbors 192.0.2.1 advertised-routes | begin <prefix>

# Confirm what the upstream world sees
whois -h whois.radb.net <prefix>
curl -s "https://stat.ripe.net/data/looking-glass/data.json?resource=<prefix>"

# Verify outbound policy is attached
show route-policy <export-policy-name>
show bgp neighbor 192.0.2.1 | include "route-map|prefix-list|filter-list"

# Trace propagation in your local RIB
show bgp <prefix> bestpath
show bgp <prefix> received-routes detail
```

## Common Root Causes

1. **Missing or malformed export filter** - `permit any` on an outbound
   policy or a prefix-list that grew during a recent peering change.
2. **AS-path filter inversion** - filter intended to block transit traffic
   accidentally permits it after an operator typo.
3. **Customer announcing more specifics** of provider space, combined with no
   max-prefix limit, leading to those routes being re-exported.
4. **RPKI not enforced** on the announcing side, so invalid routes are
   accepted and propagated.
5. **Recent automation push** rolled out a policy change that applied to the
   wrong neighbor group.

## Remediation Steps

1. Identify the leaking peering session and the specific prefixes from the
   anomaly metadata. Snapshot the current `advertised-routes` output for
   forensics before changing anything.
2. Apply an immediate outbound prefix-list scoped to the offending neighbor
   that drops the leaked prefixes. Avoid global blocks during the
   investigation; targeted filters limit collateral damage.
3. Force a soft outbound clear on the affected neighbor and watch the
   `/topology/affected` endpoint to confirm the unexpected paths disappear.
4. Within the next 30 minutes, audit all outbound policies on the affected
   router, comparing them against the source-of-truth in your IaC repo. Roll
   back any unauthorized change.
5. Notify the receiving peer via your peering coordination channel so they
   know the fix is in flight and can verify on their side.
6. Open an incident review focused on how the leak escaped pre-deploy
   validation. Add a regression test to your CI that lints outbound policies
   against an allow-list of acceptable prefix sources.
7. If RPKI was not enforced, prioritize enabling ROV on the affected sessions
   so future invalid announcements are dropped at ingress.
