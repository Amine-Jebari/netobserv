"""Anomaly scenario engine.

Each scenario mutates the data stream for a fixed duration. Between scenarios
the simulator emits only baseline traffic. Scenarios are composable: the active
scenario is consulted by each generator-emission site.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .config import ScenarioName
from .generators.topology import ANNOUNCED_PREFIXES, BGP_PEERS, DEVICE_IDS, SOURCE_IPS

if TYPE_CHECKING:
    pass


# Duration in seconds that each scenario stays active once injected.
SCENARIO_DURATIONS: dict[ScenarioName, int] = {
    "normal": 0,
    "bgp_flap": 120,
    "traffic_spike": 300,
    "route_leak": 90,
    "link_saturation": 180,
    "latency_spike": 180,
}

# Scenarios rotated in scheduled mode (order matters — round-robin).
SCHEDULED_ROTATION: tuple[ScenarioName, ...] = (
    "bgp_flap",
    "traffic_spike",
    "link_saturation",
    "latency_spike",
    "route_leak",
)


@dataclass
class ActiveScenario:
    """An in-flight anomaly — carries its parameters and expiry timestamp."""

    name: ScenarioName
    ends_at: float
    params: dict[str, object] = field(default_factory=dict)

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.ends_at


class ScenarioEngine:
    """Owns the active scenario and schedules anomaly injections."""

    def __init__(
        self,
        mode: str,
        configured: ScenarioName,
        interval_minutes: int,
        rng: random.Random | None = None,
    ) -> None:
        self._mode = mode
        self._configured = configured
        self._interval_s = interval_minutes * 60
        self._rng = rng or random.Random()
        self._active: ActiveScenario | None = None
        self._rotation_index = 0
        # First injection happens one interval after startup.
        self._next_injection_at = time.time() + self._interval_s

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def active(self) -> ActiveScenario | None:
        """Return the live scenario, clearing it if it has expired."""

        if self._active is not None and self._active.expired():
            self._active = None
        return self._active

    def tick(self, now: float | None = None) -> ActiveScenario | None:
        """Advance the scheduler; returns a newly injected scenario if any."""

        current = now or time.time()
        # Expire active scenario.
        if self._active is not None and self._active.expired(current):
            self._active = None

        if self._active is None and current >= self._next_injection_at:
            new = self._pick_next_scenario()
            self._active = self._build_active(new, current)
            self._next_injection_at = current + self._interval_s
            return self._active
        return None

    # ── scheduling ────────────────────────────────────────────────────────────

    def _pick_next_scenario(self) -> ScenarioName:
        if self._mode == "single":
            return self._configured
        if self._mode == "random":
            return self._rng.choice(SCHEDULED_ROTATION)
        # scheduled — round-robin through the rotation.
        choice = SCHEDULED_ROTATION[self._rotation_index % len(SCHEDULED_ROTATION)]
        self._rotation_index += 1
        return choice

    def _build_active(self, name: ScenarioName, now: float) -> ActiveScenario:
        duration = SCENARIO_DURATIONS[name]
        params: dict[str, object] = {}

        if name == "bgp_flap":
            params["prefix"] = self._rng.choice(ANNOUNCED_PREFIXES)
            params["peer_ip"] = self._rng.choice(BGP_PEERS)
        elif name == "traffic_spike":
            params["source_ip"] = self._rng.choice(SOURCE_IPS)
            params["multiplier"] = 10.0
        elif name == "route_leak":
            params["prefix"] = self._rng.choice(ANNOUNCED_PREFIXES)
            # A bogus AS path that should not normally advertise this prefix.
            params["as_path"] = [65999, 65998, 65997, 65001]
            params["peer_ip"] = self._rng.choice(BGP_PEERS)
        elif name == "link_saturation":
            params["device_id"] = self._rng.choice(DEVICE_IDS)
            params["utilization_pct"] = 97.0
        elif name == "latency_spike":
            params["device_id"] = self._rng.choice(DEVICE_IDS)
            params["latency_ms"] = 450.0

        return ActiveScenario(
            name=name,
            ends_at=now + max(duration, 1),
            params=params,
        )
