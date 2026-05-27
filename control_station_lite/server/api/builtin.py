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

"""Built-in actions API (Wake-on-LAN, etc.)."""

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.api.machines import _assert_access, _get_machine_or_404
from control_station_lite.server.auth.dependencies import current_user
from control_station_lite.server.core.magic_packet import broadcast
from control_station_lite.server.db.models import AuditLog, User
from control_station_lite.server.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/machines", tags=["builtin"])


class WolIn(BaseModel):
    broadcast_addr: str = "255.255.255.255"
    port: int = 9


class WolOut(BaseModel):
    sent: bool


@router.post(
    "/{machine_id}/builtin/wol",
    response_model=WolOut,
    status_code=status.HTTP_200_OK,
)
async def wake_on_lan(
    machine_id: int,
    body: WolIn = WolIn(),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> WolOut:
    machine = await _get_machine_or_404(machine_id, session)
    await _assert_access(user, machine_id, session)

    if not machine.mac_address:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Machine has no MAC address configured",
        )

    result = "success"
    details: dict[str, object] = {
        "mac_address": machine.mac_address,
        "broadcast_addr": body.broadcast_addr,
        "port": body.port,
    }
    try:
        broadcast(machine.mac_address, body.broadcast_addr, body.port)
    except Exception as exc:
        result = "failure"
        details["error"] = str(exc)
        logger.warning("WoL broadcast failed for machine %d: %s", machine_id, exc)

    session.add(
        AuditLog(
            timestamp=datetime.utcnow(),
            user_id=user.id,
            action="machine.wol",
            target_type="machine",
            target_id=str(machine_id),
            result=result,
            details_json=json.dumps(details),
        )
    )
    await session.commit()

    if result == "failure":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=details.get("error", "WoL broadcast failed"),
        )

    return WolOut(sent=True)
