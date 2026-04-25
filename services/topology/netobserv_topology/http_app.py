"""FastAPI app: topology queries, health, metrics, live WebSocket feed."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .graph import TopologyGraph
from .logging_setup import get_logger
from .metrics import TopologyMetrics
from .websocket import TopologyBroadcaster

log = get_logger(__name__)


def build_http_app(
    *,
    graph: TopologyGraph,
    broadcaster: TopologyBroadcaster,
    metrics: TopologyMetrics,
    health_probe: Callable[[], dict[str, bool]],
) -> FastAPI:
    """Construct the topology service's HTTP + WebSocket surface."""

    app = FastAPI(title="netobserv-topology", version="0.1.0")

    @app.get("/")
    def root() -> dict[str, str]:
        return {
            "service": "netobserv-topology",
            "endpoints": (
                "/health /metrics /stats "
                "/topology /topology/node/{id} /topology/path "
                "/topology/affected /topology/subgraph /ws/topology"
            ),
        }

    @app.get("/health")
    def health() -> JSONResponse:
        components = health_probe()
        healthy = all(components.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "ok" if healthy else "degraded",
                "components": components,
                "node_count": graph.node_count,
                "edge_count": graph.edge_count,
            },
        )

    @app.get("/metrics")
    def prometheus() -> Response:
        metrics.set_graph_size(graph.node_count, graph.edge_count)
        metrics.set_ws_clients(broadcaster.client_count)
        return Response(
            content=metrics.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/stats")
    def stats() -> JSONResponse:
        metrics.set_graph_size(graph.node_count, graph.edge_count)
        metrics.set_ws_clients(broadcaster.client_count)
        return JSONResponse(content=metrics.snapshot_stats())

    @app.get("/topology")
    def topology() -> JSONResponse:
        return JSONResponse(content=graph.to_json())

    @app.get("/topology/node/{node_id}")
    def node_details(node_id: str) -> JSONResponse:
        data = graph.node_with_neighbors(node_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"node {node_id} not found")
        return JSONResponse(content=data)

    @app.get("/topology/path")
    def path(
        src: str = Query(..., description="Source IP address"),
        dst: str = Query(..., description="Destination IP address"),
    ) -> JSONResponse:
        src_node = graph.find_node_by_ip(src)
        dst_node = graph.find_node_by_ip(dst)
        if src_node is None:
            raise HTTPException(
                status_code=404, detail=f"no node owns source IP {src}"
            )
        if dst_node is None:
            raise HTTPException(
                status_code=404, detail=f"no node owns destination IP {dst}"
            )
        node_ids = graph.shortest_path(src_node, dst_node)
        if node_ids is None:
            raise HTTPException(
                status_code=404,
                detail=f"no path from {src_node} to {dst_node}",
            )
        return JSONResponse(
            content={
                "src_ip": src,
                "dst_ip": dst,
                "src_node": src_node,
                "dst_node": dst_node,
                "hops": len(node_ids) - 1,
                "path": node_ids,
            }
        )

    @app.get("/topology/affected")
    def affected(
        prefix: str = Query(..., description="BGP prefix in CIDR notation"),
    ) -> JSONResponse:
        return JSONResponse(content=graph.affected_by_prefix(prefix))

    @app.get("/topology/subgraph")
    def subgraph(
        node_id: str = Query(..., description="Root node for the subgraph"),
        depth: int = Query(2, ge=0, le=10, description="BFS depth to expand"),
    ) -> JSONResponse:
        data = graph.subgraph(node_id, depth)
        if data is None:
            raise HTTPException(status_code=404, detail=f"node {node_id} not found")
        return JSONResponse(content=data)

    @app.websocket("/ws/topology")
    async def ws_topology(websocket: WebSocket) -> None:
        await broadcaster.connect(websocket)
        try:
            # Send an initial snapshot so newly-connected clients render immediately.
            await websocket.send_json(
                {"type": "snapshot", **graph.to_json()}
            )
            while True:
                # We only push; if the client closes, ``receive_text`` raises.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.warning("topology.ws_error", error=str(exc))
        finally:
            await broadcaster.disconnect(websocket)

    return app
