"""Environment-driven configuration for the topology service."""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration pulled from environment variables.

    Environment-agnostic: DynamoDB Local locally, real DynamoDB on AWS —
    the only differences are ``DYNAMODB_ENDPOINT`` and credentials.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Kafka / Redpanda ─────────────────────────────────────────────────────
    kafka_bootstrap_servers: str = Field(default="redpanda:9092")
    kafka_topic_topology_updates: str = Field(default="topology.updates")
    kafka_topic_flows: str = Field(default="network.flows")
    kafka_topic_snmp: str = Field(default="network.snmp")
    kafka_consumer_group_prefix: str = Field(default="netobserv-topology")
    kafka_auto_offset_reset: str = Field(default="latest")

    # ── DynamoDB ─────────────────────────────────────────────────────────────
    # Blank DYNAMODB_ENDPOINT → real AWS DynamoDB; populated → DynamoDB Local.
    dynamodb_endpoint: str = Field(
        default="",
        validation_alias=AliasChoices("DYNAMODB_ENDPOINT", "DYNAMODB_ENDPOINT_URL"),
    )
    dynamodb_region: str = Field(
        default="us-east-1",
        validation_alias=AliasChoices("DYNAMODB_REGION", "AWS_REGION"),
    )
    dynamodb_access_key: str = Field(
        default="",
        validation_alias=AliasChoices("DYNAMODB_ACCESS_KEY", "AWS_ACCESS_KEY_ID"),
    )
    dynamodb_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("DYNAMODB_SECRET_KEY", "AWS_SECRET_ACCESS_KEY"),
    )
    dynamodb_table_topology: str = Field(default="network-topology")
    dynamodb_table_links: str = Field(default="network-links")

    # ── Refresh ──────────────────────────────────────────────────────────────
    # Periodic full rescan of DynamoDB to self-heal from any drift caused by
    # missed Kafka events. Set to 0 to disable.
    topology_refresh_interval: float = Field(default=300.0, ge=0)

    # Traffic utilisation EWMA smoothing factor (0<α<=1).
    traffic_ewma_alpha: float = Field(default=0.2, gt=0, le=1)

    # ── HTTP server ──────────────────────────────────────────────────────────
    http_host: str = Field(default="0.0.0.0")
    http_port: int = Field(default=8080, ge=1, le=65535)

    # ── Observability ────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")


def get_settings() -> Settings:
    """Load settings from the process environment."""

    return Settings()
