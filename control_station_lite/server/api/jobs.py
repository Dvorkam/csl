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

"""Jobs API: submit, status, log streaming, kill, history."""

import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.api.machines import _assert_access, _get_machine_or_404
from control_station_lite.server.auth.dependencies import current_user
from control_station_lite.server.config import get_settings
from control_station_lite.server.core.agent_client import AgentClient, AgentClientError
from control_station_lite.server.core.crypto import decrypt
from control_station_lite.server.core.script_registry import (
    ScriptRegistryError,
    get_script_or_raise,
)
from control_station_lite.server.core.script_sync import sync_script
from control_station_lite.server.core.ssh import get_ssh_pool
from control_station_lite.server.db.models import Job, Machine, Script, User
from control_station_lite.server.db.session import get_session
from control_station_lite.shared.models import (
    ApprovalState,
    JobRequest,
    JobStatus,
    JobStatusResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SubmitJobIn(BaseModel):
    script_name: str
    params: dict[str, str | int | float | bool] = {}


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_uuid: str
    machine_id: int
    script_id: int | None
    user_id: int
    params_json: str
    status: str
    persistent: bool
    started_at: datetime
    ended_at: datetime | None
    exit_code: int | None


class ApprovalErrorOut(BaseModel):
    approval_error: str
    agent_state: str
    detail: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _decrypt_key(machine: Machine) -> bytes:
    master_key = get_settings().read_master_key()
    return decrypt(machine.ssh_key_encrypted, master_key)


async def _get_job_or_404(job_uuid: str, session: AsyncSession) -> Job:
    result = await session.execute(select(Job).where(Job.job_uuid == job_uuid))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


def _approval_error_response(agent_state: str, script_name: str) -> HTTPException:
    """Build a 409 HTTPException for non-approved script states."""
    if agent_state == ApprovalState.pending:
        code = "pending_approval (new)"
        msg = f"Script {script_name!r} is pending approval on this machine"
    elif agent_state == ApprovalState.update_pending:
        code = "pending_approval (update)"
        msg = f"Script {script_name!r} has a pending update awaiting approval"
    elif agent_state == ApprovalState.rejected:
        code = "rejected"
        msg = f"Script {script_name!r} was rejected on this machine"
    else:
        code = agent_state
        msg = f"Script {script_name!r} is not approved (state={agent_state!r})"
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"approval_error": code, "agent_state": agent_state, "detail": msg},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/api/machines/{machine_id}/jobs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobOut,
)
async def submit_job(
    machine_id: int,
    body: SubmitJobIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Job:
    machine = await _get_machine_or_404(machine_id, session)
    await _assert_access(user, machine_id, session)
    try:
        script = await get_script_or_raise(body.script_name, session)
    except ScriptRegistryError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    private_key = _decrypt_key(machine)
    pool = get_ssh_pool()

    job_id = str(uuid.uuid4())
    agent_resp: JobStatusResponse | None = None

    async with AgentClient(machine, private_key, pool) as client:
        await client.ensure_agent_running()

        # 6.2 — sync before submitting; commit so the cache is persisted
        resolved = await sync_script(machine, script, client, session)
        await session.commit()

        if resolved != ApprovalState.approved:
            raise _approval_error_response(resolved, body.script_name)

        request = JobRequest(
            job_uuid=job_id,
            script_name=body.script_name,
            params=body.params,
            persistent=script.persistent,
        )
        try:
            agent_resp = await client.submit_job(request)
        except AgentClientError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
            ) from exc

    assert agent_resp is not None
    job = Job(
        job_uuid=job_id,
        machine_id=machine_id,
        script_id=script.id,
        user_id=user.id,
        params_json=json.dumps(body.params),
        status=agent_resp.status,
        persistent=script.persistent,
        started_at=agent_resp.started_at,
        ended_at=agent_resp.ended_at,
        exit_code=agent_resp.exit_code,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


@router.get("/api/jobs/{job_uuid}", response_model=JobOut)
async def get_job(
    job_uuid: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Job:
    job = await _get_job_or_404(job_uuid, session)
    # Refresh status from agent when the job is still running
    if job.status in (JobStatus.running, JobStatus.pending):
        result = await session.execute(select(Machine).where(Machine.id == job.machine_id))
        machine = result.scalar_one_or_none()
        if machine is not None:
            try:
                private_key = _decrypt_key(machine)
                pool = get_ssh_pool()
                async with AgentClient(machine, private_key, pool) as client:
                    agent_resp = await client.get_job_status(job_uuid)
                job.status = agent_resp.status
                job.ended_at = agent_resp.ended_at
                job.exit_code = agent_resp.exit_code
                await session.commit()
                await session.refresh(job)
            except Exception:
                logger.debug("Could not refresh job %s from agent", job_uuid)
    return job


@router.get("/api/jobs/{job_uuid}/stream")
async def stream_job_logs(
    job_uuid: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    job = await _get_job_or_404(job_uuid, session)
    result = await session.execute(select(Machine).where(Machine.id == job.machine_id))
    machine = result.scalar_one_or_none()
    if machine is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found")

    private_key = _decrypt_key(machine)
    pool = get_ssh_pool()

    async def _event_stream() -> AsyncGenerator[str, None]:
        try:
            async with AgentClient(machine, private_key, pool) as client:
                async for line in client.stream_logs(job_uuid):
                    yield f"data: {line}\n\n"
        except Exception as exc:
            yield f"event: error\ndata: {exc}\n\n"

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


@router.post("/api/jobs/{job_uuid}/kill", status_code=status.HTTP_204_NO_CONTENT)
async def kill_job(
    job_uuid: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    job = await _get_job_or_404(job_uuid, session)
    if not job.persistent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot kill a non-persistent job"
        )
    result = await session.execute(select(Machine).where(Machine.id == job.machine_id))
    machine = result.scalar_one_or_none()
    if machine is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found")

    private_key = _decrypt_key(machine)
    pool = get_ssh_pool()
    async with AgentClient(machine, private_key, pool) as client:
        await client.kill_job(job_uuid)

    job.status = JobStatus.killed
    job.ended_at = datetime.utcnow()
    await session.commit()


@router.get("/api/jobs", response_model=list[JobOut])
async def list_jobs(
    machine_id: int | None = Query(default=None),
    script_name: str | None = Query(default=None),
    job_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Job]:
    q = select(Job)
    if machine_id is not None:
        q = q.where(Job.machine_id == machine_id)
    if job_status is not None:
        q = q.where(Job.status == job_status)
    if script_name is not None:
        result = await session.execute(select(Script).where(Script.name == script_name))
        script = result.scalar_one_or_none()
        if script is None:
            return []
        q = q.where(Job.script_id == script.id)
    q = q.order_by(Job.started_at.desc()).limit(limit)
    result = await session.execute(q)
    return list(result.scalars().all())
