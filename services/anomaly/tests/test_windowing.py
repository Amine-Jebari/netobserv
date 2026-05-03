"""Unit tests for the WindowBuffer flusher."""

from __future__ import annotations

import asyncio

import pytest

from services.anomaly.windowing import (
    ALL_STREAMS,
    STREAM_BGP,
    STREAM_FLOWS,
    STREAM_SNMP,
    WindowBatch,
    WindowBuffer,
)


@pytest.mark.asyncio
async def test_buffer_accumulates_per_stream() -> None:
    flushed: list[WindowBatch] = []

    async def on_flush(batch: WindowBatch) -> None:
        flushed.append(batch)

    buf = WindowBuffer(window_seconds=0.1, on_flush=on_flush)
    await buf.add(STREAM_FLOWS, {"srcaddr": "10.0.0.1"})
    await buf.add(STREAM_FLOWS, {"srcaddr": "10.0.0.2"})
    await buf.add(STREAM_BGP, {"event_type": "ANNOUNCE"})
    await buf.add(STREAM_SNMP, {"metric": "cpuLoad", "value": 0.5})

    batch = await buf.drain()
    assert batch.total_count() == 4
    assert len(batch.get(STREAM_FLOWS)) == 2
    assert len(batch.get(STREAM_BGP)) == 1
    assert len(batch.get(STREAM_SNMP)) == 1

    # After drain the buffer is empty
    next_batch = await buf.drain()
    assert next_batch.total_count() == 0
    assert flushed == []  # no flusher running


@pytest.mark.asyncio
async def test_buffer_unknown_stream_raises() -> None:
    buf = WindowBuffer(window_seconds=1.0, on_flush=_noop)
    with pytest.raises(KeyError):
        await buf.add("unknown", {})


@pytest.mark.asyncio
async def test_flusher_invokes_callback_at_interval() -> None:
    received: list[int] = []

    async def on_flush(batch: WindowBatch) -> None:
        received.append(batch.total_count())

    buf = WindowBuffer(window_seconds=0.05, on_flush=on_flush)
    await buf.start()
    try:
        await buf.add(STREAM_FLOWS, {"srcaddr": "1.1.1.1"})
        await asyncio.sleep(0.12)  # ≥ 2 windows
        await buf.add(STREAM_BGP, {"event_type": "WITHDRAW"})
        await asyncio.sleep(0.12)
    finally:
        await buf.stop()

    assert sum(received) >= 2
    # Empty windows must not invoke the callback.
    assert all(c > 0 for c in received)


@pytest.mark.asyncio
async def test_flusher_callback_failure_does_not_kill_loop() -> None:
    calls = 0

    async def on_flush(batch: WindowBatch) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    buf = WindowBuffer(window_seconds=0.05, on_flush=on_flush)
    await buf.start()
    try:
        await buf.add(STREAM_FLOWS, {"srcaddr": "1.1.1.1"})
        await asyncio.sleep(0.12)
        await buf.add(STREAM_FLOWS, {"srcaddr": "1.1.1.2"})
        await asyncio.sleep(0.12)
    finally:
        await buf.stop()

    assert calls >= 2


@pytest.mark.asyncio
async def test_buffer_invalid_window_raises() -> None:
    with pytest.raises(ValueError):
        WindowBuffer(window_seconds=0, on_flush=_noop)


@pytest.mark.asyncio
async def test_drain_assigns_window_boundaries() -> None:
    times = iter([100.0, 110.0, 120.0])

    def fake_clock() -> float:
        return next(times)

    buf = WindowBuffer(window_seconds=10.0, on_flush=_noop, clock=fake_clock)
    # First clock read at construction -> window_start = 100
    await buf.add(STREAM_FLOWS, {"srcaddr": "1.1.1.1"})
    batch1 = await buf.drain()  # second clock read -> 110
    batch2 = await buf.drain()  # third clock read -> 120

    assert batch1.window_start == 100.0
    assert batch1.window_end == 110.0
    assert batch2.window_start == 110.0
    assert batch2.window_end == 120.0


def test_all_streams_constant_is_complete() -> None:
    assert set(ALL_STREAMS) == {STREAM_FLOWS, STREAM_BGP, STREAM_SNMP}


async def _noop(batch: WindowBatch) -> None:
    return None
