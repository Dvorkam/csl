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

"""Machine detail, script run dialog, job detail and live log web routes."""

import json
import logging
import uuid as uuid_mod
from collections.abc import AsyncGenerator
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.config import get_settings
from control_station_lite.server.core.agent_client import AgentClient, AgentClientError
from control_station_lite.server.core.crypto import decrypt
from control_station_lite.server.core.script_registry import ScriptRegistryError, get_script_or_raise
from control_station_lite.server.core.script_sync import sync_script
from control_station_lite.server.core.ssh import get_ssh_pool
from control_station_lite.server.db.models import (
    Job,
    Machine,
    Script,
    ScriptTargetState,
    User,
    UserMachine,
)
from control_station_lite.server.db.session import get_session
from control_station_lite.server.web import templates
from control_station_lite.server.web.deps import pop_flash, set_flash, web_current_user
from control_station_lite.shared.models import ApprovalState, JobRequest, JobStatus
from control_station_lite.shared.script_meta import ScriptMetaError, parse_meta_yaml

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_machine_or_404(machine_id: int, session: AsyncSession) -> Machine:
    result = await session.execute(select(Machine).where(Machine.id == machine_id))
    machine = result.scalar_one_or_none()
    if machine is None:
        raise HTTPException(status_code=404, detail="Machine not found")
    return machine


async def _check_machine_access(user: User, machine_id: int, session: AsyncSession) -> None:
    if user.role == "admin":
        return
    bm = await session.execute(
        select(UserMachine).where(
            UserMachine.user_id == user.id,
            UserMachine.machine_id == machine_id,
        )
    )
    if bm.scalar_one_or_none() is None:
        raise HTTPException(status_code=403, detail="Access denied")


async def _get_job_or_404(job_uuid: str, session: AsyncSession) -> Job:
    result = await session.execute(select(Job).where(Job.job_uuid == job_uuid))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _approval_msg(state: str, script_name: str) -> str:
    if state == ApprovalState.pending:
        return f"Script '{script_name}' is pending approval on this machine"
    if state == ApprovalState.update_pending:
        return f"Script '{script_name}' has a pending update awaiting re-approval"
    if state == ApprovalState.rejected:
        return f"Script '{script_name}' was rejected on this machine"
    return f"Script '{script_name}' is not approved (state={state!r})"


# ---------------------------------------------------------------------------
# Machine detail (8.4 + 8.7)
# ---------------------------------------------------------------------------


@router.get("/machines/{machine_id}", include_in_schema=False)
async def machine_detail(
    machine_id: int,
    request: Request,
    response: Response,
    user: User = Depends(web_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    machine = await _get_machine_or_404(machine_id, session)
    await _check_machine_access(user, machine_id, session)

    scripts_res = await session.execute(select(Script).order_by(Script.name))
    all_scripts = list(scripts_res.scalars().all())

    states_res = await session.execute(
        select(ScriptTargetState).where(ScriptTargetState.machine_id == machine_id)
    )
    states_by_id = {row.script_id: row for row in states_res.scalars().all()}

    script_states = []
    for s in all_scripts:
        row = states_by_id.get(s.id)
        try:
            desc = parse_meta_yaml(s.meta_yaml).description if s.meta_yaml else ""
        except ScriptMetaError:
            desc = ""
        script_states.append(
            {
                "script": s,
                "description": desc,
                "state": row.state if row else "absent",
                "approved_md5": row.approved_md5 if row else None,
                "pending_md5": row.pending_md5 if row else None,
            }
        )

    jobs_res = await session.execute(
        select(Job)
        .where(
            Job.machine_id == machine_id,
            Job.persistent.is_(True),
            Job.status.in_([JobStatus.running, JobStatus.pending]),
        )
        .order_by(Job.started_at.desc())
    )
    running_jobs = list(jobs_res.scalars().all())

    flash = pop_flash(request, response)
    return templates.TemplateResponse(
        request,
        "machine_detail.html",
        {
            "user": user,
            "machine": machine,
            "script_states": script_states,
            "running_jobs": running_jobs,
            "flash": flash,
        },
    )


# ---------------------------------------------------------------------------
# Script run dialog (8.5)
# ---------------------------------------------------------------------------


@router.get("/machines/{machine_id}/scripts/{script_name}/run", include_in_schema=False)
async def run_form(
    machine_id: int,
    script_name: str,
    request: Request,
    user: User = Depends(web_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    machine = await _get_machine_or_404(machine_id, session)
    await _check_machine_access(user, machine_id, session)

    try:
        script = await get_script_or_raise(script_name, session)
    except ScriptRegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        params = parse_meta_yaml(script.meta_yaml).params if script.meta_yaml else []
    except ScriptMetaError:
        params = []

    return templates.TemplateResponse(
        request,
        "run_dialog.html",
        {
            "user": user,
            "machine": machine,
            "script": script,
            "params": params,
            "error": None,
        },
    )


@router.post("/machines/{machine_id}/scripts/{script_name}/run", include_in_schema=False)
async def run_submit(
    machine_id: int,
    script_name: str,
    request: Request,
    user: User = Depends(web_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    machine = await _get_machine_or_404(machine_id, session)
    await _check_machine_access(user, machine_id, session)

    try:
        script = await get_script_or_raise(script_name, session)
    except ScriptRegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        meta_params = parse_meta_yaml(script.meta_yaml).params if script.meta_yaml else []
    except ScriptMetaError:
        meta_params = []

    form = await request.form()
    params: dict[str, str | int | float | bool] = {}
    for p in meta_params:
        raw_field = form.get(p.name)
        raw: str | None = str(raw_field) if isinstance(raw_field, str) else None
        if p.type.value == "bool":
            params[p.name] = raw is not None
        elif p.type.value == "int":
            try:
                params[p.name] = int(raw) if raw else (int(p.default) if p.default is not None else 0)
            except (ValueError, TypeError):
                params[p.name] = 0
        elif p.type.value == "float":
            try:
                params[p.name] = (
                    float(raw) if raw else (float(p.default) if p.default is not None else 0.0)
                )
            except (ValueError, TypeError):
                params[p.name] = 0.0
        else:
            params[p.name] = (
                raw if raw is not None else (str(p.default) if p.default is not None else "")
            )

    private_key = decrypt(machine.ssh_key_encrypted, get_settings().read_master_key())
    pool = get_ssh_pool()
    job_id = str(uuid_mod.uuid4())

    def _error_ctx(approval_error: str) -> dict[str, object]:
        return {
            "user": user,
            "machine": machine,
            "script": script,
            "params": meta_params,
            "error": {
                "approval_error": approval_error,
                "detail": _approval_msg(approval_error, script_name),
            },
        }

    try:
        async with AgentClient(machine, private_key, pool) as client:
            await client.ensure_agent_running()
            resolved = await sync_script(machine, script, client, session)
            await session.commit()

            if resolved != ApprovalState.approved:
                return templates.TemplateResponse(
                    request, "run_dialog.html", _error_ctx(resolved), status_code=409
                )

            job_req = JobRequest(
                job_uuid=job_id,
                script_name=script_name,
                params=params,
                persistent=script.persistent,
            )
            agent_resp = await client.submit_job(job_req)
    except AgentClientError as exc:
        logger.warning("Agent error submitting job for %r: %s", script_name, exc)
        return templates.TemplateResponse(
            request, "run_dialog.html", _error_ctx("agent_error"), status_code=502
        )

    job = Job(
        job_uuid=job_id,
        machine_id=machine_id,
        script_id=script.id,
        user_id=user.id,
        params_json=json.dumps(params),
        status=agent_resp.status,
        persistent=script.persistent,
        started_at=agent_resp.started_at,
        ended_at=agent_resp.ended_at,
        exit_code=agent_resp.exit_code,
    )
    session.add(job)
    await session.commit()

    resp = RedirectResponse(f"/jobs/{job_id}", status_code=303)
    set_flash(resp, f"Job started: {script_name}", "success")
    return resp


# ---------------------------------------------------------------------------
# Approval badge: on-demand state refresh (8.7)
# ---------------------------------------------------------------------------


@router.get("/machines/{machine_id}/scripts/{script_name}/state-badge", include_in_schema=False)
async def script_state_badge(
    machine_id: int,
    script_name: str,
    request: Request,
    user: User = Depends(web_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """HTMX partial: query the agent for the current approval state of one script."""
    machine = await _get_machine_or_404(machine_id, session)
    await _check_machine_access(user, machine_id, session)

    try:
        script = await get_script_or_raise(script_name, session)
    except ScriptRegistryError:
        return templates.TemplateResponse(
            request,
            "partials/script_state_badge.html",
            {"script_id": 0, "machine_id": machine_id, "script_name": script_name,
             "state": "absent", "approved_md5": None, "pending_md5": None},
        )

    private_key = decrypt(machine.ssh_key_encrypted, get_settings().read_master_key())
    pool = get_ssh_pool()

    cached_res = await session.execute(
        select(ScriptTargetState).where(
            ScriptTargetState.machine_id == machine_id,
            ScriptTargetState.script_id == script.id,
        )
    )
    cached_row = cached_res.scalar_one_or_none()

    try:
        async with AgentClient(machine, private_key, pool) as client:
            descriptor = await client.get_script_state(script.name)
            new_state = str(descriptor.state)
    except AgentClientError:
        return templates.TemplateResponse(
            request,
            "partials/script_state_badge.html",
            {"script_id": script.id, "machine_id": machine_id, "script_name": script_name,
             "state": cached_row.state if cached_row else "absent",
             "approved_md5": cached_row.approved_md5 if cached_row else None,
             "pending_md5": cached_row.pending_md5 if cached_row else None},
        )

    now = datetime.utcnow()
    if cached_row is None:
        session.add(ScriptTargetState(
            machine_id=machine_id, script_id=script.id, state=new_state,
            approved_md5=descriptor.approved_md5, pending_md5=descriptor.pending_md5,
            last_refreshed_at=now,
        ))
    else:
        cached_row.state = new_state
        cached_row.approved_md5 = descriptor.approved_md5
        cached_row.pending_md5 = descriptor.pending_md5
        cached_row.last_refreshed_at = now
    await session.commit()

    return templates.TemplateResponse(
        request,
        "partials/script_state_badge.html",
        {"script_id": script.id, "machine_id": machine_id, "script_name": script_name,
         "state": new_state, "approved_md5": descriptor.approved_md5,
         "pending_md5": descriptor.pending_md5},
    )


# ---------------------------------------------------------------------------
# Approval badge re-stage (8.7)
# ---------------------------------------------------------------------------


@router.post("/machines/{machine_id}/scripts/{script_name}/restage", include_in_schema=False)
async def restage(
    machine_id: int,
    script_name: str,
    request: Request,
    user: User = Depends(web_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    machine = await _get_machine_or_404(machine_id, session)
    await _check_machine_access(user, machine_id, session)

    try:
        script = await get_script_or_raise(script_name, session)
    except ScriptRegistryError:
        return templates.TemplateResponse(
            request,
            "partials/script_state_badge.html",
            {
                "script_id": 0,
                "machine_id": machine_id,
                "script_name": script_name,
                "state": "absent",
                "approved_md5": None,
                "pending_md5": None,
            },
        )

    private_key = decrypt(machine.ssh_key_encrypted, get_settings().read_master_key())
    pool = get_ssh_pool()

    # Fetch current cached row before attempting SSH (used on failure path)
    cached_res = await session.execute(
        select(ScriptTargetState).where(
            ScriptTargetState.machine_id == machine_id,
            ScriptTargetState.script_id == script.id,
        )
    )
    cached_row = cached_res.scalar_one_or_none()

    try:
        async with AgentClient(machine, private_key, pool) as client:
            await client.stage_script(script.name, script.content, script.md5, script.meta_yaml)
            descriptor = await client.get_script_state(script.name)
            new_state = str(descriptor.state)
    except AgentClientError as exc:
        logger.warning("Re-stage failed for %r on machine %d: %s", script_name, machine_id, exc)
        return templates.TemplateResponse(
            request,
            "partials/script_state_badge.html",
            {
                "script_id": script.id,
                "machine_id": machine_id,
                "script_name": script_name,
                "state": cached_row.state if cached_row else "absent",
                "approved_md5": cached_row.approved_md5 if cached_row else None,
                "pending_md5": cached_row.pending_md5 if cached_row else None,
            },
        )

    now = datetime.utcnow()
    if cached_row is None:
        session.add(
            ScriptTargetState(
                machine_id=machine_id,
                script_id=script.id,
                state=new_state,
                approved_md5=descriptor.approved_md5,
                pending_md5=descriptor.pending_md5,
                last_refreshed_at=now,
            )
        )
    else:
        cached_row.state = new_state
        cached_row.approved_md5 = descriptor.approved_md5
        cached_row.pending_md5 = descriptor.pending_md5
        cached_row.last_refreshed_at = now
    await session.commit()

    return templates.TemplateResponse(
        request,
        "partials/script_state_badge.html",
        {
            "script_id": script.id,
            "machine_id": machine_id,
            "script_name": script_name,
            "state": new_state,
            "approved_md5": descriptor.approved_md5,
            "pending_md5": descriptor.pending_md5,
        },
    )


# ---------------------------------------------------------------------------
# Job detail + live log viewer (8.6)
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_uuid}", include_in_schema=False)
async def job_detail(
    job_uuid: str,
    request: Request,
    response: Response,
    user: User = Depends(web_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    job = await _get_job_or_404(job_uuid, session)

    machine_res = await session.execute(select(Machine).where(Machine.id == job.machine_id))
    machine = machine_res.scalar_one_or_none()

    script = None
    if job.script_id is not None:
        script_res = await session.execute(select(Script).where(Script.id == job.script_id))
        script = script_res.scalar_one_or_none()

    flash = pop_flash(request, response)
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "user": user,
            "job": job,
            "machine": machine,
            "script": script,
            "flash": flash,
        },
    )


@router.get("/jobs/{job_uuid}/stream", include_in_schema=False)
async def job_log_stream(
    job_uuid: str,
    user: User = Depends(web_current_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    job = await _get_job_or_404(job_uuid, session)

    machine_res = await session.execute(select(Machine).where(Machine.id == job.machine_id))
    machine = machine_res.scalar_one_or_none()

    if machine is None:

        async def _empty() -> AsyncGenerator[str, None]:
            yield "event: end\ndata: done\n\n"

        return StreamingResponse(_empty(), media_type="text/event-stream")

    private_key = decrypt(machine.ssh_key_encrypted, get_settings().read_master_key())
    pool = get_ssh_pool()

    async def _events() -> AsyncGenerator[str, None]:
        try:
            async with AgentClient(machine, private_key, pool) as client:
                async for line in client.stream_logs(job_uuid):
                    yield f"data: {line}\n\n"
        except Exception:
            yield "data: [log not available]\n\n"
        yield "event: end\ndata: done\n\n"

    return StreamingResponse(_events(), media_type="text/event-stream")


@router.post("/jobs/{job_uuid}/kill", include_in_schema=False)
async def kill_job_web(
    job_uuid: str,
    user: User = Depends(web_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    job = await _get_job_or_404(job_uuid, session)

    machine_res = await session.execute(select(Machine).where(Machine.id == job.machine_id))
    machine = machine_res.scalar_one_or_none()
    if machine is None:
        return HTMLResponse('<span class="badge badge-error">Machine not found</span>')

    private_key = decrypt(machine.ssh_key_encrypted, get_settings().read_master_key())
    pool = get_ssh_pool()

    try:
        async with AgentClient(machine, private_key, pool) as client:
            await client.kill_job(job_uuid)
    except AgentClientError as exc:
        logger.warning("Kill failed for job %s: %s", job_uuid, exc)
        return HTMLResponse(f'<span class="badge badge-error">Kill failed: {exc}</span>')

    job.status = JobStatus.killed
    job.ended_at = datetime.utcnow()
    await session.commit()

    return HTMLResponse('<span class="badge badge-killed">Killed</span>')
