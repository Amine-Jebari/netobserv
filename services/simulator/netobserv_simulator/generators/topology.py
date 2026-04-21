"""Fixed simulated topology: source IPs, destinations, devices, prefixes.

A stable topology is important so anomaly detectors can learn baselines.
"""

from __future__ import annotations

from dataclasses import dataclass

# 20 source IPs across three internal subnets.
SOURCE_IPS: tuple[str, ...] = tuple(
    [f"10.0.1.{i}" for i in range(10, 17)]
    + [f"10.0.2.{i}" for i in range(10, 17)]
    + [f"10.0.3.{i}" for i in range(10, 16)]
)

# 50 destinations — mix of public services, CDN ranges, and upstream peers.
_PUBLIC_DESTS = [
    "8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1",
    "140.82.114.3", "140.82.114.4",
    "151.101.1.69", "151.101.65.69", "151.101.129.69",
    "52.84.150.1", "52.84.150.2", "54.230.100.50",
    "104.16.132.229", "104.16.133.229",
    "172.217.164.110", "142.250.80.46",
    "13.32.110.50", "13.33.100.50",
    "157.240.22.35", "157.240.241.35",
    "20.76.201.171", "20.112.52.29",
    "185.199.108.153", "185.199.109.153", "185.199.110.153",
    "199.232.46.132", "199.232.45.132",
    "17.253.144.10", "17.253.145.10",
]

_INTERNAL_DESTS = [f"10.0.{o}.{h}" for o in (4, 5, 6) for h in (10, 20, 30, 40, 50)]

_PEER_DESTS = [
    "192.0.2.10", "192.0.2.11", "198.51.100.25", "203.0.113.45",
    "172.217.0.1", "74.125.0.1",
]

DESTINATION_IPS: tuple[str, ...] = tuple(
    (_PUBLIC_DESTS + _INTERNAL_DESTS + _PEER_DESTS)[:50]
)

# 10 simulated devices with 3 interfaces each.
DEVICE_IDS: tuple[str, ...] = tuple(f"router-{i:02d}" for i in range(1, 11))
INTERFACE_NAMES: tuple[str, ...] = ("GigabitEthernet0/0", "GigabitEthernet0/1", "TenGigE0/2")

# BGP peers and the prefixes each announces.
BGP_PEERS: tuple[str, ...] = (
    "192.0.2.1",
    "192.0.2.2",
    "198.51.100.1",
    "203.0.113.1",
)

ANNOUNCED_PREFIXES: tuple[str, ...] = (
    "10.0.0.0/16",
    "10.1.0.0/16",
    "172.16.0.0/12",
    "203.0.113.0/24",
    "198.51.100.0/24",
    "192.0.2.0/24",
)

# AS paths used in normal operation — each peer has a preferred path.
NORMAL_AS_PATHS: tuple[tuple[int, ...], ...] = (
    (65001, 65100, 65200),
    (65002, 65101, 65201),
    (65003, 65102, 65202),
    (65004, 65103, 65203),
)

# Classic protocol/port mix (realistic internal-outbound traffic).
# Weights sum to 100 so a weighted pick over this list stays interpretable.
@dataclass(frozen=True)
class ProtocolMix:
    name: str
    protocol: int
    dst_port: int
    weight: int


PROTOCOL_MIX: tuple[ProtocolMix, ...] = (
    ProtocolMix("HTTPS", 6, 443, 55),
    ProtocolMix("HTTP", 6, 80, 15),
    ProtocolMix("DNS", 17, 53, 15),
    ProtocolMix("SSH", 6, 22, 5),
    ProtocolMix("BGP", 6, 179, 4),
    ProtocolMix("NTP", 17, 123, 3),
    ProtocolMix("QUIC", 17, 443, 3),
)


def interface_for(device_id: str, index: int = 0) -> str:
    """Return a deterministic interface name for a device."""

    return INTERFACE_NAMES[index % len(INTERFACE_NAMES)]
