"""Baseline anomaly-detection model trainer (Phase 3.1).

One-shot pipeline:

    1. Pull the last ``LOOKBACK_HOURS`` of flow + SNMP samples from InfluxDB
       and BGP events from OpenSearch.
    2. Engineer per-minute feature tables (traffic / bgp / snmp) using the
       shared :mod:`features` module — same code used at inference time.
    3. Train one PyOD model per feature space:
         * Isolation Forest on traffic volume features
         * Local Outlier Factor (LOF) on BGP frequency features
         * Minimum Covariance Determinant (MCD, robust Mahalanobis) on SNMP
    4. Cross-validate each model on a held-out slice plus injected synthetic
       outliers; report AUC + observed contamination.
    5. Bundle ``<name>.joblib`` + ``metadata.json`` into a single ``.tar.gz``.
    6. Upload to MinIO/S3 at ``models/baseline-v<TIMESTAMP>.tar.gz`` and copy
       to ``models/baseline-latest.tar.gz``.

Run from the repo root:

    python -m services.anomaly.baseline

Or as a one-shot container with the ``training`` compose profile:

    docker compose --profile training up baseline
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyod
import sklearn
from pyod.models.iforest import IForest
from pyod.models.lof import LOF
from pyod.models.mcd import MCD
from sklearn.metrics import roc_auc_score

from .config import Settings, get_settings
from .features import (
    BGP_FEATURES,
    SNMP_FEATURES,
    TRAFFIC_FEATURES,
    feature_stats,
    make_bgp_features,
    make_snmp_features,
    make_traffic_features,
)
from .influx_query import query_flows, query_snmp
from .logging_setup import configure_logging, get_logger
from .model_store import (
    LATEST_ALIAS,
    copy_to_alias,
    make_s3_client,
    serialize_models,
    upload_bundle,
)
from .opensearch_query import query_bgp_events

log = get_logger(__name__)


class InsufficientDataError(RuntimeError):
    """Raised when a feature table has fewer rows than the configured floor."""


# ── Training primitives ───────────────────────────────────────────────────────


def _split(
    X: np.ndarray, *, test_split: float, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    n = len(X)
    test_n = max(int(round(n * test_split)), 1)
    indices = rng.permutation(n)
    return X[indices[:-test_n]], X[indices[-test_n:]]


def _synth_outliers(reference: np.ndarray, n: int, *, seed: int) -> np.ndarray:
    """Generate clearly anomalous points by shifting beyond the training range.

    Each synthetic point is drawn from the per-feature mean shifted by a random
    ±5σ kick. This is a standard no-label evaluation trick: it doesn't measure
    real-world recall, but it does verify the model separates *obvious*
    outliers from the bulk of the training distribution.
    """

    rng = np.random.default_rng(seed)
    if reference.size == 0:
        return np.zeros((n, 1))
    mean = reference.mean(axis=0)
    std = reference.std(axis=0)
    std = np.where(std == 0, 1.0, std)
    direction = rng.choice([-1.0, 1.0], size=(n, reference.shape[1]))
    magnitude = rng.uniform(5.0, 8.0, size=(n, reference.shape[1]))
    return mean + direction * magnitude * std


def _evaluate(
    model, X_train: np.ndarray, X_test: np.ndarray, *, n_synth: int, seed: int
) -> dict[str, float]:
    """Return AUC + contamination stats for a fitted unsupervised model."""

    X_synth = _synth_outliers(X_train, n_synth, seed=seed)
    X_eval = np.vstack([X_test, X_synth])
    y_eval = np.concatenate([np.zeros(len(X_test)), np.ones(len(X_synth))])

    scores = model.decision_function(X_eval)
    try:
        auc = float(roc_auc_score(y_eval, scores))
    except ValueError:
        # roc_auc_score requires both classes; if X_test ends up empty we
        # degrade to NaN rather than crashing the pipeline.
        auc = float("nan")

    train_scores = model.decision_function(X_train)
    threshold = float(np.percentile(train_scores, 95))
    observed_contamination = float((train_scores > threshold).mean())

    return {
        "auc": auc,
        "n_train": int(len(X_train)),
        "n_test": int(len(X_test)),
        "n_synth": int(n_synth),
        "decision_p50": float(np.median(train_scores)),
        "decision_p95": threshold,
        "observed_contamination": observed_contamination,
    }


def _ensure_min_rows(
    name: str, df: pd.DataFrame, min_rows: int
) -> None:
    if len(df) < min_rows:
        raise InsufficientDataError(
            f"{name}: need at least {min_rows} per-minute rows, got {len(df)}"
        )


def train_traffic_model(
    df: pd.DataFrame, *, settings: Settings
) -> tuple[IForest, dict[str, Any]]:
    _ensure_min_rows("traffic", df, settings.min_training_minutes)
    X = df[TRAFFIC_FEATURES].to_numpy(dtype=np.float64)
    X_train, X_test = _split(X, test_split=settings.test_split, seed=settings.random_seed)

    model = IForest(
        contamination=settings.contamination_traffic,
        random_state=settings.random_seed,
        n_estimators=200,
    )
    model.fit(X_train)
    metrics = _evaluate(
        model,
        X_train,
        X_test,
        n_synth=settings.synthetic_outliers_per_eval,
        seed=settings.random_seed + 1,
    )
    metrics["contamination_target"] = settings.contamination_traffic
    return model, metrics


def train_bgp_model(
    df: pd.DataFrame, *, settings: Settings
) -> tuple[LOF, dict[str, Any]]:
    _ensure_min_rows("bgp", df, settings.min_training_minutes)
    X = df[BGP_FEATURES].to_numpy(dtype=np.float64)
    X_train, X_test = _split(X, test_split=settings.test_split, seed=settings.random_seed)

    # LOF needs at least n_neighbors points to fit. Cap n_neighbors so a thin
    # day of data still trains.
    n_neighbors = max(5, min(20, len(X_train) // 4))
    model = LOF(contamination=settings.contamination_bgp, n_neighbors=n_neighbors, novelty=True)
    model.fit(X_train)
    metrics = _evaluate(
        model,
        X_train,
        X_test,
        n_synth=settings.synthetic_outliers_per_eval,
        seed=settings.random_seed + 2,
    )
    metrics["contamination_target"] = settings.contamination_bgp
    metrics["n_neighbors"] = int(n_neighbors)
    return model, metrics


def train_snmp_model(
    df: pd.DataFrame, *, settings: Settings
) -> tuple[MCD, dict[str, Any]]:
    _ensure_min_rows("snmp", df, settings.min_training_minutes)
    X = df[SNMP_FEATURES].to_numpy(dtype=np.float64)
    X_train, X_test = _split(X, test_split=settings.test_split, seed=settings.random_seed)

    model = MCD(
        contamination=settings.contamination_snmp,
        random_state=settings.random_seed,
        support_fraction=None,
    )
    model.fit(X_train)
    metrics = _evaluate(
        model,
        X_train,
        X_test,
        n_synth=settings.synthetic_outliers_per_eval,
        seed=settings.random_seed + 3,
    )
    metrics["contamination_target"] = settings.contamination_snmp
    return model, metrics


# ── Pipeline ──────────────────────────────────────────────────────────────────


def _library_versions() -> dict[str, str]:
    versions = {
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "scikit-learn": sklearn.__version__,
        "pyod": getattr(pyod, "__version__", "unknown"),
    }
    for pkg in ("joblib", "boto3"):
        try:
            versions[pkg] = version(pkg)
        except PackageNotFoundError:
            versions[pkg] = "unknown"
    return versions


def _build_metadata(
    *,
    settings: Settings,
    started_at: datetime,
    finished_at: datetime,
    feature_tables: Mapping[str, pd.DataFrame],
    metrics: Mapping[str, dict[str, Any] | None],
    schemas: Mapping[str, list[str]],
) -> dict[str, Any]:
    return {
        "training_started_at": started_at.isoformat(),
        "training_finished_at": finished_at.isoformat(),
        "training_window_hours": settings.lookback_hours,
        "row_counts": {name: int(len(df)) for name, df in feature_tables.items()},
        "feature_schema": dict(schemas),
        "feature_stats": {
            name: feature_stats(feature_tables[name], cols)
            for name, cols in schemas.items()
        },
        "model_metrics": dict(metrics),
        "library_versions": _library_versions(),
        "bundle_format_version": 1,
    }


def _print_summary(
    metrics: Mapping[str, dict[str, Any] | None],
) -> None:
    """Print a compact human-readable AUC table to stdout."""

    print()
    print("Baseline cross-validation summary")
    print("─────────────────────────────────")
    print(f"{'model':16} {'AUC':>8} {'contam':>10} {'n_train':>9} {'n_test':>8}")
    for name, m in metrics.items():
        if m is None:
            print(f"{name:16} {'(skipped)':>8}")
            continue
        print(
            f"{name:16} "
            f"{m['auc']:>8.3f} "
            f"{m['observed_contamination']:>10.3f} "
            f"{m['n_train']:>9d} "
            f"{m['n_test']:>8d}"
        )
    print()


def _train_or_skip(name: str, fn, df: pd.DataFrame, settings: Settings):
    """Wrap a trainer so insufficient data degrades to a skip, not a crash."""

    try:
        return fn(df, settings=settings)
    except InsufficientDataError as exc:
        log.warning("model.skipped", model=name, reason=str(exc))
        return None, None


def run(settings: Settings | None = None) -> int:
    """Execute the baseline training pipeline. Returns a process exit code."""

    settings = settings or get_settings()
    configure_logging(settings.log_level)
    started_at = datetime.now(timezone.utc)
    log.info(
        "baseline.start",
        lookback_hours=settings.lookback_hours,
        bucket=settings.s3_bucket,
        endpoint=settings.s3_endpoint or "aws-default",
    )

    # 1. Query raw data
    flows = query_flows(
        url=settings.influxdb_url,
        org=settings.influxdb_org,
        token=settings.influxdb_token,
        bucket=settings.influxdb_bucket,
        lookback_hours=settings.lookback_hours,
        timeout_seconds=settings.influxdb_query_timeout_seconds,
    )
    snmp = query_snmp(
        url=settings.influxdb_url,
        org=settings.influxdb_org,
        token=settings.influxdb_token,
        bucket=settings.influxdb_bucket,
        lookback_hours=settings.lookback_hours,
        timeout_seconds=settings.influxdb_query_timeout_seconds,
    )
    bgp = query_bgp_events(
        url=settings.opensearch_url,
        user=settings.opensearch_user,
        password=settings.opensearch_password,
        index_pattern=settings.opensearch_bgp_index_pattern,
        lookback_hours=settings.lookback_hours,
        page_size=settings.opensearch_page_size,
        verify_certs=settings.opensearch_verify_certs,
        timeout_seconds=settings.opensearch_query_timeout_seconds,
    )

    # 2. Engineer features
    traffic_df = make_traffic_features(flows)
    bgp_df = make_bgp_features(bgp)
    snmp_df = make_snmp_features(snmp)
    log.info(
        "features.built",
        traffic_minutes=len(traffic_df),
        bgp_minutes=len(bgp_df),
        snmp_minutes=len(snmp_df),
    )

    # 3. Train + cross-validate
    traffic_model, traffic_metrics = _train_or_skip(
        "traffic_iforest", train_traffic_model, traffic_df, settings
    )
    bgp_model, bgp_metrics = _train_or_skip("bgp_lof", train_bgp_model, bgp_df, settings)
    snmp_model, snmp_metrics = _train_or_skip("snmp_mcd", train_snmp_model, snmp_df, settings)

    trained: dict[str, object] = {}
    if traffic_model is not None:
        trained["traffic_iforest"] = traffic_model
    if bgp_model is not None:
        trained["bgp_lof"] = bgp_model
    if snmp_model is not None:
        trained["snmp_mcd"] = snmp_model

    if not trained:
        log.error("baseline.no_models_trained")
        return 2

    metrics = {
        "traffic_iforest": traffic_metrics,
        "bgp_lof": bgp_metrics,
        "snmp_mcd": snmp_metrics,
    }
    _print_summary(metrics)

    # 4. Bundle + metadata
    finished_at = datetime.now(timezone.utc)
    metadata = _build_metadata(
        settings=settings,
        started_at=started_at,
        finished_at=finished_at,
        feature_tables={
            "traffic": traffic_df,
            "bgp": bgp_df,
            "snmp": snmp_df,
        },
        metrics=metrics,
        schemas={
            "traffic": TRAFFIC_FEATURES,
            "bgp": BGP_FEATURES,
            "snmp": SNMP_FEATURES,
        },
    )

    timestamp = finished_at.strftime("%Y%m%dT%H%M%SZ")
    versioned_key = f"{settings.s3_model_prefix}/baseline-v{timestamp}.tar.gz"
    latest_key = f"{settings.s3_model_prefix}/{LATEST_ALIAS}"

    with tempfile.TemporaryDirectory(prefix="anomaly-out-") as tmp:
        local_bundle = Path(tmp) / f"baseline-v{timestamp}.tar.gz"
        serialize_models(models=trained, metadata=metadata, dest=local_bundle)

        # 5. Upload + alias
        s3 = make_s3_client(
            endpoint_url=settings.s3_endpoint,
            region=settings.s3_region,
            access_key=settings.aws_access_key_id,
            secret_key=settings.aws_secret_access_key,
        )
        upload_bundle(s3_client=s3, local_path=local_bundle, bucket=settings.s3_bucket, key=versioned_key)
        copy_to_alias(s3_client=s3, bucket=settings.s3_bucket, source_key=versioned_key, alias_key=latest_key)

    log.info(
        "baseline.complete",
        models=sorted(trained.keys()),
        versioned_key=versioned_key,
        latest_key=latest_key,
        duration_seconds=(finished_at - started_at).total_seconds(),
    )
    return 0


def main() -> int:  # console-script entry point
    return run()


if __name__ == "__main__":
    sys.exit(main())
