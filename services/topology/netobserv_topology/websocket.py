"""WebSocket broadcaster for live topology updates.

One broadcaster instance is shared between handlers and the FastAPI route.
Every mutation handler calls ``broadcast`` with a diff dict; connected WebSocket
clients receive the dict serialised as JSON.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from fastapi import WebSocket

from .logging_setup import get_logger

log = get_logger(__name__)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Unserialisable type: {type(obj).__name__}")


class TopologyBroadcaster:
    """Manages WebSocket clients and fans diffs out to each."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        log.info("topology.ws_connected", clients=len(self._clients))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)
        log.info("topology.ws_disconnected", clients=len(self._clients))

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, event: dict[str, Any]) -> None:
        if not self._clients:
            return
        payload = json.dumps(event, default=_json_default)
        async with self._lock:
            snapshot = list(self._clients)
        dead: list[WebSocket] = []
        for ws in snapshot:
            try:
                await ws.send_text(payload)
            except Exception as exc:
                log.warning("topology.ws_send_failed", error=str(exc))
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
