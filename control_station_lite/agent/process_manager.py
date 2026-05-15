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

import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from control_station_lite.agent.approvals import ApprovalsManager
from control_station_lite.agent.paths import CslPaths
from control_station_lite.agent.script_runner import (
    ScriptNotApprovedError,
    ScriptNotFoundError,
    build_command,
    build_env,
    find_script,
)
from control_station_lite.shared.models import ApprovalState, JobStatus, JobStatusResponse
from control_station_lite.shared.platform_info import IS_WINDOWS

__all__ = [
    "JobNotFoundError",
    "ProcessManager",
    "ScriptNotApprovedError",
    "ScriptNotFoundError",
]

logger = logging.getLogger(__name__)

# Seconds to wait for graceful SIGTERM before escalating to SIGKILL on POSIX.
_SIGTERM_GRACE_SECONDS = 5


class JobNotFoundError(KeyError):
    """Raised when a job_uuid is not tracked by this ProcessManager."""


@dataclass
class _ProcessRecord:
    job_uuid: str
    script_name: str
    # subprocess.Popen that launched the persistent job.
    # The OS owns the actual process — this handle is only for status polling
    # and signal delivery.  The agent does NOT run the job; the OS does.
    process: subprocess.Popen  # type: ignore[type-arg]
    log_path: Path
    started_at: datetime
    status: JobStatus = field(default=JobStatus.running)
    ended_at: datetime | None = field(default=None)
    exit_code: int | None = field(default=None)


class ProcessManager:
    """Start, track, and kill persistent processes for approved scripts.

    Each job runs as an independent OS process started via ``subprocess.Popen``.
    This class holds the ``Popen`` handle so it can poll exit status and send
    signals; it does not schedule, loop, or manage the process in any other way.
    """

    def __init__(
        self,
        paths: CslPaths,
        approvals: ApprovalsManager,
    ) -> None:
        self._paths = paths
        self._approvals = approvals
        self._lock = threading.Lock()
        self._jobs: dict[str, _ProcessRecord] = {}

    def start(
        self,
        name: str,
        params: dict[str, str | int | float | bool],
        job_uuid: str,
    ) -> JobStatusResponse:
        """Start a persistent OS process for the approved script *name*.

        Raises:
            ScriptNotApprovedError: script is not in ``approved`` state.
            ScriptNotFoundError: no script file found for *name*.
        """
        descriptor = self._approvals.get_state(name)
        if descriptor.state != ApprovalState.approved:
            raise ScriptNotApprovedError(
                f"refusing to start '{name}': approval state is"
                f" '{descriptor.state}' (must be approved)"
            )

        script_path = find_script(name, self._paths.scripts_dir)
        command = build_command(script_path)
        env = build_env(params)

        self._paths.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._paths.logs_dir / f"{job_uuid}.log"

        logger.info("starting persistent process '%s' job=%s", name, job_uuid)

        with open(log_path, "w") as log_file:
            proc = _popen(command, env, log_file)

        record = _ProcessRecord(
            job_uuid=job_uuid,
            script_name=name,
            process=proc,
            log_path=log_path,
            started_at=datetime.now(UTC),
        )

        with self._lock:
            self._jobs[job_uuid] = record

        return _to_response(record)

    def kill(self, job_uuid: str) -> JobStatusResponse:
        """Terminate the process group for *job_uuid*.

        On POSIX: sends SIGTERM, waits up to ``_SIGTERM_GRACE_SECONDS``,
        then escalates to SIGKILL if the process is still alive.
        On Windows: ``taskkill /F /T`` is unconditional — no two-step needed.

        If the process has already exited, returns its final status unchanged.

        Raises:
            JobNotFoundError: if *job_uuid* is not tracked.
        """
        record = self._get_record(job_uuid)
        _poll_record(record)
        if record.status != JobStatus.running:
            return _to_response(record)

        logger.info("killing job=%s pid=%d", job_uuid, record.process.pid)
        _kill_process(record.process)

        record.status = JobStatus.killed
        record.ended_at = datetime.now(UTC)
        record.exit_code = record.process.returncode
        return _to_response(record)

    def get_status(self, job_uuid: str) -> JobStatusResponse:
        """Return the current status of *job_uuid*.

        Raises:
            JobNotFoundError: if *job_uuid* is not tracked.
        """
        record = self._get_record(job_uuid)
        _poll_record(record)
        return _to_response(record)

    def list_jobs(self) -> list[JobStatusResponse]:
        """Return current status for all tracked jobs."""
        with self._lock:
            records = list(self._jobs.values())
        for record in records:
            _poll_record(record)
        return [_to_response(r) for r in records]

    def running_count(self) -> int:
        """Return the number of currently running persistent jobs."""
        with self._lock:
            records = list(self._jobs.values())
        for record in records:
            _poll_record(record)
        return sum(1 for r in records if r.status == JobStatus.running)

    def get_log_path(self, job_uuid: str) -> Path:
        """Return the log file path for *job_uuid*.

        Raises:
            JobNotFoundError: if *job_uuid* is not tracked.
        """
        return self._get_record(job_uuid).log_path

    def _get_record(self, job_uuid: str) -> _ProcessRecord:
        with self._lock:
            record = self._jobs.get(job_uuid)
        if record is None:
            raise JobNotFoundError(job_uuid)
        return record


# ---------------------------------------------------------------------------
# Platform helpers (module-level so tests can patch them)
# ---------------------------------------------------------------------------


def _popen(command: list[str], env: dict[str, str], log_file: object) -> subprocess.Popen:  # type: ignore[type-arg]
    """Start *command* with stdout/stderr redirected to *log_file*.

    On POSIX, a new session is created so the whole process group can be
    killed as a unit.  On Windows, ``CREATE_NEW_PROCESS_GROUP`` (0x200)
    achieves the same effect and is required before sending Ctrl+Break.
    """
    kwargs: dict[str, Any] = {
        "env": env,
        "stdout": log_file,
        "stderr": log_file,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def _kill_process(proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
    """Kill *proc* and its entire process group/tree, blocking until dead.

    POSIX: SIGTERM → wait ``_SIGTERM_GRACE_SECONDS`` → SIGKILL if still alive.
    Windows: ``taskkill /F /T`` is unconditional (TerminateProcess); one step.
    """
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            check=False,
            capture_output=True,
        )
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("process pid=%d did not exit after taskkill", proc.pid)
    else:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            proc.wait()
            return

        try:
            proc.wait(timeout=_SIGTERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning(
                "process pid=%d ignored SIGTERM after %ds, escalating to SIGKILL",
                proc.pid,
                _SIGTERM_GRACE_SECONDS,
            )
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


def _poll_record(record: _ProcessRecord) -> None:
    if record.status == JobStatus.running:
        ret = record.process.poll()
        if ret is not None:
            record.exit_code = ret
            record.ended_at = datetime.now(UTC)
            record.status = JobStatus.completed if ret == 0 else JobStatus.failed


def _to_response(record: _ProcessRecord) -> JobStatusResponse:
    return JobStatusResponse(
        job_uuid=record.job_uuid,
        script_name=record.script_name,
        status=record.status,
        persistent=True,
        started_at=record.started_at,
        ended_at=record.ended_at,
        exit_code=record.exit_code,
    )
