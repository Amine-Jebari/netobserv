"""Synchronous OpenSearch query helper for BGP-event extraction.

The collector indexes BGP events into ``bgp-events-YYYY.MM.DD`` (one index per
UTC day). We pull the last N hours via a single ``_search`` with a
``search_after`` paginated cursor. If OpenSearch is unreachable or empty, the
caller receives an empty DataFrame and trains the rest of the bundle anyway.
"""

from __future__ import annotations

from typing import Any

import httpx
import pandas as pd

from .logging_setup import get_logger

log = get_logger(__name__)


class OpenSearchQueryError(RuntimeError):
    """Raised when OpenSearch returns a non-2xx response."""


def query_bgp_events(
    *,
    url: str,
    user: str,
    password: str,
    index_pattern: str,
    lookback_hours: int,
    page_size: int = 5000,
    verify_certs: bool = False,
    timeout_seconds: float = 60.0,
) -> pd.DataFrame:
    """Pull the last ``lookback_hours`` of BGP events as a DataFrame.

    Columns: ``timestamp, event_type, peer_ip, prefix, as_path_length``.
    Returns an empty DataFrame on connection failure (logged at WARNING) so the
    rest of the baseline pipeline can still complete.
    """

    auth: tuple[str, str] | None = (user, password) if user else None
    base = url.rstrip("/")
    body: dict[str, Any] = {
        "size": page_size,
        "sort": [{"timestamp": "asc"}, {"_id": "asc"}],
        "_source": ["timestamp", "event_type", "peer_ip", "prefix", "as_path"],
        "query": {"range": {"timestamp": {"gte": f"now-{lookback_hours}h"}}},
    }

    rows: list[dict[str, Any]] = []
    search_after: list[Any] | None = None

    try:
        with httpx.Client(
            base_url=base,
            auth=auth,
            verify=verify_certs,
            timeout=timeout_seconds,
        ) as client:
            while True:
                payload = dict(body)
                if search_after is not None:
                    payload["search_after"] = search_after
                resp = client.post(f"/{index_pattern}/_search", json=payload)
                if resp.status_code == 404:
                    log.warning("opensearch.bgp.no_indices", index_pattern=index_pattern)
                    return _empty()
                if resp.status_code >= 400:
                    raise OpenSearchQueryError(
                        f"OpenSearch returned {resp.status_code}: {resp.text[:500]}"
                    )

                data = resp.json()
                hits = data.get("hits", {}).get("hits", []) or []
                if not hits:
                    break

                for hit in hits:
                    src = hit.get("_source", {}) or {}
                    rows.append(
                        {
                            "timestamp": src.get("timestamp"),
                            "event_type": src.get("event_type"),
                            "peer_ip": src.get("peer_ip"),
                            "prefix": src.get("prefix"),
                            "as_path_length": _as_path_length(src.get("as_path")),
                        }
                    )

                if len(hits) < page_size:
                    break
                search_after = hits[-1].get("sort")
                if not search_after:
                    break
    except httpx.HTTPError as exc:
        # Real-world ops: never let a flaky search index break the rest of the
        # training pipeline. Skip BGP, log loudly, train traffic + SNMP only.
        log.warning("opensearch.bgp.unreachable", error=str(exc))
        return _empty()

    if not rows:
        log.warning("opensearch.bgp.empty", lookback_hours=lookback_hours)
        return _empty()

    df = pd.DataFrame(rows)
    log.info("opensearch.bgp.loaded", rows=len(df))
    return df


def _empty() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["timestamp", "event_type", "peer_ip", "prefix", "as_path_length"]
    )


def _as_path_length(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str):
        return len([p for p in value.split() if p])
    return 0
