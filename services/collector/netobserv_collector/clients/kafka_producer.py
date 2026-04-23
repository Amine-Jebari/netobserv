"""Async Kafka producer — publishes topology updates derived from BGP events."""

from __future__ import annotations

import json
from datetime import datetime

from aiokafka import AIOKafkaProducer

from ..logging_setup import get_logger

log = get_logger(__name__)


def _json_default(obj: object) -> object:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Unserialisable type: {type(obj).__name__}")


class KafkaProducer:
    """Thin wrapper around ``aiokafka`` with a connection flag."""

    def __init__(self, bootstrap_servers: str, client_id: str) -> None:
        self._bootstrap = bootstrap_servers
        self._client_id = client_id
        self._producer: AIOKafkaProducer | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            client_id=self._client_id,
            acks="all",
            linger_ms=20,
            enable_idempotence=True,
        )
        await self._producer.start()
        self._connected = True
        log.info("kafka_producer.started", bootstrap=self._bootstrap)

    async def stop(self) -> None:
        self._connected = False
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def send_json(self, topic: str, payload: dict[str, object], key: str | None = None) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaProducer.start() was not called")
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        key_bytes = key.encode("utf-8") if key else None
        try:
            await self._producer.send_and_wait(topic, value=body, key=key_bytes)
        except Exception as exc:
            self._connected = False
            log.error("kafka_producer.send_failed", topic=topic, error=str(exc))
            raise
