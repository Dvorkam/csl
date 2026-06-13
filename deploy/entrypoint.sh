#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Container entrypoint for the control station.
#
# Runs forward-only Alembic migrations to head, then execs the command passed
# as arguments (by default `csl-server`). `alembic upgrade head` is idempotent:
# on a database that is already current it is a no-op, so this is safe to run on
# every container start.
set -eu

echo "entrypoint: applying database migrations (alembic upgrade head)"
alembic upgrade head

echo "entrypoint: migrations applied; starting: $*"
exec "$@"
