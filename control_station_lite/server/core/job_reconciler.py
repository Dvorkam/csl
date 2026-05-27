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

"""Periodic background task: poll agents for running jobs and update the DB.

The reconciler runs every ``_INTERVAL`` seconds.  It finds all jobs whose
status is still ``running`` or ``pending``, groups them by machine, opens one
AgentClient per machine, and refreshes each job's status from the agent.

Jobs that the agent no longer knows about are marked ``failed`` so they don't
stay stuck in ``running`` forever.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_station_lite.server.core.agent_client import AgentClient, AgentClientError
from control_station_lite.server.core.crypto import decrypt
from control_station_lite.server.core.ssh import get_ssh_pool
from control_station_lite.server.db.models import Job, Machine
from control_station_lite.shared.models import JobStatus

logger = logging.getLogger(__name__)

_INTERVAL = 30.0  # seconds between reconciliation passes
_ACTIVE_STATUSES = (JobStatus.running, JobStatus.pending)


async def reconcile_once(factory: async_sessionmaker[AsyncSession], master_key: bytes) -> None:
    """Run a single reconciliation pass."""
    async with factory() as session:
        result = await session.execute(select(Job).where(Job.status.in_(_ACTIVE_STATUSES)))
        jobs = list(result.scalars().all())

    if not jobs:
        return

    machine_ids = {j.machine_id for j in jobs}
    machines: dict[int, Machine] = {}
    async with factory() as session:
        for mid in machine_ids:
            res = await session.execute(select(Machine).where(Machine.id == mid))
            m = res.scalar_one_or_none()
            if m is not None:
                machines[mid] = m

    pool = get_ssh_pool()
    for machine_id, machine in machines.items():
        machine_jobs = [j for j in jobs if j.machine_id == machine_id]
        try:
            private_key = decrypt(machine.ssh_key_encrypted, master_key)
            async with AgentClient(machine, private_key, pool) as client:
                for job in machine_jobs:
                    try:
                        agent_resp = await client.get_job_status(job.job_uuid)
                        if agent_resp.status not in _ACTIVE_STATUSES:
                            async with factory() as session:
                                job_res = await session.execute(
                                    select(Job).where(Job.job_uuid == job.job_uuid)
                                )
                                job_row: Job | None = job_res.scalar_one_or_none()
                                if job_row is not None:
                                    job_row.status = agent_resp.status
                                    job_row.ended_at = agent_resp.ended_at
                                    job_row.exit_code = agent_resp.exit_code
                                    await session.commit()
                    except AgentClientError as exc:
                        logger.debug("Could not poll job %s: %s", job.job_uuid, exc)
                    except Exception as exc:
                        # Job no longer known to agent — mark failed
                        logger.warning(
                            "Job %s not found on agent, marking failed: %s", job.job_uuid, exc
                        )
                        async with factory() as session:
                            job_res = await session.execute(
                                select(Job).where(Job.job_uuid == job.job_uuid)
                            )
                            job_row = job_res.scalar_one_or_none()
                            if job_row is not None:
                                job_row.status = JobStatus.failed
                                await session.commit()
        except Exception as exc:
            logger.warning("Reconciler failed for machine %s: %s", machine.name, exc)


async def reconciler_loop(factory: async_sessionmaker[AsyncSession], master_key: bytes) -> None:
    """Run reconcile_once in a loop, sleeping _INTERVAL seconds between passes."""
    while True:
        await asyncio.sleep(_INTERVAL)
        try:
            await reconcile_once(factory, master_key)
        except Exception as exc:
            logger.exception("Unexpected error in reconciler: %s", exc)
