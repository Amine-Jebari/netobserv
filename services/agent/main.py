"""Agent service entry point: FastAPI app + Kafka consumer.

Lifecycle::

  startup
    │
    ├─ build Toolbox (httpx client + RAG search closure)
    ├─ build ChatGoogleGenerativeAI bound with the 4 LangChain tools
    ├─ start AIOKafkaConsumer on ``anomalies.scored``
    ├─ start uvicorn (HTTP + Prometheus metrics)
    │
  steady state
    │
    ├─ on each Kafka record:
    │     - INFO   → log + skip (cost-conscious)
    │     - WARN   → schedule investigation
    │     - CRITICAL → schedule investigation
    │
  shutdown
    └─ stop consumer, close http client, drain background tasks

The HTTP surface mirrors the rest of the platform (``/health`` + ``/metrics``)
plus the agent-specific:

  GET  /investigations              - recent investigations (summary view)
  GET  /investigations/{id}         - full row including reasoning trace
  POST /investigate                 - manually trigger an investigation by
                                      anomaly_id (body: {"anomaly": {...}})

Run from the repo root::

  python -m services.agent.main
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from contextlib import suppress
from typing import Any

import uvicorn
from aiokafka import AIOKafkaConsumer
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .agent import build_chat_model, run_investigation_for_anomaly
from .config import Settings, get_settings
from .db import close_pool, get_pool
from .investigations_store import (
    get_by_anomaly_id,
    get_by_id,
    list_recent,
)
from .logging_setup import configure_logging, get_logger
from .metrics import AgentMetrics
from .runbook import render_runbook
from .tools import Toolbox, build_tools

log = get_logger(__name__)


# ── Service container ────────────────────────────────────────────────────────


class AgentService:
    """Owns every long-lived resource the agent service needs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._metrics = AgentMetrics()
        self._toolbox: Toolbox | None = None
        self._chat_model = None  # lazily built so tests can inject
        self._consumer: AIOKafkaConsumer | None = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._investigation_tasks: set[asyncio.Task[Any]] = set()
        self._severities = settings.severities_to_investigate()

    # ── properties for HTTP layer ────────────────────────────────────────────

    @property
    def metrics(self) -> AgentMetrics:
        return self._metrics

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def consumer_connected(self) -> bool:
        return self._consumer is not None

    def health_probe(self) -> dict[str, Any]:
        return {
            "kafka_consumer": self.consumer_connected,
            "model_configured": bool(self._settings.google_api_key),
            "in_flight_investigations": len(self._investigation_tasks),
        }

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        # Warm the Postgres pool early so the first investigation does not
        # pay the connect latency.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, get_pool, self._settings)

        self._toolbox = Toolbox.build(self._settings)
        chat_model = build_chat_model(self._settings)
        tools = build_tools(self._toolbox)
        # ChatGoogleGenerativeAI exposes .bind_tools; this returns a wrapped
        # runnable that the agent loop invokes.
        self._chat_model = chat_model.bind_tools(tools)

        self._consumer = AIOKafkaConsumer(
            self._settings.kafka_topic_anomalies,
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            group_id=self._settings.kafka_consumer_group,
            client_id="netobserv-agent",
            enable_auto_commit=True,
            auto_offset_reset=self._settings.kafka_auto_offset_reset,
            value_deserializer=lambda b: b,
        )
        await self._consumer.start()
        self._metrics.kafka_consumer_connected.set(1)
        self._consumer_task = asyncio.create_task(
            self._consume_loop(), name="agent-consumer"
        )
        log.info(
            "agent.started",
            kafka=self._settings.kafka_bootstrap_servers,
            topic=self._settings.kafka_topic_anomalies,
            group=self._settings.kafka_consumer_group,
            model=self._settings.gemini_model,
            investigate_severities=sorted(self._severities),
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._consumer_task
            self._consumer_task = None
        if self._consumer is not None:
            with suppress(Exception):
                await self._consumer.stop()
            self._consumer = None
            self._metrics.kafka_consumer_connected.set(0)

        # Drain in-flight investigations briefly so we don't leave half-written
        # rows behind.
        if self._investigation_tasks:
            log.info(
                "agent.draining_investigations",
                in_flight=len(self._investigation_tasks),
            )
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*self._investigation_tasks, return_exceptions=True),
                    timeout=10.0,
                )

        if self._toolbox is not None:
            self._toolbox.close()
            self._toolbox = None
        close_pool()
        log.info("agent.stopped")

    # ── kafka consume loop ───────────────────────────────────────────────────

    async def _consume_loop(self) -> None:
        assert self._consumer is not None
        try:
            async for message in self._consumer:
                if self._stop.is_set():
                    break
                payload = message.value
                try:
                    record = json.loads(payload)
                except (TypeError, ValueError) as exc:
                    log.warning("agent.kafka_bad_json", error=str(exc))
                    continue
                if not isinstance(record, dict):
                    continue
                self._route_anomaly(record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._metrics.kafka_consumer_connected.set(0)
            log.error("agent.consumer_crashed", error=str(exc))

    def _route_anomaly(self, anomaly: dict[str, Any]) -> None:
        severity = str(anomaly.get("severity") or "UNKNOWN").upper()
        if severity not in self._severities:
            decision = "skip_info" if severity == "INFO" else "skip_unknown"
            self._metrics.anomalies_consumed.labels(
                severity=severity, decision=decision
            ).inc()
            log.info(
                "agent.anomaly_skipped",
                anomaly_id=anomaly.get("anomaly_id"),
                severity=severity,
                reason="severity below investigate threshold",
            )
            return

        self._metrics.anomalies_consumed.labels(
            severity=severity, decision="investigate"
        ).inc()
        task = asyncio.create_task(
            self._run_one(anomaly), name=f"investigate-{anomaly.get('anomaly_id')}"
        )
        self._investigation_tasks.add(task)
        task.add_done_callback(self._investigation_tasks.discard)

    async def _run_one(self, anomaly: dict[str, Any]) -> None:
        """Run one investigation off the event loop thread."""
        if self._toolbox is None or self._chat_model is None:
            log.error("agent.run_one_called_before_start")
            return
        loop = asyncio.get_running_loop()
        try:
            investigation_id, result = await loop.run_in_executor(
                None,
                lambda: run_investigation_for_anomaly(
                    anomaly=anomaly,
                    settings=self._settings,
                    toolbox=self._toolbox,
                    chat_model=self._chat_model,
                    metrics=self._metrics,
                    runbook_renderer=render_runbook,
                ),
            )
            log.info(
                "agent.anomaly_investigated",
                anomaly_id=anomaly.get("anomaly_id"),
                investigation_id=investigation_id,
                status=result.status,
                tool_calls=result.tool_call_count,
            )
        except Exception as exc:
            log.error(
                "agent.investigation_failed",
                anomaly_id=anomaly.get("anomaly_id"),
                error=str(exc),
            )

    # ── used by HTTP POST /investigate ───────────────────────────────────────

    async def investigate_now(self, anomaly: dict[str, Any]) -> dict[str, Any]:
        """Synchronously run an investigation and return its summary."""
        if self._toolbox is None or self._chat_model is None:
            raise RuntimeError("agent service not started")
        loop = asyncio.get_running_loop()
        investigation_id, result = await loop.run_in_executor(
            None,
            lambda: run_investigation_for_anomaly(
                anomaly=anomaly,
                settings=self._settings,
                toolbox=self._toolbox,
                chat_model=self._chat_model,
                metrics=self._metrics,
                runbook_renderer=render_runbook,
            ),
        )
        return {
            "investigation_id": investigation_id,
            "status": result.status,
            "verdict": result.verdict,
            "tool_call_count": result.tool_call_count,
            "duration_seconds": result.duration_seconds,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "error": result.error,
        }


# ── HTTP API ─────────────────────────────────────────────────────────────────


class InvestigateRequest(BaseModel):
    anomaly: dict[str, Any]


def build_http_app(service: AgentService) -> FastAPI:
    app = FastAPI(
        title="netobserv-agent",
        version="0.2.0",
        docs_url="/docs",
        redoc_url=None,
    )

    @app.get("/")
    def root() -> dict[str, str]:
        return {
            "service": "netobserv-agent",
            "endpoints": (
                "/health /metrics "
                "/investigations /investigations/{id} /investigate"
            ),
        }

    @app.get("/health")
    def health() -> JSONResponse:
        probe = service.health_probe()
        healthy = probe["kafka_consumer"]
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "degraded", **probe},
        )

    @app.get("/metrics")
    def prom() -> Response:
        return Response(
            content=service.metrics.render_prometheus(),
            media_type=service.metrics.content_type,
        )

    @app.get("/investigations")
    def list_investigations(
        limit: int = Query(50, ge=1, le=500),
        severity: str | None = Query(None),
    ) -> JSONResponse:
        rows = list_recent(
            limit=limit, severity=severity, settings=service.settings
        )
        return JSONResponse(content={"items": [r.to_summary() for r in rows]})

    @app.get("/investigations/{investigation_id}")
    def get_investigation(investigation_id: str) -> JSONResponse:
        row = get_by_id(investigation_id, settings=service.settings)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"investigation {investigation_id} not found"
            )
        return JSONResponse(content=row.to_full())

    @app.post("/investigate")
    async def investigate(req: InvestigateRequest) -> JSONResponse:
        anomaly = req.anomaly or {}
        if not anomaly.get("anomaly_id") or not anomaly.get("anomaly_type"):
            raise HTTPException(
                status_code=400,
                detail="anomaly must include anomaly_id and anomaly_type",
            )
        # If we already have an investigation row for this anomaly, return it
        # rather than re-running (a small UX nicety for demos).
        existing = get_by_anomaly_id(
            anomaly["anomaly_id"], settings=service.settings
        )
        if existing is not None and existing.status == "completed":
            log.info(
                "agent.investigate.replay",
                anomaly_id=anomaly["anomaly_id"],
                investigation_id=existing.id,
            )
            return JSONResponse(
                status_code=200,
                content={
                    "investigation_id": existing.id,
                    "status": existing.status,
                    "verdict": existing.verdict,
                    "tool_call_count": existing.tool_call_count,
                    "duration_seconds": existing.duration_seconds,
                    "replayed": True,
                },
            )
        try:
            summary = await service.investigate_now(anomaly)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(content=summary)

    return app


# ── Entry point ──────────────────────────────────────────────────────────────


async def _amain() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    log.info(
        "agent.starting",
        kafka=settings.kafka_bootstrap_servers,
        topic=settings.kafka_topic_anomalies,
        model=settings.gemini_model,
        max_tool_calls=settings.max_tool_calls,
        timeout_seconds=settings.investigation_timeout_seconds,
    )

    service = AgentService(settings)
    try:
        await service.start()
    except Exception as exc:
        log.error("agent.start_failed", error=str(exc))
        await service.stop()
        return 1

    app = build_http_app(service)
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
        log.info("agent.shutdown_signal")
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    try:
        await server.serve()
    finally:
        await service.stop()
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
