"""Async Kafka producer wrapper for the simulator."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from aiokafka import AIOKafkaProducer
from pydantic import BaseModel

from .logging_setup import get_logger

log = get_logger(__name__)


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Unserialisable type: {type(obj).__name__}")


def _serialize(record: BaseModel) -> bytes:
    return json.dumps(record.model_dump(mode="json"), default=_default).encode("utf-8")


class TelemetryProducer:
    """Thin async wrapper around aiokafka with a health flag."""

    def __init__(self, bootstrap_servers: str, client_id: str) -> None:
        self._bootstrap = bootstrap_servers
        self._client_id = client_id
        self._producer: AIOKafkaProducer | None = None
        self._healthy = False
        self._messages_sent = 0

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def messages_sent(self) -> int:
        return self._messages_sent

    async def start(self) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            client_id=self._client_id,
            acks="all",
            linger_ms=20,
            enable_idempotence=True,
        )
        await self._producer.start()
        self._healthy = True
        log.info("producer.started", bootstrap=self._bootstrap)

    async def stop(self) -> None:
        self._healthy = False
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None
        log.info("producer.stopped", messages_sent=self._messages_sent)

    async def send(self, topic: str, record: BaseModel, key: str | None = None) -> None:
        if self._producer is None:
            raise RuntimeError("Producer has not been started")
        payload = _serialize(record)
        key_bytes = key.encode("utf-8") if key else None
        try:
            await self._producer.send_and_wait(topic, value=payload, key=key_bytes)
            self._messages_sent += 1
        except Exception as exc:
            self._healthy = False
            log.error("producer.send_failed", topic=topic, error=str(exc))
            raise
