"""Model loading + scoring logic for the real-time engine.

The scorer wraps the bundle written by :mod:`baseline` (Phase 3.1) and turns a
``WindowBatch`` into zero-or-more anomaly events. One scorer instance handles
all three feature spaces; it is hot-swappable via :meth:`load`, which atomically
replaces the active model state from a freshly downloaded bundle.

Score normalization
-------------------
PyOD models produce decision-function scores on incompatible scales (Isolation
Forest, LOF, MCD), so we cannot compare or threshold raw outputs directly. The
trainer records the 50th and 95th percentile training-time scores per model in
``metadata.json``. At inference we map a raw score onto a 0-1 unified scale by
linear interpolation between p50 → 0.0 and p95 → 0.5, then continue linearly so
that values well above p95 land near 1.0. Values below p50 clamp to 0.0.

This is a pragmatic baseline, not a calibrated probability — the goal is to
make the three scores commensurate so a single set of severity thresholds
applies uniformly.
"""

from __future__ import annotations

import json
import math
import tarfile
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from .features import (
    BGP_FEATURES,
    SNMP_FEATURES,
    TRAFFIC_FEATURES,
    make_bgp_features,
    make_snmp_features,
    make_traffic_features,
)
from .logging_setup import get_logger
from .model_store import METADATA_FILENAME
from .windowing import STREAM_BGP, STREAM_FLOWS, STREAM_SNMP, WindowBatch

log = get_logger(__name__)


# Map stream name → (feature columns, model key in bundle, anomaly_type label)
_STREAM_SPEC: dict[str, tuple[list[str], str, str]] = {
    STREAM_FLOWS: (TRAFFIC_FEATURES, "traffic_iforest", "traffic"),
    STREAM_BGP: (BGP_FEATURES, "bgp_lof", "bgp"),
    STREAM_SNMP: (SNMP_FEATURES, "snmp_mcd", "snmp"),
}


@dataclass
class ScoredAnomaly:
    """One row of features that has been scored above the INFO threshold."""

    anomaly_type: str
    score: float
    severity: str
    raw_score: float
    feature_vector: dict[str, float]
    top_features: list[dict[str, float]]
    window_start: datetime
    window_end: datetime


@dataclass
class _StreamModel:
    feature_columns: list[str]
    model: Any
    decision_p50: float
    decision_p95: float
    feature_stats: dict[str, dict[str, float]]


@dataclass
class LoadedBundle:
    metadata: dict[str, Any]
    streams: dict[str, _StreamModel]
    loaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def feature_schema(self) -> dict[str, list[str]]:
        return {name: m.feature_columns for name, m in self.streams.items()}


class Scorer:
    """Thread-safe holder for the current model bundle + scoring helpers.

    The engine treats this object as the single source of truth for "is a
    model loaded?", "how is a window scored?", and "what severity does this
    score map to?". Loads are atomic — readers see either the previous bundle
    or the new one, never a half-replaced state.
    """

    def __init__(
        self,
        *,
        info_threshold: float,
        warn_threshold: float,
        critical_threshold: float,
    ) -> None:
        self._lock = threading.Lock()
        self._bundle: LoadedBundle | None = None
        self._info = float(info_threshold)
        self._warn = float(warn_threshold)
        self._critical = float(critical_threshold)
        if not (0 <= self._info < self._warn < self._critical <= 1):
            raise ValueError(
                "thresholds must satisfy 0 <= info < warn < critical <= 1"
            )

    @property
    def loaded(self) -> bool:
        return self._bundle is not None

    @property
    def bundle(self) -> LoadedBundle | None:
        return self._bundle

    def severity_for(self, score: float) -> str | None:
        """Return INFO/WARN/CRITICAL for ``score``, or ``None`` to drop."""

        if score < self._info:
            return None
        if score < self._warn:
            return "INFO"
        if score < self._critical:
            return "WARN"
        return "CRITICAL"

    # ── Loading ──────────────────────────────────────────────────────────────

    def load_from_path(self, path: Path) -> LoadedBundle:
        bundle = _load_bundle_from_tar(path)
        with self._lock:
            self._bundle = bundle
        log.info(
            "scorer.bundle_loaded",
            models=sorted(bundle.streams.keys()),
            metadata_keys=sorted(bundle.metadata.keys()),
            loaded_at=bundle.loaded_at.isoformat(),
        )
        return bundle

    # ── Scoring ──────────────────────────────────────────────────────────────

    def score_batch(self, batch: WindowBatch) -> list[ScoredAnomaly]:
        """Return one ``ScoredAnomaly`` per stream-row above the INFO threshold."""

        bundle = self._bundle
        if bundle is None:
            raise RuntimeError("Scorer.score_batch called before a bundle was loaded")

        results: list[ScoredAnomaly] = []
        window_start = datetime.fromtimestamp(batch.window_start, tz=timezone.utc)
        window_end = datetime.fromtimestamp(batch.window_end, tz=timezone.utc)

        for stream, (columns, model_key, anomaly_type) in _STREAM_SPEC.items():
            messages = batch.get(stream)
            if not messages:
                continue
            stream_model = bundle.streams.get(stream)
            if stream_model is None:
                # Trainer skipped this model (insufficient data). Stay quiet.
                continue
            feature_df = _features_for_stream(stream, messages)
            if feature_df.empty:
                continue
            X = feature_df[columns].to_numpy(dtype=np.float64)
            raw_scores = stream_model.model.decision_function(X)

            for row_idx in range(X.shape[0]):
                raw = float(raw_scores[row_idx])
                score = _normalize_score(
                    raw,
                    p50=stream_model.decision_p50,
                    p95=stream_model.decision_p95,
                )
                severity = self.severity_for(score)
                if severity is None:
                    continue
                feature_vector = {
                    col: float(X[row_idx, j]) for j, col in enumerate(columns)
                }
                top = _top_features_by_zscore(
                    feature_vector,
                    stats=stream_model.feature_stats,
                    top_n=3,
                )
                results.append(
                    ScoredAnomaly(
                        anomaly_type=anomaly_type,
                        score=score,
                        severity=severity,
                        raw_score=raw,
                        feature_vector=feature_vector,
                        top_features=top,
                        window_start=window_start,
                        window_end=window_end,
                    )
                )
        return results


# ── Helpers ──────────────────────────────────────────────────────────────────


def _features_for_stream(stream: str, messages: list[dict[str, Any]]) -> pd.DataFrame:
    """Engineer features for a single stream's window of raw messages."""

    if stream == STREAM_FLOWS:
        return make_traffic_features(_flows_to_frame(messages))
    if stream == STREAM_BGP:
        return make_bgp_features(_bgp_to_frame(messages))
    if stream == STREAM_SNMP:
        return make_snmp_features(_snmp_to_frame(messages))
    raise KeyError(f"unknown stream: {stream!r}")


def _flows_to_frame(messages: list[dict[str, Any]]) -> pd.DataFrame:
    """Coerce raw flow messages into the columns ``make_traffic_features`` expects."""

    if not messages:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for m in messages:
        ts = m.get("end_ts") or m.get("start_ts") or m.get("timestamp")
        if ts is None:
            continue
        rows.append(
            {
                "timestamp": _to_datetime(ts),
                "srcaddr": m.get("srcaddr"),
                "dstaddr": m.get("dstaddr"),
                "packets": m.get("packets", 0),
                "bytes": m.get("bytes", 0),
            }
        )
    return pd.DataFrame(rows)


def _bgp_to_frame(messages: list[dict[str, Any]]) -> pd.DataFrame:
    if not messages:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for m in messages:
        as_path = m.get("as_path") or []
        rows.append(
            {
                "timestamp": _to_datetime(m.get("timestamp")),
                "event_type": m.get("event_type"),
                "peer_ip": m.get("peer_ip"),
                "prefix": m.get("prefix"),
                "as_path_length": len(as_path) if isinstance(as_path, list) else 0,
            }
        )
    return pd.DataFrame(rows)


def _snmp_to_frame(messages: list[dict[str, Any]]) -> pd.DataFrame:
    if not messages:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for m in messages:
        rows.append(
            {
                "timestamp": _to_datetime(m.get("timestamp")),
                "metric": m.get("metric"),
                "value": m.get("value"),
                "device_id": m.get("device_id"),
                "interface": m.get("interface"),
            }
        )
    return pd.DataFrame(rows)


def _to_datetime(value: Any) -> Any:
    """Best-effort coercion to a tz-aware UTC pandas Timestamp."""

    if value is None:
        return pd.NaT
    if isinstance(value, (int, float)):
        # Heuristic: epoch seconds vs. milliseconds
        if value > 1e12:
            return pd.to_datetime(value, unit="ms", utc=True)
        return pd.to_datetime(value, unit="s", utc=True)
    return pd.to_datetime(value, utc=True, errors="coerce")


def _normalize_score(raw: float, *, p50: float, p95: float) -> float:
    """Map a raw decision-function score to [0, 1] using the trainer's quantiles.

    p50 → 0.0, p95 → 0.5, then linearly extrapolated and clamped to 1.0. NaN
    inputs collapse to 0.0 so a degenerate row never produces a phantom alert.
    """

    if math.isnan(raw):
        return 0.0
    spread = p95 - p50
    if spread <= 0:
        # Degenerate quantiles (constant training scores). Treat anything above
        # p95 as a maximum-severity outlier; everything else as nominal.
        return 1.0 if raw > p95 else 0.0
    if raw <= p50:
        return 0.0
    norm = 0.5 * (raw - p50) / spread
    if norm >= 1.0:
        return 1.0
    return float(norm)


def _top_features_by_zscore(
    features: dict[str, float],
    *,
    stats: dict[str, dict[str, float]],
    top_n: int,
) -> list[dict[str, float]]:
    """Rank features by absolute z-score against the training mean/std."""

    scored: list[tuple[float, str, float, float]] = []
    for name, value in features.items():
        s = stats.get(name) or {}
        mean = float(s.get("mean", 0.0))
        std = float(s.get("std", 1.0)) or 1.0
        z = (value - mean) / std
        scored.append((abs(z), name, value, z))
    scored.sort(reverse=True)
    out: list[dict[str, float]] = []
    for _, name, value, z in scored[:top_n]:
        out.append(
            {
                "feature": name,
                "value": round(value, 6),
                "zscore": round(z, 4),
            }
        )
    return out


def _load_bundle_from_tar(path: Path) -> LoadedBundle:
    """Reverse of :func:`model_store.serialize_models`, plus quantile extraction."""

    workdir = Path(tempfile.mkdtemp(prefix="anomaly-engine-bundle-"))
    try:
        with tarfile.open(path, "r:gz") as tf:
            tf.extractall(workdir, filter="data")
        meta_path = workdir / METADATA_FILENAME
        if not meta_path.exists():
            raise ValueError(f"bundle missing {METADATA_FILENAME}")
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))

        feature_schema: dict[str, list[str]] = metadata.get("feature_schema", {})
        feature_stats: dict[str, dict[str, dict[str, float]]] = metadata.get(
            "feature_stats", {}
        )
        model_metrics: dict[str, dict[str, Any]] = metadata.get("model_metrics", {})

        streams: dict[str, _StreamModel] = {}
        for stream_name, (default_cols, model_key, _) in _STREAM_SPEC.items():
            joblib_path = workdir / f"{model_key}.joblib"
            if not joblib_path.exists():
                continue
            metrics = model_metrics.get(model_key) or {}
            # Map our internal stream key (flows/bgp/snmp) to schema key
            # (traffic/bgp/snmp) used by the trainer.
            schema_key = "traffic" if stream_name == STREAM_FLOWS else stream_name
            cols = feature_schema.get(schema_key, default_cols)
            streams[stream_name] = _StreamModel(
                feature_columns=list(cols),
                model=joblib.load(joblib_path),
                decision_p50=float(metrics.get("decision_p50", 0.0)),
                decision_p95=float(metrics.get("decision_p95", 1.0)),
                feature_stats=feature_stats.get(schema_key, {}),
            )
        if not streams:
            raise ValueError("bundle contained no recognized model files")
        return LoadedBundle(metadata=metadata, streams=streams)
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)
