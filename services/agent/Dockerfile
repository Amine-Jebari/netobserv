# syntax=docker/dockerfile:1.7
#
# Image used by both:
#   - the Phase 4.1 one-shot seeder (`docker compose --profile seeding up
#     seed-runbooks`)
#   - the long-running Phase 4.2 agent service (default ENTRYPOINT, will be
#     filled in next phase).
#
# The seed compose service overrides ENTRYPOINT to ``python -m
# scripts.seed_runbooks`` so a single image serves both lifecycles, mirroring
# how services/anomaly hosts both the trainer and the engine.

# ── Stage 1: install dependency closure into an isolated virtualenv ──────────
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# sentence-transformers / torch wheels are usually published, but a build
# toolchain keeps us safe on architectures (e.g. arm64) where a wheel may be
# missing.
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ git && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# Pre-download the embedding model so the seed script does not have to fetch
# it at runtime (and so air-gapped images keep working). The model name is
# parameterized to match the EMBEDDING_MODEL_NAME env var the runtime reads.
ARG EMBEDDING_MODEL_NAME=sentence-transformers/all-MiniLM-L6-v2
ENV SENTENCE_TRANSFORMERS_HOME=/opt/models
RUN python -c "import os; from sentence_transformers import SentenceTransformer; \
    SentenceTransformer(os.environ.get('EMBEDDING_MODEL_NAME','${EMBEDDING_MODEL_NAME}'))"

# ── Stage 2: minimal runtime ─────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    SENTENCE_TRANSFORMERS_HOME=/opt/models

# curl is used for the Phase 4.2 /health probe; harmless overhead for the
# seeder image.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10004 agent && \
    useradd  --system --uid 10004 --gid agent --no-create-home --shell /usr/sbin/nologin agent

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models /opt/models
RUN chown -R agent:agent /opt/models

# Recreate the ``services.agent`` package layout the modules expect.
WORKDIR /app
RUN mkdir -p /app/services/agent /app/scripts /app/data/runbooks && \
    touch    /app/services/__init__.py && \
    chown -R agent:agent /app

# Agent package modules
COPY --chown=agent:agent __init__.py              /app/services/agent/__init__.py
COPY --chown=agent:agent __main__.py              /app/services/agent/__main__.py
COPY --chown=agent:agent config.py                /app/services/agent/config.py
COPY --chown=agent:agent logging_setup.py         /app/services/agent/logging_setup.py
COPY --chown=agent:agent metrics.py               /app/services/agent/metrics.py
COPY --chown=agent:agent embeddings.py            /app/services/agent/embeddings.py
COPY --chown=agent:agent chunking.py              /app/services/agent/chunking.py
COPY --chown=agent:agent db.py                    /app/services/agent/db.py
COPY --chown=agent:agent rag.py                   /app/services/agent/rag.py
COPY --chown=agent:agent investigations_store.py  /app/services/agent/investigations_store.py
COPY --chown=agent:agent tools.py                 /app/services/agent/tools.py
COPY --chown=agent:agent agent.py                 /app/services/agent/agent.py
COPY --chown=agent:agent runbook.py               /app/services/agent/runbook.py
COPY --chown=agent:agent main.py                  /app/services/agent/main.py

USER agent

EXPOSE 8080

# Default to the long-running agent. The Phase 4.1 seed compose service
# overrides this entrypoint to run ``scripts.seed_runbooks``.
ENTRYPOINT ["python", "-m", "services.agent"]
