"""Tests for the model bundle serializer / loader."""

from __future__ import annotations

import tarfile

import numpy as np
import pytest

from services.anomaly.model_store import (
    LATEST_ALIAS,
    METADATA_FILENAME,
    load_bundle,
    serialize_models,
)


class _ToyModel:
    """Tiny picklable stand-in for a PyOD model — joblib handles it the same."""

    def __init__(self, name: str, weights: np.ndarray) -> None:
        self.name = name
        self.weights = weights

    def decision_function(self, X: np.ndarray) -> np.ndarray:
        return X @ self.weights


def test_serialize_then_load_round_trip(tmp_path) -> None:
    model_a = _ToyModel("a", np.array([1.0, 2.0, 3.0]))
    model_b = _ToyModel("b", np.array([0.5, -0.5]))
    metadata = {
        "training_started_at": "2026-04-27T12:00:00+00:00",
        "feature_schema": {"traffic": ["x", "y", "z"]},
        "row_counts": {"traffic": 1234},
    }
    bundle = tmp_path / "baseline-test.tar.gz"

    out_path = serialize_models(
        models={"traffic_iforest": model_a, "snmp_mcd": model_b},
        metadata=metadata,
        dest=bundle,
    )

    assert out_path == bundle
    assert bundle.exists() and bundle.stat().st_size > 0

    with tarfile.open(bundle, "r:gz") as tf:
        names = sorted(tf.getnames())
    assert names == sorted([METADATA_FILENAME, "snmp_mcd.joblib", "traffic_iforest.joblib"])

    loaded = load_bundle(bundle)
    assert loaded["metadata"] == metadata
    assert set(loaded["models"].keys()) == {"traffic_iforest", "snmp_mcd"}

    # Reconstructed models behave the same as before serialization.
    np.testing.assert_array_equal(
        loaded["models"]["traffic_iforest"].decision_function(np.eye(3)),
        np.array([1.0, 2.0, 3.0]),
    )
    np.testing.assert_array_equal(
        loaded["models"]["snmp_mcd"].decision_function(np.eye(2)),
        np.array([0.5, -0.5]),
    )


def test_serialize_overwrites_existing_destination(tmp_path) -> None:
    bundle = tmp_path / "baseline-test.tar.gz"
    bundle.write_bytes(b"stale junk that must be replaced")
    serialize_models(
        models={"x": _ToyModel("x", np.array([1.0]))},
        metadata={"k": "v"},
        dest=bundle,
    )
    # Successfully opens as a real tarball — old junk is gone.
    with tarfile.open(bundle, "r:gz") as tf:
        assert METADATA_FILENAME in tf.getnames()


def test_metadata_serializes_numpy_and_datetime(tmp_path) -> None:
    """The custom JSON encoder must coerce numpy scalars and datetimes."""

    import datetime as dt

    metadata = {
        "score": np.float64(0.123),
        "n_train": np.int64(42),
        "started_at": dt.datetime(2026, 4, 27, 12, 0, 0, tzinfo=dt.timezone.utc),
    }
    bundle = tmp_path / "b.tar.gz"
    serialize_models(models={"x": _ToyModel("x", np.array([1.0]))}, metadata=metadata, dest=bundle)
    loaded = load_bundle(bundle)
    assert loaded["metadata"]["score"] == pytest.approx(0.123)
    assert loaded["metadata"]["n_train"] == 42
    assert loaded["metadata"]["started_at"].startswith("2026-04-27T12:00:00")


def test_latest_alias_constant_matches_documented_path() -> None:
    """Phase 3.2 will hard-code this alias when fetching from S3 — keep it pinned."""

    assert LATEST_ALIAS == "baseline-latest.tar.gz"
