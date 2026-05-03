"""Unit tests for severity thresholds, score normalization, and the Scorer.

We avoid spinning up real PyOD models — instead we stub each model with a
``decision_function`` that returns a fixed array, and verify that the Scorer
normalizes, thresholds, and emits exactly the events we expect.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import numpy as np
import pytest

from services.anomaly import scorer as scorer_mod
from services.anomaly.features import (
    BGP_FEATURES,
    SNMP_FEATURES,
    TRAFFIC_FEATURES,
)
from services.anomaly.scorer import (
    LoadedBundle,
    Scorer,
    _StreamModel,
    _normalize_score,
    _top_features_by_zscore,
)
from services.anomaly.windowing import (
    STREAM_BGP,
    STREAM_FLOWS,
    STREAM_SNMP,
    WindowBatch,
)


# ── Severity threshold tests ─────────────────────────────────────────────────


def test_severity_thresholds_drop_below_info() -> None:
    s = Scorer(info_threshold=0.3, warn_threshold=0.6, critical_threshold=0.85)
    assert s.severity_for(0.0) is None
    assert s.severity_for(0.29) is None


def test_severity_thresholds_assign_each_band() -> None:
    s = Scorer(info_threshold=0.3, warn_threshold=0.6, critical_threshold=0.85)
    assert s.severity_for(0.30) == "INFO"
    assert s.severity_for(0.59) == "INFO"
    assert s.severity_for(0.60) == "WARN"
    assert s.severity_for(0.84) == "WARN"
    assert s.severity_for(0.85) == "CRITICAL"
    assert s.severity_for(1.0) == "CRITICAL"


def test_thresholds_must_be_strictly_ordered() -> None:
    with pytest.raises(ValueError):
        Scorer(info_threshold=0.6, warn_threshold=0.3, critical_threshold=0.85)
    with pytest.raises(ValueError):
        Scorer(info_threshold=0.3, warn_threshold=0.85, critical_threshold=0.6)


# ── Normalization tests ──────────────────────────────────────────────────────


def test_normalize_below_p50_is_zero() -> None:
    assert _normalize_score(0.0, p50=0.5, p95=1.0) == 0.0
    assert _normalize_score(0.5, p50=0.5, p95=1.0) == 0.0


def test_normalize_at_p95_is_half() -> None:
    assert _normalize_score(1.0, p50=0.5, p95=1.0) == pytest.approx(0.5)


def test_normalize_above_p95_extrapolates_then_clamps() -> None:
    # Linear continuation: p50=0, p95=1 → at raw=2, half-spread=1, norm = 0.5*(2-0)/1 = 1.0
    assert _normalize_score(2.0, p50=0.0, p95=1.0) == pytest.approx(1.0)
    # Extreme value clamps
    assert _normalize_score(50.0, p50=0.0, p95=1.0) == 1.0


def test_normalize_handles_degenerate_quantiles() -> None:
    # Spread of 0 — anything strictly above p95 maxes; otherwise zero.
    assert _normalize_score(0.5, p50=1.0, p95=1.0) == 0.0
    assert _normalize_score(2.0, p50=1.0, p95=1.0) == 1.0


def test_normalize_nan_is_zero() -> None:
    assert _normalize_score(float("nan"), p50=0.0, p95=1.0) == 0.0


# ── Top-features ranking ─────────────────────────────────────────────────────


def test_top_features_by_zscore_picks_largest_abs() -> None:
    features = {"a": 100.0, "b": 0.0, "c": 50.0}
    stats = {
        "a": {"mean": 0.0, "std": 10.0},  # z = 10
        "b": {"mean": 0.0, "std": 1.0},   # z = 0
        "c": {"mean": 50.0, "std": 1.0},  # z = 0
    }
    top = _top_features_by_zscore(features, stats=stats, top_n=2)
    assert [t["feature"] for t in top] == ["a", "b"] or [t["feature"] for t in top][0] == "a"
    assert top[0]["feature"] == "a"


# ── Scorer.score_batch with stub models ─────────────────────────────────────


class _StubModel:
    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def decision_function(self, X: np.ndarray) -> np.ndarray:
        n = X.shape[0]
        return np.array(self._scores[:n], dtype=np.float64)


def _stub_bundle(*, traffic_score: float = 0.0, p50: float = 0.0, p95: float = 1.0) -> LoadedBundle:
    """Build a LoadedBundle with stub models for traffic only."""

    return LoadedBundle(
        metadata={"bundle_format_version": 1},
        streams={
            STREAM_FLOWS: _StreamModel(
                feature_columns=TRAFFIC_FEATURES,
                model=_StubModel([traffic_score]),
                decision_p50=p50,
                decision_p95=p95,
                feature_stats={c: {"mean": 0.0, "std": 1.0} for c in TRAFFIC_FEATURES},
            ),
        },
    )


def _flow_message(ts_epoch: int, *, srcaddr: str = "10.0.0.1", dstaddr: str = "10.0.0.2") -> dict[str, Any]:
    return {
        "srcaddr": srcaddr,
        "dstaddr": dstaddr,
        "packets": 10,
        "bytes": 1500,
        "start_ts": ts_epoch,
        "end_ts": ts_epoch + 1,
        "action": "ACCEPT",
    }


def test_scorer_emits_critical_when_score_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Scorer(info_threshold=0.3, warn_threshold=0.6, critical_threshold=0.85)
    # Inject the stub bundle directly (skip the tarball loader)
    s._bundle = _stub_bundle(traffic_score=2.0, p50=0.0, p95=1.0)  # raw 2.0 -> norm 1.0

    batch = WindowBatch(
        window_start=1_700_000_000.0,
        window_end=1_700_000_010.0,
        messages={
            STREAM_FLOWS: [_flow_message(1_700_000_000)],
            STREAM_BGP: [],
            STREAM_SNMP: [],
        },
    )
    result = s.score_batch(batch)
    assert len(result) == 1
    assert result[0].severity == "CRITICAL"
    assert result[0].score == pytest.approx(1.0)
    assert result[0].anomaly_type == "traffic"
    assert result[0].window_start == datetime.fromtimestamp(1_700_000_000.0, tz=timezone.utc)


def test_scorer_drops_below_info_threshold() -> None:
    s = Scorer(info_threshold=0.3, warn_threshold=0.6, critical_threshold=0.85)
    s._bundle = _stub_bundle(traffic_score=-1.0, p50=0.0, p95=1.0)  # raw < p50 -> norm 0.0

    batch = WindowBatch(
        window_start=1_700_000_000.0,
        window_end=1_700_000_010.0,
        messages={
            STREAM_FLOWS: [_flow_message(1_700_000_000)],
            STREAM_BGP: [],
            STREAM_SNMP: [],
        },
    )
    assert s.score_batch(batch) == []


def test_scorer_warn_band() -> None:
    s = Scorer(info_threshold=0.3, warn_threshold=0.6, critical_threshold=0.85)
    # raw=1.4, p50=0, p95=1 → norm = 0.5*1.4 = 0.7 → WARN
    s._bundle = _stub_bundle(traffic_score=1.4, p50=0.0, p95=1.0)
    batch = WindowBatch(
        window_start=1_700_000_000.0,
        window_end=1_700_000_010.0,
        messages={
            STREAM_FLOWS: [_flow_message(1_700_000_000)],
            STREAM_BGP: [],
            STREAM_SNMP: [],
        },
    )
    out = s.score_batch(batch)
    assert len(out) == 1
    assert out[0].severity == "WARN"
    assert 0.6 <= out[0].score < 0.85


def test_scorer_skips_streams_without_models() -> None:
    s = Scorer(info_threshold=0.3, warn_threshold=0.6, critical_threshold=0.85)
    s._bundle = _stub_bundle(traffic_score=2.0)  # only flows model loaded

    batch = WindowBatch(
        window_start=1_700_000_000.0,
        window_end=1_700_000_010.0,
        messages={
            STREAM_FLOWS: [],
            STREAM_BGP: [
                {
                    "timestamp": "2026-04-30T00:00:00Z",
                    "event_type": "ANNOUNCE",
                    "peer_ip": "10.0.0.1",
                    "prefix": "192.0.2.0/24",
                    "as_path": [65000, 65001],
                }
            ],
            STREAM_SNMP: [],
        },
    )
    # No BGP model loaded → no events even though BGP messages exist
    assert s.score_batch(batch) == []


def test_scorer_raises_when_no_bundle() -> None:
    s = Scorer(info_threshold=0.3, warn_threshold=0.6, critical_threshold=0.85)
    batch = WindowBatch(
        window_start=0.0,
        window_end=1.0,
        messages={STREAM_FLOWS: [], STREAM_BGP: [], STREAM_SNMP: []},
    )
    with pytest.raises(RuntimeError):
        s.score_batch(batch)


def test_scorer_top_features_attached() -> None:
    s = Scorer(info_threshold=0.3, warn_threshold=0.6, critical_threshold=0.85)
    s._bundle = _stub_bundle(traffic_score=2.0, p50=0.0, p95=1.0)
    batch = WindowBatch(
        window_start=1_700_000_000.0,
        window_end=1_700_000_010.0,
        messages={
            STREAM_FLOWS: [_flow_message(1_700_000_000)],
            STREAM_BGP: [],
            STREAM_SNMP: [],
        },
    )
    out = s.score_batch(batch)
    assert out
    top = out[0].top_features
    assert len(top) == 3
    assert all("feature" in entry and "value" in entry and "zscore" in entry for entry in top)
