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

"""Script synchronisation between the control station and a remote agent.

State vocabulary
----------------
The agent uses :class:`~control_station_lite.shared.models.ApprovalState`
(absent / pending / approved / update_pending / rejected).

The control station adds one extra server-side state:

``approved_stale``
    The agent approved the script in the past, but the canonical content on
    the control station has since changed.  The approved MD5 the agent holds
    no longer matches ``scripts.md5``.  The script will be re-staged, putting
    it into ``update_pending`` on the agent and requiring the target owner to
    re-approve before it can run again.

All states (including ``approved_stale``) are stored as plain strings in
``script_target_state.state`` — the column is TEXT and imposes no enum
constraint.
"""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.core.agent_client import AgentClient
from control_station_lite.server.db.models import Machine, Script, ScriptTargetState
from control_station_lite.shared.models import ApprovalState

logger = logging.getLogger(__name__)

# Server-side only — never produced by the agent.
APPROVED_STALE = "approved_stale"

# States from which we never re-stage automatically (human decision required).
_NO_RESTAGE = {ApprovalState.pending, ApprovalState.update_pending, ApprovalState.rejected}


async def sync_script(
    machine: Machine,
    script: Script,
    client: AgentClient,
    session: AsyncSession,
) -> str:
    """Fetch approval state from the agent, stage if needed, persist the cache.

    Returns the resolved state string after any staging that was performed.

    The *client* must already be entered as an async context manager by the
    caller (i.e. the SSH tunnel is open).  This function does not open or
    close the tunnel.

    Staging is attempted when:
    - The script is ``absent`` on the agent (first deploy).
    - The script is ``approved`` but the approved MD5 is stale (content has
      changed since the last approval).

    Staging is **not** attempted when the state is ``pending``,
    ``update_pending`` (already queued for review), or ``rejected`` (requires
    explicit human action).
    """
    descriptor = await client.get_script_state(script.name)
    agent_state: ApprovalState = descriptor.state

    # Determine server-side resolved state.
    if agent_state == ApprovalState.approved:
        if descriptor.approved_md5 != script.md5:
            resolved = APPROVED_STALE
        else:
            resolved = ApprovalState.approved
    else:
        resolved = agent_state

    # Stage when the script is absent or the approved version is stale.
    if agent_state == ApprovalState.absent or resolved == APPROVED_STALE:
        logger.info("Staging script %r on %r (state=%s)", script.name, machine.name, resolved)
        stage_resp = await client.stage_script(
            script.name, script.content, script.md5, script.meta_yaml
        )
        # After staging, use the state the agent reported back.
        resolved = stage_resp.state

    await _upsert_state(machine.id, script.id, resolved, descriptor, session)
    return resolved


async def _upsert_state(
    machine_id: int,
    script_id: int,
    resolved: str,
    descriptor: object,
    session: AsyncSession,
) -> None:
    from control_station_lite.shared.models import (
        ScriptDescriptor,  # avoid circular at module level
    )

    desc: ScriptDescriptor = descriptor  # type: ignore[assignment]
    result = await session.execute(
        select(ScriptTargetState).where(
            ScriptTargetState.machine_id == machine_id,
            ScriptTargetState.script_id == script_id,
        )
    )
    row = result.scalar_one_or_none()
    now = datetime.utcnow()
    if row is None:
        session.add(
            ScriptTargetState(
                machine_id=machine_id,
                script_id=script_id,
                state=str(resolved),
                approved_md5=desc.approved_md5,
                pending_md5=desc.pending_md5,
                last_refreshed_at=now,
            )
        )
    else:
        row.state = str(resolved)
        row.approved_md5 = desc.approved_md5
        row.pending_md5 = desc.pending_md5
        row.last_refreshed_at = now
