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

COPY --chown=anomaly:anomaly __init__.py        /app/services/anomaly/__init__.py
COPY --chown=anomaly:anomaly baseline.py        /app/services/anomaly/baseline.py
COPY --chown=anomaly:anomaly features.py        /app/services/anomaly/features.py
COPY --chown=anomaly:anomaly config.py          /app/services/anomaly/config.py
COPY --chown=anomaly:anomaly logging_setup.py   /app/services/anomaly/logging_setup.py
COPY --chown=anomaly:anomaly influx_query.py    /app/services/anomaly/influx_query.py
COPY --chown=anomaly:anomaly opensearch_query.py /app/services/anomaly/opensearch_query.py
COPY --chown=anomaly:anomaly model_store.py     /app/services/anomaly/model_store.py

USER anomaly

ENTRYPOINT ["python", "-m", "services.anomaly.baseline"]
