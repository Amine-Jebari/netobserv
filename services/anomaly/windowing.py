"""Fixed-duration windowing buffer for the real-time scoring engine.

Messages from the three telemetry topics arrive interleaved at high rates. The
scorer needs to evaluate features over a fixed time slice (default 10 s). This
module owns that buffering: a single :class:`WindowBuffer` is shared across all
three consumers, partitioned by stream name (``flows`` / ``bgp`` / ``snmp``).

A background flusher task wakes every ``window_seconds`` and hands the
accumulated batch to a callback. Buffering happens in plain Python lists
guarded by an ``asyncio.Lock`` — message volume is well within what one event
loop can absorb at our target rates, and a lock keeps semantics obvious.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .logging_setup import get_logger

log = get_logger(__name__)

# Stream names — kept stable so metrics/labels don't drift if topic names change.
STREAM_FLOWS = "flows"
STREAM_BGP = "bgp"
STREAM_SNMP = "snmp"
ALL_STREAMS = (STREAM_FLOWS, STREAM_BGP, STREAM_SNMP)


@dataclass
class WindowBatch:
    """One closed window's worth of messages, grouped by stream."""

    window_start: float
    window_end: float
    messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def total_count(self) -> int:
        return sum(len(v) for v in self.messages.values())

    def get(self, stream: str) -> list[dict[str, Any]]:
        return self.messages.get(stream, [])


FlushCallback = Callable[[WindowBatch], Awaitable[None]]


class WindowBuffer:
    """Per-stream buffer that flushes every ``window_seconds`` to a callback.

    The flusher runs as a long-lived asyncio task started by :meth:`start` and
    cancelled by :meth:`stop`. Calling ``start()`` more than once is an error;
    callers should construct one buffer per engine instance.
    """

    def __init__(
        self,
        *,
        window_seconds: float,
        on_flush: FlushCallback,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._window_seconds = float(window_seconds)
        self._on_flush = on_flush
        self._clock = clock
        self._lock = asyncio.Lock()
        self._buffers: dict[str, list[dict[str, Any]]] = {s: [] for s in ALL_STREAMS}
        self._window_start: float = self._clock()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def window_seconds(self) -> float:
        return self._window_seconds

    async def add(self, stream: str, message: dict[str, Any]) -> None:
        if stream not in self._buffers:
            raise KeyError(f"unknown stream: {stream!r}")
        async with self._lock:
            self._buffers[stream].append(message)

    async def drain(self) -> WindowBatch:
        """Atomically swap the live buffers for empty ones and return a batch."""

        async with self._lock:
            window_end = self._clock()
            batch = WindowBatch(
                window_start=self._window_start,
                window_end=window_end,
                messages={s: self._buffers[s] for s in ALL_STREAMS},
            )
            self._buffers = {s: [] for s in ALL_STREAMS}
            self._window_start = window_end
            return batch

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("WindowBuffer already started")
        self._stop.clear()
        self._window_start = self._clock()
        self._task = asyncio.create_task(self._run(), name="window-flusher")
        log.info("window.buffer_started", window_seconds=self._window_seconds)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        log.info("window.buffer_stopped")

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._window_seconds
                    )
                    # Stop signalled — fall through to do one final flush below.
                    break
                except asyncio.TimeoutError:
                    pass
                await self._flush_once()
            # Final drain on shutdown so in-flight messages are not lost in tests.
            await self._flush_once()
        except asyncio.CancelledError:
            raise

    async def _flush_once(self) -> None:
        batch = await self.drain()
        if batch.total_count() == 0:
            return
        try:
            await self._on_flush(batch)
        except Exception as exc:  # pragma: no cover — logged for ops
            log.error(
                "window.flush_callback_failed",
                error=str(exc),
                counts={k: len(v) for k, v in batch.messages.items()},
            )
