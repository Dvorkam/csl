# syntax=docker/dockerfile:1
#
# Control station image. Two targets:
#
#   prod (default) — package baked in at build time, no runtime dependency on
#                    the source tree. Built/pushed by release.yml, runs as a
#                    non-root user. This is what docker-compose.yml uses.
#   dev            — editable install; docker-compose.override.yml bind-mounts
#                    the source tree over /app so code changes are reflected
#                    without rebuilding. Runs as root so the bind mount is
#                    writable. The asymmetry with prod is intentional.

# --- shared base -----------------------------------------------------------
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Migrations run from /app at startup; alembic.ini's script_location is relative.
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["csl-server"]

# --- production ------------------------------------------------------------
FROM base AS prod

# Non-root runtime user; owns /app so a writable bind/volume is not required.
RUN groupadd --system csl \
    && useradd --system --gid csl --home-dir /app --shell /usr/sbin/nologin csl

COPY pyproject.toml README.md alembic.ini ./
COPY control_station_lite/ ./control_station_lite/

RUN pip install --no-cache-dir ".[server]"

USER csl

# --- development -----------------------------------------------------------
FROM base AS dev

COPY pyproject.toml README.md alembic.ini ./
COPY control_station_lite/ ./control_station_lite/

# Editable install: the source under /app/control_station_lite is bind-mounted
# by docker-compose.override.yml, so edits take effect without a rebuild.
RUN pip install --no-cache-dir -e ".[server]"
