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

from fastapi import FastAPI, HTTPException

from control_station_lite.agent.config import AgentConfig, load_config
from control_station_lite.shared.models import (
    AgentHealth,
    JobRequest,
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
    application.state.config = load_config()
    logger.info(
        "agent starting on %s:%d",
        _AGENT_HOST,
        application.state.config.agent.listen_port,
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
async def healthz() -> AgentHealth:
    """Report agent liveness and basic runtime state."""
    return AgentHealth(
        version=_version(),
        running_persistent_jobs=0,
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
