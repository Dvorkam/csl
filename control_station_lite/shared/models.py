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

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ApprovalState(StrEnum):
    absent = "absent"
    pending = "pending"
    approved = "approved"
    update_pending = "update_pending"
    rejected = "rejected"


class JobStatus(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    killed = "killed"


# ---------------------------------------------------------------------------
# Agent API request/response models
# ---------------------------------------------------------------------------


class JobRequest(BaseModel):
    job_uuid: str
    script_name: str
    # Parameter values before env-var serialisation; bools/ints/floats preserved.
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    persistent: bool = False


class JobStatusResponse(BaseModel):
    job_uuid: str
    script_name: str
    status: JobStatus
    persistent: bool
    started_at: datetime
    ended_at: datetime | None = None
    exit_code: int | None = None


class LogChunk(BaseModel):
    job_uuid: str
    line: str
    stream: Literal["stdout", "stderr"]
    timestamp: datetime


class AgentHealth(BaseModel):
    version: str
    running_persistent_jobs: int
    idle_seconds: float


class ScriptDescriptor(BaseModel):
    name: str
    state: ApprovalState
    persistent: bool = False
    approved_md5: str | None = None
    pending_md5: str | None = None


class StageScriptRequest(BaseModel):
    content: str
    md5: str
    meta_yaml: str | None = None


class StageScriptResponse(BaseModel):
    name: str
    state: ApprovalState
