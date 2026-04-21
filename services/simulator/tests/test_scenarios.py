"""Tests for the scenario engine: injection, expiry, and parameter shape."""

from __future__ import annotations

import random
import time

import pytest

from netobserv_simulator.generators.topology import (
    ANNOUNCED_PREFIXES,
    BGP_PEERS,
    DEVICE_IDS,
    SOURCE_IPS,
)
from netobserv_simulator.scenarios import (
    SCENARIO_DURATIONS,
    SCHEDULED_ROTATION,
    ScenarioEngine,
)


@pytest.fixture
def rng() -> random.Random:
    return random.Random(42)


def test_no_active_scenario_before_interval(rng: random.Random) -> None:
    engine = ScenarioEngine(
        mode="scheduled", configured="normal", interval_minutes=15, rng=rng
    )
    assert engine.active is None
    # Tick immediately — not enough time has passed.
    assert engine.tick(now=time.time()) is None
    assert engine.active is None


def test_scheduled_injection_after_interval(rng: random.Random) -> None:
    engine = ScenarioEngine(
        mode="scheduled", configured="normal", interval_minutes=15, rng=rng
    )
    future = time.time() + 15 * 60 + 1
    injected = engine.tick(now=future)
    assert injected is not None
    assert injected.name == SCHEDULED_ROTATION[0]


def test_scheduled_rotation_cycles(rng: random.Random) -> None:
    engine = ScenarioEngine(
        mode="scheduled", configured="normal", interval_minutes=1, rng=rng
    )
    start = time.time()
    picked = []
    for i in range(1, len(SCHEDULED_ROTATION) * 2 + 1):
        # Wind forward by one interval each loop, plus enough to clear the
        # previous scenario's duration so a fresh one can fire.
        t = start + i * (60 + max(SCENARIO_DURATIONS.values()) + 1)
        injected = engine.tick(now=t)
        if injected is not None:
            picked.append(injected.name)
    # Must have hit every scenario at least once.
    assert set(picked) >= set(SCHEDULED_ROTATION)


def test_single_mode_always_picks_configured(rng: random.Random) -> None:
    engine = ScenarioEngine(
        mode="single", configured="bgp_flap", interval_minutes=1, rng=rng
    )
    injected = engine.tick(now=time.time() + 61)
    assert injected is not None
    assert injected.name == "bgp_flap"


def test_random_mode_stays_within_rotation(rng: random.Random) -> None:
    engine = ScenarioEngine(
        mode="random", configured="normal", interval_minutes=1, rng=rng
    )
    start = time.time()
    for i in range(1, 15):
        t = start + i * (60 + max(SCENARIO_DURATIONS.values()) + 1)
        injected = engine.tick(now=t)
        if injected is not None:
            assert injected.name in SCHEDULED_ROTATION


def test_bgp_flap_params(rng: random.Random) -> None:
    engine = ScenarioEngine(
        mode="single", configured="bgp_flap", interval_minutes=1, rng=rng
    )
    injected = engine.tick(now=time.time() + 61)
    assert injected is not None
    assert injected.params["prefix"] in ANNOUNCED_PREFIXES
    assert injected.params["peer_ip"] in BGP_PEERS


def test_traffic_spike_params(rng: random.Random) -> None:
    engine = ScenarioEngine(
        mode="single", configured="traffic_spike", interval_minutes=1, rng=rng
    )
    injected = engine.tick(now=time.time() + 61)
    assert injected is not None
    assert injected.params["source_ip"] in SOURCE_IPS
    assert injected.params["multiplier"] == 10.0


def test_route_leak_has_bogus_as_path(rng: random.Random) -> None:
    engine = ScenarioEngine(
        mode="single", configured="route_leak", interval_minutes=1, rng=rng
    )
    injected = engine.tick(now=time.time() + 61)
    assert injected is not None
    assert injected.params["prefix"] in ANNOUNCED_PREFIXES
    assert isinstance(injected.params["as_path"], list)
    assert injected.params["as_path"][0] >= 65000  # bogus AS range


def test_link_saturation_targets_device(rng: random.Random) -> None:
    engine = ScenarioEngine(
        mode="single", configured="link_saturation", interval_minutes=1, rng=rng
    )
    injected = engine.tick(now=time.time() + 61)
    assert injected is not None
    assert injected.params["device_id"] in DEVICE_IDS
    assert injected.params["utilization_pct"] >= 95.0


def test_latency_spike_has_elevated_value(rng: random.Random) -> None:
    engine = ScenarioEngine(
        mode="single", configured="latency_spike", interval_minutes=1, rng=rng
    )
    injected = engine.tick(now=time.time() + 61)
    assert injected is not None
    assert injected.params["device_id"] in DEVICE_IDS
    assert float(injected.params["latency_ms"]) >= 100.0


def test_scenario_expires_after_duration(rng: random.Random) -> None:
    engine = ScenarioEngine(
        mode="single", configured="bgp_flap", interval_minutes=1, rng=rng
    )
    t0 = time.time() + 61
    injected = engine.tick(now=t0)
    assert injected is not None
    assert engine.active is not None
    # Force expiry by setting ends_at to the past — .active should then clear it.
    assert engine._active is not None
    engine._active.ends_at = 0.0
    assert engine.active is None
