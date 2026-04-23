"""Shared batching utilities for fan-out handlers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from ..logging_setup import get_logger
from ..metrics import CollectorMetrics

T = TypeVar("T")

log = get_logger(__name__)


class BatchFlusher(Generic[T]):
    """Accumulates items and calls ``flush`` when size or age threshold is hit.

    A dedicated ``asyncio.Task`` owns the flush cadence so no handler call path
    ever blocks on IO. Failures are isolated: an error flushing to one
    destination does not drop items queued for another destination.
    """

    def __init__(
        self,
        name: str,
        destination: str,
        topic: str,
        batch_size: int,
        flush_interval_seconds: float,
        flush_fn: Callable[[list[T]], Awaitable[None]],
        metrics: CollectorMetrics,
    ) -> None:
        self._name = name
        self._destination = destination
        self._topic = topic
        self._batch_size = batch_size
        self._interval = flush_interval_seconds
        self._flush_fn = flush_fn
        self._metrics = metrics
        self._buffer: list[T] = []
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"flusher-{self._name}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None
        await self._flush_now()   # drain on shutdown

    async def add(self, item: T) -> None:
        async with self._lock:
            self._buffer.append(item)
            should_flush = len(self._buffer) >= self._batch_size
        if should_flush:
            await self._flush_now()

    async def _run(self) -> None:
        last_flush = time.monotonic()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            if time.monotonic() - last_flush >= self._interval:
                await self._flush_now()
                last_flush = time.monotonic()

    async def _flush_now(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            batch = self._buffer
            self._buffer = []
        size = len(batch)
        try:
            await self._flush_fn(batch)
            self._metrics.record_written(self._topic, self._destination, size)
            log.debug(
                "flusher.flushed",
                name=self._name,
                destination=self._destination,
                count=size,
            )
        except Exception as exc:
            self._metrics.record_error(self._topic, self._destination, size)
            log.error(
                "flusher.flush_failed",
                name=self._name,
                destination=self._destination,
                count=size,
                error=str(exc),
            )
