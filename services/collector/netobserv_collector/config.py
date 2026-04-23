"""Environment-driven configuration for the collector service."""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration pulled from environment variables.

    The collector is environment-agnostic: the same code runs against MinIO
    locally and real S3 on AWS — only the endpoint env var changes.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Kafka / Redpanda ─────────────────────────────────────────────────────
    kafka_bootstrap_servers: str = Field(default="redpanda:9092")
    kafka_topic_flows: str = Field(default="network.flows")
    kafka_topic_bgp: str = Field(default="network.bgp")
    kafka_topic_snmp: str = Field(default="network.snmp")
    kafka_topic_topology_updates: str = Field(default="topology.updates")
    kafka_consumer_group_prefix: str = Field(default="netobserv-collector")
    kafka_auto_offset_reset: str = Field(default="latest")

    # ── InfluxDB ─────────────────────────────────────────────────────────────
    influxdb_url: str = Field(default="http://influxdb:8086")
    influxdb_token: str = Field(default="")
    influxdb_org: str = Field(default="netobserv")
    influxdb_bucket: str = Field(default="metrics")
    influxdb_batch_size: int = Field(default=500, ge=1, le=10_000)
    influxdb_flush_interval_seconds: float = Field(default=5.0, gt=0)

    # ── OpenSearch ───────────────────────────────────────────────────────────
    # Accept OPENSEARCH_URL (prompt spec) or OPENSEARCH_HOST (repo-wide .env).
    opensearch_url: str = Field(
        default="http://opensearch:9200",
        validation_alias=AliasChoices("OPENSEARCH_URL", "OPENSEARCH_HOST"),
    )
    opensearch_user: str = Field(default="")
    opensearch_password: str = Field(default="")
    opensearch_bulk_size: int = Field(default=100, ge=1, le=5_000)
    opensearch_flush_interval_seconds: float = Field(default=5.0, gt=0)
    opensearch_verify_certs: bool = Field(default=False)

    # ── S3 / MinIO ───────────────────────────────────────────────────────────
    # Blank S3_ENDPOINT → use real AWS S3; populated → MinIO. Same code.
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
    s3_access_key: str = Field(
        default="",
        validation_alias=AliasChoices("S3_ACCESS_KEY", "AWS_ACCESS_KEY_ID"),
    )
    s3_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("S3_SECRET_KEY", "AWS_SECRET_ACCESS_KEY"),
    )
    s3_flush_interval_seconds: float = Field(default=60.0, gt=0)
    s3_path_prefix: str = Field(default="flows")

    # ── DynamoDB (env passthrough — collector itself does not write topology) ─
    dynamodb_endpoint: str = Field(
        default="",
        validation_alias=AliasChoices("DYNAMODB_ENDPOINT", "DYNAMODB_ENDPOINT_URL"),
    )

    # ── HTTP server ──────────────────────────────────────────────────────────
    http_host: str = Field(default="0.0.0.0")
    http_port: int = Field(default=8080, ge=1, le=65535)

    # ── Observability ────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")


def get_settings() -> Settings:
    """Load settings from the process environment."""

    return Settings()
