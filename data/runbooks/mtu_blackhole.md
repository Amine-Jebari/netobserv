---
title: MTU Blackhole Diagnosis
anomaly_type: traffic
severity: warn
tags: [mtu, pmtud, fragmentation, tunnel]
---

# MTU Blackhole Diagnosis

## Summary

An MTU blackhole occurs when packets larger than the path MTU are silently
dropped because Path MTU Discovery (PMTUD) is broken between the source and
destination. Small packets pass; large packets vanish. Symptoms look
nothing like classical packet loss because TCP handshake completes (small
packets) before the data transfer hangs (large segments). This runbook
isolates blackholes on tunnels, cloud overlays, and inter-AS paths.

## Symptoms

- TCP three-way handshake succeeds, but the connection stalls during data
  transfer.
- Truncated flow records that abruptly stop without a graceful close.
- Application timeouts that disappear when the client uses a smaller
  request body.
- ICMP "fragmentation needed" packets dropped somewhere on the return path,
  evident from `iptables -L -nv` counters or upstream firewall logs.
- TLS handshake stalls after `ClientHello` because the server's certificate
  message exceeds the path MTU.

## Diagnostic Commands

```bash
# Probe path MTU directly
tracepath <dst>
ping -M do -s 1472 -c 5 <dst>     # 1472 + 28 = 1500
ping -M do -s 1372 -c 5 <dst>     # narrow down

# Confirm where ICMP-needed is being dropped
tcpdump -nni any 'icmp[icmptype]==3 and icmp[icmpcode]==4'

# Check tunnel and overlay configuration
ip link show <tunnel_if>
ip -d link show <tunnel_if>      # look for IFLA_MTU
ip route show table all | grep <prefix>

# Cisco / Juniper interface MTU
show interfaces <if> | include MTU
show interfaces <tunnel_if> | display set | match mtu
```

## Common Root Causes

1. **GRE / IPSec / VXLAN tunnel encapsulation** reduces effective payload
   below 1500, but neither end clamps TCP MSS.
2. **Firewall blocks ICMP type 3 code 4** on the return path, breaking
   PMTUD entirely.
3. **Jumbo frames enabled on one end** of a peering, but the other end
   cannot accept frames larger than 1500.
4. **PPPoE access circuits** with the typical 1492 MTU and downstream
   tunnels that further reduce the effective MTU.
5. **Cloud overlay** (AWS Transit Gateway, Azure VNet Gateway) imposing a
   sub-1500 path MTU that the application stack does not detect.
6. **Asymmetric routing** combined with a one-way ICMP filter, so the
   smaller-MTU hop only exists in one direction.

## Remediation Steps

1. Confirm the blackhole by probing path MTU with `ping -M do -s` while
   stepping the size down by 8 byte increments. Record the largest size
   that succeeds.
2. Compare the discovered path MTU to the configured tunnel MTU and
   interface MTU on every hop you control. Lower the tunnel MTU on the
   ingress side until it matches the bottleneck.
3. Enable TCP MSS clamping on the tunnel ingress
   (`iptables -t mangle -A FORWARD ... --set-mss <path_mtu - 40>` on
   Linux, `ip tcp adjust-mss <value>` on Cisco) so TCP sessions advertise
   a small enough MSS regardless of what PMTUD does.
4. Make sure ICMP type 3 code 4 is permitted at every firewall on the
   return path. Killing PMTUD is a common misconfig in security
   hardening templates.
5. If the bottleneck is in a cloud overlay you do not control, use the
   provider's recommended MTU for the overlay (1500 minus their overhead)
   and adjust your interface MTU upstream of the overlay accordingly.
6. After the fix, re-run the probe and verify that 1500 byte packets
   succeed. Watch the anomaly stream for a 30 minute window.
7. Add automated synthetic probes that send 1500 byte payloads every five
   minutes to alert quickly on future blackholes.
