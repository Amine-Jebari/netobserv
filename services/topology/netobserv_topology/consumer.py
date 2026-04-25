"""Per-topic Kafka consumer loop."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiokafka import AIOKafkaConsumer

from .logging_setup import get_logger
from .metrics import TopologyMetrics

log = get_logger(__name__)

HandlerFn = Callable[[dict[str, Any]], Awaitable[None]]


class TopicConsumer:
    """Consumes one Kafka topic with its own consumer group."""

    def __init__(
        self,
        *,
        topic: str,
        bootstrap_servers: str,
        group_id: str,
        handler: HandlerFn,
        metrics: TopologyMetrics,
        auto_offset_reset: str = "latest",
        client_id: str | None = None,
    ) -> None:
        self._topic = topic
        self._bootstrap = bootstrap_servers
        self._group_id = group_id
        self._handler = handler
        self._metrics = metrics
        self._auto_offset_reset = auto_offset_reset
        self._client_id = client_id or f"netobserv-topology-{topic}"
        self._consumer: AIOKafkaConsumer | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def topic(self) -> str:
        return self._topic

    async def start(self) -> None:
        self._consumer = AIOKafkaConsumer(
            self._topic,
            bootstrap_servers=self._bootstrap,
            group_id=self._group_id,
            client_id=self._client_id,
            enable_auto_commit=True,
            auto_offset_reset=self._auto_offset_reset,
            value_deserializer=lambda b: b,
        )
        await self._consumer.start()
        self._connected = True
        self._task = asyncio.create_task(self._run(), name=f"consumer-{self._topic}")
        log.info(
            "consumer.started",
            topic=self._topic,
            group_id=self._group_id,
            bootstrap=self._bootstrap,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None
        self._connected = False
        log.info("consumer.stopped", topic=self._topic)

    async def _run(self) -> None:
        assert self._consumer is not None
        try:
            async for message in self._consumer:
                if self._stop.is_set():
                    break
                payload = message.value
                try:
                    record = json.loads(payload)
                except (TypeError, ValueError) as exc:
                    log.warning("consumer.bad_json", topic=self._topic, error=str(exc))
                    self._metrics.record_error(self._topic)
                    continue
                if not isinstance(record, dict):
                    log.warning("consumer.unexpected_shape", topic=self._topic)
                    self._metrics.record_error(self._topic)
                    continue
                try:
                    await self._handler(record)
                except Exception as exc:
                    log.error(
                        "consumer.handler_failed",
                        topic=self._topic,
                        error=str(exc),
                    )
                    self._metrics.record_error(self._topic)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._connected = False
            log.error("consumer.crashed", topic=self._topic, error=str(exc))
