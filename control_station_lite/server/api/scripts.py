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

"""Script library API: CRUD endpoints for the canonical script store."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.auth.dependencies import current_user, require_admin
from control_station_lite.server.core.audit import record_audit
from control_station_lite.server.core.script_registry import (
    ScriptRegistryError,
    create_script,
    delete_script,
    get_script_or_raise,
    list_scripts,
    update_script,
)
from control_station_lite.server.db.models import Script, User
from control_station_lite.server.db.session import get_session

router = APIRouter(prefix="/api/scripts", tags=["scripts"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ScriptIn(BaseModel):
    name: str
    content: str
    meta_yaml: str | None = None


class ScriptUpdateIn(BaseModel):
    content: str
    meta_yaml: str | None = None


class ScriptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    content: str
    meta_yaml: str | None
    md5: str
    persistent: bool
    updated_at: datetime
    updated_by: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ScriptOut])
async def list_scripts_endpoint(
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Script]:
    return await list_scripts(session)


@router.get("/{name}", response_model=ScriptOut)
async def get_script_endpoint(
    name: str,
    _user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Script:
    try:
        return await get_script_or_raise(name, session)
    except ScriptRegistryError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ScriptOut)
async def create_script_endpoint(
    body: ScriptIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Script:
    try:
        script = await create_script(
            name=body.name,
            content=body.content,
            meta_yaml=body.meta_yaml,
            user_id=admin.id,
            session=session,
        )
        await session.flush()
        await record_audit(
            session,
            action="script.create",
            target_type="script",
            target_id=script.name,
            result="success",
            user_id=admin.id,
            details={"md5": script.md5},
        )
        await session.commit()
        await session.refresh(script)
        return script
    except ScriptRegistryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.put("/{name}", response_model=ScriptOut)
async def update_script_endpoint(
    name: str,
    body: ScriptUpdateIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Script:
    try:
        script = await update_script(
            name=name,
            content=body.content,
            meta_yaml=body.meta_yaml,
            user_id=admin.id,
            session=session,
        )
        await session.flush()
        await record_audit(
            session,
            action="script.update",
            target_type="script",
            target_id=script.name,
            result="success",
            user_id=admin.id,
            details={"md5": script.md5},
        )
        await session.commit()
        await session.refresh(script)
        return script
    except ScriptRegistryError as exc:
        status_code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in str(exc)
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_script_endpoint(
    name: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    try:
        await delete_script(name, session)
        await record_audit(
            session,
            action="script.delete",
            target_type="script",
            target_id=name,
            result="success",
            user_id=admin.id,
        )
        await session.commit()
    except ScriptRegistryError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
