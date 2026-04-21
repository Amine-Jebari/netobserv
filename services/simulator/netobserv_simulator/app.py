"""Main simulator application — orchestrates generators, scenarios, and Kafka."""

from __future__ import annotations

import asyncio
import random
import signal
import time
from contextlib import suppress
from typing import cast

import uvicorn

from .config import Settings, get_settings
from .generators.bgp import BgpGenerator
from .generators.flows import FlowGenerator
from .generators.snmp import SnmpGenerator
from .generators.topology import BGP_PEERS, SOURCE_IPS
from .health import build_health_app
from .logging_setup import configure_logging, get_logger
from .models import SnmpMetricName
from .producer import TelemetryProducer
from .scenarios import ActiveScenario, ScenarioEngine

log = get_logger(__name__)


class Simulator:
    """Runs the producer, scenario engine, and all three emission loops."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._rng = random.Random(settings.random_seed)
        self._producer = TelemetryProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            client_id=settings.kafka_client_id,
        )
        self._flows = FlowGenerator(
            account_id=settings.account_id,
            interface_id_prefix=settings.interface_id_prefix,
            rng=self._rng,
        )
        self._bgp = BgpGenerator(rng=self._rng)
        self._snmp = SnmpGenerator(rng=self._rng)
        self._scenarios = ScenarioEngine(
            mode=settings.scenario_mode,
            configured=settings.scenario,
            interval_minutes=settings.anomaly_interval_minutes,
            rng=self._rng,
        )
        self._stop_event = asyncio.Event()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._producer.start()

    async def stop(self) -> None:
        self._stop_event.set()
        await self._producer.stop()

    def request_stop(self) -> None:
        self._stop_event.set()

    def active_scenario_name(self) -> str | None:
        active = self._scenarios.active
        return active.name if active else None

    # ── emission loops ────────────────────────────────────────────────────────

    async def _flow_loop(self) -> None:
        rate = self._settings.producer_rate
        batch_interval = 0.1  # emit 10 batches per second
        per_batch = max(1, int(rate * batch_interval))
        topic = self._settings.kafka_topic_flows

        while not self._stop_event.is_set():
            start = time.perf_counter()
            active = self._scenarios.active

            batch_size = per_batch
            for _ in range(batch_size):
                record = self._flow_record_for(active)
                await self._producer.send(topic, record, key=record.srcaddr)

            if active and active.name == "traffic_spike":
                spike_extra = int(per_batch * 9)  # 10x total (1x baseline + 9x)
                src = cast(str, active.params.get("source_ip"))
                for _ in range(spike_extra):
                    record = self._flows.generate(srcaddr=src, bytes_multiplier=1.0)
                    await self._producer.send(topic, record, key=record.srcaddr)

            elapsed = time.perf_counter() - start
            await asyncio.sleep(max(0.0, batch_interval - elapsed))

    def _flow_record_for(self, active: ActiveScenario | None):
        if active and active.name == "traffic_spike":
            src = cast(str, active.params.get("source_ip"))
            return self._flows.generate(srcaddr=src)
        return self._flows.generate()

    async def _bgp_loop(self) -> None:
        topic = self._settings.kafka_topic_bgp
        lo = self._settings.bgp_events_per_minute_min
        hi = self._settings.bgp_events_per_minute_max

        while not self._stop_event.is_set():
            active = self._scenarios.active

            if active and active.name == "bgp_flap":
                # Rapid ANNOUNCE/WITHDRAW pair every ~2 seconds.
                prefix = cast(str, active.params["prefix"])
                peer = cast(str, active.params["peer_ip"])
                ann = self._bgp.generate(event_type="ANNOUNCE", peer_ip=peer, prefix=prefix)
                await self._producer.send(topic, ann, key=prefix)
                log.warning(
                    "bgp.flap_event",
                    bgp_event="ANNOUNCE",
                    prefix=prefix,
                    peer_ip=peer,
                )
                await asyncio.sleep(1.0)
                wd = self._bgp.generate(event_type="WITHDRAW", peer_ip=peer, prefix=prefix)
                await self._producer.send(topic, wd, key=prefix)
                log.warning(
                    "bgp.flap_event",
                    bgp_event="WITHDRAW",
                    prefix=prefix,
                    peer_ip=peer,
                )
                await asyncio.sleep(1.0)
                continue

            if active and active.name == "route_leak":
                prefix = cast(str, active.params["prefix"])
                peer = cast(str, active.params["peer_ip"])
                as_path = cast(list[int], active.params["as_path"])
                leak = self._bgp.generate(
                    event_type="ANNOUNCE",
                    peer_ip=peer,
                    prefix=prefix,
                    as_path=as_path,
                )
                await self._producer.send(topic, leak, key=prefix)
                log.warning(
                    "bgp.route_leak_event",
                    prefix=prefix,
                    peer_ip=peer,
                    as_path=as_path,
                )

            # Normal cadence: lo..hi events per minute.
            events_this_minute = self._rng.randint(lo, hi)
            interval = 60.0 / max(events_this_minute, 1)
            for _ in range(events_this_minute):
                if self._stop_event.is_set():
                    break
                event = self._bgp.generate()
                await self._producer.send(topic, event, key=event.prefix)
                await asyncio.sleep(interval)

    async def _snmp_loop(self) -> None:
        topic = self._settings.kafka_topic_snmp
        interval = self._settings.snmp_interval_seconds

        while not self._stop_event.is_set():
            active = self._scenarios.active

            for device in self._snmp.devices:
                for iface in self._snmp.interfaces:
                    samples = self._snmp.baseline_metrics(device, iface)
                    # Link saturation: override utilization + octets for target device.
                    if active and active.name == "link_saturation":
                        target = cast(str, active.params.get("device_id"))
                        if device == target:
                            samples = self._apply_link_saturation(device, iface, samples)

                    # Latency spike: emit an extra latencyMs sample.
                    if active and active.name == "latency_spike":
                        target = cast(str, active.params.get("device_id"))
                        if device == target:
                            latency = cast(float, active.params["latency_ms"])
                            samples.append(
                                self._snmp.generate(
                                    device_id=device,
                                    interface=iface,
                                    metric="latencyMs",
                                    override_value=latency,
                                )
                            )
                    else:
                        samples.append(
                            self._snmp.generate(
                                device_id=device,
                                interface=iface,
                                metric="latencyMs",
                            )
                        )

                    for s in samples:
                        await self._producer.send(topic, s, key=f"{device}:{iface}")

            # Sleep until next poll, but wake promptly on shutdown.
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=float(interval))

    def _apply_link_saturation(self, device: str, iface: str, samples):
        util_pct = 97.0
        rebuilt = []
        for s in samples:
            if s.metric == "ifUtilizationPct":
                rebuilt.append(
                    self._snmp.generate(
                        device_id=device,
                        interface=iface,
                        metric="ifUtilizationPct",
                        override_value=util_pct,
                    )
                )
            elif s.metric in ("ifInOctets", "ifOutOctets"):
                rebuilt.append(
                    self._snmp.generate(
                        device_id=device,
                        interface=iface,
                        metric=cast(SnmpMetricName, s.metric),
                        override_value=s.value * 5.0,
                    )
                )
            else:
                rebuilt.append(s)
        return rebuilt

    async def _scenario_loop(self) -> None:
        """Ticks the scenario engine every second; logs injections prominently."""

        while not self._stop_event.is_set():
            injected = self._scenarios.tick()
            if injected is not None:
                log.warning(
                    "scenario.injected",
                    scenario=injected.name,
                    duration_s=int(injected.ends_at - time.time()),
                    params=injected.params,
                )
            await asyncio.sleep(1.0)

    async def run_emission(self) -> None:
        """Run all emission loops concurrently until stopped."""

        await asyncio.gather(
            self._scenario_loop(),
            self._flow_loop(),
            self._bgp_loop(),
            self._snmp_loop(),
        )


async def _amain() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    log.info(
        "simulator.starting",
        kafka=settings.kafka_bootstrap_servers,
        producer_rate=settings.producer_rate,
        scenario_mode=settings.scenario_mode,
        anomaly_interval_minutes=settings.anomaly_interval_minutes,
    )

    sim = Simulator(settings)
    await sim.start()

    # Health endpoint runs alongside the emission loops.
    health_app = build_health_app(sim._producer, sim.active_scenario_name)
    uvicorn_config = uvicorn.Config(
        health_app,
        host=settings.health_host,
        port=settings.health_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)

    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        log.info("simulator.shutdown_signal")
        sim.request_stop()
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    try:
        await asyncio.gather(
            sim.run_emission(),
            server.serve(),
        )
    finally:
        await sim.stop()
        log.info("simulator.stopped")


def run() -> None:
    """Console-script entry point."""

    asyncio.run(_amain())


if __name__ == "__main__":
    run()
