"""LangChain tool definitions used by the agent during an investigation.

Four tools are exposed:

* ``query_topology``     - resolve nodes / prefixes via the topology service.
* ``get_metrics``        - pull recent time-series points from InfluxDB.
* ``search_runbooks``    - semantic search over the pgvector runbook corpus.
* ``get_recent_events``  - last-N events from OpenSearch (BGP + scored anomalies).

Each tool is a thin, synchronous, side-effect-free function that returns a
small JSON-serializable dict. The LangChain Gemini binding sees it as a
function-calling tool; the agent loop in :mod:`agent` invokes the underlying
Python implementation directly (via :func:`invoke_tool`) so we can reliably
log results and bound output size.

Tools are constructed via :func:`build_tools`, which captures a :class:`Toolbox`
holding the live HTTP client, the runbook search callable, and Settings. Tests
build a Toolbox with a stub ``runbook_searcher`` and an httpx mock transport.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from langchain_core.tools import StructuredTool

from .config import Settings, get_settings
from .logging_setup import get_logger
from .rag import RunbookChunk, search_runbooks as rag_search_runbooks

log = get_logger(__name__)


# ── Toolbox: holds the live HTTP clients + Settings ──────────────────────────


@dataclass
class Toolbox:
    """Live dependencies the four tool callables share.

    Constructed once per agent process. ``httpx.Client`` is reused across
    calls; thread safety is fine because tool calls execute one at a time
    inside the agent loop.
    """

    settings: Settings
    http: httpx.Client
    runbook_searcher: Callable[[str, int], list[RunbookChunk]]

    @classmethod
    def build(cls, settings: Settings | None = None) -> "Toolbox":
        settings = settings or get_settings()
        timeout = httpx.Timeout(
            max(
                settings.topology_request_timeout_seconds,
                settings.influxdb_request_timeout_seconds,
                settings.opensearch_request_timeout_seconds,
            )
        )
        http = httpx.Client(timeout=timeout, verify=settings.opensearch_verify_certs)
        return cls(
            settings=settings,
            http=http,
            runbook_searcher=lambda q, k: rag_search_runbooks(q, k=k, settings=settings),
        )

    def close(self) -> None:
        try:
            self.http.close()
        except Exception:  # pragma: no cover - defensive
            pass


# ── Tool 1: query_topology ───────────────────────────────────────────────────


def query_topology_impl(
    *,
    toolbox: Toolbox,
    node_id: str | None = None,
    prefix: str | None = None,
) -> dict[str, Any]:
    """Resolve a node-with-neighbors or a prefix's affected subgraph.

    Exactly one of ``node_id`` / ``prefix`` should be supplied. Both are
    optional in the schema so the LLM can call with whichever it knows.
    """

    if not node_id and not prefix:
        return {"error": "either node_id or prefix is required"}

    base = toolbox.settings.topology_service_url.rstrip("/")
    timeout = toolbox.settings.topology_request_timeout_seconds

    try:
        if node_id:
            resp = toolbox.http.get(
                f"{base}/topology/node/{node_id}", timeout=timeout
            )
        else:
            resp = toolbox.http.get(
                f"{base}/topology/affected",
                params={"prefix": prefix},
                timeout=timeout,
            )
    except httpx.HTTPError as exc:
        log.warning("tool.query_topology.http_error", error=str(exc))
        return {"error": f"topology service unreachable: {exc}"}

    if resp.status_code == 404:
        return {"error": "not_found", "detail": resp.text[:200]}
    if resp.status_code >= 400:
        return {
            "error": f"topology returned HTTP {resp.status_code}",
            "detail": resp.text[:200],
        }

    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError):
        return {"error": "topology returned invalid JSON"}

    return _summarize_topology(data, node_id=node_id, prefix=prefix)


def _summarize_topology(
    data: dict[str, Any],
    *,
    node_id: str | None,
    prefix: str | None,
) -> dict[str, Any]:
    """Project the verbose topology payload into a small agent-friendly shape."""

    nodes_in = data.get("nodes") or []
    edges_in = data.get("edges") or data.get("links") or []

    nodes_out: list[dict[str, Any]] = []
    for n in nodes_in[:20]:
        if not isinstance(n, dict):
            continue
        nodes_out.append(
            {
                "id": n.get("node_id") or n.get("id"),
                "kind": n.get("kind") or n.get("type"),
                "ip": n.get("ip") or n.get("address"),
                "as_number": n.get("as_number") or n.get("asn"),
                "status": n.get("status"),
            }
        )

    edges_out: list[dict[str, Any]] = []
    for e in edges_in[:30]:
        if not isinstance(e, dict):
            continue
        edges_out.append(
            {
                "src": e.get("src") or e.get("from"),
                "dst": e.get("dst") or e.get("to"),
                "utilization": e.get("utilization") or e.get("utilization_pct"),
                "status": e.get("status"),
            }
        )

    summary: dict[str, Any] = {
        "query": {"node_id": node_id, "prefix": prefix},
        "node_count": len(nodes_in),
        "edge_count": len(edges_in),
        "nodes": nodes_out,
        "edges": edges_out,
    }
    if "node" in data:
        node = data["node"]
        summary["focus_node"] = {
            "id": node.get("node_id") or node.get("id"),
            "kind": node.get("kind"),
            "ip": node.get("ip"),
            "as_number": node.get("as_number"),
        }
    return summary


# ── Tool 2: get_metrics (InfluxDB) ───────────────────────────────────────────


_FLUX_TEMPLATE = """
from(bucket: "{bucket}")
  |> range(start: -{minutes}m)
  |> filter(fn: (r) => r._field == "{metric}" or r.metric == "{metric}")
  |> filter(fn: (r) =>
        r.srcaddr == "{target}" or r.dstaddr == "{target}" or
        r.device_id == "{target}" or r.interface == "{target}")
  |> keep(columns: ["_time","_value"])
  |> sort(columns: ["_time"])
  |> limit(n: 500)
"""


def _build_flux(*, bucket: str, target: str, metric: str, lookback_minutes: int) -> str:
    """Compose the Flux query, sanitizing user-influenced values.

    We restrict ``target`` and ``metric`` to a conservative ASCII set so even
    though the LLM controls the values, the resulting Flux is well-formed.
    """
    safe_target = "".join(
        ch for ch in target if ch.isalnum() or ch in {".", ":", "-", "_", "/"}
    )
    safe_metric = "".join(
        ch for ch in metric if ch.isalnum() or ch in {"_", "-"}
    )
    return _FLUX_TEMPLATE.format(
        bucket=bucket,
        minutes=int(lookback_minutes),
        metric=safe_metric,
        target=safe_target,
    )


def get_metrics_impl(
    *,
    toolbox: Toolbox,
    target: str,
    metric: str,
    lookback_minutes: int = 30,
) -> dict[str, Any]:
    """Return downsampled time-series points for ``metric`` filtered to ``target``.

    Output shape::

        {
            "target": "10.0.0.5",
            "metric": "bytes",
            "lookback_minutes": 30,
            "points": [{"ts": "2026-05-04T...", "value": 12345.0}, ...],   # ≤ 50
            "downsampled": True | False
        }
    """
    if not target or not metric:
        return {"error": "target and metric are required"}
    lookback = max(1, min(int(lookback_minutes), 720))

    settings = toolbox.settings
    if not settings.influxdb_token:
        return {"error": "influxdb token not configured"}

    flux = _build_flux(
        bucket=settings.influxdb_bucket,
        target=target,
        metric=metric,
        lookback_minutes=lookback,
    )
    headers = {
        "Authorization": f"Token {settings.influxdb_token}",
        "Content-Type": "application/vnd.flux",
        "Accept": "application/csv",
    }
    url = f"{settings.influxdb_url.rstrip('/')}/api/v2/query"
    try:
        resp = toolbox.http.post(
            url,
            params={"org": settings.influxdb_org},
            content=flux.encode("utf-8"),
            headers=headers,
            timeout=settings.influxdb_request_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        log.warning("tool.get_metrics.http_error", error=str(exc))
        return {"error": f"influxdb unreachable: {exc}"}

    if resp.status_code >= 400:
        return {
            "error": f"influxdb returned HTTP {resp.status_code}",
            "detail": resp.text[:200],
        }

    points = _parse_flux_csv(resp.text)
    points, downsampled = _downsample(points, max_points=50)
    return {
        "target": target,
        "metric": metric,
        "lookback_minutes": lookback,
        "point_count": len(points),
        "downsampled": downsampled,
        "points": points,
    }


def _parse_flux_csv(text: str) -> list[dict[str, Any]]:
    """Parse Flux annotated CSV into ``[{"ts": ISO, "value": float}, ...]``.

    Flux CSV has ``#group``/``#datatype``/``#default`` annotation rows
    (prefixed with ``#``), then a header row, then data rows. Multiple result
    tables are separated by blank lines or repeated header rows.
    """

    header_idx: tuple[int, int] | None = None
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip("\r")
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if "_time" in parts and "_value" in parts:
            try:
                header_idx = (parts.index("_time"), parts.index("_value"))
            except ValueError:
                header_idx = None
            continue
        if header_idx is None or len(parts) <= max(header_idx):
            continue
        ts = parts[header_idx[0]].strip()
        try:
            value = float(parts[header_idx[1]])
        except ValueError:
            continue
        if not ts:
            continue
        out.append({"ts": ts, "value": value})
    return out


def _downsample(
    points: list[dict[str, Any]], *, max_points: int
) -> tuple[list[dict[str, Any]], bool]:
    """Reduce ``points`` to at most ``max_points`` via uniform stride sampling."""
    if len(points) <= max_points or max_points <= 0:
        return points, False
    stride = len(points) / max_points
    sampled: list[dict[str, Any]] = []
    i = 0.0
    while int(i) < len(points) and len(sampled) < max_points:
        sampled.append(points[int(i)])
        i += stride
    if sampled and sampled[-1] is not points[-1]:
        sampled[-1] = points[-1]
    return sampled, True


# ── Tool 3: search_runbooks ──────────────────────────────────────────────────


def search_runbooks_impl(
    *,
    toolbox: Toolbox,
    query: str,
    k: int = 3,
) -> dict[str, Any]:
    """Top-``k`` (default 3) runbook chunks ranked by semantic similarity."""

    if not query or not query.strip():
        return {"error": "query must be non-empty"}
    k = max(1, min(int(k), 10))

    try:
        chunks = toolbox.runbook_searcher(query, k)
    except Exception as exc:
        log.warning("tool.search_runbooks.failed", error=str(exc))
        return {"error": f"runbook search failed: {exc}"}

    return {
        "query": query,
        "result_count": len(chunks),
        "results": [
            {
                "source_file": c.source_file,
                "section_title": c.section_title,
                "score": round(c.score, 4),
                "metadata": c.metadata,
                "content": _truncate(c.content, 1200),
                "citation": f"{c.source_file}#{c.section_title}",
            }
            for c in chunks
        ],
    }


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " …"


# ── Tool 4: get_recent_events (OpenSearch) ───────────────────────────────────


def get_recent_events_impl(
    *,
    toolbox: Toolbox,
    prefix: str | None = None,
    node_id: str | None = None,
    lookback_minutes: int = 30,
) -> dict[str, Any]:
    """Last 20 BGP / anomaly events relevant to ``prefix`` or ``node_id``."""

    lookback = max(1, min(int(lookback_minutes), 1440))
    settings = toolbox.settings
    base = settings.opensearch_url.rstrip("/")
    auth = (
        (settings.opensearch_user, settings.opensearch_password)
        if settings.opensearch_user
        else None
    )

    must: list[dict[str, Any]] = [
        {"range": {"timestamp": {"gte": f"now-{lookback}m"}}}
    ]
    should: list[dict[str, Any]] = []
    if prefix:
        should.append({"term": {"prefix": prefix}})
    if node_id:
        should.append(
            {
                "multi_match": {
                    "query": node_id,
                    "fields": [
                        "peer_ip",
                        "device_id",
                        "node_id",
                        "srcaddr",
                        "dstaddr",
                    ],
                }
            }
        )

    body: dict[str, Any] = {
        "size": 20,
        "sort": [{"timestamp": "desc"}],
        "query": {
            "bool": {
                "must": must,
                **(
                    {"should": should, "minimum_should_match": 1}
                    if should
                    else {}
                ),
            }
        },
    }

    # Search across BGP events and the anomalies-* index pattern so the
    # agent gets a unified timeline.
    index = (
        f"{settings.opensearch_bgp_index_pattern},"
        f"{settings.opensearch_anomalies_index_pattern}"
    )

    try:
        resp = toolbox.http.post(
            f"{base}/{index}/_search",
            json=body,
            auth=auth,
            timeout=settings.opensearch_request_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        log.warning("tool.get_recent_events.http_error", error=str(exc))
        return {"error": f"opensearch unreachable: {exc}"}

    if resp.status_code == 404:
        return {"events": [], "result_count": 0, "note": "no matching indices"}
    if resp.status_code >= 400:
        return {
            "error": f"opensearch returned HTTP {resp.status_code}",
            "detail": resp.text[:200],
        }

    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError):
        return {"error": "opensearch returned invalid JSON"}

    hits = (data.get("hits") or {}).get("hits") or []
    events = [
        {
            "index": h.get("_index"),
            **{k: v for k, v in (h.get("_source") or {}).items() if v is not None},
        }
        for h in hits
    ]
    return {
        "filters": {
            "prefix": prefix,
            "node_id": node_id,
            "lookback_minutes": lookback,
        },
        "result_count": len(events),
        "events": events,
    }


# ── LangChain tool registry ──────────────────────────────────────────────────


_TOOL_DESCRIPTIONS = {
    "query_topology": (
        "Return the network topology surrounding a node OR the subgraph affected "
        "by a BGP prefix. Provide exactly one of node_id (e.g. 'router-1' or an "
        "IP) or prefix (CIDR like '10.0.0.0/16'). Use this to understand "
        "blast radius and neighboring devices."
    ),
    "get_metrics": (
        "Pull a recent time-series from InfluxDB for the given target (IP, "
        "device_id, or interface) and metric (e.g. 'bytes', 'packets', "
        "'utilization', 'latency_ms'). Returns up to 50 (ts, value) points "
        "downsampled from the lookback window. lookback_minutes defaults to 30."
    ),
    "search_runbooks": (
        "Semantic search over the curated runbook corpus. Returns the top-3 "
        "most relevant chunks with citations. Use this for known-pattern "
        "diagnoses and remediation steps. The 'citation' field on each result "
        "MUST be quoted in the final verdict for any claim it informs."
    ),
    "get_recent_events": (
        "Last 20 BGP/network events from OpenSearch filtered by prefix or "
        "node_id. Use this to construct a timeline (e.g. did a flap precede "
        "the traffic anomaly?). lookback_minutes defaults to 30."
    ),
}


def build_tools(toolbox: Toolbox) -> list[StructuredTool]:
    """Construct the four LangChain ``StructuredTool`` objects.

    The returned tools delegate to the ``*_impl`` functions defined above; we
    use ``StructuredTool.from_function`` so LangChain extracts the JSON schema
    from each wrapper's signature, which is what Gemini's function-calling
    API expects.
    """

    def _qt(node_id: str | None = None, prefix: str | None = None) -> dict[str, Any]:
        """Look up topology by node_id or prefix."""
        return query_topology_impl(toolbox=toolbox, node_id=node_id, prefix=prefix)

    def _gm(target: str, metric: str, lookback_minutes: int = 30) -> dict[str, Any]:
        """Fetch a metric time-series for target."""
        return get_metrics_impl(
            toolbox=toolbox,
            target=target,
            metric=metric,
            lookback_minutes=lookback_minutes,
        )

    def _sr(query: str, k: int = 3) -> dict[str, Any]:
        """Semantic search over runbooks."""
        return search_runbooks_impl(toolbox=toolbox, query=query, k=k)

    def _re(
        prefix: str | None = None,
        node_id: str | None = None,
        lookback_minutes: int = 30,
    ) -> dict[str, Any]:
        """Recent BGP / anomaly events."""
        return get_recent_events_impl(
            toolbox=toolbox,
            prefix=prefix,
            node_id=node_id,
            lookback_minutes=lookback_minutes,
        )

    return [
        StructuredTool.from_function(
            func=_qt,
            name="query_topology",
            description=_TOOL_DESCRIPTIONS["query_topology"],
        ),
        StructuredTool.from_function(
            func=_gm,
            name="get_metrics",
            description=_TOOL_DESCRIPTIONS["get_metrics"],
        ),
        StructuredTool.from_function(
            func=_sr,
            name="search_runbooks",
            description=_TOOL_DESCRIPTIONS["search_runbooks"],
        ),
        StructuredTool.from_function(
            func=_re,
            name="get_recent_events",
            description=_TOOL_DESCRIPTIONS["get_recent_events"],
        ),
    ]


# ── Direct dispatcher used by agent.py ───────────────────────────────────────


def invoke_tool(
    toolbox: Toolbox,
    name: str,
    args: dict[str, Any] | None,
) -> dict[str, Any]:
    """Execute the named tool directly with ``args`` (no LangChain wrapper).

    The agent loop in :mod:`agent` uses this so we can:
      (a) coerce argument types defensively,
      (b) capture per-step structured logs,
      (c) avoid the StructuredTool reflection cost for every call.
    """
    args = dict(args or {})
    if name == "query_topology":
        return query_topology_impl(
            toolbox=toolbox,
            node_id=args.get("node_id"),
            prefix=args.get("prefix"),
        )
    if name == "get_metrics":
        return get_metrics_impl(
            toolbox=toolbox,
            target=str(args.get("target") or ""),
            metric=str(args.get("metric") or ""),
            lookback_minutes=int(args.get("lookback_minutes") or 30),
        )
    if name == "search_runbooks":
        return search_runbooks_impl(
            toolbox=toolbox,
            query=str(args.get("query") or ""),
            k=int(args.get("k") or 3),
        )
    if name == "get_recent_events":
        return get_recent_events_impl(
            toolbox=toolbox,
            prefix=args.get("prefix"),
            node_id=args.get("node_id"),
            lookback_minutes=int(args.get("lookback_minutes") or 30),
        )
    return {"error": f"unknown tool: {name}"}
