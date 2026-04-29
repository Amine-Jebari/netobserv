"""Synchronous InfluxDB 2.x query helpers for the baseline trainer.

Uses ``httpx`` directly against the Flux ``/api/v2/query`` endpoint so we don't
pull in the heavier ``influxdb-client`` SDK. Output is parsed straight into a
pandas DataFrame.
"""

from __future__ import annotations

import io

import httpx
import pandas as pd

from .logging_setup import get_logger

log = get_logger(__name__)


_FLOWS_FLUX = """
from(bucket: "{bucket}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "flows")
  |> filter(fn: (r) => r._field == "packets" or r._field == "bytes")
  |> pivot(rowKey:["_time","srcaddr","dstaddr","protocol"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time","srcaddr","dstaddr","protocol","packets","bytes"])
"""

_SNMP_FLUX = """
from(bucket: "{bucket}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "snmp")
  |> filter(fn: (r) => r._field == "value")
  |> keep(columns: ["_time","device_id","interface","metric","_value"])
  |> rename(columns: {{_value: "value"}})
"""


class InfluxQueryError(RuntimeError):
    """Raised when InfluxDB returns a non-2xx response."""


def _post_flux(
    *,
    url: str,
    org: str,
    token: str,
    flux: str,
    timeout_seconds: float,
) -> str:
    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/vnd.flux",
        "Accept": "application/csv",
    }
    try:
        resp = httpx.post(
            f"{url.rstrip('/')}/api/v2/query",
            params={"org": org},
            content=flux.encode("utf-8"),
            headers=headers,
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise InfluxQueryError(f"InfluxDB request failed: {exc}") from exc

    if resp.status_code >= 400:
        raise InfluxQueryError(
            f"InfluxDB query returned {resp.status_code}: {resp.text[:500]}"
        )
    return resp.text


def _flux_csv_to_df(csv_text: str) -> pd.DataFrame:
    """Parse Flux annotated CSV output into a DataFrame.

    Flux returns ``#group``, ``#datatype``, ``#default`` annotation rows
    prefixed with ``#`` followed by the header and data rows. We strip the
    annotations and let pandas parse the rest.
    """

    lines = [line for line in csv_text.splitlines() if line and not line.startswith("#")]
    if not lines:
        return pd.DataFrame()
    df = pd.read_csv(io.StringIO("\n".join(lines)))
    drop = [c for c in ("", "result", "table", "_start", "_stop", "_measurement") if c in df.columns]
    if drop:
        df = df.drop(columns=drop)
    return df


def query_flows(
    *,
    url: str,
    org: str,
    token: str,
    bucket: str,
    lookback_hours: int,
    timeout_seconds: float = 120.0,
) -> pd.DataFrame:
    """Return the last ``lookback_hours`` of flow points as a DataFrame.

    Columns: ``timestamp, srcaddr, dstaddr, protocol, packets, bytes``.
    """

    flux = _FLOWS_FLUX.format(bucket=bucket, hours=lookback_hours)
    csv_text = _post_flux(url=url, org=org, token=token, flux=flux, timeout_seconds=timeout_seconds)
    df = _flux_csv_to_df(csv_text)
    if df.empty:
        log.warning("influx.flows.empty", lookback_hours=lookback_hours)
        return pd.DataFrame(columns=["timestamp", "srcaddr", "dstaddr", "protocol", "packets", "bytes"])
    df = df.rename(columns={"_time": "timestamp"})
    log.info("influx.flows.loaded", rows=len(df))
    return df


def query_snmp(
    *,
    url: str,
    org: str,
    token: str,
    bucket: str,
    lookback_hours: int,
    timeout_seconds: float = 120.0,
) -> pd.DataFrame:
    """Return the last ``lookback_hours`` of SNMP samples as a DataFrame.

    Columns: ``timestamp, device_id, interface, metric, value``.
    """

    flux = _SNMP_FLUX.format(bucket=bucket, hours=lookback_hours)
    csv_text = _post_flux(url=url, org=org, token=token, flux=flux, timeout_seconds=timeout_seconds)
    df = _flux_csv_to_df(csv_text)
    if df.empty:
        log.warning("influx.snmp.empty", lookback_hours=lookback_hours)
        return pd.DataFrame(columns=["timestamp", "device_id", "interface", "metric", "value"])
    df = df.rename(columns={"_time": "timestamp"})
    log.info("influx.snmp.loaded", rows=len(df))
    return df
