"""FastAPI app exposing /health, /metrics, /stats."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from .metrics import CollectorMetrics


def build_http_app(
    metrics: CollectorMetrics,
    health_probe: Callable[[], dict[str, bool]],
) -> FastAPI:
    """Build the internal HTTP app. ``health_probe`` returns {component: connected}."""

    app = FastAPI(title="netobserv-collector", version="0.1.0")

    @app.get("/")
    def root() -> dict[str, str]:
        return {
            "service": "netobserv-collector",
            "endpoints": "/health /metrics /stats",
        }

    @app.get("/health")
    def health() -> JSONResponse:
        components = health_probe()
        healthy = all(components.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "ok" if healthy else "degraded",
                "components": components,
            },
        )

    @app.get("/metrics")
    def prometheus() -> Response:
        return Response(
            content=metrics.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/stats")
    def stats() -> JSONResponse:
        return JSONResponse(content=metrics.snapshot_stats())

    return app
