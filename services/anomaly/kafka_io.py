"""Async Kafka helpers used by the engine.

A thin wrapper over aiokafka so the engine itself stays focused on scoring +
enrichment + alerting. Mirrors the pattern used by the collector service.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from .logging_setup import get_logger

log = get_logger(__name__)

HandlerFn = Callable[[dict[str, Any]], Awaitable[None]]


class StreamConsumer:
    """One AIOKafkaConsumer per topic, dispatching JSON-decoded records."""

    def __init__(
        self,
        *,
        topic: str,
        bootstrap_servers: str,
        group_id: str,
        handler: HandlerFn,
        auto_offset_reset: str = "latest",
        client_id: str | None = None,
    ) -> None:
        self._topic = topic
        self._bootstrap = bootstrap_servers
        self._group_id = group_id
        self._handler = handler
        self._auto_offset_reset = auto_offset_reset
        self._client_id = client_id or f"netobserv-anomaly-{topic}"
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
        self._task = asyncio.create_task(self._run(), name=f"engine-consumer-{self._topic}")
        log.info(
            "engine.consumer_started",
            topic=self._topic,
            group_id=self._group_id,
            bootstrap=self._bootstrap,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None
        self._connected = False
        log.info("engine.consumer_stopped", topic=self._topic)

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
                    log.warning(
                        "engine.consumer_bad_json", topic=self._topic, error=str(exc)
                    )
                    continue
                if not isinstance(record, dict):
                    continue
                try:
                    await self._handler(record)
                except Exception as exc:
                    log.error(
                        "engine.handler_failed",
                        topic=self._topic,
                        error=str(exc),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._connected = False
            log.error("engine.consumer_crashed", topic=self._topic, error=str(exc))


class JsonProducer:
    """Lazy AIOKafkaProducer that JSON-encodes dict payloads."""

    def __init__(self, *, bootstrap_servers: str, client_id: str) -> None:
        self._bootstrap = bootstrap_servers
        self._client_id = client_id
        self._producer: AIOKafkaProducer | None = None

    @property
    def connected(self) -> bool:
        return self._producer is not None

    async def start(self) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            client_id=self._client_id,
            value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
            acks="all",
        )
        await self._producer.start()
        log.info("engine.producer_started", bootstrap=self._bootstrap)

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None
        log.info("engine.producer_stopped")

    async def send(self, *, topic: str, value: dict[str, Any], key: str | None = None) -> None:
        if self._producer is None:
            raise RuntimeError("JsonProducer not started")
        encoded_key = key.encode("utf-8") if key is not None else None
        await self._producer.send_and_wait(topic, value=value, key=encoded_key)
