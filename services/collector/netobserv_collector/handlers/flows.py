"""Flow record handler — fans out to InfluxDB, OpenSearch, and MinIO/S3.

The three destinations run on *independent* flushers so a failure in one does
not block the others. Messages are parsed once, then appended to each flusher's
queue.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..clients.influx import InfluxClient, InfluxPoint
from ..clients.object_store import ArchiveClient
from ..clients.opensearch import OpenSearchClient, OpenSearchDoc
from ..logging_setup import get_logger
from ..metrics import CollectorMetrics
from .base import BatchFlusher

log = get_logger(__name__)


class FlowsHandler:
    """Fans one flow record out to three storage backends."""

    def __init__(
        self,
        *,
        topic: str,
        influx: InfluxClient,
        opensearch: OpenSearchClient,
        archive: ArchiveClient,
        metrics: CollectorMetrics,
        influx_batch_size: int,
        influx_flush_interval: float,
        opensearch_bulk_size: int,
        opensearch_flush_interval: float,
        archive_flush_interval: float,
        archive_path_prefix: str = "flows",
    ) -> None:
        self._topic = topic
        self._metrics = metrics
        self._influx = influx
        self._opensearch = opensearch
        self._archive = archive
        self._archive_prefix = archive_path_prefix

        self._influx_flush: BatchFlusher[InfluxPoint] = BatchFlusher(
            name="flows-influx",
            destination="influxdb",
            topic=topic,
            batch_size=influx_batch_size,
            flush_interval_seconds=influx_flush_interval,
            flush_fn=self._flush_influx,
            metrics=metrics,
        )
        self._opensearch_flush: BatchFlusher[OpenSearchDoc] = BatchFlusher(
            name="flows-opensearch",
            destination="opensearch",
            topic=topic,
            batch_size=opensearch_bulk_size,
            flush_interval_seconds=opensearch_flush_interval,
            flush_fn=self._flush_opensearch,
            metrics=metrics,
        )
        self._archive_flush: BatchFlusher[dict[str, Any]] = BatchFlusher(
            name="flows-archive",
            destination="s3",
            topic=topic,
            # Drive archive purely by time — 1-min chunks regardless of volume.
            batch_size=10_000_000,
            flush_interval_seconds=archive_flush_interval,
            flush_fn=self._flush_archive,
            metrics=metrics,
        )

    def start(self) -> None:
        self._influx_flush.start()
        self._opensearch_flush.start()
        self._archive_flush.start()

    async def stop(self) -> None:
        await self._influx_flush.stop()
        await self._opensearch_flush.stop()
        await self._archive_flush.stop()

    async def handle(self, record: dict[str, Any]) -> None:
        self._metrics.record_consumed(self._topic, message_ts=_message_ts(record))

        point = self._to_point(record)
        if point is not None:
            await self._influx_flush.add(point)

        doc = OpenSearchDoc(
            index_prefix="flows",
            body=record,
            timestamp=_datetime_for(record),
        )
        await self._opensearch_flush.add(doc)
        await self._archive_flush.add(record)

    # ── flush adapters ───────────────────────────────────────────────────────

    async def _flush_influx(self, batch: list[InfluxPoint]) -> None:
        await self._influx.write_batch(batch)

    async def _flush_opensearch(self, batch: list[OpenSearchDoc]) -> None:
        await self._opensearch.bulk_index(batch)

    async def _flush_archive(self, batch: list[dict[str, Any]]) -> None:
        await self._archive.put_ndjson(self._archive_prefix, batch)

    # ── parsing ──────────────────────────────────────────────────────────────

    def _to_point(self, record: dict[str, Any]) -> InfluxPoint | None:
        try:
            return InfluxPoint(
                measurement="flows",
                tags={
                    "srcaddr": str(record["srcaddr"]),
                    "dstaddr": str(record["dstaddr"]),
                    "protocol": str(record["protocol"]),
                },
                fields={
                    "packets": int(record.get("packets", 0)),
                    "bytes": int(record.get("bytes", 0)),
                },
                timestamp_ns=_timestamp_ns(record),
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("flows.bad_record", error=str(exc))
            self._metrics.record_error(self._topic, "influxdb")
            return None


def _timestamp_ns(record: dict[str, Any]) -> int:
    end = record.get("end_ts")
    if isinstance(end, int):
        return end * 1_000_000_000
    return int(time.time() * 1_000_000_000)


def _message_ts(record: dict[str, Any]) -> float | None:
    end = record.get("end_ts")
    if isinstance(end, (int, float)):
        return float(end)
    return None


def _datetime_for(record: dict[str, Any]) -> datetime:
    end = record.get("end_ts")
    if isinstance(end, (int, float)):
        return datetime.fromtimestamp(float(end), tz=timezone.utc)
    return datetime.now(tz=timezone.utc)
