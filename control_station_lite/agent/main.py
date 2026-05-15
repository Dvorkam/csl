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

import importlib.metadata
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from control_station_lite.agent.approvals import ApprovalsManager
from control_station_lite.agent.config import AgentConfig, load_config
from control_station_lite.agent.log_stream import make_sse_response
from control_station_lite.agent.process_manager import JobNotFoundError, ProcessManager
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

_NOT_IMPLEMENTED: dict[int | str, dict[str, object]] = {501: {"description": "Not yet implemented"}}


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    cfg: AgentConfig = load_config()
    paths = cfg.agent.to_csl_paths()
    paths.ensure_dirs()

    approvals = ApprovalsManager(paths, auto_approve_list=cfg.approval_policy.auto_approve)
    process_mgr = ProcessManager(paths, approvals)

    application.state.config = cfg
    application.state.approvals = approvals
    application.state.process_manager = process_mgr

    logger.info(
        "agent starting on %s:%d",
        _AGENT_HOST,
        cfg.agent.listen_port,
    )
    yield


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


@app.get("/healthz", response_model=AgentHealth)
async def healthz(request: Request) -> AgentHealth:
    """Report agent liveness and basic runtime state."""
    pm: ProcessManager = request.app.state.process_manager
    return AgentHealth(
        version=_version(),
        running_persistent_jobs=pm.running_count(),
        idle_seconds=0.0,
    )


@app.get(
    "/scripts/{name}/state",
    response_model=ScriptDescriptor,
    responses=_NOT_IMPLEMENTED,
)
async def get_script_state(name: str) -> ScriptDescriptor:
    """Return the current approval state of a script on this agent."""
    raise HTTPException(status_code=501, detail="approvals not yet implemented")


@app.post(
    "/scripts/{name}/stage",
    response_model=StageScriptResponse,
    responses=_NOT_IMPLEMENTED,
)
async def stage_script(name: str, body: StageScriptRequest) -> StageScriptResponse:
    """Stage a script for target-owner review."""
    raise HTTPException(status_code=501, detail="approvals not yet implemented")


@app.post(
    "/jobs",
    response_model=JobStatusResponse,
    status_code=202,
    responses=_NOT_IMPLEMENTED,
)
async def submit_job(body: JobRequest) -> JobStatusResponse:
    """Submit a job for execution (script must already be approved)."""
    raise HTTPException(status_code=501, detail="job execution not yet implemented")


@app.get("/jobs/{job_uuid}/stream", response_class=StreamingResponse)
async def stream_job_logs(
    job_uuid: str,
    request: Request,
    tail: int = 1000,
) -> StreamingResponse:
    """Stream stdout/stderr from a persistent job as SSE events.

    Each event carries one log line: ``data: <line>\\n\\n``.
    A final ``event: done`` signals the end of the stream.

    ``tail`` controls how many existing log lines are replayed on connect
    (default 1 000).  Pass ``tail=-1`` to replay the entire log, or
    ``tail=0`` to receive only live output.
    """
    pm: ProcessManager = request.app.state.process_manager
    try:
        log_path = pm.get_log_path(job_uuid)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job {job_uuid!r} not found") from exc

    if not log_path.exists():
        raise HTTPException(status_code=404, detail=f"log file for job {job_uuid!r} not found")

    def _is_done() -> bool:
        try:
            return pm.get_status(job_uuid).status != JobStatus.running
        except JobNotFoundError:
            return True

    return make_sse_response(log_path, is_done=_is_done, tail_lines=tail)


def main() -> None:
    """Entry point for the csl-agent server process."""
    import uvicorn

    cfg: AgentConfig = load_config()
    uvicorn.run(
        "control_station_lite.agent.main:app",
        host=_AGENT_HOST,
        port=cfg.agent.listen_port,
        log_level="info",
    )
