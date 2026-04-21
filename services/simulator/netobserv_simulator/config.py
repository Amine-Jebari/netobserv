"""Environment-driven configuration for the simulator service."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ScenarioName = Literal[
    "normal",
    "bgp_flap",
    "traffic_spike",
    "route_leak",
    "link_saturation",
    "latency_spike",
]

ScenarioMode = Literal["random", "scheduled", "single"]


class Settings(BaseSettings):
    """Runtime configuration pulled from environment variables."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    # Kafka / Redpanda
    kafka_bootstrap_servers: str = Field(default="redpanda:9092")
    kafka_topic_flows: str = Field(default="network.flows")
    kafka_topic_bgp: str = Field(default="network.bgp")
    kafka_topic_snmp: str = Field(default="network.snmp")
    kafka_client_id: str = Field(default="netobserv-simulator")

    # Telemetry volume
    producer_rate: int = Field(
        default=75,
        ge=1,
        le=5000,
        description="Target flow records per second in the normal scenario.",
    )
    snmp_interval_seconds: int = Field(default=30, ge=5, le=300)
    snmp_device_count: int = Field(default=10, ge=1, le=200)
    bgp_events_per_minute_min: int = Field(default=1, ge=0, le=100)
    bgp_events_per_minute_max: int = Field(default=5, ge=1, le=200)

    # Scenario scheduler
    scenario_mode: ScenarioMode = Field(default="scheduled")
    scenario: ScenarioName = Field(
        default="normal",
        description="Used when scenario_mode=single; the scenario to inject continuously.",
    )
    anomaly_interval_minutes: int = Field(default=15, ge=1, le=1440)

    # Health / observability
    health_host: str = Field(default="0.0.0.0")
    health_port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = Field(default="INFO")

    # VPC flow log context
    account_id: str = Field(default="123456789012")
    interface_id_prefix: str = Field(default="eni-")

    # Determinism
    random_seed: int | None = Field(
        default=None,
        description="If set, seeds the RNG for reproducible traffic mixes.",
    )


def get_settings() -> Settings:
    """Load settings from the process environment."""

    return Settings()
