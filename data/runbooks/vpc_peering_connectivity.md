---
title: VPC Peering Connectivity
anomaly_type: traffic
severity: warn
tags: [vpc, peering, cloud, connectivity, transit-gateway]
---

# VPC Peering Connectivity

## Summary

Cloud VPC peering and transit-gateway attachments are easy to misconfigure
because the data plane works only when route tables, security groups, and
network ACLs align across both VPCs. A peering failure usually presents as
"works from VPC A to VPC B but not from B to A" or as a partial-overlap
failure where one subnet inside the VPC cannot reach the peer. This runbook
isolates whether the failure is in routing, filtering, or DNS resolution.

## Symptoms

- Anomaly events on flows showing forward traffic from VPC A to VPC B with
  no return traffic visible.
- Application timeouts that disappear when you bypass peering with a public
  endpoint.
- Reachability differs depending on which subnet the source instance is in.
- New attachments work intermittently for the first few minutes, then stop.
- Cross-region peering hits sustained packet loss that does not exist on
  same-region peerings.

## Diagnostic Commands

```bash
# AWS - inventory peering and routes
aws ec2 describe-vpc-peering-connections --filters "Name=status-code,Values=active"
aws ec2 describe-route-tables --filters "Name=vpc-id,Values=<vpc>"
aws ec2 describe-transit-gateway-route-tables
aws ec2 describe-transit-gateway-attachments

# Verify security group allows the traffic
aws ec2 describe-security-groups --group-ids <sg>
aws ec2 describe-network-acls --filters "Name=vpc-id,Values=<vpc>"

# Reachability Analyzer (use sparingly - paid)
aws ec2 create-network-insights-path --source <eni> --destination <eni> \
  --protocol tcp --destination-port 443
aws ec2 start-network-insights-analysis --network-insights-path-id <id>

# Inside the instance
ip route get <peer_cidr>
ss -ti dst <peer_ip>:443
mtr --report -c 30 <peer_ip>
```

## Common Root Causes

1. **Route table on only one side** - the peering connection accepts the
   route but the target VPC's route table never adds it.
2. **Security group with stale CIDR references** that excludes the peer
   address space.
3. **Network ACL** denying ephemeral return ports because of asymmetric
   configuration between subnets.
4. **CIDR overlap** between VPCs preventing the peering from being
   established at all, or causing local routes to win over peering routes.
5. **Transit gateway propagation disabled** for the route table, requiring
   static routes that nobody added.
6. **DNS resolution disabled** for the peering, so private hostnames
   resolve to public addresses that hit the internet path.
7. **Cross-region peering MTU** of 1500 versus same-region 8500, causing
   surprise fragmentation drops on jumbo frames.

## Remediation Steps

1. Establish whether forward and return paths both exist. Run a packet
   capture on both sides simultaneously - if one direction does not see
   the packet, the failure is on that side.
2. Audit the four control-plane layers in order: peering status, route
   table, security group, network ACL. The first mismatch is almost
   always the cause.
3. For missing routes, add the peer CIDR explicitly. For transit gateways,
   verify route propagation is on for the relevant attachment, or add a
   static route.
4. For overlapping CIDRs, the only durable answer is to renumber one
   side. As an emergency workaround, NAT through a transit VPC to give
   the destination a non-overlapping address.
5. For DNS, enable cross-VPC DNS resolution on the peering and verify
   private hosted zones are associated with both VPCs.
6. For cross-region MTU surprises, enable TCP MSS clamping on the egress
   security appliances or rely on PMTUD by allowing ICMP fragmentation
   needed messages through the security groups.
7. After the fix, run synthetic probes from every subnet in both VPCs to
   build a full reachability matrix. Save the matrix as a regression
   test for future infrastructure changes.
