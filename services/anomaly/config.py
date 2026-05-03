"""Environment-driven configuration for the anomaly trainer + real-time engine."""

from __future__ import annotations

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration pulled from environment variables.

    A single Settings class serves both the one-shot baseline trainer (Phase
    3.1) and the long-lived scoring engine (Phase 3.2): a trainer-only run
    leaves the engine knobs at their defaults and vice versa. Environment-
    agnostic by design — a blank ``S3_ENDPOINT`` targets real AWS S3, a
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
    model_key: str = Field(
        default="models/baseline-latest.tar.gz",
        validation_alias=AliasChoices("MODEL_KEY", "ANOMALY_MODEL_KEY"),
        description="Object key the engine fetches as the active model bundle.",
    )

    # ── Engine: Kafka ────────────────────────────────────────────────────────
    kafka_bootstrap_servers: str = Field(default="redpanda:9092")
    kafka_topic_flows: str = Field(default="network.flows")
    kafka_topic_bgp: str = Field(default="network.bgp")
    kafka_topic_snmp: str = Field(default="network.snmp")
    kafka_topic_anomalies: str = Field(default="anomalies.scored")
    kafka_consumer_group_prefix: str = Field(
        default="netobserv-anomaly",
        validation_alias=AliasChoices(
            "KAFKA_CONSUMER_GROUP_PREFIX", "KAFKA_ANOMALY_GROUP_PREFIX"
        ),
    )
    kafka_auto_offset_reset: str = Field(default="latest")

    # ── Engine: scoring windows + thresholds ─────────────────────────────────
    window_seconds: float = Field(default=10.0, gt=0, le=600)
    info_threshold: float = Field(default=0.3, ge=0.0, le=1.0)
    warn_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    critical_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    @field_validator("warn_threshold")
    @classmethod
    def _warn_above_info(cls, v: float, info) -> float:
        info_threshold = info.data.get("info_threshold", 0.3)
        if v <= info_threshold:
            raise ValueError(
                f"WARN_THRESHOLD ({v}) must exceed INFO_THRESHOLD ({info_threshold})"
            )
        return v

    @field_validator("critical_threshold")
    @classmethod
    def _crit_above_warn(cls, v: float, info) -> float:
        warn = info.data.get("warn_threshold", 0.6)
        if v <= warn:
            raise ValueError(
                f"CRITICAL_THRESHOLD ({v}) must exceed WARN_THRESHOLD ({warn})"
            )
        return v

    # ── Engine: enrichment + alerting ────────────────────────────────────────
    topology_service_url: str = Field(
        default="http://topology:8080",
        description="Base URL of the topology REST API. /topology/affected is queried for BGP context.",
    )
    topology_request_timeout_seconds: float = Field(default=2.0, gt=0)
    slack_webhook_url: str = Field(default="")
    slack_request_timeout_seconds: float = Field(default=5.0, gt=0)

    # ── Engine: HTTP server + startup wait ───────────────────────────────────
    http_host: str = Field(default="0.0.0.0")
    http_port: int = Field(default=8080, ge=1, le=65535)
    model_wait_poll_seconds: float = Field(default=10.0, gt=0)
    model_wait_max_seconds: float = Field(default=0.0, ge=0.0,
        description="If > 0, give up after this many seconds and exit non-zero. 0 = wait forever.",
    )
    raw_metrics_sample_size: int = Field(default=5, ge=0, le=200)

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
