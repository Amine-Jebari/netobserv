# syntax=docker/dockerfile:1.7

# ── Stage 1: build wheel into an isolated virtualenv ──────────────────────────
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
COPY netobserv_topology ./netobserv_topology

RUN pip install --upgrade pip && \
    pip install .

# ── Stage 2: minimal runtime image ────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is kept only for the container HEALTHCHECK; remove apt lists afterward.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10002 topology && \
    useradd  --system --uid 10002 --gid topology --no-create-home --shell /usr/sbin/nologin topology

COPY --from=builder /opt/venv /opt/venv

USER topology

WORKDIR /app
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS http://localhost:8080/health || exit 1

ENTRYPOINT ["netobserv-topology"]
