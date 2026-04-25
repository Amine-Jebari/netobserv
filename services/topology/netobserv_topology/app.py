"""Topology service entry point — wires store, graph, consumers, and HTTP app."""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress

import uvicorn

from .config import Settings, get_settings
from .consumer import TopicConsumer
from .dynamodb_store import TopologyStore
from .graph import TopologyGraph
from .handlers import BgpHandler, FlowHandler, SnmpHandler
from .http_app import build_http_app
from .logging_setup import configure_logging, get_logger
from .metrics import TopologyMetrics
from .websocket import TopologyBroadcaster

log = get_logger(__name__)


class TopologyService:
    """Composite owning every subsystem of the topology service."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._metrics = TopologyMetrics()
        self._graph = TopologyGraph()
        self._broadcaster = TopologyBroadcaster()

        self._store = TopologyStore(
            endpoint_url=settings.dynamodb_endpoint,
            region=settings.dynamodb_region,
            access_key=settings.dynamodb_access_key,
            secret_key=settings.dynamodb_secret_key,
            table_nodes=settings.dynamodb_table_topology,
            table_links=settings.dynamodb_table_links,
        )

        self._bgp_handler = BgpHandler(
            topic=settings.kafka_topic_topology_updates,
            graph=self._graph,
            store=self._store,
            broadcaster=self._broadcaster,
            metrics=self._metrics,
        )
        self._flow_handler = FlowHandler(
            topic=settings.kafka_topic_flows,
            graph=self._graph,
            store=self._store,
            broadcaster=self._broadcaster,
            metrics=self._metrics,
            ewma_alpha=settings.traffic_ewma_alpha,
        )
        self._snmp_handler = SnmpHandler(
            topic=settings.kafka_topic_snmp,
            graph=self._graph,
            store=self._store,
            broadcaster=self._broadcaster,
            metrics=self._metrics,
        )

        prefix = settings.kafka_consumer_group_prefix
        self._consumers: list[TopicConsumer] = [
            TopicConsumer(
                topic=settings.kafka_topic_topology_updates,
                bootstrap_servers=settings.kafka_bootstrap_servers,
                group_id=f"{prefix}-bgp",
                handler=self._bgp_handler.handle,
                metrics=self._metrics,
                auto_offset_reset=settings.kafka_auto_offset_reset,
            ),
            TopicConsumer(
                topic=settings.kafka_topic_flows,
                bootstrap_servers=settings.kafka_bootstrap_servers,
                group_id=f"{prefix}-flows",
                handler=self._flow_handler.handle,
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

        self._refresh_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def graph(self) -> TopologyGraph:
        return self._graph

    @property
    def broadcaster(self) -> TopologyBroadcaster:
        return self._broadcaster

    @property
    def metrics(self) -> TopologyMetrics:
        return self._metrics

    def health_probe(self) -> dict[str, bool]:
        return {
            "dynamodb": self._store.connected,
            **{f"consumer_{c.topic}": c.connected for c in self._consumers},
        }

    async def start(self) -> None:
        await self._store.start()
        await self._graph.rehydrate(self._store)
        self._metrics.set_graph_size(self._graph.node_count, self._graph.edge_count)
        for consumer in self._consumers:
            await consumer.start()
        if self._settings.topology_refresh_interval > 0:
            self._refresh_task = asyncio.create_task(
                self._refresh_loop(), name="topology-refresh"
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._refresh_task is not None:
            await self._refresh_task
            self._refresh_task = None
        for consumer in self._consumers:
            await consumer.stop()
        await self._store.stop()

    async def _refresh_loop(self) -> None:
        interval = self._settings.topology_refresh_interval
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._graph.rehydrate(self._store)
                self._metrics.set_graph_size(
                    self._graph.node_count, self._graph.edge_count
                )
            except Exception as exc:
                log.error("topology.refresh_failed", error=str(exc))


async def _amain() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    log.info(
        "topology.starting",
        kafka=settings.kafka_bootstrap_servers,
        dynamodb=settings.dynamodb_endpoint or "aws",
        nodes_table=settings.dynamodb_table_topology,
        links_table=settings.dynamodb_table_links,
    )

    service = TopologyService(settings)
    await service.start()

    app = build_http_app(
        graph=service.graph,
        broadcaster=service.broadcaster,
        metrics=service.metrics,
        health_probe=service.health_probe,
    )
    uvicorn_config = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)

    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        log.info("topology.shutdown_signal")
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    try:
        await server.serve()
    finally:
        await service.stop()
        log.info("topology.stopped")


def run() -> None:
    """Console-script entry point."""

    asyncio.run(_amain())


if __name__ == "__main__":
    run()
