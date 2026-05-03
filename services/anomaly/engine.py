"""Real-time anomaly detection engine (Phase 3.2).

Long-lived service that:

  1. Loads the latest model bundle from S3/MinIO at startup (waits for it if
     the baseline trainer has not run yet).
  2. Consumes ``network.flows`` / ``network.bgp`` / ``network.snmp`` and
     buffers each stream in fixed-duration windows.
  3. At the end of every window, computes per-stream features (using the same
     :mod:`features` module the trainer used) and scores them against the
     three loaded PyOD models.
  4. Maps raw decision-function scores onto a unified 0-1 scale, applies the
     INFO/WARN/CRITICAL thresholds, and emits enriched anomaly events to the
     ``anomalies.scored`` Kafka topic.
  5. For CRITICAL events, additionally posts a Slack webhook so the on-call
     channel still surfaces issues even if the downstream agent service is
     unavailable.

Run from the repo root:

    python -m services.anomaly.engine

The same module exposes a small FastAPI app for ``/health``, ``/metrics``,
``/stats``, and ``POST /reload-model`` (hot model reload without a restart).
"""

from __future__ import annotations

import asyncio
import signal
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import Settings, get_settings
from .engine_metrics import EngineMetrics
from .kafka_io import JsonProducer, StreamConsumer
from .logging_setup import configure_logging, get_logger
from .model_fetch import (
    download_bundle,
    make_client_from_settings,
    wait_for_bundle,
)
from .scorer import ScoredAnomaly, Scorer
from .slack_alerter import SlackAlerter
from .topology_client import TopologyClient
from .windowing import (
    STREAM_BGP,
    STREAM_FLOWS,
    STREAM_SNMP,
    WindowBatch,
    WindowBuffer,
)

log = get_logger(__name__)


# ── Topic ↔ stream wiring ────────────────────────────────────────────────────

# Returns the stream key for a given topic — set up once per engine instance
# so consumer setup stays declarative.
def _stream_for_topic(settings: Settings, topic: str) -> str | None:
    if topic == settings.kafka_topic_flows:
        return STREAM_FLOWS
    if topic == settings.kafka_topic_bgp:
        return STREAM_BGP
    if topic == settings.kafka_topic_snmp:
        return STREAM_SNMP
    return None


# ── Engine ───────────────────────────────────────────────────────────────────


class AnomalyEngine:
    """Owns every long-lived resource the service needs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._metrics = EngineMetrics()
        self._scorer = Scorer(
            info_threshold=settings.info_threshold,
            warn_threshold=settings.warn_threshold,
            critical_threshold=settings.critical_threshold,
        )
        self._buffer = WindowBuffer(
            window_seconds=settings.window_seconds,
            on_flush=self._on_window_flush,
        )
        self._topology = TopologyClient(
            base_url=settings.topology_service_url,
            timeout_seconds=settings.topology_request_timeout_seconds,
        )
        self._slack = SlackAlerter(
            webhook_url=settings.slack_webhook_url,
            timeout_seconds=settings.slack_request_timeout_seconds,
        )
        self._producer = JsonProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            client_id="netobserv-anomaly-producer",
        )
        self._consumers: list[StreamConsumer] = []
        self._s3_client = make_client_from_settings(settings)
        self._reload_lock = asyncio.Lock()

    # ── Properties for HTTP layer ────────────────────────────────────────────

    @property
    def metrics(self) -> EngineMetrics:
        return self._metrics

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def model_loaded(self) -> bool:
        return self._scorer.loaded

    def health_probe(self) -> dict[str, Any]:
        return {
            "model_loaded": self._scorer.loaded,
            "consumers": {c.topic: c.connected for c in self._consumers},
            "kafka_producer": self._producer.connected,
            "slack_enabled": self._slack.enabled,
        }

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self._wait_and_load_initial_model()
        await self._topology.start()
        await self._slack.start()
        await self._producer.start()
        await self._buffer.start()

        prefix = self._settings.kafka_consumer_group_prefix
        topics_streams = (
            (self._settings.kafka_topic_flows, STREAM_FLOWS),
            (self._settings.kafka_topic_bgp, STREAM_BGP),
            (self._settings.kafka_topic_snmp, STREAM_SNMP),
        )
        for topic, stream in topics_streams:
            consumer = StreamConsumer(
                topic=topic,
                bootstrap_servers=self._settings.kafka_bootstrap_servers,
                group_id=f"{prefix}-{stream}",
                handler=self._make_consumer_handler(stream),
                auto_offset_reset=self._settings.kafka_auto_offset_reset,
            )
            await consumer.start()
            self._consumers.append(consumer)

    async def stop(self) -> None:
        for consumer in self._consumers:
            await consumer.stop()
        self._consumers.clear()
        await self._buffer.stop()
        await self._producer.stop()
        await self._slack.stop()
        await self._topology.stop()

    # ── Model handling ───────────────────────────────────────────────────────

    async def _wait_and_load_initial_model(self) -> None:
        """Block startup until the trainer has produced a bundle."""

        ok = await wait_for_bundle(
            s3_client=self._s3_client,
            bucket=self._settings.s3_bucket,
            key=self._settings.model_key,
            poll_seconds=self._settings.model_wait_poll_seconds,
            max_seconds=self._settings.model_wait_max_seconds,
        )
        if not ok:
            raise RuntimeError(
                f"timed out waiting for model bundle s3://{self._settings.s3_bucket}/{self._settings.model_key}"
            )
        await self._load_latest_locked()

    async def reload_model(self) -> dict[str, Any]:
        """Public entry point for ``POST /reload-model``."""

        async with self._reload_lock:
            await self._load_latest_locked()
        bundle = self._scorer.bundle
        return {
            "loaded_at": bundle.loaded_at.isoformat() if bundle else None,
            "models": sorted(bundle.streams.keys()) if bundle else [],
        }

    async def _load_latest_locked(self) -> None:
        loop = asyncio.get_running_loop()
        with tempfile.TemporaryDirectory(prefix="anomaly-engine-") as tmp:
            dest = Path(tmp) / "bundle.tar.gz"
            await loop.run_in_executor(
                None,
                lambda: download_bundle(
                    s3_client=self._s3_client,
                    bucket=self._settings.s3_bucket,
                    key=self._settings.model_key,
                    dest=dest,
                ),
            )
            await loop.run_in_executor(None, self._scorer.load_from_path, dest)
        self._metrics.mark_model_reloaded()

    # ── Per-message ingest ───────────────────────────────────────────────────

    def _make_consumer_handler(self, stream: str):
        async def _handle(record: dict[str, Any]) -> None:
            self._metrics.record_message(stream)
            await self._buffer.add(stream, record)

        return _handle

    # ── Window flush → score → publish ──────────────────────────────────────

    async def _on_window_flush(self, batch: WindowBatch) -> None:
        if not self._scorer.loaded:
            log.debug("engine.score_skipped_no_model")
            return

        loop = asyncio.get_running_loop()
        start = time.perf_counter()
        try:
            scored = await loop.run_in_executor(None, self._scorer.score_batch, batch)
        except Exception as exc:
            log.error("engine.scoring_failed", error=str(exc))
            return
        elapsed = time.perf_counter() - start
        self._metrics.observe_scoring_latency(elapsed)

        if not scored:
            return

        for anomaly in scored:
            event = await self._enrich(anomaly, batch)
            self._metrics.record_anomaly(anomaly.severity)
            log.info(
                "anomaly.scored",
                anomaly_id=event["anomaly_id"],
                severity=anomaly.severity,
                score=round(anomaly.score, 4),
                anomaly_type=anomaly.anomaly_type,
                affected_nodes=event["affected_nodes"],
                affected_prefixes=event["affected_prefixes"],
            )
            try:
                await self._producer.send(
                    topic=self._settings.kafka_topic_anomalies,
                    value=event,
                    key=event["anomaly_id"],
                )
            except Exception as exc:
                log.error(
                    "engine.publish_failed",
                    anomaly_id=event["anomaly_id"],
                    error=str(exc),
                )
            if anomaly.severity == "CRITICAL" and self._slack.enabled:
                ok = await self._slack.alert(event)
                if ok:
                    self._metrics.record_slack_sent()
                else:
                    self._metrics.record_slack_failure()

    async def _enrich(
        self, anomaly: ScoredAnomaly, batch: WindowBatch
    ) -> dict[str, Any]:
        """Build the event payload, querying the topology service for context."""

        flows = batch.get(STREAM_FLOWS)
        bgp = batch.get(STREAM_BGP)
        snmp = batch.get(STREAM_SNMP)

        affected_nodes: set[str] = set()
        affected_prefixes: set[str] = set()

        # Per-stream extraction of identifiers from raw messages.
        if anomaly.anomaly_type == "traffic":
            for m in flows:
                if isinstance(m.get("srcaddr"), str):
                    affected_nodes.add(m["srcaddr"])
                if isinstance(m.get("dstaddr"), str):
                    affected_nodes.add(m["dstaddr"])
        elif anomaly.anomaly_type == "bgp":
            for m in bgp:
                if isinstance(m.get("prefix"), str):
                    affected_prefixes.add(m["prefix"])
                if isinstance(m.get("peer_ip"), str):
                    affected_nodes.add(m["peer_ip"])
        elif anomaly.anomaly_type == "snmp":
            for m in snmp:
                if isinstance(m.get("device_id"), str):
                    affected_nodes.add(m["device_id"])

        # Topology enrichment is best-effort and only meaningful for prefixes.
        if affected_prefixes:
            extra_nodes: set[str] = set()
            for prefix in list(affected_prefixes)[:8]:
                resp = await self._topology.affected_by_prefix(prefix)
                if not resp:
                    continue
                for node in resp.get("nodes") or []:
                    nid = node.get("node_id") if isinstance(node, dict) else None
                    if isinstance(nid, str):
                        extra_nodes.add(nid)
            affected_nodes.update(extra_nodes)

        sample_size = self._settings.raw_metrics_sample_size
        raw_sample = _select_sample(
            flows if anomaly.anomaly_type == "traffic"
            else bgp if anomaly.anomaly_type == "bgp"
            else snmp,
            sample_size,
        )

        anomaly_id = str(uuid.uuid4())
        return {
            "anomaly_id": anomaly_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": anomaly.severity,
            "score": round(anomaly.score, 6),
            "raw_score": round(anomaly.raw_score, 6),
            "anomaly_type": anomaly.anomaly_type,
            "affected_nodes": sorted(affected_nodes),
            "affected_prefixes": sorted(affected_prefixes),
            "top_features": anomaly.top_features,
            "feature_vector": anomaly.feature_vector,
            "context_window_start": anomaly.window_start.isoformat(),
            "context_window_end": anomaly.window_end.isoformat(),
            "raw_metrics_sample": raw_sample,
        }


def _select_sample(messages: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    if n <= 0 or not messages:
        return []
    if len(messages) <= n:
        return list(messages)
    # Take the last N — most recent context is the most useful for triage.
    return list(messages[-n:])


# ── HTTP app ─────────────────────────────────────────────────────────────────


def build_http_app(engine: AnomalyEngine) -> FastAPI:
    app = FastAPI(
        title="netobserv-anomaly-engine",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
    )

    @app.get("/")
    def root() -> JSONResponse:
        return JSONResponse(
            content={
                "service": "anomaly-engine",
                "endpoints": "/health /stats /metrics /reload-model",
            }
        )

    @app.get("/health")
    def health() -> JSONResponse:
        probe = engine.health_probe()
        if not probe["model_loaded"]:
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", **probe, "reason": "no model loaded"},
            )
        return JSONResponse(content={"status": "ok", **probe})

    @app.get("/stats")
    def stats() -> JSONResponse:
        return JSONResponse(content=engine.metrics.snapshot())

    @app.get("/metrics")
    def prom_metrics() -> PlainTextResponse:
        return PlainTextResponse(
            content=engine.metrics.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.post("/reload-model")
    async def reload_model() -> JSONResponse:
        try:
            result = await engine.reload_model()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"reload failed: {exc}") from exc
        return JSONResponse(content={"status": "reloaded", **result})

    return app


# ── Entry point ──────────────────────────────────────────────────────────────


async def _amain() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    log.info(
        "engine.starting",
        kafka=settings.kafka_bootstrap_servers,
        bucket=settings.s3_bucket,
        model_key=settings.model_key,
        topology=settings.topology_service_url,
        window_seconds=settings.window_seconds,
        slack_enabled=bool(settings.slack_webhook_url),
    )

    engine = AnomalyEngine(settings)
    try:
        await engine.start()
    except Exception as exc:
        log.error("engine.start_failed", error=str(exc))
        await engine.stop()
        return 1

    app = build_http_app(engine)
    config = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        log.info("engine.shutdown_signal")
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    try:
        await server.serve()
    finally:
        await engine.stop()
        log.info("engine.stopped")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
