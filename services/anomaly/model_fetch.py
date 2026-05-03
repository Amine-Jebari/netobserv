"""S3/MinIO model-bundle fetch helpers used by the engine at startup + reload.

The engine never produces bundles — that's :mod:`baseline`'s job — so all we
need here is the read side: poll the bucket until the alias key exists, then
download it to a local path the scorer can untar.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from .logging_setup import get_logger
from .model_store import make_s3_client

log = get_logger(__name__)


def _head_object(s3_client, *, bucket: str, key: str) -> bool:
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except (ClientError, BotoCoreError):
        return False


def download_bundle(
    *,
    s3_client,
    bucket: str,
    key: str,
    dest: Path,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    s3_client.download_file(bucket, key, str(dest))
    log.info(
        "model.downloaded",
        bucket=bucket,
        key=key,
        path=str(dest),
        size_bytes=dest.stat().st_size,
    )
    return dest


async def wait_for_bundle(
    *,
    s3_client,
    bucket: str,
    key: str,
    poll_seconds: float,
    max_seconds: float = 0.0,
) -> bool:
    """Poll ``head_object`` until the key appears. ``max_seconds<=0`` waits forever."""

    started = time.time()
    attempt = 0
    while True:
        attempt += 1
        if _head_object(s3_client, bucket=bucket, key=key):
            log.info(
                "model.found",
                bucket=bucket,
                key=key,
                wait_seconds=round(time.time() - started, 2),
                attempts=attempt,
            )
            return True
        if max_seconds > 0 and (time.time() - started) >= max_seconds:
            log.error(
                "model.wait_timeout",
                bucket=bucket,
                key=key,
                waited_seconds=round(time.time() - started, 2),
            )
            return False
        log.info(
            "model.waiting",
            bucket=bucket,
            key=key,
            attempt=attempt,
            poll_seconds=poll_seconds,
        )
        await asyncio.sleep(poll_seconds)


def make_client_from_settings(settings) -> object:
    """Construct an S3 client wired to the engine settings."""

    return make_s3_client(
        endpoint_url=settings.s3_endpoint,
        region=settings.s3_region,
        access_key=settings.aws_access_key_id,
        secret_key=settings.aws_secret_access_key,
    )
