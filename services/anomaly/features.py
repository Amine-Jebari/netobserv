"""Per-minute feature engineering for the three anomaly detection models.

This module is the single source of truth for how raw telemetry (VPC flow
records, BGP events, SNMP samples) is rolled up into the feature vectors that
both the baseline trainer and the real-time scoring engine consume. Keeping it
here — and importing it from both Phase 3.1 (baseline) and Phase 3.2 (engine) —
guarantees training and inference compute features identically.

Three feature spaces, one per model:

* ``TRAFFIC_FEATURES`` — per-minute volume/diversity, fed to Isolation Forest.
* ``BGP_FEATURES`` — per-minute BGP event frequencies, fed to LOF.
* ``SNMP_FEATURES`` — per-minute SNMP metric aggregates, fed to MCD (Mahalanobis).

Each public function takes a *long* DataFrame of raw records and returns a
*wide* per-minute DataFrame with a ``minute`` index column followed by the
feature columns in the canonical order declared above.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ── Canonical feature schemas ─────────────────────────────────────────────────
# The order here is the order the ML models will see at training and inference.
# Never reorder these without bumping the bundle version.

TRAFFIC_FEATURES: list[str] = [
    "total_bytes",
    "total_packets",
    "num_flows",
    "unique_src_count",
    "unique_dst_count",
    "top_pair_share",
    "bytes_per_packet",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
]

BGP_FEATURES: list[str] = [
    "announce_count",
    "withdraw_count",
    "update_count",
    "unique_prefixes",
    "unique_peers",
    "as_path_mean",
    "as_path_std",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
]

SNMP_FEATURES: list[str] = [
    "cpu_load_mean",
    "cpu_load_max",
    "latency_ms_mean",
    "latency_ms_p95",
    "if_utilization_mean",
    "if_utilization_max",
    "if_in_errors_total",
    "if_out_errors_total",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
]


def add_diurnal(df: pd.DataFrame, ts_col: str = "minute") -> pd.DataFrame:
    """Append hour-of-day and day-of-week sine/cosine encodings.

    Cyclic encoding avoids the discontinuity at midnight / Sunday-Monday a
    plain integer hour or weekday would produce.
    """

    if df.empty:
        out = df.copy()
        for col in ("hour_sin", "hour_cos", "dow_sin", "dow_cos"):
            out[col] = pd.Series(dtype="float64")
        return out

    ts = pd.to_datetime(df[ts_col], utc=True)
    hour = ts.dt.hour + ts.dt.minute / 60.0
    dow = ts.dt.dayofweek

    out = df.copy()
    out["hour_sin"] = np.sin(2.0 * np.pi * hour / 24.0)
    out["hour_cos"] = np.cos(2.0 * np.pi * hour / 24.0)
    out["dow_sin"] = np.sin(2.0 * np.pi * dow / 7.0)
    out["dow_cos"] = np.cos(2.0 * np.pi * dow / 7.0)
    return out


def _empty_frame(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in ["minute", *columns]})


# ── Traffic ───────────────────────────────────────────────────────────────────


def make_traffic_features(flows: pd.DataFrame) -> pd.DataFrame:
    """Roll a long flow-record table into per-minute traffic feature rows.

    Required input columns: ``timestamp`` (parseable to UTC), ``srcaddr``,
    ``dstaddr``, ``packets``, ``bytes``.
    """

    if flows.empty:
        return _empty_frame(TRAFFIC_FEATURES)

    df = flows.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["minute"] = df["timestamp"].dt.floor("1min")
    df["packets"] = pd.to_numeric(df["packets"], errors="coerce").fillna(0).astype("int64")
    df["bytes"] = pd.to_numeric(df["bytes"], errors="coerce").fillna(0).astype("int64")

    pair_bytes = (
        df.groupby(["minute", "srcaddr", "dstaddr"], observed=True)["bytes"]
        .sum()
    )
    minute_bytes = pair_bytes.groupby(level="minute").sum()
    top_pair_bytes = pair_bytes.groupby(level="minute").max()
    top_pair_share = (top_pair_bytes / minute_bytes.replace(0, np.nan)).fillna(0.0)

    by_min = df.groupby("minute", observed=True).agg(
        total_bytes=("bytes", "sum"),
        total_packets=("packets", "sum"),
        num_flows=("bytes", "size"),
        unique_src_count=("srcaddr", "nunique"),
        unique_dst_count=("dstaddr", "nunique"),
    )
    by_min["top_pair_share"] = top_pair_share.reindex(by_min.index).fillna(0.0)
    by_min["bytes_per_packet"] = (
        by_min["total_bytes"] / by_min["total_packets"].replace(0, np.nan)
    ).fillna(0.0)

    out = by_min.reset_index()
    out = add_diurnal(out, "minute")
    return _finalize(out, TRAFFIC_FEATURES)


# ── BGP ───────────────────────────────────────────────────────────────────────


def make_bgp_features(events: pd.DataFrame) -> pd.DataFrame:
    """Roll a long BGP-event table into per-minute frequency feature rows.

    Required input columns: ``timestamp``, ``event_type`` (ANNOUNCE | WITHDRAW
    | UPDATE), ``peer_ip``, ``prefix``, ``as_path_length`` (int).
    """

    if events.empty:
        return _empty_frame(BGP_FEATURES)

    df = events.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["minute"] = df["timestamp"].dt.floor("1min")
    df["as_path_length"] = pd.to_numeric(df["as_path_length"], errors="coerce")

    by_min = df.groupby("minute", observed=True).agg(
        announce_count=("event_type", lambda s: int((s == "ANNOUNCE").sum())),
        withdraw_count=("event_type", lambda s: int((s == "WITHDRAW").sum())),
        update_count=("event_type", lambda s: int((s == "UPDATE").sum())),
        unique_prefixes=("prefix", "nunique"),
        unique_peers=("peer_ip", "nunique"),
        as_path_mean=("as_path_length", "mean"),
        as_path_std=("as_path_length", "std"),
    )
    by_min[["as_path_mean", "as_path_std"]] = (
        by_min[["as_path_mean", "as_path_std"]].fillna(0.0)
    )

    out = by_min.reset_index()
    out = add_diurnal(out, "minute")
    return _finalize(out, BGP_FEATURES)


# ── SNMP ──────────────────────────────────────────────────────────────────────


def _p95(values: pd.Series) -> float:
    arr = values.dropna().to_numpy(dtype=float)
    if arr.size == 0:
        return 0.0
    return float(np.percentile(arr, 95))


def make_snmp_features(samples: pd.DataFrame) -> pd.DataFrame:
    """Roll a long SNMP-sample table into per-minute aggregate feature rows.

    Required input columns: ``timestamp``, ``metric`` (one of the SNMP metric
    names emitted by the simulator/collector), ``value`` (float).
    """

    if samples.empty:
        return _empty_frame(SNMP_FEATURES)

    df = samples.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["minute"] = df["timestamp"].dt.floor("1min")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")

    agg_mean = df.pivot_table(index="minute", columns="metric", values="value", aggfunc="mean")
    agg_max = df.pivot_table(index="minute", columns="metric", values="value", aggfunc="max")
    agg_p95 = df.pivot_table(index="minute", columns="metric", values="value", aggfunc=_p95)
    agg_sum = df.pivot_table(index="minute", columns="metric", values="value", aggfunc="sum")

    def _col(table: pd.DataFrame, name: str) -> pd.Series:
        if name in table.columns:
            return table[name].fillna(0.0)
        return pd.Series(0.0, index=table.index)

    by_min = pd.DataFrame(index=agg_mean.index)
    by_min["cpu_load_mean"] = _col(agg_mean, "cpuLoad")
    by_min["cpu_load_max"] = _col(agg_max, "cpuLoad")
    by_min["latency_ms_mean"] = _col(agg_mean, "latencyMs")
    by_min["latency_ms_p95"] = _col(agg_p95, "latencyMs")
    by_min["if_utilization_mean"] = _col(agg_mean, "ifUtilizationPct")
    by_min["if_utilization_max"] = _col(agg_max, "ifUtilizationPct")
    by_min["if_in_errors_total"] = _col(agg_sum, "ifInErrors")
    by_min["if_out_errors_total"] = _col(agg_sum, "ifOutErrors")

    out = by_min.reset_index()
    out = add_diurnal(out, "minute")
    return _finalize(out, SNMP_FEATURES)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _finalize(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Coerce types, sort, and pin to the canonical column order."""

    if df.empty:
        return _empty_frame(columns)
    out = df[["minute", *columns]].copy()
    for col in columns:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0).astype("float64")
    out = out.sort_values("minute").reset_index(drop=True)
    return out


def feature_stats(df: pd.DataFrame, columns: list[str]) -> dict[str, dict[str, float]]:
    """Per-column mean/std snapshot, for inference-time normalization parity."""

    if df.empty:
        return {c: {"mean": 0.0, "std": 1.0} for c in columns}
    stats: dict[str, dict[str, float]] = {}
    for col in columns:
        vals = df[col].astype(float)
        std = float(vals.std(ddof=0))
        stats[col] = {"mean": float(vals.mean()), "std": std if std > 0 else 1.0}
    return stats
