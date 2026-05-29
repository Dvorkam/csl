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

"""Machine detail, script run dialog, job history, job detail and live log web routes."""

import json
import logging
import uuid as uuid_mod
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.config import get_settings
from control_station_lite.server.core.agent_client import AgentClient, AgentClientError
from control_station_lite.server.core.crypto import decrypt
from control_station_lite.server.core.script_registry import (
    ScriptRegistryError,
    get_script_or_raise,
)
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
    show_all: bool = False,
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
        state = row.state if row else "absent"
        # Default view: hide scripts that have never been staged to this machine
        if not show_all and state == "absent":
            continue
        try:
            desc = parse_meta_yaml(s.meta_yaml).description if s.meta_yaml else ""
        except ScriptMetaError:
            desc = ""
        script_states.append(
            {
                "script": s,
                "description": desc,
                "state": state,
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
            "show_all": show_all,
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
                params[p.name] = (
                    int(raw) if raw else (int(p.default) if p.default is not None else 0)
                )  # noqa: E501
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
# Batch state sync: single SSH tunnel for all staged scripts (load-time refresh)
# ---------------------------------------------------------------------------


@router.get("/machines/{machine_id}/sync-states", include_in_schema=False)
async def sync_states(
    machine_id: int,
    request: Request,
    user: User = Depends(web_current_user),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX batch: open one SSH tunnel and refresh all staged scripts' approval states.

    Returns an OOB-swap response: each badge div is updated via hx-swap-oob so
    that machine_detail.html needs only a single on-load HTTP request instead of
    one per script.
    """
    machine = await _get_machine_or_404(machine_id, session)
    await _check_machine_access(user, machine_id, session)

    rows_res = await session.execute(
        select(ScriptTargetState, Script)
        .join(Script, ScriptTargetState.script_id == Script.id)
        .where(ScriptTargetState.machine_id == machine_id)
        .order_by(Script.name)
    )
    staged = list(rows_res.all())

    if not staged:
        return HTMLResponse("")

    private_key = decrypt(machine.ssh_key_encrypted, get_settings().read_master_key())
    pool = get_ssh_pool()
    now = datetime.utcnow()

    badge_tmpl = templates.env.get_template("partials/script_state_badge.html")

    def _oob_div(script_id: int, ctx: dict[str, object]) -> str:
        inner = badge_tmpl.render(ctx)
        return f'<div id="state-{script_id}" hx-swap-oob="innerHTML">{inner}</div>'

    oob_parts: list[str] = []

    try:
        async with AgentClient(machine, private_key, pool) as client:
            await client.ensure_agent_running()
            for sts, script in staged:
                try:
                    descriptor = await client.get_script_state(script.name)
                    agent_state = str(descriptor.state)
                    if agent_state == "approved" and descriptor.approved_md5 != script.md5:
                        new_state = "approved_stale"
                    else:
                        new_state = agent_state
                    sts.state = new_state
                    sts.approved_md5 = descriptor.approved_md5
                    sts.pending_md5 = descriptor.pending_md5
                    sts.last_refreshed_at = now
                    ctx: dict[str, object] = {
                        "script_id": script.id,
                        "machine_id": machine_id,
                        "script_name": script.name,
                        "script_md5": script.md5,
                        "state": new_state,
                        "approved_md5": descriptor.approved_md5,
                        "pending_md5": descriptor.pending_md5,
                        "agent_unreachable": False,
                    }
                except Exception:
                    ctx = {
                        "script_id": script.id,
                        "machine_id": machine_id,
                        "script_name": script.name,
                        "script_md5": script.md5,
                        "state": sts.state,
                        "approved_md5": sts.approved_md5,
                        "pending_md5": sts.pending_md5,
                        "agent_unreachable": True,
                    }
                oob_parts.append(_oob_div(script.id, ctx))
    except Exception:
        for sts, script in staged:
            ctx = {
                "script_id": script.id,
                "machine_id": machine_id,
                "script_name": script.name,
                "script_md5": script.md5,
                "state": sts.state,
                "approved_md5": sts.approved_md5,
                "pending_md5": sts.pending_md5,
                "agent_unreachable": True,
            }
            oob_parts.append(_oob_div(script.id, ctx))

    await session.commit()
    return HTMLResponse("\n".join(oob_parts))


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

    cached_res = await session.execute(
        select(ScriptTargetState).where(
            ScriptTargetState.machine_id == machine_id,
            ScriptTargetState.script_id == script.id,
        )
    )
    cached_row = cached_res.scalar_one_or_none()

    def _cached_badge_response() -> Response:
        return templates.TemplateResponse(
            request,
            "partials/script_state_badge.html",
            {
                "script_id": script.id,
                "machine_id": machine_id,
                "script_name": script_name,
                "script_md5": script.md5,
                "state": cached_row.state if cached_row else "absent",
                "approved_md5": cached_row.approved_md5 if cached_row else None,
                "pending_md5": cached_row.pending_md5 if cached_row else None,
                "agent_unreachable": True,
            },
        )

    try:
        async with AgentClient(machine, private_key, pool) as client:
            descriptor = await client.get_script_state(script.name)
    except Exception:
        # Agent unreachable or SSH failed — return cached state rather than 500
        return _cached_badge_response()

    # Compute effective state: approved_stale when the canonical script has been
    # edited since the agent last approved it.
    agent_state = str(descriptor.state)
    if agent_state == "approved" and descriptor.approved_md5 != script.md5:
        new_state = "approved_stale"
    else:
        new_state = agent_state

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
            "script_md5": script.md5,
            "state": new_state,
            "approved_md5": descriptor.approved_md5,
            "pending_md5": descriptor.pending_md5,
            "agent_unreachable": False,
        },
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
                "script_md5": script.md5,
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
            "script_md5": script.md5,
            "state": new_state,
            "approved_md5": descriptor.approved_md5,
            "pending_md5": descriptor.pending_md5,
        },
    )


# ---------------------------------------------------------------------------
# Job history list (8.13)
# ---------------------------------------------------------------------------

_JOB_PAGE_SIZE = 50


@router.get("/jobs", include_in_schema=False)
async def jobs_list(
    request: Request,
    response: Response,
    machine_id: str | None = None,
    script_name: str | None = None,
    status: str | None = None,
    page: int = 1,
    user: User = Depends(web_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    # HTML <select> submits empty string for "All" option; treat as no filter
    machine_id_int: int | None = int(machine_id) if machine_id else None
    script_name = script_name or None
    status = status or None
    page = max(1, page)
    offset = (page - 1) * _JOB_PAGE_SIZE

    # Machines available to this user (for filter dropdown + access control)
    if user.role == "admin":
        machines_res = await session.execute(select(Machine).order_by(Machine.name))
        accessible_machines = list(machines_res.scalars().all())
        accessible_ids: set[int] = {m.id for m in accessible_machines}
    else:
        bm_res = await session.execute(
            select(Machine)
            .join(UserMachine, Machine.id == UserMachine.machine_id)
            .where(UserMachine.user_id == user.id)
            .order_by(Machine.name)
        )
        accessible_machines = list(bm_res.scalars().all())
        accessible_ids = {m.id for m in accessible_machines}

    scripts_res = await session.execute(select(Script).order_by(Script.name))
    all_scripts = list(scripts_res.scalars().all())

    stmt = (
        select(Job, Machine, Script)
        .outerjoin(Machine, Job.machine_id == Machine.id)
        .outerjoin(Script, Job.script_id == Script.id)
        .where(Job.machine_id.in_(accessible_ids))
        .order_by(Job.started_at.desc())
    )
    if machine_id_int is not None:
        stmt = stmt.where(Job.machine_id == machine_id_int)
    if script_name:
        stmt = stmt.where(Script.name == script_name)
    if status:
        stmt = stmt.where(Job.status == status)

    count_stmt = select(Job.id).where(Job.machine_id.in_(accessible_ids))
    if machine_id_int is not None:
        count_stmt = count_stmt.where(Job.machine_id == machine_id_int)
    if script_name:
        count_stmt = count_stmt.outerjoin(Script, Job.script_id == Script.id).where(
            Script.name == script_name
        )
    if status:
        count_stmt = count_stmt.where(Job.status == status)

    total_res = await session.execute(count_stmt)
    total = len(total_res.all())

    rows_res = await session.execute(stmt.offset(offset).limit(_JOB_PAGE_SIZE))
    rows = rows_res.all()

    now_utc = datetime.now(UTC)

    def _duration(job: Job) -> str:
        if job.ended_at and job.started_at:
            secs = int((job.ended_at - job.started_at).total_seconds())
        elif job.started_at:
            started = job.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            secs = int((now_utc - started).total_seconds())
        else:
            return "—"
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"

    jobs_ctx = [
        {
            "job": job,
            "machine": machine,
            "script": script,
            "duration": _duration(job),
            "running": job.status in (JobStatus.running, JobStatus.pending),
        }
        for job, machine, script in rows
    ]

    flash = pop_flash(request, response)
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "user": user,
            "jobs": jobs_ctx,
            "machines": accessible_machines,
            "scripts": all_scripts,
            "statuses": [s.value for s in JobStatus],
            "filter_machine_id": machine_id_int,
            "filter_script_name": script_name,
            "filter_status": status,
            "page": page,
            "total": total,
            "page_size": _JOB_PAGE_SIZE,
            "has_prev": page > 1,
            "has_next": offset + _JOB_PAGE_SIZE < total,
            "flash": flash,
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
