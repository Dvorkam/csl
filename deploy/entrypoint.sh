#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# control-station-lite
# Copyright (C) 2026 Michal Dvořák
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version, with an additional permission for
# distribution through app stores (see LICENSE).

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
