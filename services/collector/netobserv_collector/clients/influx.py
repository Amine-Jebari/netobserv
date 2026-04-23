"""Async wrapper around the InfluxDB 2.x line-protocol HTTP API.

We build line protocol by hand and post it via ``httpx`` rather than pulling in
the synchronous ``influxdb-client``; this keeps the runtime image small and
gives us first-class control over batching and retries from inside our event
loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from ..logging_setup import get_logger

log = get_logger(__name__)


def _escape_tag(value: str) -> str:
    """Escape commas, equals, and spaces in tag keys/values (line protocol)."""

    return value.replace("\\", "\\\\").replace(",", r"\,").replace("=", r"\=").replace(" ", r"\ ")


def _escape_measurement(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", r"\,").replace(" ", r"\ ")


def _format_field_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', r"\"")
        return f'"{escaped}"'
    raise TypeError(f"Unsupported field value type: {type(value).__name__}")


@dataclass
class InfluxPoint:
    """One point in InfluxDB line-protocol form, pre-escape."""

    measurement: str
    tags: dict[str, str] = field(default_factory=dict)
    fields: dict[str, object] = field(default_factory=dict)
    timestamp_ns: int | None = None

    def to_line(self) -> str:
        parts = [_escape_measurement(self.measurement)]
        if self.tags:
            tag_str = ",".join(
                f"{_escape_tag(k)}={_escape_tag(str(v))}"
                for k, v in sorted(self.tags.items())
            )
            parts[0] = f"{parts[0]},{tag_str}"
        if not self.fields:
            raise ValueError("InfluxPoint requires at least one field")
        field_str = ",".join(
            f"{_escape_tag(k)}={_format_field_value(v)}"
            for k, v in self.fields.items()
        )
        line = f"{parts[0]} {field_str}"
        if self.timestamp_ns is not None:
            line = f"{line} {self.timestamp_ns}"
        return line


class InfluxClient:
    """Posts line-protocol batches to InfluxDB and tracks health."""

    def __init__(
        self,
        url: str,
        token: str,
        org: str,
        bucket: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._org = org
        self._bucket = bucket
        self._client: httpx.AsyncClient | None = None
        self._timeout = timeout_seconds
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        headers = {
            "Authorization": f"Token {self._token}",
            "Content-Type": "text/plain; charset=utf-8",
            "Accept": "application/json",
        }
        self._client = httpx.AsyncClient(
            base_url=self._url,
            headers=headers,
            timeout=self._timeout,
        )
        try:
            resp = await self._client.get("/health")
            self._connected = resp.status_code == 200
        except httpx.HTTPError as exc:
            log.warning("influx.health_check_failed", error=str(exc))
            self._connected = False

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._connected = False

    async def write_batch(self, points: list[InfluxPoint]) -> None:
        if not points:
            return
        if self._client is None:
            raise RuntimeError("InfluxClient.start() was not called")
        body = "\n".join(p.to_line() for p in points).encode("utf-8")
        params = {"org": self._org, "bucket": self._bucket, "precision": "ns"}
        try:
            resp = await self._client.post("/api/v2/write", params=params, content=body)
            resp.raise_for_status()
            self._connected = True
        except httpx.HTTPError as exc:
            self._connected = False
            log.error(
                "influx.write_failed",
                error=str(exc),
                points=len(points),
            )
            raise
