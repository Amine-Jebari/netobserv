"""Environment-driven configuration for the agent service.

This Settings class is used by both:

* the Phase 4.1 one-shot seed script (only the Postgres + embedding fields), and
* the Phase 4.2 long-running LangChain Gemini agent (everything below).

All defaults match docker-compose.yml; production overrides flow in via env
vars. Per project convention, no endpoint or credential is hardcoded.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration pulled from environment variables."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Postgres (pgvector knowledge base + investigations table) ────────────
    postgres_host: str = Field(default="postgres")
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = Field(default="netobserv")
    postgres_user: str = Field(default="netobserv")
    postgres_password: str = Field(default="")
    postgres_pool_size: int = Field(default=5, ge=1, le=64)
    postgres_connect_timeout_seconds: float = Field(default=10.0, gt=0)

    runbook_table: str = Field(default="runbook_chunks")
    investigations_table: str = Field(default="investigations")

    # ── Embeddings ───────────────────────────────────────────────────────────
    embedding_model_name: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        validation_alias=AliasChoices("EMBEDDING_MODEL_NAME", "VECTOR_EMBEDDING_MODEL"),
    )
    embedding_dim: int = Field(
        default=384,
        validation_alias=AliasChoices("EMBEDDING_DIM", "VECTOR_DIMENSIONS"),
    )

    # ── RAG retrieval defaults ───────────────────────────────────────────────
    rag_top_k: int = Field(
        default=5,
        ge=1,
        le=50,
        validation_alias=AliasChoices("RAG_TOP_K", "AGENT_RAG_TOP_K"),
    )

    # ── Seed-script knobs ────────────────────────────────────────────────────
    runbooks_dir: str = Field(
        default="/data/runbooks",
        description="Directory containing markdown runbooks the seed script ingests.",
    )
    chunk_max_tokens: int = Field(default=300, ge=64, le=2048)
    chunk_overlap_tokens: int = Field(default=50, ge=0, le=512)

    # ── LLM provider (Google Gemini via LangChain) ───────────────────────────
    google_api_key: str = Field(default="")
    # Project mandate: gemini-2.0-flash. Override via GEMINI_MODEL or
    # CLAUDE_MODEL (legacy alias kept so .env.example doesn't need to change
    # when the LLM switches providers).
    gemini_model: str = Field(
        default="gemini-2.0-flash",
        validation_alias=AliasChoices("GEMINI_MODEL", "CLAUDE_MODEL", "AGENT_MODEL"),
    )
    agent_max_tokens: int = Field(default=4096, ge=64, le=8192)
    agent_temperature: float = Field(default=0.2, ge=0.0, le=1.0)

    # ── Kafka (anomalies.scored consumer + optional runbooks producer) ───────
    kafka_bootstrap_servers: str = Field(default="redpanda:9092")
    kafka_topic_anomalies: str = Field(default="anomalies.scored")
    kafka_topic_runbooks: str = Field(default="anomalies.runbooks")
    kafka_consumer_group: str = Field(default="netobserv-agent")
    kafka_auto_offset_reset: str = Field(default="latest")

    # ── Topology service (HTTP) ──────────────────────────────────────────────
    topology_service_url: str = Field(default="http://topology:8080")
    topology_request_timeout_seconds: float = Field(default=5.0, gt=0)

    # ── InfluxDB (metric tool queries) ───────────────────────────────────────
    influxdb_url: str = Field(default="http://influxdb:8086")
    influxdb_token: str = Field(default="")
    influxdb_org: str = Field(default="netobserv")
    influxdb_bucket: str = Field(default="metrics")
    influxdb_request_timeout_seconds: float = Field(default=15.0, gt=0)

    # ── OpenSearch (recent-events tool) ──────────────────────────────────────
    opensearch_url: str = Field(
        default="http://opensearch:9200",
        validation_alias=AliasChoices("OPENSEARCH_URL", "OPENSEARCH_HOST"),
    )
    opensearch_user: str = Field(default="")
    opensearch_password: str = Field(default="")
    opensearch_bgp_index_pattern: str = Field(default="bgp-events-*")
    opensearch_anomalies_index_pattern: str = Field(default="anomalies-*")
    opensearch_request_timeout_seconds: float = Field(default=10.0, gt=0)
    opensearch_verify_certs: bool = Field(default=False)

    # ── Agent loop safety ────────────────────────────────────────────────────
    max_tool_calls: int = Field(
        default=8,
        ge=1,
        le=32,
        description="Hard cap on tool invocations per investigation.",
    )
    investigation_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        description="Wall-clock timeout for a single investigation.",
    )
    investigate_severities: str = Field(
        default="WARN,CRITICAL",
        description="Comma-separated severities that trigger an investigation.",
    )

    # ── HTTP server ──────────────────────────────────────────────────────────
    http_host: str = Field(default="0.0.0.0")
    http_port: int = Field(default=8080, ge=1, le=65535)

    # ── Observability ────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def severities_to_investigate(self) -> set[str]:
        return {
            s.strip().upper()
            for s in self.investigate_severities.split(",")
            if s.strip()
        }


def get_settings() -> Settings:
    return Settings()
