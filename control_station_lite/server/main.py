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
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from control_station_lite.server.api import (
    admin,
    audit,
    auth,
    builtin,
    health,
    jobs,
    machines,
    scripts,
)
from control_station_lite.server.core.errors import install_error_handlers
from control_station_lite.server.middleware import RequestIdMiddleware
from control_station_lite.server.web import admin as web_admin
from control_station_lite.server.web import auth as web_auth
from control_station_lite.server.web import dashboard as web_dashboard
from control_station_lite.server.web import machines as web_machines

_SERVER_DIR = Path(__file__).parent

logger = logging.getLogger(__name__)

# Every real endpoint must appear here. Adding an endpoint without updating
# this set will cause test_expected_endpoints_matches_openapi to fail.
_EXPECTED_ENDPOINTS: set[tuple[str, str]] = {
    ("GET", "/healthz"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/refresh"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/machines"),
    ("GET", "/api/machines"),
    ("GET", "/api/machines/{machine_id}"),
    ("DELETE", "/api/machines/{machine_id}"),
    ("POST", "/api/machines/{machine_id}/bookmark"),
    ("DELETE", "/api/machines/{machine_id}/bookmark"),
    ("GET", "/api/machines/{machine_id}/ping"),
    ("GET", "/api/machines/{machine_id}/agent-status"),
    ("POST", "/api/machines/{machine_id}/jobs"),
    ("GET", "/api/scripts"),
    ("GET", "/api/scripts/{name}"),
    ("POST", "/api/scripts"),
    ("PUT", "/api/scripts/{name}"),
    ("DELETE", "/api/scripts/{name}"),
    ("POST", "/api/jobs/{job_uuid}/kill"),
    ("GET", "/api/jobs/{job_uuid}"),
    ("GET", "/api/jobs/{job_uuid}/stream"),
    ("GET", "/api/jobs"),
    ("POST", "/api/machines/{machine_id}/builtin/wol"),
    ("GET", "/api/audit"),
}


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    from control_station_lite.server.config import get_settings
    from control_station_lite.server.core.job_reconciler import reconciler_loop
    from control_station_lite.server.db.session import _session_factory

    try:
        settings = get_settings()
        master_key = settings.read_master_key()
        factory = _session_factory()
        task = asyncio.create_task(reconciler_loop(factory, master_key))
        logger.info("Job reconciler started")
    except Exception as exc:  # pragma: no cover
        logger.warning("Job reconciler could not start (settings not available): %s", exc)
        task = None

    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="control-station-lite", lifespan=_lifespan)

# Correlation-id middleware: outermost so every request (incl. errors) gets an id.
app.add_middleware(RequestIdMiddleware)

# Structured error responses with stable codes (ARCHITECTURE §10).
install_error_handlers(app)

app.mount("/static", StaticFiles(directory=str(_SERVER_DIR / "static")), name="static")

# Web (HTML) routes — included before API so /login takes priority over any future conflict
app.include_router(web_auth.router)
app.include_router(web_dashboard.router)
app.include_router(web_machines.router)
app.include_router(web_admin.router)

# JSON API routes
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(machines.router)
app.include_router(scripts.router)
app.include_router(jobs.router)
app.include_router(builtin.router)
app.include_router(audit.router)
app.include_router(admin.router)


def main() -> None:  # pragma: no cover
    import argparse

    from control_station_lite.server.config import get_settings
    from control_station_lite.server.logging_config import configure_logging

    parser = argparse.ArgumentParser(prog="csl-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", default=False)
    args = parser.parse_args()

    # Structured JSON logging to stdout (ARCHITECTURE §10); pass log_config=None
    # so uvicorn keeps our handler instead of installing its own dictConfig.
    try:
        log_level = get_settings().log_level
    except Exception:
        log_level = "INFO"
    configure_logging(log_level)

    uvicorn.run(
        "control_station_lite.server.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,
    )
