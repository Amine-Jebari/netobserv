# Package marker so ``python -m scripts.seed_runbooks`` resolves correctly
# both inside the container (WORKDIR=/app, scripts/ is mounted at /app/scripts)
# and from the repo root during local development.
