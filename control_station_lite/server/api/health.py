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

import importlib.metadata
import functools

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.db.session import get_session

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    db: str

@functools.lru_cache(maxsize=1)
def _package_version() -> str:
    try:
        return importlib.metadata.version("control-station-lite")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


@router.get("/healthz", response_model=HealthResponse)
async def healthz(session: AsyncSession = Depends(get_session)) -> HealthResponse:
    try:
        await session.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"

    overall = "ok" if db_status == "ok" else "degraded"
    return HealthResponse(status=overall, version=_package_version(), db=db_status)
