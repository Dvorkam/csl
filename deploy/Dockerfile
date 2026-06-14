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
# Set an absolute mode (not `chmod +x`): COPY preserves the build-context file's
# permissions, so on a host with a restrictive umask (e.g. a root `umask 077`,
# as on Synology DSM) the source file can be mode 600 and `+x` would leave it
# unreadable by the non-root runtime user. 0755 makes it world-readable+exec.
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["csl-server"]

# --- production ------------------------------------------------------------
FROM base AS prod

# Non-root runtime user. The UID is pinned (not auto-assigned) so the host can
# `chown` the bind-mounted data dirs (db/secrets/logs) to a known id — see
# scripts/setup.sh and docs/guides/operator.md.
RUN groupadd --system --gid 999 csl \
    && useradd --system --uid 999 --gid csl --home-dir /app --shell /usr/sbin/nologin csl

COPY pyproject.toml README.md alembic.ini ./
COPY control_station_lite/ ./control_station_lite/

RUN pip install --no-cache-dir ".[server]"

# COPY preserves build-context permissions; under a restrictive host umask the
# copied files (alembic.ini, the package) can be unreadable by `csl`. Force
# read+traverse for all so the image runs regardless of how it was checked out.
RUN chmod -R a+rX /app

USER csl

# --- development -----------------------------------------------------------
FROM base AS dev

COPY pyproject.toml README.md alembic.ini ./
COPY control_station_lite/ ./control_station_lite/

# Editable install: the source under /app/control_station_lite is bind-mounted
# by docker-compose.override.yml, so edits take effect without a rebuild.
RUN pip install --no-cache-dir -e ".[server]"
