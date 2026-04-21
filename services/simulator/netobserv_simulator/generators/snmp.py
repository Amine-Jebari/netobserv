"""SNMP metric sample generator."""

from __future__ import annotations

import random
from datetime import UTC, datetime

from ..models import SnmpMetricName, SnmpSample
from .topology import DEVICE_IDS, INTERFACE_NAMES


_BASELINE_RANGES: dict[SnmpMetricName, tuple[float, float]] = {
    "ifInOctets": (1_000_000.0, 50_000_000.0),
    "ifOutOctets": (1_000_000.0, 50_000_000.0),
    "ifInErrors": (0.0, 5.0),
    "ifOutErrors": (0.0, 5.0),
    "sysUpTime": (100_000.0, 10_000_000.0),
    "cpuLoad": (5.0, 35.0),
    "ifUtilizationPct": (5.0, 60.0),
    "latencyMs": (0.5, 15.0),
}


class SnmpGenerator:
    """Generates per-device, per-interface SNMP samples."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._uptime_base = {d: self._rng.randint(100_000, 10_000_000) for d in DEVICE_IDS}

    @property
    def devices(self) -> tuple[str, ...]:
        return DEVICE_IDS

    @property
    def interfaces(self) -> tuple[str, ...]:
        return INTERFACE_NAMES

    def _baseline_value(self, metric: SnmpMetricName, device_id: str) -> float:
        if metric == "sysUpTime":
            return float(self._uptime_base[device_id])
        lo, hi = _BASELINE_RANGES[metric]
        return self._rng.uniform(lo, hi)

    def generate(
        self,
        *,
        device_id: str,
        interface: str,
        metric: SnmpMetricName,
        override_value: float | None = None,
    ) -> SnmpSample:
        value = (
            override_value
            if override_value is not None
            else self._baseline_value(metric, device_id)
        )
        return SnmpSample(
            timestamp=datetime.now(tz=UTC),
            device_id=device_id,
            interface=interface,
            metric=metric,
            value=float(value),
        )

    def baseline_metrics(self, device_id: str, interface: str) -> list[SnmpSample]:
        """Return the standard set of samples emitted per device per polling interval."""

        metrics: tuple[SnmpMetricName, ...] = (
            "ifInOctets",
            "ifOutOctets",
            "ifInErrors",
            "ifOutErrors",
            "sysUpTime",
            "cpuLoad",
            "ifUtilizationPct",
        )
        return [
            self.generate(device_id=device_id, interface=interface, metric=m)
            for m in metrics
        ]
