"""Shared mock clients for collector unit tests."""

from __future__ import annotations

from typing import Any

import pytest

from netobserv_collector.clients.influx import InfluxPoint
from netobserv_collector.clients.opensearch import OpenSearchDoc
from netobserv_collector.metrics import CollectorMetrics


class FakeInflux:
    def __init__(self, fail: bool = False) -> None:
        self.batches: list[list[InfluxPoint]] = []
        self._fail = fail
        self.connected = True

    async def write_batch(self, points: list[InfluxPoint]) -> None:
        if self._fail:
            raise RuntimeError("influx down")
        self.batches.append(list(points))


class FakeOpenSearch:
    def __init__(self, fail: bool = False) -> None:
        self.batches: list[list[OpenSearchDoc]] = []
        self._fail = fail
        self.connected = True

    async def bulk_index(self, docs: list[OpenSearchDoc]) -> None:
        if self._fail:
            raise RuntimeError("opensearch down")
        self.batches.append(list(docs))


class FakeArchive:
    def __init__(self, fail: bool = False) -> None:
        self.puts: list[list[dict[str, Any]]] = []
        self.keys: list[str] = []
        self._fail = fail
        self.connected = True

    async def put_ndjson(
        self, path_prefix: str, records: list[dict[str, Any]], now: Any = None
    ) -> str | None:
        if self._fail:
            raise RuntimeError("archive down")
        self.puts.append(list(records))
        key = f"{path_prefix}/test.json"
        self.keys.append(key)
        return key


class FakeKafkaProducer:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []
        self._fail = fail
        self.connected = True

    async def send_json(self, topic: str, payload: dict[str, Any], key: str | None = None) -> None:
        if self._fail:
            raise RuntimeError("kafka down")
        self.sent.append((topic, payload, key))


@pytest.fixture
def metrics() -> CollectorMetrics:
    return CollectorMetrics()


@pytest.fixture
def fake_influx() -> FakeInflux:
    return FakeInflux()


@pytest.fixture
def fake_opensearch() -> FakeOpenSearch:
    return FakeOpenSearch()


@pytest.fixture
def fake_archive() -> FakeArchive:
    return FakeArchive()


@pytest.fixture
def fake_kafka_producer() -> FakeKafkaProducer:
    return FakeKafkaProducer()
