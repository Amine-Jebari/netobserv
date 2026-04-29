"""Bundle, persist, and ship the trained model artefacts.

A bundle is a single ``.tar.gz`` containing one ``<name>.joblib`` per trained
model and a ``metadata.json`` describing the training context (dates, feature
schema, per-feature mean/std, contamination, evaluation metrics, library
versions). The same bundle layout is consumed by the real-time engine in
Phase 3.2, so anything written here must be stable.
"""

from __future__ import annotations

import json
import shutil
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import boto3
import joblib
from botocore.client import Config as BotoConfig

from .logging_setup import get_logger

log = get_logger(__name__)

METADATA_FILENAME = "metadata.json"
LATEST_ALIAS = "baseline-latest.tar.gz"


def serialize_models(
    *,
    models: Mapping[str, object],
    metadata: Mapping[str, Any],
    dest: Path,
) -> Path:
    """Write each model + metadata.json into a single ``.tar.gz``.

    ``dest``'s parent must exist; the tarball replaces ``dest`` if present.
    """

    workdir = Path(tempfile.mkdtemp(prefix="anomaly-bundle-"))
    try:
        for name, model in models.items():
            joblib.dump(model, workdir / f"{name}.joblib")
        (workdir / METADATA_FILENAME).write_text(
            json.dumps(metadata, indent=2, sort_keys=True, default=_json_default),
            encoding="utf-8",
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        with tarfile.open(dest, "w:gz") as tf:
            for entry in sorted(workdir.iterdir()):
                tf.add(entry, arcname=entry.name)
        log.info(
            "bundle.serialized",
            path=str(dest),
            size_bytes=dest.stat().st_size,
            members=sorted(p.name for p in workdir.iterdir()),
        )
        return dest
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def load_bundle(path: Path) -> dict[str, Any]:
    """Reverse of :func:`serialize_models`. Returns ``{models, metadata}``."""

    workdir = Path(tempfile.mkdtemp(prefix="anomaly-bundle-load-"))
    try:
        with tarfile.open(path, "r:gz") as tf:
            # ``filter="data"`` rejects symlinks/abs-paths and is required to
            # avoid a DeprecationWarning on Python 3.12+ (and a hard error from
            # 3.14 onward). We control bundle creation, so the safe filter is
            # appropriate here.
            tf.extractall(workdir, filter="data")
        meta_path = workdir / METADATA_FILENAME
        metadata = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        models: dict[str, Any] = {}
        for joblib_file in sorted(workdir.glob("*.joblib")):
            models[joblib_file.stem] = joblib.load(joblib_file)
        return {"models": models, "metadata": metadata}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def make_s3_client(
    *,
    endpoint_url: str,
    region: str,
    access_key: str,
    secret_key: str,
):
    """Return a boto3 S3 client wired for either real S3 or MinIO."""

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or None,
        region_name=region,
        aws_access_key_id=access_key or None,
        aws_secret_access_key=secret_key or None,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def upload_bundle(
    *,
    s3_client,
    local_path: Path,
    bucket: str,
    key: str,
) -> str:
    """Upload a tarball to S3/MinIO. Returns the key."""

    s3_client.upload_file(str(local_path), bucket, key)
    log.info("bundle.uploaded", bucket=bucket, key=key, size_bytes=local_path.stat().st_size)
    return key


def copy_to_alias(
    *,
    s3_client,
    bucket: str,
    source_key: str,
    alias_key: str,
) -> str:
    """Copy ``source_key`` to ``alias_key`` so the engine can fetch ``-latest``."""

    s3_client.copy_object(
        Bucket=bucket,
        CopySource={"Bucket": bucket, "Key": source_key},
        Key=alias_key,
    )
    log.info("bundle.alias_updated", bucket=bucket, source=source_key, alias=alias_key)
    return alias_key


def _json_default(value: Any) -> Any:
    """Best-effort JSON serializer for numpy scalars / datetimes."""

    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, TypeError):
            pass
    return str(value)
