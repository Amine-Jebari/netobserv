"""Unit tests for the four agent tools.

Each tool is a thin shim over an HTTP backend (topology, InfluxDB, OpenSearch)
or the RAG retrieval layer. We exercise the *successful* path and at least one
error path per tool, using ``httpx.MockTransport`` so no network is touched.

The runbook search tool stubs the embedding/pgvector dependencies via a fake
``runbook_searcher`` callable on the Toolbox.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytest.importorskip("langchain_core", reason="langchain-core not installed")

from services.agent.config import Settings
from services.agent.rag import RunbookChunk
from services.agent.tools import (
    Toolbox,
    get_metrics_impl,
    get_recent_events_impl,
    invoke_tool,
    query_topology_impl,
    search_runbooks_impl,
    _downsample,
    _parse_flux_csv,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _settings() -> Settings:
    return Settings(
        topology_service_url="http://topology:8080",
        influxdb_url="http://influxdb:8086",
        influxdb_token="t0ken",
        influxdb_org="org",
        influxdb_bucket="metrics",
        opensearch_url="http://opensearch:9200",
        google_api_key="dummy",  # not exercised here
    )


def _toolbox_with_transport(handler, *, runbook_searcher=None) -> Toolbox:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    settings = _settings()
    return Toolbox(
        settings=settings,
        http=client,
        runbook_searcher=runbook_searcher or (lambda q, k: []),
    )


# ── query_topology ───────────────────────────────────────────────────────────


def test_query_topology_node_summarizes_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/topology/node/router-1"
        return httpx.Response(
            200,
            json={
                "node": {"node_id": "router-1", "kind": "router", "ip": "10.0.0.1"},
                "nodes": [{"node_id": "router-2", "kind": "router"}],
                "edges": [{"src": "router-1", "dst": "router-2", "utilization": 0.4}],
            },
        )

    tb = _toolbox_with_transport(handler)
    out = query_topology_impl(toolbox=tb, node_id="router-1")
    assert "error" not in out
    assert out["node_count"] == 1
    assert out["edges"][0]["src"] == "router-1"
    assert out["focus_node"]["id"] == "router-1"


def test_query_topology_requires_one_argument() -> None:
    tb = _toolbox_with_transport(lambda r: httpx.Response(200, json={}))
    out = query_topology_impl(toolbox=tb)
    assert out["error"]


def test_query_topology_handles_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    tb = _toolbox_with_transport(handler)
    out = query_topology_impl(toolbox=tb, node_id="nope")
    assert out["error"] == "not_found"


def test_query_topology_handles_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    tb = _toolbox_with_transport(handler)
    out = query_topology_impl(toolbox=tb, prefix="10.0.0.0/24")
    assert "unreachable" in out["error"]


# ── get_metrics ──────────────────────────────────────────────────────────────


_FLUX_CSV = (
    "#group,false,false,true,true,false,false\r\n"
    "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double\r\n"
    "#default,_result,,,,,\r\n"
    ",result,table,_start,_stop,_time,_value\r\n"
    ",,0,2026-05-04T00:00:00Z,2026-05-04T00:30:00Z,2026-05-04T00:00:00Z,1.5\r\n"
    ",,0,2026-05-04T00:00:00Z,2026-05-04T00:30:00Z,2026-05-04T00:01:00Z,2.5\r\n"
    ",,0,2026-05-04T00:00:00Z,2026-05-04T00:30:00Z,2026-05-04T00:02:00Z,3.5\r\n"
)


def test_get_metrics_parses_flux_csv() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8")
        assert request.url.params.get("org") == "org"
        return httpx.Response(200, text=_FLUX_CSV)

    tb = _toolbox_with_transport(handler)
    out = get_metrics_impl(
        toolbox=tb, target="10.0.0.5", metric="bytes", lookback_minutes=15
    )
    assert "error" not in out
    assert out["point_count"] == 3
    assert out["points"][0]["value"] == 1.5
    assert out["downsampled"] is False
    assert "/api/v2/query" in captured["url"]
    # Sanity-check the Flux body uses our values.
    assert "10.0.0.5" in captured["body"]
    assert 'r._field == "bytes"' in captured["body"]


def test_get_metrics_requires_target_and_metric() -> None:
    tb = _toolbox_with_transport(lambda r: httpx.Response(200, text=""))
    assert "error" in get_metrics_impl(toolbox=tb, target="", metric="bytes")
    assert "error" in get_metrics_impl(toolbox=tb, target="x", metric="")


def test_get_metrics_handles_influx_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    tb = _toolbox_with_transport(handler)
    out = get_metrics_impl(toolbox=tb, target="x", metric="bytes")
    assert out["error"].startswith("influxdb returned HTTP 500")


def test_get_metrics_sanitizes_target_and_metric_chars(monkeypatch) -> None:
    """Hostile characters must not bleed into the Flux query."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200, text="")

    tb = _toolbox_with_transport(handler)
    get_metrics_impl(
        toolbox=tb,
        target='10.0.0.5"; OR 1=1 --',
        metric='bytes"; DROP',
    )
    assert '"; OR' not in captured["body"]
    assert "DROP" not in captured["body"]


def test_downsample_caps_to_max_points() -> None:
    pts = [{"ts": str(i), "value": float(i)} for i in range(200)]
    out, downsampled = _downsample(pts, max_points=50)
    assert downsampled is True
    assert len(out) == 50
    # First point preserved, last point preserved (we anchor it).
    assert out[0]["value"] == 0.0
    assert out[-1]["value"] == 199.0


def test_parse_flux_csv_handles_empty_string() -> None:
    assert _parse_flux_csv("") == []


# ── search_runbooks ──────────────────────────────────────────────────────────


def test_search_runbooks_returns_chunks_with_citations() -> None:
    chunk = RunbookChunk(
        id="00000000-0000-0000-0000-000000000001",
        source_file="bgp_flap_troubleshooting.md",
        section_title="Common Root Causes",
        content="Flapping is often caused by hold-timer mismatch.",
        metadata={"anomaly_type": "bgp"},
        score=0.92,
    )

    def stub_search(query: str, k: int) -> list[RunbookChunk]:
        assert query == "bgp flap"
        assert k == 3
        return [chunk]

    tb = _toolbox_with_transport(
        lambda r: httpx.Response(200, json={}),
        runbook_searcher=stub_search,
    )
    out = search_runbooks_impl(toolbox=tb, query="bgp flap", k=3)
    assert out["result_count"] == 1
    assert out["results"][0]["citation"] == (
        "bgp_flap_troubleshooting.md#Common Root Causes"
    )
    assert out["results"][0]["score"] == 0.92


def test_search_runbooks_rejects_empty_query() -> None:
    tb = _toolbox_with_transport(lambda r: httpx.Response(200, json={}))
    out = search_runbooks_impl(toolbox=tb, query="   ")
    assert out["error"]


def test_search_runbooks_propagates_search_errors() -> None:
    def boom(_q: str, _k: int) -> list[RunbookChunk]:
        raise RuntimeError("pgvector down")

    tb = _toolbox_with_transport(
        lambda r: httpx.Response(200, json={}), runbook_searcher=boom
    )
    out = search_runbooks_impl(toolbox=tb, query="anything")
    assert "pgvector down" in out["error"]


# ── get_recent_events ────────────────────────────────────────────────────────


def test_get_recent_events_filters_by_prefix() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        captured["body"] = body.decode("utf-8") if body else ""
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "hits": {
                    "hits": [
                        {
                            "_index": "bgp-events-2026.05.04",
                            "_source": {
                                "timestamp": "2026-05-04T00:00:00Z",
                                "event_type": "withdraw",
                                "prefix": "10.0.0.0/16",
                                "peer_ip": "192.0.2.1",
                            },
                        }
                    ]
                }
            },
        )

    tb = _toolbox_with_transport(handler)
    out = get_recent_events_impl(toolbox=tb, prefix="10.0.0.0/16")
    assert out["result_count"] == 1
    assert out["events"][0]["event_type"] == "withdraw"
    assert "10.0.0.0/16" in captured["body"]
    assert "/bgp-events-*,anomalies-*/_search" in captured["path"]


def test_get_recent_events_handles_missing_indices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="no such index")

    tb = _toolbox_with_transport(handler)
    out = get_recent_events_impl(toolbox=tb, prefix="10.0.0.0/24")
    assert out["events"] == []
    assert out["note"]


def test_get_recent_events_handles_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    tb = _toolbox_with_transport(handler)
    out = get_recent_events_impl(toolbox=tb, node_id="router-1")
    assert "unreachable" in out["error"]


# ── invoke_tool dispatcher ───────────────────────────────────────────────────


def test_invoke_tool_dispatches_known_names() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nodes": [], "edges": []})

    tb = _toolbox_with_transport(handler)
    out = invoke_tool(tb, "query_topology", {"node_id": "router-1"})
    assert "error" not in out


def test_invoke_tool_unknown_name() -> None:
    tb = _toolbox_with_transport(lambda r: httpx.Response(200, json={}))
    assert invoke_tool(tb, "nonexistent", {}) == {"error": "unknown tool: nonexistent"}
