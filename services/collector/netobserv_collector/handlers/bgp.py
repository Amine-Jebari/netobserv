"""BGP event handler — indexes to OpenSearch and republishes topology updates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..clients.kafka_producer import KafkaProducer
from ..clients.opensearch import OpenSearchClient, OpenSearchDoc
from ..logging_setup import get_logger
from ..metrics import CollectorMetrics
from .base import BatchFlusher

log = get_logger(__name__)


class BgpHandler:
    """Indexes BGP events and forwards them to the topology update topic."""

    def __init__(
        self,
        *,
        topic: str,
        topology_topic: str,
        opensearch: OpenSearchClient,
        kafka_producer: KafkaProducer,
        metrics: CollectorMetrics,
        opensearch_bulk_size: int,
        opensearch_flush_interval: float,
    ) -> None:
        self._topic = topic
        self._topology_topic = topology_topic
        self._opensearch = opensearch
        self._kafka_producer = kafka_producer
        self._metrics = metrics

        self._opensearch_flush: BatchFlusher[OpenSearchDoc] = BatchFlusher(
            name="bgp-opensearch",
            destination="opensearch",
            topic=topic,
            batch_size=opensearch_bulk_size,
            flush_interval_seconds=opensearch_flush_interval,
            flush_fn=self._flush_opensearch,
            metrics=metrics,
        )

    def start(self) -> None:
        self._opensearch_flush.start()

    async def stop(self) -> None:
        await self._opensearch_flush.stop()

    async def handle(self, record: dict[str, Any]) -> None:
        self._metrics.record_consumed(self._topic, message_ts=_message_ts(record))

        doc = OpenSearchDoc(
            index_prefix="bgp-events",
            body=record,
            timestamp=_datetime_for(record),
        )
        await self._opensearch_flush.add(doc)

        # Topology updates are tiny and latency-sensitive — publish synchronously.
        try:
            await self._kafka_producer.send_json(
                self._topology_topic,
                record,
                key=str(record.get("prefix", "")),
            )
            self._metrics.record_written(self._topic, "kafka-topology", 1)
        except Exception:
            self._metrics.record_error(self._topic, "kafka-topology", 1)

    async def _flush_opensearch(self, batch: list[OpenSearchDoc]) -> None:
        await self._opensearch.bulk_index(batch)


def _message_ts(record: dict[str, Any]) -> float | None:
    ts = record.get("timestamp")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _datetime_for(record: dict[str, Any]) -> datetime:
    ts = record.get("timestamp")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)
