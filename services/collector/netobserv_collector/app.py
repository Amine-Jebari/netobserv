"""Collector entry point — wires clients, handlers, consumers, and the HTTP app."""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress

import uvicorn

from .clients.influx import InfluxClient
from .clients.kafka_producer import KafkaProducer
from .clients.object_store import ArchiveClient
from .clients.opensearch import OpenSearchClient
from .config import Settings, get_settings
from .consumer import TopicConsumer
from .handlers import BgpHandler, FlowsHandler, SnmpHandler
from .http_app import build_http_app
from .logging_setup import configure_logging, get_logger
from .metrics import CollectorMetrics

log = get_logger(__name__)


class Collector:
    """Composite owning every client, handler, and consumer in the service."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._metrics = CollectorMetrics()

        self._influx = InfluxClient(
            url=settings.influxdb_url,
            token=settings.influxdb_token,
            org=settings.influxdb_org,
            bucket=settings.influxdb_bucket,
        )
        self._opensearch = OpenSearchClient(
            url=settings.opensearch_url,
            username=settings.opensearch_user,
            password=settings.opensearch_password,
            verify_certs=settings.opensearch_verify_certs,
        )
        self._archive = ArchiveClient(
            endpoint=settings.s3_endpoint,
            region=settings.s3_region,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            bucket=settings.s3_bucket,
        )
        self._kafka_producer = KafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            client_id="netobserv-collector-producer",
        )

        self._flows_handler = FlowsHandler(
            topic=settings.kafka_topic_flows,
            influx=self._influx,
            opensearch=self._opensearch,
            archive=self._archive,
            metrics=self._metrics,
            influx_batch_size=settings.influxdb_batch_size,
            influx_flush_interval=settings.influxdb_flush_interval_seconds,
            opensearch_bulk_size=settings.opensearch_bulk_size,
            opensearch_flush_interval=settings.opensearch_flush_interval_seconds,
            archive_flush_interval=settings.s3_flush_interval_seconds,
            archive_path_prefix=settings.s3_path_prefix,
        )
        self._bgp_handler = BgpHandler(
            topic=settings.kafka_topic_bgp,
            topology_topic=settings.kafka_topic_topology_updates,
            opensearch=self._opensearch,
            kafka_producer=self._kafka_producer,
            metrics=self._metrics,
            opensearch_bulk_size=settings.opensearch_bulk_size,
            opensearch_flush_interval=settings.opensearch_flush_interval_seconds,
        )
        self._snmp_handler = SnmpHandler(
            topic=settings.kafka_topic_snmp,
            influx=self._influx,
            metrics=self._metrics,
            influx_batch_size=settings.influxdb_batch_size,
            influx_flush_interval=settings.influxdb_flush_interval_seconds,
        )

        prefix = settings.kafka_consumer_group_prefix
        self._consumers: list[TopicConsumer] = [
            TopicConsumer(
                topic=settings.kafka_topic_flows,
                bootstrap_servers=settings.kafka_bootstrap_servers,
                group_id=f"{prefix}-flows",
                handler=self._flows_handler.handle,
                metrics=self._metrics,
                auto_offset_reset=settings.kafka_auto_offset_reset,
            ),
            TopicConsumer(
                topic=settings.kafka_topic_bgp,
                bootstrap_servers=settings.kafka_bootstrap_servers,
                group_id=f"{prefix}-bgp",
                handler=self._bgp_handler.handle,
                metrics=self._metrics,
                auto_offset_reset=settings.kafka_auto_offset_reset,
            ),
            TopicConsumer(
                topic=settings.kafka_topic_snmp,
                bootstrap_servers=settings.kafka_bootstrap_servers,
                group_id=f"{prefix}-snmp",
                handler=self._snmp_handler.handle,
                metrics=self._metrics,
                auto_offset_reset=settings.kafka_auto_offset_reset,
            ),
        ]

    @property
    def metrics(self) -> CollectorMetrics:
        return self._metrics

    def health_probe(self) -> dict[str, bool]:
        return {
            "influxdb": self._influx.connected,
            "opensearch": self._opensearch.connected,
            "archive": self._archive.connected,
            "kafka_producer": self._kafka_producer.connected,
            **{f"consumer_{c.topic}": c.connected for c in self._consumers},
        }

    async def start(self) -> None:
        await asyncio.gather(
            self._influx.start(),
            self._opensearch.start(),
            self._archive.start(),
            self._kafka_producer.start(),
        )
        self._flows_handler.start()
        self._bgp_handler.start()
        self._snmp_handler.start()
        for consumer in self._consumers:
            await consumer.start()

    async def stop(self) -> None:
        for consumer in self._consumers:
            await consumer.stop()
        await asyncio.gather(
            self._flows_handler.stop(),
            self._bgp_handler.stop(),
            self._snmp_handler.stop(),
        )
        await asyncio.gather(
            self._influx.stop(),
            self._opensearch.stop(),
            self._archive.stop(),
            self._kafka_producer.stop(),
        )


async def _amain() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    log.info(
        "collector.starting",
        kafka=settings.kafka_bootstrap_servers,
        influxdb=settings.influxdb_url,
        opensearch=settings.opensearch_url,
        s3_endpoint=settings.s3_endpoint or "aws",
        s3_bucket=settings.s3_bucket,
    )

    collector = Collector(settings)
    await collector.start()

    app = build_http_app(collector.metrics, collector.health_probe)
    uvicorn_config = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        log.info("collector.shutdown_signal")
        stop_event.set()
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    try:
        await server.serve()
    finally:
        await collector.stop()
        log.info("collector.stopped")


def run() -> None:
    """Console-script entry point."""

    asyncio.run(_amain())


if __name__ == "__main__":
    run()
