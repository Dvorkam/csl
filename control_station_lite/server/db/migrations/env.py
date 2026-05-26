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

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

# Ensure the project root is on sys.path so the package is importable when
# Alembic loads this file via its own module loader (which doesn't inherit
# the CWD-based '' entry that `uv run` adds).
_project_root = Path(__file__).resolve().parents[4]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from alembic import context  # noqa: E402
from sqlalchemy import Connection  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from control_station_lite.server.db.models import Base  # noqa: E402

# ---------------------------------------------------------------------------
# Minimal .env loader — pydantic-settings does not write to os.environ, so
# the alembic CLI would miss .env values without this.  Actual env vars and
# values already in os.environ always take precedence (setdefault).
# ---------------------------------------------------------------------------
_env_file = Path(".env")
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# Alembic boilerplate
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

_DEFAULT_URL = "sqlite+aiosqlite:///data/control-station.sqlite"


def _db_url() -> str:
    return os.environ.get("CSL_DATABASE_URL", _DEFAULT_URL)


# ---------------------------------------------------------------------------
# Offline mode — emit raw SQL without connecting
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode — connect via async engine
# ---------------------------------------------------------------------------
def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine = create_async_engine(_db_url())
    async with engine.begin() as conn:
        await conn.run_sync(_do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
