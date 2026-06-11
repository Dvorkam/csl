"""Tests for agent/process_manager.py.

Structure:
  TestApprovalEnforcement     — cross-platform: approval gate before any execution
  TestJobNotFound             — cross-platform: unknown job_uuid raises JobNotFoundError
  TestSaveAndRestoreState     — cross-platform: running.json round-trip and PID recovery
  TestStart                   — linux_only: process launches, log file created
  TestStatus                  — linux_only: status transitions (running→completed/failed/killed)
  TestKill                    — linux_only: kill cleans up child processes; SIGKILL fallback
  TestListJobs                — linux_only: list_jobs reflects all tracked processes
  TestRunningCount            — linux_only: running_count tracks live processes
  TestWindowsStart            — windows_only: process launches on Windows via PowerShell
  TestWindowsStatus           — windows_only: status transitions on Windows
  TestWindowsKill             — windows_only: taskkill terminates the process tree
  TestWindowsListJobs         — windows_only: list_jobs reflects all tracked processes
  TestWindowsRunningCount     — windows_only: running_count tracks live processes
  TestRestoreRealProcess      — linux_only: full recovery integration (start → restart → restore)
  TestWindowsRestoreRealProcess — windows_only: same integration via PowerShell
"""

import hashlib
import time
import uuid
from pathlib import Path

import pytest

from control_station_lite.agent.approvals import ApprovalsManager
from control_station_lite.agent.paths import CslPaths
from control_station_lite.agent.process_manager import (
    JobNotFoundError,
    ProcessManager,
    ScriptNotApprovedError,
    _pid_alive,
    _ReattachedProcess,
)
from control_station_lite.agent.script_runner import ScriptIntegrityError
from control_station_lite.shared.models import JobStatus
from control_station_lite.shared.platform_info import IS_WINDOWS

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def paths(tmp_path: Path) -> CslPaths:
    return CslPaths(
        scripts_dir=tmp_path / "scripts",
        pending_dir=tmp_path / "scripts.pending",
        logs_dir=tmp_path / "logs",
        approvals_path=tmp_path / "agent" / "approvals.json",
        state_path=tmp_path / "agent" / "running.json",
    )


@pytest.fixture
def approvals(paths: CslPaths) -> ApprovalsManager:
    return ApprovalsManager(paths)


@pytest.fixture
def manager(paths: CslPaths, approvals: ApprovalsManager) -> ProcessManager:
    return ProcessManager(paths, approvals)


def _approve_script(
    mgr: ApprovalsManager,
    paths: CslPaths,
    name: str,
    content: str,
    extension: str = ".sh",
) -> None:
    """Stage, approve, and write script *name* with platform extension."""
    md5 = hashlib.md5(content.encode()).hexdigest()
    mgr.stage(name, content, md5)
    mgr.approve(name)
    approved = paths.scripts_dir / name
    if approved.exists() and extension:
        approved.rename(paths.scripts_dir / f"{name}{extension}")


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _write_string_param_meta(paths: CslPaths, name: str, param: str) -> None:
    """Write a minimal meta.yaml declaring a single required string param."""
    (paths.scripts_dir / f"{name}.meta.yaml").write_text(
        f"params:\n  - name: {param}\n    type: string\n    required: true\n"
    )


# ---------------------------------------------------------------------------
# TestApprovalEnforcement — cross-platform
# ---------------------------------------------------------------------------


class TestApprovalEnforcement:
    def test_absent_raises(self, manager: ProcessManager) -> None:
        with pytest.raises(ScriptNotApprovedError, match="absent"):
            manager.start("sleep_machine", {}, _new_uuid())

    def test_pending_raises(self, approvals: ApprovalsManager, manager: ProcessManager) -> None:
        md5 = hashlib.md5(b"content").hexdigest()
        approvals.stage("script", "content", md5)
        with pytest.raises(ScriptNotApprovedError, match="pending"):
            manager.start("script", {}, _new_uuid())

    def test_rejected_raises(self, approvals: ApprovalsManager, manager: ProcessManager) -> None:
        md5 = hashlib.md5(b"content").hexdigest()
        approvals.stage("script", "content", md5)
        approvals.reject("script")
        with pytest.raises(ScriptNotApprovedError, match="rejected"):
            manager.start("script", {}, _new_uuid())

    def test_update_pending_raises(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "script", "v1")
        md5_v2 = hashlib.md5(b"v2").hexdigest()
        approvals.stage("script", "v2", md5_v2)
        with pytest.raises(ScriptNotApprovedError, match="update_pending"):
            manager.start("script", {}, _new_uuid())

    def test_tampered_file_raises_integrity_error(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        # Use a platform-appropriate extension so find_script can locate the
        # approved file (Windows looks for .ps1/.bat/.cmd, not .sh).
        ext = ".ps1" if IS_WINDOWS else ".sh"
        _approve_script(approvals, paths, "script", "#!/bin/bash\nsleep 1\n", ext)
        # Tamper the approved file on disk, outside the approval flow.
        (paths.scripts_dir / f"script{ext}").write_text("#!/bin/bash\nmalicious\n")
        with pytest.raises(ScriptIntegrityError):
            manager.start("script", {}, _new_uuid())


# ---------------------------------------------------------------------------
# TestJobNotFound — cross-platform
# ---------------------------------------------------------------------------


class TestJobNotFound:
    def test_get_status_unknown_uuid(self, manager: ProcessManager) -> None:
        with pytest.raises(JobNotFoundError):
            manager.get_status("nonexistent-uuid")

    def test_kill_unknown_uuid(self, manager: ProcessManager) -> None:
        with pytest.raises(JobNotFoundError):
            manager.kill("nonexistent-uuid")


# ---------------------------------------------------------------------------
# TestSaveAndRestoreState — cross-platform
# ---------------------------------------------------------------------------


class TestSaveAndRestoreState:
    def test_save_state_creates_running_json(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        manager.save_state()
        assert paths.state_path.exists()

    def test_save_state_empty_when_no_running_jobs(
        self, paths: CslPaths, manager: ProcessManager
    ) -> None:
        manager.save_state()
        from control_station_lite.agent.state import load_running_state

        assert load_running_state(paths.state_path) == {}

    def test_restore_state_no_file_is_noop(self, paths: CslPaths, manager: ProcessManager) -> None:
        manager.restore_state()
        assert manager.list_jobs() == []

    def test_restore_state_dead_pid_not_reattached(
        self, paths: CslPaths, manager: ProcessManager
    ) -> None:
        from datetime import UTC, datetime

        from control_station_lite.agent.state import JobEntry, save_running_state

        # Use PID 1 which exists on Linux/macOS but is not ours to track,
        # and a definitely-dead PID (99999999) for the dead case.
        dead_pid = 99999999
        save_running_state(
            paths.state_path,
            {
                "dead-uuid": JobEntry(
                    script_name="s",
                    pid=dead_pid,
                    log_path=paths.logs_dir / "dead-uuid.log",
                    started_at=datetime.now(UTC),
                )
            },
        )
        manager.restore_state()
        assert manager.list_jobs() == []

    def test_restore_state_alive_pid_is_reattached(
        self, paths: CslPaths, manager: ProcessManager
    ) -> None:
        """A process whose PID is still alive must appear in list_jobs as running."""
        import os
        from datetime import UTC, datetime

        from control_station_lite.agent.state import JobEntry, save_running_state

        my_pid = os.getpid()  # current process is definitely alive
        job_uuid = "alive-uuid"
        save_running_state(
            paths.state_path,
            {
                job_uuid: JobEntry(
                    script_name="alive_script",
                    pid=my_pid,
                    log_path=paths.logs_dir / f"{job_uuid}.log",
                    started_at=datetime.now(UTC),
                )
            },
        )
        manager.restore_state()
        jobs = manager.list_jobs()
        assert any(j.job_uuid == job_uuid for j in jobs)
        found = next(j for j in jobs if j.job_uuid == job_uuid)
        assert found.status == JobStatus.running
        assert found.script_name == "alive_script"

    def test_pid_alive_returns_true_for_own_pid(self) -> None:
        import os

        assert _pid_alive(os.getpid()) is True

    def test_pid_alive_returns_false_for_nonexistent_pid(self) -> None:
        assert _pid_alive(99999999) is False

    def test_reattached_process_poll_alive(self) -> None:
        import os

        rp = _ReattachedProcess(os.getpid())
        assert rp.poll() is None  # current process is alive → running

    def test_reattached_process_poll_dead(self) -> None:
        rp = _ReattachedProcess(99999999)
        assert rp.poll() == -1

    def test_reattached_process_returncode_cached_after_dead(self) -> None:
        rp = _ReattachedProcess(99999999)
        rp.poll()
        assert rp.returncode == -1
        # second call must not change anything
        assert rp.poll() == -1

    @pytest.mark.linux_only
    def test_pid_alive_returns_false_for_zombie(self) -> None:
        """Zombie processes (exited, not yet reaped) must not be reported as alive."""
        import os as _os

        child_pid = _os.fork()
        if child_pid == 0:
            _os._exit(0)  # child exits immediately — becomes zombie in parent

        time.sleep(0.05)  # allow child to exit and enter zombie state
        try:
            result = _pid_alive(child_pid)
        finally:
            _os.waitpid(child_pid, 0)  # reap zombie regardless of test outcome
        assert result is False


# ---------------------------------------------------------------------------
# TestStart — linux_only
# ---------------------------------------------------------------------------


@pytest.mark.linux_only
class TestStart:
    def test_log_file_created(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "noop", "#!/bin/bash\nsleep 0.1\n")
        job_uuid = _new_uuid()
        manager.start("noop", {}, job_uuid)
        assert (paths.logs_dir / f"{job_uuid}.log").exists()

    def test_returns_running_status(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "noop", "#!/bin/bash\nsleep 0.5\n")
        job_uuid = _new_uuid()
        resp = manager.start("noop", {}, job_uuid)
        assert resp.status == JobStatus.running
        assert resp.persistent is True
        assert resp.job_uuid == job_uuid
        assert resp.script_name == "noop"

    def test_params_passed_via_env(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        script = "#!/bin/bash\necho $CSL_PARAM_MESSAGE\n"
        _approve_script(approvals, paths, "echo_param", script)
        _write_string_param_meta(paths, "echo_param", "message")
        job_uuid = _new_uuid()
        manager.start("echo_param", {"message": "hello"}, job_uuid)
        time.sleep(0.3)
        log_path = paths.logs_dir / f"{job_uuid}.log"
        assert "hello" in log_path.read_text()

    def test_start_persists_to_running_json(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        from control_station_lite.agent.state import load_running_state

        _approve_script(approvals, paths, "sleeper", "#!/bin/bash\nsleep 60\n")
        job_uuid = _new_uuid()
        manager.start("sleeper", {}, job_uuid)
        entries = load_running_state(paths.state_path)
        assert job_uuid in entries
        assert entries[job_uuid].script_name == "sleeper"


# ---------------------------------------------------------------------------
# TestStatus — linux_only
# ---------------------------------------------------------------------------


@pytest.mark.linux_only
class TestStatus:
    def test_running_then_completed(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "quick", "#!/bin/bash\nsleep 0.1\n")
        job_uuid = _new_uuid()
        manager.start("quick", {}, job_uuid)
        time.sleep(0.5)
        resp = manager.get_status(job_uuid)
        assert resp.status == JobStatus.completed
        assert resp.exit_code == 0
        assert resp.ended_at is not None

    def test_failed_exit_code(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "fails", "#!/bin/bash\nexit 42\n")
        job_uuid = _new_uuid()
        manager.start("fails", {}, job_uuid)
        time.sleep(0.3)
        resp = manager.get_status(job_uuid)
        assert resp.status == JobStatus.failed
        assert resp.exit_code == 42


# ---------------------------------------------------------------------------
# TestKill — linux_only
# ---------------------------------------------------------------------------


@pytest.mark.linux_only
class TestKill:
    def test_kill_running_process(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "sleeper", "#!/bin/bash\nsleep 60\n")
        job_uuid = _new_uuid()
        manager.start("sleeper", {}, job_uuid)
        time.sleep(0.1)
        resp = manager.kill(job_uuid)
        assert resp.status == JobStatus.killed
        assert resp.ended_at is not None

    def test_kill_already_finished_returns_final_status(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "quick", "#!/bin/bash\nsleep 0.05\n")
        job_uuid = _new_uuid()
        manager.start("quick", {}, job_uuid)
        time.sleep(0.5)
        resp = manager.kill(job_uuid)
        assert resp.status == JobStatus.completed

    def test_kill_cleans_up_child_processes(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        """Kill must terminate child processes, not just the shell."""
        script = "#!/bin/bash\n(sleep 60) &\nwait\n"
        _approve_script(approvals, paths, "parent_child", script)
        job_uuid = _new_uuid()
        manager.start("parent_child", {}, job_uuid)
        time.sleep(0.2)
        resp = manager.kill(job_uuid)
        assert resp.status == JobStatus.killed

    def test_sigkill_fallback_terminates_sigterm_ignorer(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        """A process that traps SIGTERM must still be killed via SIGKILL escalation."""
        script = "#!/bin/bash\ntrap '' SIGTERM\nsleep 60\n"
        _approve_script(approvals, paths, "sigterm_ignorer", script)
        job_uuid = _new_uuid()
        manager.start("sigterm_ignorer", {}, job_uuid)
        time.sleep(0.2)
        resp = manager.kill(job_uuid)
        assert resp.status == JobStatus.killed


# ---------------------------------------------------------------------------
# TestListJobs — linux_only
# ---------------------------------------------------------------------------


@pytest.mark.linux_only
class TestListJobs:
    def test_empty_initially(self, manager: ProcessManager) -> None:
        assert manager.list_jobs() == []

    def test_lists_started_jobs(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "sleeper", "#!/bin/bash\nsleep 60\n")
        uuid_a = _new_uuid()
        uuid_b = _new_uuid()
        manager.start("sleeper", {}, uuid_a)
        manager.start("sleeper", {}, uuid_b)
        jobs = manager.list_jobs()
        uuids = {j.job_uuid for j in jobs}
        assert {uuid_a, uuid_b}.issubset(uuids)

    def test_completed_jobs_remain_in_list(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "quick", "#!/bin/bash\nsleep 0.05\n")
        job_uuid = _new_uuid()
        manager.start("quick", {}, job_uuid)
        time.sleep(0.3)
        jobs = manager.list_jobs()
        found = next((j for j in jobs if j.job_uuid == job_uuid), None)
        assert found is not None
        assert found.status == JobStatus.completed


# ---------------------------------------------------------------------------
# TestRunningCount — linux_only
# ---------------------------------------------------------------------------


@pytest.mark.linux_only
class TestRunningCount:
    def test_zero_with_no_jobs(self, manager: ProcessManager) -> None:
        assert manager.running_count() == 0

    def test_increments_on_start(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "sleeper", "#!/bin/bash\nsleep 60\n")
        manager.start("sleeper", {}, _new_uuid())
        assert manager.running_count() == 1

    def test_decrements_on_completion(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "quick", "#!/bin/bash\nsleep 0.05\n")
        manager.start("quick", {}, _new_uuid())
        time.sleep(0.4)
        assert manager.running_count() == 0


# ---------------------------------------------------------------------------
# TestWindowsStart — windows_only
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
class TestWindowsStart:
    def test_log_file_created(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "noop", "Start-Sleep -Milliseconds 200\r\n", ".ps1")
        job_uuid = _new_uuid()
        manager.start("noop", {}, job_uuid)
        assert (paths.logs_dir / f"{job_uuid}.log").exists()

    def test_returns_running_status(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "noop", "Start-Sleep -Seconds 5\r\n", ".ps1")
        job_uuid = _new_uuid()
        resp = manager.start("noop", {}, job_uuid)
        assert resp.status == JobStatus.running
        assert resp.persistent is True
        assert resp.job_uuid == job_uuid

    def test_params_passed_via_env(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        script = "Write-Output $env:CSL_PARAM_MESSAGE\r\n"
        _approve_script(approvals, paths, "echo_param", script, ".ps1")
        _write_string_param_meta(paths, "echo_param", "message")
        job_uuid = _new_uuid()
        manager.start("echo_param", {"message": "hello"}, job_uuid)
        time.sleep(1.0)
        log_path = paths.logs_dir / f"{job_uuid}.log"
        assert "hello" in log_path.read_text()


# ---------------------------------------------------------------------------
# TestWindowsStatus — windows_only
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
class TestWindowsStatus:
    def test_running_then_completed(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "quick", "Start-Sleep -Milliseconds 200\r\n", ".ps1")
        job_uuid = _new_uuid()
        manager.start("quick", {}, job_uuid)
        time.sleep(2.0)
        resp = manager.get_status(job_uuid)
        assert resp.status == JobStatus.completed
        assert resp.exit_code == 0
        assert resp.ended_at is not None

    def test_failed_exit_code(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "fails", "exit 42\r\n", ".ps1")
        job_uuid = _new_uuid()
        manager.start("fails", {}, job_uuid)
        time.sleep(2.0)
        resp = manager.get_status(job_uuid)
        assert resp.status == JobStatus.failed
        assert resp.exit_code == 42


# ---------------------------------------------------------------------------
# TestWindowsKill — windows_only
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
class TestWindowsKill:
    def test_kill_running_process(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "sleeper", "Start-Sleep -Seconds 60\r\n", ".ps1")
        job_uuid = _new_uuid()
        manager.start("sleeper", {}, job_uuid)
        time.sleep(0.5)
        resp = manager.kill(job_uuid)
        assert resp.status == JobStatus.killed
        assert resp.ended_at is not None

    def test_kill_already_finished_returns_final_status(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "quick", "Start-Sleep -Milliseconds 100\r\n", ".ps1")
        job_uuid = _new_uuid()
        manager.start("quick", {}, job_uuid)
        time.sleep(2.0)
        resp = manager.kill(job_uuid)
        assert resp.status == JobStatus.completed

    def test_kill_terminates_child_processes(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        """taskkill /F /T must terminate the entire process tree."""
        script = "Start-Job { Start-Sleep 60 } | Out-Null; Start-Sleep 60\r\n"
        _approve_script(approvals, paths, "parent_child", script, ".ps1")
        job_uuid = _new_uuid()
        manager.start("parent_child", {}, job_uuid)
        time.sleep(0.5)
        resp = manager.kill(job_uuid)
        assert resp.status == JobStatus.killed


# ---------------------------------------------------------------------------
# TestWindowsListJobs — windows_only
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
class TestWindowsListJobs:
    def test_empty_initially(self, manager: ProcessManager) -> None:
        assert manager.list_jobs() == []

    def test_lists_started_jobs(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "sleeper", "Start-Sleep -Seconds 60\r\n", ".ps1")
        uuid_a = _new_uuid()
        uuid_b = _new_uuid()
        manager.start("sleeper", {}, uuid_a)
        manager.start("sleeper", {}, uuid_b)
        jobs = manager.list_jobs()
        uuids = {j.job_uuid for j in jobs}
        assert {uuid_a, uuid_b}.issubset(uuids)

    def test_completed_jobs_remain_in_list(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "quick", "Start-Sleep -Milliseconds 200\r\n", ".ps1")
        job_uuid = _new_uuid()
        manager.start("quick", {}, job_uuid)
        time.sleep(2.0)
        jobs = manager.list_jobs()
        found = next((j for j in jobs if j.job_uuid == job_uuid), None)
        assert found is not None
        assert found.status == JobStatus.completed


# ---------------------------------------------------------------------------
# TestWindowsRunningCount — windows_only
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
class TestWindowsRunningCount:
    def test_zero_with_no_jobs(self, manager: ProcessManager) -> None:
        assert manager.running_count() == 0

    def test_increments_on_start(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "sleeper", "Start-Sleep -Seconds 60\r\n", ".ps1")
        manager.start("sleeper", {}, _new_uuid())
        assert manager.running_count() == 1

    def test_decrements_on_completion(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "quick", "Start-Sleep -Milliseconds 200\r\n", ".ps1")
        manager.start("quick", {}, _new_uuid())
        time.sleep(2.0)
        assert manager.running_count() == 0


# ---------------------------------------------------------------------------
# TestRestoreRealProcess — linux_only: full recovery integration tests
# ---------------------------------------------------------------------------


@pytest.mark.linux_only
class TestRestoreRealProcess:
    """End-to-end tests for agent-restart recovery.

    These tests start a real subprocess via one ProcessManager, then discard
    that manager (simulating an agent restart) and create a fresh one backed
    by the same paths.  restore_state() must correctly reattach or discard
    each process.
    """

    def test_running_process_appears_after_restore(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "sleeper", "#!/bin/bash\nsleep 60\n")
        job_uuid = _new_uuid()
        manager.start("sleeper", {}, job_uuid)
        time.sleep(0.1)

        new_manager = ProcessManager(paths, approvals)
        new_manager.restore_state()

        jobs = new_manager.list_jobs()
        found = next((j for j in jobs if j.job_uuid == job_uuid), None)
        assert found is not None
        assert found.status == JobStatus.running
        assert found.script_name == "sleeper"

        new_manager.kill(job_uuid)

    def test_reattached_process_is_killable(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "sleeper", "#!/bin/bash\nsleep 60\n")
        job_uuid = _new_uuid()
        manager.start("sleeper", {}, job_uuid)
        time.sleep(0.1)

        new_manager = ProcessManager(paths, approvals)
        new_manager.restore_state()

        resp = new_manager.kill(job_uuid)
        assert resp.status == JobStatus.killed

    def test_completed_process_not_reattached(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        """A process that exits while the agent is down must be silently discarded."""
        _approve_script(approvals, paths, "quick", "#!/bin/bash\nsleep 0.05\n")
        job_uuid = _new_uuid()
        manager.start("quick", {}, job_uuid)
        time.sleep(0.5)

        new_manager = ProcessManager(paths, approvals)
        new_manager.restore_state()

        assert not any(j.job_uuid == job_uuid for j in new_manager.list_jobs())

    def test_restore_preserves_script_name_and_log_path(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "sleeper", "#!/bin/bash\nsleep 60\n")
        job_uuid = _new_uuid()
        manager.start("sleeper", {}, job_uuid)
        time.sleep(0.1)

        new_manager = ProcessManager(paths, approvals)
        new_manager.restore_state()

        resp = new_manager.get_status(job_uuid)
        assert resp.script_name == "sleeper"
        assert new_manager.get_log_path(job_uuid) == paths.logs_dir / f"{job_uuid}.log"

        new_manager.kill(job_uuid)


# ---------------------------------------------------------------------------
# TestWindowsRestoreRealProcess — windows_only: full recovery integration
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
class TestWindowsRestoreRealProcess:
    """Windows equivalent of TestRestoreRealProcess using PowerShell scripts."""

    def test_running_process_appears_after_restore(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "sleeper", "Start-Sleep -Seconds 60\r\n", ".ps1")
        job_uuid = _new_uuid()
        manager.start("sleeper", {}, job_uuid)
        time.sleep(0.5)

        new_manager = ProcessManager(paths, approvals)
        new_manager.restore_state()

        jobs = new_manager.list_jobs()
        found = next((j for j in jobs if j.job_uuid == job_uuid), None)
        assert found is not None
        assert found.status == JobStatus.running
        assert found.script_name == "sleeper"

        new_manager.kill(job_uuid)

    def test_reattached_process_is_killable(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "sleeper", "Start-Sleep -Seconds 60\r\n", ".ps1")
        job_uuid = _new_uuid()
        manager.start("sleeper", {}, job_uuid)
        time.sleep(0.5)

        new_manager = ProcessManager(paths, approvals)
        new_manager.restore_state()

        resp = new_manager.kill(job_uuid)
        assert resp.status == JobStatus.killed

    def test_completed_process_not_reattached(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "quick", "Start-Sleep -Milliseconds 200\r\n", ".ps1")
        job_uuid = _new_uuid()
        manager.start("quick", {}, job_uuid)
        time.sleep(2.0)

        new_manager = ProcessManager(paths, approvals)
        new_manager.restore_state()

        assert not any(j.job_uuid == job_uuid for j in new_manager.list_jobs())

    def test_restore_preserves_script_name_and_log_path(
        self, approvals: ApprovalsManager, paths: CslPaths, manager: ProcessManager
    ) -> None:
        _approve_script(approvals, paths, "sleeper", "Start-Sleep -Seconds 60\r\n", ".ps1")
        job_uuid = _new_uuid()
        manager.start("sleeper", {}, job_uuid)
        time.sleep(0.5)

        new_manager = ProcessManager(paths, approvals)
        new_manager.restore_state()

        resp = new_manager.get_status(job_uuid)
        assert resp.script_name == "sleeper"
        assert new_manager.get_log_path(job_uuid) == paths.logs_dir / f"{job_uuid}.log"

        new_manager.kill(job_uuid)
