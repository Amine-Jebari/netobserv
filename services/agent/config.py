"""Environment-driven configuration for the agent service (Phase 4.1: RAG)."""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration pulled from environment variables.

    The Phase 4.1 surface only exercises the Postgres + embedding fields; the
    Claude / Kafka / S3 fields are wired here so the same Settings class will
    serve the Phase 4.2 LangChain agent without a refactor.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Postgres (pgvector knowledge base) ───────────────────────────────────
    postgres_host: str = Field(default="postgres")
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = Field(default="netobserv")
    postgres_user: str = Field(default="netobserv")
    postgres_password: str = Field(default="")
    postgres_pool_size: int = Field(default=5, ge=1, le=64)
    postgres_connect_timeout_seconds: float = Field(default=10.0, gt=0)

    runbook_table: str = Field(default="runbook_chunks")

    # ── Embeddings ───────────────────────────────────────────────────────────
    # all-MiniLM-L6-v2 is 384-dim; the runbook_chunks.embedding column is sized
    # to match. Bumping this requires rebuilding the table with the new column
    # type AND re-running the seed script.
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

    # ── Claude (Phase 4.2) ───────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="")
    claude_model: str = Field(default="claude-sonnet-4-6")
    agent_max_tokens: int = Field(default=4096, ge=64, le=8192)
    agent_temperature: float = Field(default=0.2, ge=0.0, le=1.0)

    # ── Observability ────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")


def get_settings() -> Settings:
    return Settings()
