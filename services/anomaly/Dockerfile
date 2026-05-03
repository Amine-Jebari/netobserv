# syntax=docker/dockerfile:1.7

# ── Stage 1: install dependency closure into an isolated virtualenv ──────────
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# scikit-learn / pyod wheels are usually available, but a build toolchain keeps
# us safe on architectures (e.g. arm64) where a wheel might be missing.
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# ── Stage 2: minimal runtime ─────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is only needed for the Docker healthcheck on the engine entry point.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10003 anomaly && \
    useradd  --system --uid 10003 --gid anomaly --no-create-home --shell /usr/sbin/nologin anomaly

COPY --from=builder /opt/venv /opt/venv

# Recreate the ``services.anomaly`` package layout the modules expect. The
# parent ``services`` package marker is created here (not pulled from the build
# context) so the Dockerfile remains buildable from this directory only.
WORKDIR /app
RUN mkdir -p /app/services/anomaly && \
    touch    /app/services/__init__.py && \
    chown -R anomaly:anomaly /app

# Trainer + shared modules
COPY --chown=anomaly:anomaly __init__.py            /app/services/anomaly/__init__.py
COPY --chown=anomaly:anomaly baseline.py            /app/services/anomaly/baseline.py
COPY --chown=anomaly:anomaly features.py            /app/services/anomaly/features.py
COPY --chown=anomaly:anomaly config.py              /app/services/anomaly/config.py
COPY --chown=anomaly:anomaly logging_setup.py       /app/services/anomaly/logging_setup.py
COPY --chown=anomaly:anomaly influx_query.py        /app/services/anomaly/influx_query.py
COPY --chown=anomaly:anomaly opensearch_query.py    /app/services/anomaly/opensearch_query.py
COPY --chown=anomaly:anomaly model_store.py         /app/services/anomaly/model_store.py

# Phase 3.2 engine modules
COPY --chown=anomaly:anomaly engine.py              /app/services/anomaly/engine.py
COPY --chown=anomaly:anomaly engine_metrics.py      /app/services/anomaly/engine_metrics.py
COPY --chown=anomaly:anomaly windowing.py           /app/services/anomaly/windowing.py
COPY --chown=anomaly:anomaly scorer.py              /app/services/anomaly/scorer.py
COPY --chown=anomaly:anomaly kafka_io.py            /app/services/anomaly/kafka_io.py
COPY --chown=anomaly:anomaly model_fetch.py         /app/services/anomaly/model_fetch.py
COPY --chown=anomaly:anomaly topology_client.py     /app/services/anomaly/topology_client.py
COPY --chown=anomaly:anomaly slack_alerter.py       /app/services/anomaly/slack_alerter.py

USER anomaly

# Default to the long-running engine. The one-shot baseline trainer overrides
# the entrypoint via ``docker compose --profile training up baseline``.
ENTRYPOINT ["python", "-m", "services.anomaly.engine"]
