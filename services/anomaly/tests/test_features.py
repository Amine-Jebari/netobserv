"""Determinism + correctness tests for the shared feature engineering module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from services.anomaly.features import (
    BGP_FEATURES,
    SNMP_FEATURES,
    TRAFFIC_FEATURES,
    add_diurnal,
    feature_stats,
    make_bgp_features,
    make_snmp_features,
    make_traffic_features,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

BASE_TS = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)


def _flows_fixture() -> pd.DataFrame:
    """Three flows in minute 12:00, two in minute 12:01."""

    rows = [
        {"timestamp": BASE_TS,                          "srcaddr": "10.0.0.1", "dstaddr": "8.8.8.8", "packets": 10, "bytes": 1500},
        {"timestamp": BASE_TS + timedelta(seconds=15),  "srcaddr": "10.0.0.2", "dstaddr": "8.8.8.8", "packets":  5, "bytes":  800},
        {"timestamp": BASE_TS + timedelta(seconds=30),  "srcaddr": "10.0.0.1", "dstaddr": "8.8.4.4", "packets": 15, "bytes": 2300},
        {"timestamp": BASE_TS + timedelta(seconds=70),  "srcaddr": "10.0.0.3", "dstaddr": "1.1.1.1", "packets":  8, "bytes": 1200},
        {"timestamp": BASE_TS + timedelta(seconds=90),  "srcaddr": "10.0.0.1", "dstaddr": "1.1.1.1", "packets": 12, "bytes": 1800},
    ]
    return pd.DataFrame(rows)


def _bgp_fixture() -> pd.DataFrame:
    rows = [
        {"timestamp": BASE_TS,                          "event_type": "ANNOUNCE", "peer_ip": "10.1.1.1", "prefix": "10.0.0.0/16", "as_path_length": 3},
        {"timestamp": BASE_TS + timedelta(seconds=10),  "event_type": "ANNOUNCE", "peer_ip": "10.1.1.2", "prefix": "10.0.0.0/16", "as_path_length": 4},
        {"timestamp": BASE_TS + timedelta(seconds=20),  "event_type": "WITHDRAW", "peer_ip": "10.1.1.1", "prefix": "10.0.1.0/24", "as_path_length": 5},
        {"timestamp": BASE_TS + timedelta(seconds=70),  "event_type": "UPDATE",   "peer_ip": "10.1.1.3", "prefix": "10.0.2.0/24", "as_path_length": 2},
    ]
    return pd.DataFrame(rows)


def _snmp_fixture() -> pd.DataFrame:
    rows = [
        {"timestamp": BASE_TS,                         "metric": "cpuLoad",          "value": 0.30},
        {"timestamp": BASE_TS + timedelta(seconds=15), "metric": "cpuLoad",          "value": 0.50},
        {"timestamp": BASE_TS + timedelta(seconds=30), "metric": "cpuLoad",          "value": 0.70},
        {"timestamp": BASE_TS,                         "metric": "latencyMs",        "value": 5.0},
        {"timestamp": BASE_TS + timedelta(seconds=20), "metric": "latencyMs",        "value": 7.0},
        {"timestamp": BASE_TS + timedelta(seconds=40), "metric": "latencyMs",        "value": 50.0},
        {"timestamp": BASE_TS,                         "metric": "ifUtilizationPct", "value": 20.0},
        {"timestamp": BASE_TS + timedelta(seconds=30), "metric": "ifUtilizationPct", "value": 65.0},
        {"timestamp": BASE_TS,                         "metric": "ifInErrors",       "value": 1.0},
        {"timestamp": BASE_TS + timedelta(seconds=20), "metric": "ifInErrors",       "value": 2.0},
        {"timestamp": BASE_TS,                         "metric": "ifOutErrors",      "value": 0.0},
    ]
    return pd.DataFrame(rows)


# ── Determinism ───────────────────────────────────────────────────────────────


def test_traffic_features_deterministic() -> None:
    a = make_traffic_features(_flows_fixture())
    b = make_traffic_features(_flows_fixture())
    pd.testing.assert_frame_equal(a, b)


def test_bgp_features_deterministic() -> None:
    a = make_bgp_features(_bgp_fixture())
    b = make_bgp_features(_bgp_fixture())
    pd.testing.assert_frame_equal(a, b)


def test_snmp_features_deterministic() -> None:
    a = make_snmp_features(_snmp_fixture())
    b = make_snmp_features(_snmp_fixture())
    pd.testing.assert_frame_equal(a, b)


# ── Aggregation correctness ───────────────────────────────────────────────────


def test_traffic_features_aggregation_values() -> None:
    out = make_traffic_features(_flows_fixture())

    assert list(out.columns) == ["minute", *TRAFFIC_FEATURES]
    assert len(out) == 2

    minute1, minute2 = out.iloc[0], out.iloc[1]
    assert minute1["total_bytes"] == 1500 + 800 + 2300
    assert minute1["total_packets"] == 10 + 5 + 15
    assert minute1["num_flows"] == 3
    assert minute1["unique_src_count"] == 2
    assert minute1["unique_dst_count"] == 2
    assert minute1["bytes_per_packet"] == pytest.approx(4600 / 30)

    # Minute 1: pair (10.0.0.1 → 8.8.4.4)=2300 of 4600 → share=0.5
    assert minute1["top_pair_share"] == pytest.approx(2300 / 4600)
    # Minute 2 has two flows from different src/dst pairs of equal byte total
    # share — top share = 1800 / (1800 + 1200) = 0.6
    assert minute2["top_pair_share"] == pytest.approx(1800 / (1800 + 1200))


def test_traffic_features_empty_frame_returns_empty_with_schema() -> None:
    empty = pd.DataFrame(columns=["timestamp", "srcaddr", "dstaddr", "packets", "bytes"])
    out = make_traffic_features(empty)
    assert list(out.columns) == ["minute", *TRAFFIC_FEATURES]
    assert len(out) == 0


def test_bgp_features_aggregation_values() -> None:
    out = make_bgp_features(_bgp_fixture())

    assert list(out.columns) == ["minute", *BGP_FEATURES]
    assert len(out) == 2

    m1 = out.iloc[0]
    assert m1["announce_count"] == 2
    assert m1["withdraw_count"] == 1
    assert m1["update_count"] == 0
    assert m1["unique_prefixes"] == 2
    assert m1["unique_peers"] == 2
    assert m1["as_path_mean"] == pytest.approx(np.mean([3, 4, 5]))
    assert m1["as_path_std"] == pytest.approx(np.std([3, 4, 5], ddof=1))


def test_snmp_features_aggregation_values() -> None:
    out = make_snmp_features(_snmp_fixture())

    assert list(out.columns) == ["minute", *SNMP_FEATURES]
    assert len(out) == 1

    row = out.iloc[0]
    assert row["cpu_load_mean"] == pytest.approx(np.mean([0.3, 0.5, 0.7]))
    assert row["cpu_load_max"] == pytest.approx(0.7)
    assert row["latency_ms_mean"] == pytest.approx(np.mean([5.0, 7.0, 50.0]))
    assert row["latency_ms_p95"] == pytest.approx(np.percentile([5.0, 7.0, 50.0], 95))
    assert row["if_utilization_mean"] == pytest.approx(np.mean([20.0, 65.0]))
    assert row["if_utilization_max"] == pytest.approx(65.0)
    assert row["if_in_errors_total"] == pytest.approx(3.0)
    assert row["if_out_errors_total"] == pytest.approx(0.0)


def test_snmp_features_handles_missing_metrics() -> None:
    """A window where only cpuLoad was sampled must still return the schema."""

    df = pd.DataFrame(
        [
            {"timestamp": BASE_TS, "metric": "cpuLoad", "value": 0.4},
            {"timestamp": BASE_TS + timedelta(seconds=30), "metric": "cpuLoad", "value": 0.5},
        ]
    )
    out = make_snmp_features(df)
    row = out.iloc[0]
    assert row["cpu_load_mean"] == pytest.approx(0.45)
    # Missing metrics filled with 0.0 — model never sees NaN.
    assert row["latency_ms_mean"] == 0.0
    assert row["if_utilization_max"] == 0.0
    assert row["if_in_errors_total"] == 0.0


# ── Diurnal encoding ──────────────────────────────────────────────────────────


def test_diurnal_features_unit_circle() -> None:
    df = pd.DataFrame({"minute": [BASE_TS, BASE_TS + timedelta(hours=6)]})
    out = add_diurnal(df, "minute")
    for col in ("hour_sin", "hour_cos", "dow_sin", "dow_cos"):
        assert col in out.columns
    radius = np.hypot(out["hour_sin"].to_numpy(), out["hour_cos"].to_numpy())
    np.testing.assert_allclose(radius, 1.0, atol=1e-12)


# ── feature_stats ─────────────────────────────────────────────────────────────


def test_feature_stats_avoids_zero_std() -> None:
    """Zero-variance columns must report std=1.0 to avoid div-by-zero downstream."""

    df = pd.DataFrame({c: [1.0, 1.0, 1.0] for c in TRAFFIC_FEATURES})
    df["minute"] = pd.date_range(BASE_TS, periods=3, freq="1min", tz="UTC")
    stats = feature_stats(df, TRAFFIC_FEATURES)
    for col, s in stats.items():
        assert s["mean"] == 1.0
        assert s["std"] == 1.0


def test_feature_stats_empty_frame_safe_defaults() -> None:
    stats = feature_stats(pd.DataFrame(), TRAFFIC_FEATURES)
    assert set(stats.keys()) == set(TRAFFIC_FEATURES)
    for s in stats.values():
        assert s == {"mean": 0.0, "std": 1.0}
