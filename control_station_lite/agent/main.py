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

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, HTTPException, Path, Request, Response
from fastapi.responses import StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from control_station_lite.agent.approvals import ApprovalError, ApprovalsManager
from control_station_lite.agent.config import AgentConfig, load_config
from control_station_lite.agent.lifecycle import IdleTracker
from control_station_lite.agent.log_stream import make_sse_response
from control_station_lite.agent.process_manager import JobNotFoundError, ProcessManager
from control_station_lite.agent.script_runner import (
    ScriptNotApprovedError,
    ScriptNotFoundError,
    run_script,
)
from control_station_lite.shared.models import (
    AgentHealth,
    JobRequest,
    JobStatus,
    JobStatusResponse,
    ScriptDescriptor,
    StageScriptRequest,
    StageScriptResponse,
)

__all__ = ["app", "main"]

logger = logging.getLogger(__name__)

# The agent must only bind to localhost — it communicates exclusively through
# SSH tunnels and must never be reachable from the network directly.
_AGENT_HOST = "127.0.0.1"


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    cfg: AgentConfig = load_config()
    paths = cfg.agent.to_csl_paths()
    paths.ensure_dirs()

    approvals = ApprovalsManager(paths, auto_approve_list=cfg.approval_policy.auto_approve)
    process_mgr = ProcessManager(paths, approvals)
    tracker = IdleTracker(timeout_seconds=cfg.agent.idle_timeout_seconds)

    process_mgr.restore_state()

    application.state.config = cfg
    application.state.approvals = approvals
    application.state.process_manager = process_mgr
    application.state.tracker = tracker

    logger.info(
        "agent starting on %s:%d",
        _AGENT_HOST,
        cfg.agent.listen_port,
    )
    shutdown_task = asyncio.create_task(
        tracker.run_loop(
            process_mgr,
            check_interval=cfg.agent.lifecycle_check_interval_seconds,
        )
    )
    try:
        yield
    finally:
        shutdown_task.cancel()
        try:
            await shutdown_task
        except asyncio.CancelledError:
            pass


def _version() -> str:
    try:
        return importlib.metadata.version("control-station-lite")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0-dev"


app = FastAPI(
    title="CSL Agent",
    version=_version(),
    lifespan=_lifespan,
)


class _ActivityMiddleware(BaseHTTPMiddleware):
    """Reset the idle clock on every incoming HTTP request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        tracker: IdleTracker | None = getattr(request.app.state, "tracker", None)
        if tracker is not None:
            tracker.record_activity()
        return await call_next(request)


app.add_middleware(_ActivityMiddleware)


@app.get("/healthz", response_model=AgentHealth)
async def healthz(request: Request) -> AgentHealth:
    """Report agent liveness and basic runtime state."""
    pm: ProcessManager = request.app.state.process_manager
    tracker: IdleTracker = request.app.state.tracker
    return AgentHealth(
        version=_version(),
        running_persistent_jobs=pm.running_count(),
        idle_seconds=tracker.idle_seconds,
    )


_SAFE_NAME = Path(pattern=r"^[A-Za-z0-9_\-.]+$")


@app.get("/scripts/{name}/state", response_model=ScriptDescriptor)
async def get_script_state(name: Annotated[str, _SAFE_NAME], request: Request) -> ScriptDescriptor:
    """Return the current approval state of a script on this agent."""
    approvals: ApprovalsManager = request.app.state.approvals
    return approvals.get_state(name)


@app.post("/scripts/{name}/stage", response_model=StageScriptResponse)
async def stage_script(
    name: Annotated[str, _SAFE_NAME], body: StageScriptRequest, request: Request
) -> StageScriptResponse:
    """Stage a script for target-owner review."""
    approvals: ApprovalsManager = request.app.state.approvals
    try:
        new_state = approvals.stage(name, body.content, body.md5, body.meta_yaml)
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StageScriptResponse(name=name, state=new_state)


@app.post("/jobs", response_model=JobStatusResponse, status_code=202)
async def submit_job(body: JobRequest, request: Request) -> JobStatusResponse:
    """Submit a job for execution (script must already be approved)."""
    approvals: ApprovalsManager = request.app.state.approvals
    pm: ProcessManager = request.app.state.process_manager
    cfg: AgentConfig = request.app.state.config

    if body.persistent:
        try:
            return pm.start(body.script_name, body.params, body.job_uuid)
        except ScriptNotApprovedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ScriptNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    paths = cfg.agent.to_csl_paths()
    started_at = datetime.now(UTC)
    try:
        result = run_script(body.script_name, body.params, approvals, cfg.agent.scripts_dir)
    except ScriptNotApprovedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ScriptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = paths.logs_dir / f"{body.job_uuid}.log"
    with log_path.open("w", encoding="utf-8") as lf:
        if result.stdout:
            lf.write(result.stdout)
        if result.stderr:
            lf.write(result.stderr)
        if result.timed_out:
            lf.write("\n[timed out]\n")

    return JobStatusResponse(
        job_uuid=body.job_uuid,
        script_name=body.script_name,
        status=JobStatus.completed if result.exit_code == 0 else JobStatus.failed,
        persistent=False,
        started_at=started_at,
        ended_at=datetime.now(UTC),
        exit_code=result.exit_code,
    )


@app.get("/jobs/{job_uuid}", response_model=JobStatusResponse)
async def get_job_status(job_uuid: str, request: Request) -> JobStatusResponse:
    """Return the current status of a persistent job."""
    pm: ProcessManager = request.app.state.process_manager
    try:
        return pm.get_status(job_uuid)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job {job_uuid!r} not found") from exc


@app.delete("/jobs/{job_uuid}", response_model=JobStatusResponse)
async def kill_job(job_uuid: str, request: Request) -> JobStatusResponse:
    """Kill a running persistent job."""
    pm: ProcessManager = request.app.state.process_manager
    try:
        return pm.kill(job_uuid)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job {job_uuid!r} not found") from exc


@app.get("/jobs/{job_uuid}/stream", response_class=StreamingResponse)
async def stream_job_logs(
    job_uuid: str,
    request: Request,
    tail: int | None = None,
) -> StreamingResponse:
    """Stream stdout/stderr from a persistent job as SSE events.

    Each event carries one log line: ``data: <line>\\n\\n``.
    A final ``event: done`` signals the end of the stream.

    ``tail`` controls how many existing log lines are replayed on connect
    (default 1 000).  Pass ``tail=-1`` to replay the entire log, or
    ``tail=0`` to receive only live output.
    """
    cfg: AgentConfig = request.app.state.config
    effective_tail = tail if tail is not None else cfg.agent.log_tail_lines
    pm: ProcessManager = request.app.state.process_manager
    try:
        log_path = pm.get_log_path(job_uuid)
    except JobNotFoundError:
        log_path = cfg.agent.to_csl_paths().logs_dir / f"{job_uuid}.log"
        if not log_path.exists():
            raise HTTPException(status_code=404, detail=f"job {job_uuid!r} not found") from None

    if not log_path.exists():
        raise HTTPException(status_code=404, detail=f"log file for job {job_uuid!r} not found")

    def _is_done() -> bool:
        try:
            return pm.get_status(job_uuid).status != JobStatus.running
        except JobNotFoundError:
            return True

    return make_sse_response(log_path, is_done=_is_done, tail_lines=effective_tail)


def main() -> None:
    """Entry point for the csl-agent server process."""
    import logging
    import sys

    import uvicorn

    # Uvicorn's default log formatter calls sys.stdout.isatty() to detect
    # colour support.  Under pythonw.exe (Task Scheduler, no console) both
    # sys.stdout and sys.stderr are None, which causes an AttributeError
    # before the server even starts.  We configure logging ourselves and
    # pass log_config=None so uvicorn skips its dictConfig entirely.
    if sys.stderr is not None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        )
    else:
        # Headless — discard global agent logs for now; per-job output goes
        # to individual log files under ~/.csl/logs/.
        logging.basicConfig(handlers=[logging.NullHandler()])

    cfg: AgentConfig = load_config()
    uvicorn.run(
        "control_station_lite.agent.main:app",
        host=_AGENT_HOST,
        port=cfg.agent.listen_port,
        log_level="info",
        log_config=None,
    )
