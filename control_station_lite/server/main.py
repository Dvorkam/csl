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

import uvicorn
from fastapi import FastAPI

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
    ("GET", "/api/scripts"),
    ("GET", "/api/scripts/{name}"),
    ("POST", "/api/scripts"),
    ("PUT", "/api/scripts/{name}"),
    ("DELETE", "/api/scripts/{name}"),
}

app = FastAPI(title="control-station-lite")

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(machines.router)
app.include_router(scripts.router)
app.include_router(jobs.router)
app.include_router(builtin.router)
app.include_router(audit.router)
app.include_router(admin.router)


def main() -> None:
    uvicorn.run("control_station_lite.server.main:app", host="127.0.0.1", port=8000, reload=False)
