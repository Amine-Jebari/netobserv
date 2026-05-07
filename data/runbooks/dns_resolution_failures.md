---
title: DNS Resolution Failures
anomaly_type: traffic
severity: warn
tags: [dns, resolution, latency, control-plane]
---

# DNS Resolution Failures

## Summary

DNS issues present as broad-spectrum service failures - applications that
cannot reach upstream APIs, resolvers timing out, or systems flapping
between healthy and unhealthy. The anomaly engine surfaces these indirectly
through traffic anomalies on resolver IPs, sudden drops in DNS query rate,
or latency spikes in flow records on UDP/53 and TCP/53. This runbook
isolates whether the problem is the resolver, the authoritative server,
the cache, or the network in between.

## Symptoms

- Sudden surge of NXDOMAIN responses to a normally-resolvable name.
- DNS query latency p95 above 100 ms when steady-state is under 5 ms.
- Application logs full of `getaddrinfo failed` or
  `Temporary failure in name resolution`.
- Anomaly events on flows to UDP/53 with high source-diversity drops -
  fewer unique clients reaching the resolver.
- TCP/53 fallback ratio rising sharply, indicating UDP truncation or loss.
- DNSSEC validation failures appearing in resolver logs.

## Diagnostic Commands

```bash
# Direct resolver test
dig +trace +nodnssec @<resolver_ip> example.com
dig +short @<resolver_ip> example.com
dig +stats @<resolver_ip> example.com

# Recursive vs authoritative split
dig @<auth_ip> example.com SOA
nslookup -type=ANY example.com <resolver_ip>

# Resolver health
systemctl status unbound
unbound-control stats_noreset | grep -E "queries|recursion|cache"
journalctl -u unbound.service --since "10 min ago"

# Network path
mtr --report --no-dns -c 30 <resolver_ip>
tcpdump -nni any 'port 53'
```

## Common Root Causes

1. **Authoritative server outage** for a specific zone. Other zones
   resolve fine; only that one fails.
2. **Resolver cache poisoning protection** rejecting otherwise-valid
   answers because of mismatched query IDs.
3. **DNSSEC chain-of-trust break** - a parent zone's DS records do not
   match the child's DNSKEY after a rollover.
4. **UDP fragmentation drops** on large DNSSEC responses, forcing TCP
   fallback that some firewalls also drop.
5. **Recursive resolver overloaded** - CPU or memory exhausted, dropping
   queries silently.
6. **Anycast pool drain** that pushed all traffic to a single node which
   then saturated.
7. **Hosts file or systemd-resolved misconfiguration** masking the
   network DNS path on individual clients.

## Remediation Steps

1. Test resolution from at least three vantage points (resolver itself,
   client, and a third-party DNS like 1.1.1.1) against the same name. The
   matrix tells you whether the issue is global, resolver-specific, or
   client-specific.
2. If the authoritative server is unreachable, page the team that owns
   the zone and use `dig` against secondary servers as a workaround.
   Update zone TTLs after the fix to limit blast radius for the next
   outage.
3. For DNSSEC failures, run `delv +cd example.com` to bypass validation.
   If that succeeds, the chain of trust is broken upstream - fall back
   to the parent's DS records or coordinate a rollover with the operator.
4. For UDP fragmentation drops, set the resolver's
   `edns-buffer-size` to 1232 to keep responses inside a single
   un-fragmented IPv6 packet, then verify TCP/53 also works through any
   firewall on the path.
5. For overloaded resolvers, scale out the resolver pool. Anycast lets
   you add capacity without changing client configuration.
6. After the fix, run a synthetic dig probe every 30 seconds for an hour
   and confirm zero NXDOMAIN or SERVFAIL responses.
7. Capture the timing in your incident review and add an early-warning
   metric for query rate or NXDOMAIN ratio so future events alert sooner.
