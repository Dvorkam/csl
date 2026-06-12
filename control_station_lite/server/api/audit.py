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

"""Audit-log read API (admin-only).

JSON counterpart of the admin web viewer (Task 8.12). Same filter semantics:
``action`` (substring match), ``target_type`` (exact), ``username`` (exact),
with offset/limit pagination.
"""

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.auth.dependencies import require_admin
from control_station_lite.server.db.models import AuditLog, User
from control_station_lite.server.db.session import get_session

router = APIRouter(prefix="/api/audit", tags=["audit"])


class AuditEntryOut(BaseModel):
    id: int
    timestamp: datetime
    user_id: int | None
    username: str | None
    action: str
    target_type: str
    target_id: str
    result: str
    details: dict[str, Any] | None


class AuditPageOut(BaseModel):
    items: list[AuditEntryOut]
    limit: int
    offset: int
    has_next: bool


@router.get("", response_model=AuditPageOut)
async def list_audit_entries(
    action: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    username: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AuditPageOut:
    stmt = select(AuditLog, User.username).outerjoin(User, AuditLog.user_id == User.id)
    if action:
        stmt = stmt.where(AuditLog.action.contains(action))
    if target_type:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if username:
        stmt = stmt.where(User.username == username)

    # Fetch one extra row to determine whether a further page exists.
    stmt = stmt.order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
    stmt = stmt.offset(offset).limit(limit + 1)
    rows = list((await session.execute(stmt)).all())
    has_next = len(rows) > limit
    rows = rows[:limit]

    items = [
        AuditEntryOut(
            id=entry.id,
            timestamp=entry.timestamp,
            user_id=entry.user_id,
            username=uname,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            result=entry.result,
            details=json.loads(entry.details_json) if entry.details_json else None,
        )
        for entry, uname in rows
    ]
    return AuditPageOut(items=items, limit=limit, offset=offset, has_next=has_next)
