"""FastAPI health-check server embedded in the simulator process."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .producer import TelemetryProducer


def build_health_app(producer: TelemetryProducer, scenario_getter) -> FastAPI:
    """Build the health endpoint app. scenario_getter() returns the active scenario name."""

    app = FastAPI(title="netobserv-simulator", version="0.1.0")

    @app.get("/health")
    def health() -> JSONResponse:
        active = scenario_getter()
        status_code = 200 if producer.healthy else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ok" if producer.healthy else "degraded",
                "messages_sent": producer.messages_sent,
                "active_scenario": active,
            },
        )

    @app.get("/")
    def root() -> dict[str, str]:
        return {"service": "netobserv-simulator", "health": "/health"}

    return app
