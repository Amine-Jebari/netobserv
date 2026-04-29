"""Environment-driven configuration for the anomaly baseline trainer."""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration pulled from environment variables.

    Environment-agnostic: a blank ``S3_ENDPOINT`` targets real AWS S3, a
    populated one targets MinIO. OpenSearch is queried for BGP events because
    the collector indexes BGP into OpenSearch (not InfluxDB) — InfluxDB only
    stores flow and SNMP samples.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    # ── InfluxDB (flows + SNMP) ──────────────────────────────────────────────
    influxdb_url: str = Field(default="http://influxdb:8086")
    influxdb_token: str = Field(default="")
    influxdb_org: str = Field(default="netobserv")
    influxdb_bucket: str = Field(default="metrics")
    influxdb_query_timeout_seconds: float = Field(default=120.0, gt=0)

    # ── OpenSearch (BGP frequency) ───────────────────────────────────────────
    opensearch_url: str = Field(
        default="http://opensearch:9200",
        validation_alias=AliasChoices("OPENSEARCH_URL", "OPENSEARCH_HOST"),
    )
    opensearch_user: str = Field(default="")
    opensearch_password: str = Field(default="")
    opensearch_bgp_index_pattern: str = Field(default="bgp-events-*")
    opensearch_verify_certs: bool = Field(default=False)
    opensearch_query_timeout_seconds: float = Field(default=60.0, gt=0)
    opensearch_page_size: int = Field(default=5000, ge=100, le=10_000)

    # ── S3 / MinIO (model artifact destination) ──────────────────────────────
    s3_endpoint: str = Field(
        default="",
        validation_alias=AliasChoices("S3_ENDPOINT", "S3_ENDPOINT_URL"),
    )
    s3_bucket: str = Field(
        default="netobserv-archive",
        validation_alias=AliasChoices("S3_BUCKET", "S3_BUCKET_ARCHIVE"),
    )
    s3_region: str = Field(
        default="us-east-1",
        validation_alias=AliasChoices("S3_REGION", "AWS_REGION"),
    )
    aws_access_key_id: str = Field(
        default="",
        validation_alias=AliasChoices("AWS_ACCESS_KEY_ID", "S3_ACCESS_KEY"),
    )
    aws_secret_access_key: str = Field(
        default="",
        validation_alias=AliasChoices("AWS_SECRET_ACCESS_KEY", "S3_SECRET_KEY"),
    )
    s3_model_prefix: str = Field(default="models")

    # ── Training knobs ───────────────────────────────────────────────────────
    lookback_hours: int = Field(default=24, ge=1, le=168)
    contamination_traffic: float = Field(default=0.05, gt=0, lt=0.5)
    contamination_bgp: float = Field(default=0.05, gt=0, lt=0.5)
    contamination_snmp: float = Field(default=0.05, gt=0, lt=0.5)
    test_split: float = Field(default=0.2, gt=0, lt=0.5)
    synthetic_outliers_per_eval: int = Field(default=20, ge=1, le=10_000)
    min_training_minutes: int = Field(default=30, ge=10)
    random_seed: int = Field(default=42)

    # ── Observability ────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")


def get_settings() -> Settings:
    return Settings()
