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
import time
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
from control_station_lite.agent.state import JobEntry, load_running_state, save_running_state
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


class _ReattachedProcess:
    """PID-only handle for a process that survived an agent restart.

    We cannot obtain a ``subprocess.Popen`` from an existing PID, but we can
    check liveness and send signals using OS APIs.  This class provides the
    subset of the ``Popen`` interface used by ``ProcessManager`` so that
    reattached jobs can be polled and killed through the same code paths as
    freshly-started jobs.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if not _pid_alive(self.pid):
            self.returncode = -1
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Poll until the process is gone or *timeout* seconds elapse."""
        if self.returncode is not None:
            return self.returncode
        deadline = time.monotonic() + timeout if timeout is not None else None
        while _pid_alive(self.pid):
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd=[], timeout=timeout or 0.0)
            time.sleep(0.05)
        self.returncode = -1
        return self.returncode


@dataclass
class _ProcessRecord:
    job_uuid: str
    script_name: str
    # Either a subprocess.Popen (freshly started) or a _ReattachedProcess
    # (recovered from running.json on startup).  The OS owns the actual
    # process — this handle is only for status polling and signal delivery.
    process: subprocess.Popen | _ReattachedProcess  # type: ignore[type-arg]
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

        self.save_state()
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
        self.save_state()
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

    def save_state(self) -> None:
        """Write all currently-running jobs to ``running.json``.

        Only jobs in ``running`` status are persisted; completed, failed, and
        killed jobs are excluded so the file always reflects live processes.
        """
        with self._lock:
            entries = {
                uuid: JobEntry(
                    script_name=r.script_name,
                    pid=r.process.pid,
                    log_path=r.log_path,
                    started_at=r.started_at,
                )
                for uuid, r in self._jobs.items()
                if r.status == JobStatus.running
            }
        save_running_state(self._paths.state_path, entries)

    def restore_state(self) -> None:
        """Read ``running.json`` and reattach any processes whose PIDs are alive.

        For each entry in the saved state:
        - If the PID is still alive: a ``_ReattachedProcess`` is created and
          the job is added to the tracking dict with ``running`` status.
        - If the PID is gone: the entry is silently discarded (the job ended
          while the agent was down).

        This method is called once during agent startup, before any requests
        are accepted, so no locking is required.
        """
        entries = load_running_state(self._paths.state_path)
        if not entries:
            return

        recovered = 0
        for job_uuid, entry in entries.items():
            if _pid_alive(entry.pid):
                record = _ProcessRecord(
                    job_uuid=job_uuid,
                    script_name=entry.script_name,
                    process=_ReattachedProcess(entry.pid),
                    log_path=entry.log_path,
                    started_at=entry.started_at,
                )
                self._jobs[job_uuid] = record
                recovered += 1
                logger.info(
                    "reattached job=%s script=%s pid=%d",
                    job_uuid,
                    entry.script_name,
                    entry.pid,
                )
            else:
                logger.info(
                    "job=%s script=%s pid=%d exited while agent was down",
                    job_uuid,
                    entry.script_name,
                    entry.pid,
                )

        logger.info(
            "state restore: %d reattached, %d terminated while down",
            recovered,
            len(entries) - recovered,
        )

    def _get_record(self, job_uuid: str) -> _ProcessRecord:
        with self._lock:
            record = self._jobs.get(job_uuid)
        if record is None:
            raise JobNotFoundError(job_uuid)
        return record


# ---------------------------------------------------------------------------
# Platform helpers (module-level so tests can patch them)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Return ``True`` if a process with *pid* is currently alive (not zombie).

    On POSIX, ``os.kill(pid, 0)`` checks existence without delivering a signal.
    On Windows, ``os.kill(pid, 0)`` would call ``TerminateProcess`` — which
    kills the process — so we use ``OpenProcess`` with a read-only access mask
    and ``GetExitCodeProcess`` instead.

    Zombie processes on Linux pass the ``os.kill(pid, 0)`` check (they remain
    in the process table until reaped) but are functionally dead.  We detect
    them via ``/proc/{pid}/status`` and report them as not alive.  On platforms
    without ``/proc`` (macOS, BSD), zombies are rare in real deployments because
    init/systemd reaps them immediately, so we accept the minor imprecision.
    """
    if IS_WINDOWS:
        import ctypes
        import ctypes.wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            code = ctypes.wintypes.DWORD()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))  # type: ignore[attr-defined]
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we lack permission to signal it (different UID).
            return True

        # Exclude zombie processes — they pass os.kill(pid, 0) but are dead.
        try:
            status_text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
            for line in status_text.splitlines():
                if line.startswith("State:"):
                    return "\tZ" not in line
        except OSError:
            pass  # /proc not available (macOS/BSD) — accept os.kill result

        return True


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


def _kill_process(proc: subprocess.Popen | _ReattachedProcess) -> None:  # type: ignore[type-arg]
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
