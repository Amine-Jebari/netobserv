"""Async S3/MinIO archive writer.

Aggregates raw JSON records into newline-delimited JSON (NDJSON) objects flushed
on a 1-minute cadence. ``S3_ENDPOINT`` being empty targets real AWS S3; a
populated endpoint (e.g. ``http://minio:9000``) targets MinIO. No branching —
``aioboto3`` is endpoint-agnostic.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import aioboto3
from botocore.config import Config as BotoConfig

from ..logging_setup import get_logger

log = get_logger(__name__)


class ArchiveClient:
    """Writes aggregated NDJSON archive objects to S3 or MinIO."""

    def __init__(
        self,
        endpoint: str,
        region: str,
        access_key: str,
        secret_key: str,
        bucket: str,
    ) -> None:
        self._endpoint = endpoint or None          # None → real AWS S3
        self._region = region
        self._access_key = access_key or None
        self._secret_key = secret_key or None
        self._bucket = bucket
        self._session: aioboto3.Session | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        self._session = aioboto3.Session(
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region,
        )
        try:
            async with self._client() as s3:
                await s3.head_bucket(Bucket=self._bucket)
            self._connected = True
        except Exception as exc:
            log.warning("archive.head_bucket_failed", error=str(exc), bucket=self._bucket)
            self._connected = False

    async def stop(self) -> None:
        self._session = None
        self._connected = False

    def _client(self):
        if self._session is None:
            raise RuntimeError("ArchiveClient.start() was not called")
        return self._session.client(
            "s3",
            endpoint_url=self._endpoint,
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    async def put_ndjson(
        self,
        path_prefix: str,
        records: list[dict[str, object]],
        now: datetime | None = None,
    ) -> str | None:
        """Upload a batch of records as one NDJSON object; returns the key."""

        if not records:
            return None
        now = now or datetime.now(timezone.utc)
        key = _build_key(path_prefix, now)
        body = ("\n".join(json.dumps(r, default=_json_default) for r in records) + "\n").encode(
            "utf-8"
        )
        try:
            async with self._client() as s3:
                await s3.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/x-ndjson",
                )
            self._connected = True
            return key
        except Exception as exc:
            self._connected = False
            log.error(
                "archive.put_failed",
                error=str(exc),
                bucket=self._bucket,
                key=key,
            )
            raise


def _build_key(prefix: str, ts: datetime) -> str:
    ts = ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return f"{prefix}/{ts:%Y/%m/%d/%H}/{uuid.uuid4()}.json"


def _json_default(obj: object) -> object:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Unserialisable type: {type(obj).__name__}")
