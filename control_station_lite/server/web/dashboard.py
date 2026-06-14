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

"""Dashboard and HTMX partial endpoints for the browser frontend."""

import time

import asyncssh
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.config import get_settings
from control_station_lite.server.core.crypto import decrypt
from control_station_lite.server.core.ssh import build_known_hosts
from control_station_lite.server.db.models import Machine, User, UserMachine
from control_station_lite.server.db.session import get_session
from control_station_lite.server.web import templates
from control_station_lite.server.web.deps import clear_flash, read_flash, web_current_user

router = APIRouter(tags=["web"])


@router.get("/", include_in_schema=False)
async def dashboard(
    request: Request,
    user: User = Depends(web_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Main dashboard: list of bookmarked machines."""
    if user.role == "admin":
        result = await session.execute(select(Machine))
        machines = list(result.scalars().all())
    else:
        result = await session.execute(
            select(Machine)
            .join(UserMachine, UserMachine.machine_id == Machine.id)
            .where(UserMachine.user_id == user.id)
        )
        machines = list(result.scalars().all())

    resp = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "machines": machines,
            "flash": read_flash(request),
        },
    )
    clear_flash(resp)
    return resp


@router.get("/machines/{machine_id}/ping-badge", include_in_schema=False)
async def ping_badge(
    machine_id: int,
    request: Request,
    user: User = Depends(web_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """HTMX partial: SSH reachability badge for one machine."""
    result = await session.execute(select(Machine).where(Machine.id == machine_id))
    machine = result.scalar_one_or_none()
    if machine is None:
        return templates.TemplateResponse(
            request,
            "partials/ping_badge.html",
            {"reachable": False, "latency_ms": None},
        )

    # Access control: non-admins need a bookmark
    if user.role != "admin":
        bm = await session.execute(
            select(UserMachine).where(
                UserMachine.user_id == user.id,
                UserMachine.machine_id == machine_id,
            )
        )
        if bm.scalar_one_or_none() is None:
            return templates.TemplateResponse(
                request,
                "partials/ping_badge.html",
                {"reachable": False, "latency_ms": None},
            )

    private_key_bytes = decrypt(machine.ssh_key_encrypted, get_settings().read_master_key())

    t0 = time.monotonic()
    try:
        async with asyncssh.connect(
            machine.ssh_host,
            port=machine.ssh_port,
            username=machine.ssh_user,
            client_keys=[asyncssh.import_private_key(private_key_bytes)],
            known_hosts=build_known_hosts(machine.ssh_host_key),
            connect_timeout=5.0,
        ):
            latency_ms = round((time.monotonic() - t0) * 1000, 2)
        return templates.TemplateResponse(
            request,
            "partials/ping_badge.html",
            {"reachable": True, "latency_ms": latency_ms},
        )
    except (OSError, asyncssh.Error):
        return templates.TemplateResponse(
            request,
            "partials/ping_badge.html",
            {"reachable": False, "latency_ms": None},
        )
