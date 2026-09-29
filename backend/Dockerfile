FROM ghcr.io/astral-sh/uv:0.8.4 AS uv
FROM python:3.12-slim AS source
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock README.md ./
COPY backend/pyproject.toml ./backend/pyproject.toml
COPY packages/core/pyproject.toml ./packages/core/pyproject.toml
# The workspace lock needs the research manifest, not research code or artifacts.
COPY data-science/pyproject.toml ./data-science/pyproject.toml
COPY backend/src ./backend/src
COPY packages/core/src ./packages/core/src

# One-shot operational tools (migrations, bounded replay loader, local inbox watcher).
# Built only on request (`target: ops`); adds the `data` group for DuckDB/Parquet.
# Never used for the HTTP API and never contains data, weights or secrets.
FROM source AS ops
RUN uv sync --locked --no-dev --no-editable --group data
COPY backend/migrations ./backend/migrations
COPY backend/scripts ./backend/scripts
RUN useradd --create-home --uid 10001 app
USER app
CMD ["python", "backend/scripts/migrate_operational_db.py"]

# Ingestion worker (B1): `python -m infra_pulse_backend.worker` parses uploaded CSV,
# loads PostgreSQL and calls the forecast recompute. Extra `worker` (core features for
# B2) plus the `data` group; applies the idempotent migrations at start. The upload
# directory is created with the app owner so a fresh named volume inherits it.
FROM source AS worker
RUN uv sync --locked --no-dev --no-editable --group data \
 && uv sync --locked --no-dev --no-editable --inexact --package infra-pulse-backend --extra worker
COPY backend/migrations ./backend/migrations
RUN useradd --create-home --uid 10001 app && install -d -o app -g app /app/var/uploads
USER app
CMD ["python", "-m", "infra_pulse_backend.worker"]

# HTTP API: the default (last) stage, base dependencies only.
FROM source AS api
RUN uv sync --locked --no-dev --no-editable
# G1: aggregated «Исследование» summary exported from the v9 report (no data, no IDs);
# served outside fixture mode via INFRA_RESEARCH_SUMMARY_PATH.
COPY backend/research ./backend/research
# B1: uploads are written to the shared volume at /app/var/uploads (INFRA_UPLOAD_DIR).
RUN useradd --create-home --uid 10001 app && install -d -o app -g app /app/var/uploads
USER app
EXPOSE 8000
CMD ["uvicorn", "infra_pulse_backend.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
