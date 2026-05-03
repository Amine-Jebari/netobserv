"""Thin async client for the topology service's enrichment endpoints.

We only need the read-only ``/topology/affected`` endpoint right now, so this
file is small. A short timeout and a per-call try/except keep the engine alive
when the topology service is restarting — enrichment is best-effort, not a
hard dependency.
"""

from __future__ import annotations

from typing import Any

import httpx

from .logging_setup import get_logger

log = get_logger(__name__)


class TopologyClient:
    """Async wrapper around the topology REST API."""

    def __init__(self, *, base_url: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def affected_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        """Best-effort fetch of nodes/edges associated with ``prefix``."""

        if self._client is None or not prefix:
            return None
        url = f"{self._base_url}/topology/affected"
        try:
            resp = await self._client.get(url, params={"prefix": prefix})
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("topology.affected_failed", prefix=prefix, error=str(exc))
            return None
