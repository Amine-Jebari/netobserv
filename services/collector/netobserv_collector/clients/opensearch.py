"""Async OpenSearch client using the _bulk HTTP API.

The official ``opensearch-py`` package is synchronous; ``opensearchpy.AsyncOpenSearch``
exists but carries heavy transport machinery we do not need. Posting NDJSON to
``/_bulk`` via ``httpx`` gives us exactly what the collector needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class OpenSearchDoc:
    """One document awaiting bulk indexing."""

    index_prefix: str          # e.g. "flows"
    body: dict[str, object]
    timestamp: datetime        # used to compute the daily index suffix


def _daily_index(prefix: str, ts: datetime) -> str:
    ts = ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return f"{prefix}-{ts.strftime('%Y.%m.%d')}"


class OpenSearchClient:
    """Posts NDJSON bulk batches to OpenSearch and tracks connection health."""

    def __init__(
        self,
        url: str,
        username: str = "",
        password: str = "",
        verify_certs: bool = False,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._url = url.rstrip("/")
        self._auth: tuple[str, str] | None = (
            (username, password) if username or password else None
        )
        self._verify = verify_certs
        self._client: httpx.AsyncClient | None = None
        self._timeout = timeout_seconds
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._url,
            auth=self._auth,
            verify=self._verify,
            timeout=self._timeout,
            headers={"Content-Type": "application/x-ndjson"},
        )
        try:
            resp = await self._client.get("/_cluster/health")
            self._connected = resp.status_code == 200
        except httpx.HTTPError as exc:
            log.warning("opensearch.health_check_failed", error=str(exc))
            self._connected = False

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._connected = False

    async def bulk_index(self, docs: list[OpenSearchDoc]) -> None:
        if not docs:
            return
        if self._client is None:
            raise RuntimeError("OpenSearchClient.start() was not called")

        lines: list[str] = []
        for d in docs:
            index_name = _daily_index(d.index_prefix, d.timestamp)
            lines.append(json.dumps({"index": {"_index": index_name}}))
            lines.append(json.dumps(d.body, default=_json_default))
        body = ("\n".join(lines) + "\n").encode("utf-8")

        try:
            resp = await self._client.post("/_bulk", content=body)
            resp.raise_for_status()
            payload = resp.json()
            self._connected = True
            if payload.get("errors"):
                errored = sum(
                    1
                    for item in payload.get("items", [])
                    if item.get("index", {}).get("error")
                )
                log.warning(
                    "opensearch.partial_bulk_failure",
                    total=len(docs),
                    errored=errored,
                )
                if errored == len(docs):
                    raise RuntimeError("OpenSearch rejected every doc in the bulk batch")
        except httpx.HTTPError as exc:
            self._connected = False
            log.error("opensearch.bulk_failed", error=str(exc), docs=len(docs))
            raise


def _json_default(obj: object) -> object:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Unserialisable type: {type(obj).__name__}")
