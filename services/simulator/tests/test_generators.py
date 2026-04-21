"""Schema-conformance tests for the three generators."""

from __future__ import annotations

import random

import pytest

from netobserv_simulator.generators.bgp import BgpGenerator
from netobserv_simulator.generators.flows import FlowGenerator
from netobserv_simulator.generators.snmp import SnmpGenerator
from netobserv_simulator.generators.topology import (
    ANNOUNCED_PREFIXES,
    BGP_PEERS,
    DESTINATION_IPS,
    DEVICE_IDS,
    INTERFACE_NAMES,
    PROTOCOL_MIX,
    SOURCE_IPS,
)
from netobserv_simulator.models import BgpEvent, SnmpSample, VpcFlowRecord


@pytest.fixture
def rng() -> random.Random:
    return random.Random(1234)


class TestTopology:
    def test_source_ip_count(self) -> None:
        assert len(SOURCE_IPS) == 20

    def test_destination_ip_count(self) -> None:
        assert len(DESTINATION_IPS) == 50

    def test_device_count(self) -> None:
        assert len(DEVICE_IDS) == 10

    def test_protocol_mix_weights_sum_to_100(self) -> None:
        assert sum(p.weight for p in PROTOCOL_MIX) == 100


class TestFlowGenerator:
    def test_generates_valid_vpc_flow_record(self, rng: random.Random) -> None:
        gen = FlowGenerator(account_id="123456789012", rng=rng)
        record = gen.generate()
        assert isinstance(record, VpcFlowRecord)
        assert record.version == 2
        assert record.account_id == "123456789012"
        assert record.srcaddr in SOURCE_IPS
        assert record.dstaddr in DESTINATION_IPS
        assert 0 <= record.srcport <= 65535
        assert record.packets >= 1
        assert record.bytes >= record.packets  # ≥ 1 byte per packet
        assert record.action in {"ACCEPT", "REJECT"}
        assert record.log_status == "OK"
        assert record.start_ts <= record.end_ts

    def test_interface_id_format(self, rng: random.Random) -> None:
        gen = FlowGenerator(account_id="1", interface_id_prefix="eni-", rng=rng)
        record = gen.generate()
        assert record.interface_id.startswith("eni-")
        assert len(record.interface_id) == len("eni-") + 16

    def test_override_srcaddr(self, rng: random.Random) -> None:
        gen = FlowGenerator(account_id="1", rng=rng)
        record = gen.generate(srcaddr="10.99.99.99")
        assert record.srcaddr == "10.99.99.99"

    def test_bytes_multiplier_scales_volume(self, rng: random.Random) -> None:
        gen = FlowGenerator(account_id="1", rng=rng)
        samples_low = [gen.generate(bytes_multiplier=1.0).bytes for _ in range(200)]
        gen2 = FlowGenerator(account_id="1", rng=random.Random(1234))
        samples_high = [gen2.generate(bytes_multiplier=10.0).bytes for _ in range(200)]
        assert sum(samples_high) > sum(samples_low) * 3

    def test_protocol_is_tcp_or_udp(self, rng: random.Random) -> None:
        gen = FlowGenerator(account_id="1", rng=rng)
        for _ in range(50):
            record = gen.generate()
            assert record.protocol in {6, 17}


class TestBgpGenerator:
    def test_generates_valid_event(self, rng: random.Random) -> None:
        gen = BgpGenerator(rng=rng)
        event = gen.generate()
        assert isinstance(event, BgpEvent)
        assert event.peer_ip in BGP_PEERS
        assert event.prefix in ANNOUNCED_PREFIXES
        assert event.event_type in {"ANNOUNCE", "WITHDRAW", "UPDATE"}
        assert len(event.as_path) >= 1

    def test_override_parameters(self, rng: random.Random) -> None:
        gen = BgpGenerator(rng=rng)
        event = gen.generate(
            event_type="WITHDRAW",
            peer_ip="203.0.113.1",
            prefix="10.0.0.0/16",
            as_path=[65999, 65001],
        )
        assert event.event_type == "WITHDRAW"
        assert event.peer_ip == "203.0.113.1"
        assert event.prefix == "10.0.0.0/16"
        assert event.as_path == [65999, 65001]


class TestSnmpGenerator:
    def test_baseline_samples_cover_expected_metrics(self, rng: random.Random) -> None:
        gen = SnmpGenerator(rng=rng)
        samples = gen.baseline_metrics("router-01", "GigabitEthernet0/0")
        assert all(isinstance(s, SnmpSample) for s in samples)
        metrics = {s.metric for s in samples}
        assert metrics == {
            "ifInOctets",
            "ifOutOctets",
            "ifInErrors",
            "ifOutErrors",
            "sysUpTime",
            "cpuLoad",
            "ifUtilizationPct",
        }

    def test_sample_fields(self, rng: random.Random) -> None:
        gen = SnmpGenerator(rng=rng)
        sample = gen.generate(
            device_id="router-01",
            interface="GigabitEthernet0/0",
            metric="cpuLoad",
        )
        assert sample.device_id == "router-01"
        assert sample.interface in INTERFACE_NAMES
        assert sample.metric == "cpuLoad"
        assert isinstance(sample.value, float)
        assert 0.0 <= sample.value <= 100.0  # baseline cpu load range

    def test_override_value(self, rng: random.Random) -> None:
        gen = SnmpGenerator(rng=rng)
        sample = gen.generate(
            device_id="router-02",
            interface="GigabitEthernet0/0",
            metric="ifUtilizationPct",
            override_value=97.5,
        )
        assert sample.value == 97.5
