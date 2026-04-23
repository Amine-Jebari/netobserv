"""SNMP sample handler — writes metrics to InfluxDB only."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..clients.influx import InfluxClient, InfluxPoint
from ..logging_setup import get_logger
from ..metrics import CollectorMetrics
from .base import BatchFlusher

log = get_logger(__name__)


class SnmpHandler:
    """Writes SNMP metric samples to InfluxDB in batches."""

    def __init__(
        self,
        *,
        topic: str,
        influx: InfluxClient,
        metrics: CollectorMetrics,
        influx_batch_size: int,
        influx_flush_interval: float,
    ) -> None:
        self._topic = topic
        self._influx = influx
        self._metrics = metrics

        self._influx_flush: BatchFlusher[InfluxPoint] = BatchFlusher(
            name="snmp-influx",
            destination="influxdb",
            topic=topic,
            batch_size=influx_batch_size,
            flush_interval_seconds=influx_flush_interval,
            flush_fn=self._flush_influx,
            metrics=metrics,
        )

    def start(self) -> None:
        self._influx_flush.start()

    async def stop(self) -> None:
        await self._influx_flush.stop()

    async def handle(self, record: dict[str, Any]) -> None:
        self._metrics.record_consumed(self._topic, message_ts=_message_ts(record))

        point = self._to_point(record)
        if point is not None:
            await self._influx_flush.add(point)

    async def _flush_influx(self, batch: list[InfluxPoint]) -> None:
        await self._influx.write_batch(batch)

    def _to_point(self, record: dict[str, Any]) -> InfluxPoint | None:
        try:
            return InfluxPoint(
                measurement="snmp",
                tags={
                    "device_id": str(record["device_id"]),
                    "interface": str(record["interface"]),
                    "metric": str(record["metric"]),
                },
                fields={"value": float(record["value"])},
                timestamp_ns=_timestamp_ns(record),
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("snmp.bad_record", error=str(exc))
            self._metrics.record_error(self._topic, "influxdb")
            return None


def _timestamp_ns(record: dict[str, Any]) -> int | None:
    ts = record.get("timestamp")
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000_000)
        except ValueError:
            return None
    return None


def _message_ts(record: dict[str, Any]) -> float | None:
    ts_ns = _timestamp_ns(record)
    return (ts_ns / 1_000_000_000) if ts_ns is not None else None
