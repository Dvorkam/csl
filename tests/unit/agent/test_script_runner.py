"""Tests for agent/script_runner.py.

Structure:
  TestApprovalEnforcement     — cross-platform: approval gate before any execution
  TestScriptDiscovery         — platform-split: _find_script extension resolution
  TestCommandBuilding         — platform-split: _build_command per extension type
  TestParamEncoding           — cross-platform: _build_env param → CSL_PARAM_* conversion
  TestLinuxExecution          — linux_only: real .sh scripts executed via bash
  TestWindowsExecutionPS1     — windows_only: real .ps1 scripts via PowerShell
  TestWindowsExecutionBatch   — windows_only: real .bat/.cmd scripts via cmd
"""

import hashlib
from pathlib import Path

import pytest

from control_station_lite.agent.approvals import ApprovalsManager
from control_station_lite.agent.script_runner import (
    ScriptNotApprovedError,
    ScriptNotFoundError,
    ScriptResult,
    _build_command,
    _build_env,
    _find_script,
    run_script,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    return {
        "approvals": tmp_path / "agent" / "approvals.json",
        "scripts": tmp_path / "scripts",
        "pending": tmp_path / "scripts.pending",
    }


@pytest.fixture
def approvals(dirs: dict[str, Path]) -> ApprovalsManager:
    return ApprovalsManager(
        approvals_path=dirs["approvals"],
        scripts_dir=dirs["scripts"],
        pending_dir=dirs["pending"],
    )


def _approve_script(
    mgr: ApprovalsManager,
    dirs: dict[str, Path],
    name: str,
    content: str,
    extension: str,
) -> None:
    """Stage and approve *content* as *name*, renaming the approved file to *name*+*extension*."""
    md5 = hashlib.md5(content.encode()).hexdigest()
    mgr.stage(name, content, md5)
    mgr.approve(name)
    # approve() moves pending/<name> → scripts/<name> (no extension).
    # Rename to the platform-appropriate extension so _find_script can locate it.
    approved = dirs["scripts"] / name
    if approved.exists() and extension:
        approved.rename(dirs["scripts"] / f"{name}{extension}")


# ---------------------------------------------------------------------------
# TestApprovalEnforcement — cross-platform
# ---------------------------------------------------------------------------


class TestApprovalEnforcement:
    def test_absent_raises(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        with pytest.raises(ScriptNotApprovedError, match="absent"):
            run_script("sleep_machine", {}, approvals, dirs["scripts"])

    def test_pending_raises(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        approvals.stage("s", "content", "md5")
        with pytest.raises(ScriptNotApprovedError, match="pending"):
            run_script("s", {}, approvals, dirs["scripts"])

    def test_rejected_raises(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        approvals.stage("s", "content", "md5")
        approvals.reject("s")
        with pytest.raises(ScriptNotApprovedError, match="rejected"):
            run_script("s", {}, approvals, dirs["scripts"])

    def test_approved_but_file_missing_raises(
        self, approvals: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
        """Approved in JSON but file deleted — exercises the JSON/filesystem drift case."""
        dirs["scripts"].mkdir(parents=True, exist_ok=True)
        approvals.stage("s", "content", "md5")
        approvals.approve("s")
        (dirs["scripts"] / "s").unlink()
        with pytest.raises(ScriptNotFoundError):
            run_script("s", {}, approvals, dirs["scripts"])


# ---------------------------------------------------------------------------
# TestScriptDiscovery — platform-split
# ---------------------------------------------------------------------------


class TestScriptDiscovery:
    @pytest.mark.linux_only
    def test_sh_extension_found(self, tmp_path: Path) -> None:
        (tmp_path / "sleep_machine.sh").write_text("#!/bin/bash\n")
        assert _find_script("sleep_machine", tmp_path).name == "sleep_machine.sh"

    @pytest.mark.windows_only
    def test_ps1_extension_found(self, tmp_path: Path) -> None:
        (tmp_path / "sleep_machine.ps1").write_text("Write-Host hi\n")
        assert _find_script("sleep_machine", tmp_path).name == "sleep_machine.ps1"

    @pytest.mark.windows_only
    def test_bat_extension_found(self, tmp_path: Path) -> None:
        (tmp_path / "sleep_machine.bat").write_text("@echo off\n")
        assert _find_script("sleep_machine", tmp_path).name == "sleep_machine.bat"

    def test_no_extension_found(self, tmp_path: Path) -> None:
        (tmp_path / "sleep_machine").write_text("#!/bin/bash\n")
        assert _find_script("sleep_machine", tmp_path).name == "sleep_machine"

    def test_missing_raises_script_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(ScriptNotFoundError, match="sleep_machine"):
            _find_script("sleep_machine", tmp_path)

    @pytest.mark.linux_only
    def test_sh_preferred_over_no_extension(self, tmp_path: Path) -> None:
        (tmp_path / "s.sh").write_text("#!/bin/bash\n")
        (tmp_path / "s").write_text("#!/bin/bash\n")
        assert _find_script("s", tmp_path).suffix == ".sh"

    @pytest.mark.windows_only
    def test_ps1_preferred_over_bat(self, tmp_path: Path) -> None:
        (tmp_path / "s.ps1").write_text("")
        (tmp_path / "s.bat").write_text("")
        assert _find_script("s", tmp_path).suffix == ".ps1"


# ---------------------------------------------------------------------------
# TestCommandBuilding — platform-split
# ---------------------------------------------------------------------------


class TestCommandBuilding:
    @pytest.mark.linux_only
    def test_sh_uses_bash(self, tmp_path: Path) -> None:
        p = tmp_path / "s.sh"
        p.touch()
        cmd = _build_command(p)
        assert cmd[0] == "bash"
        assert str(p) in cmd

    @pytest.mark.linux_only
    def test_bash_extension_uses_bash(self, tmp_path: Path) -> None:
        p = tmp_path / "s.bash"
        p.touch()
        assert _build_command(p)[0] == "bash"

    @pytest.mark.linux_only
    def test_no_extension_uses_bash_on_linux(self, tmp_path: Path) -> None:
        p = tmp_path / "s"
        p.touch()
        assert _build_command(p)[0] == "bash"

    @pytest.mark.windows_only
    def test_ps1_uses_powershell(self, tmp_path: Path) -> None:
        p = tmp_path / "s.ps1"
        p.touch()
        cmd = _build_command(p)
        assert cmd[0] == "powershell"
        assert "-File" in cmd
        assert str(p) in cmd

    @pytest.mark.windows_only
    def test_ps1_has_bypass_execution_policy(self, tmp_path: Path) -> None:
        p = tmp_path / "s.ps1"
        p.touch()
        cmd = _build_command(p)
        assert "-ExecutionPolicy" in cmd
        assert "Bypass" in cmd

    @pytest.mark.windows_only
    def test_bat_uses_cmd(self, tmp_path: Path) -> None:
        p = tmp_path / "s.bat"
        p.touch()
        cmd = _build_command(p)
        assert cmd[0] == "cmd"
        assert "/c" in cmd

    @pytest.mark.windows_only
    def test_cmd_extension_uses_cmd(self, tmp_path: Path) -> None:
        p = tmp_path / "s.cmd"
        p.touch()
        assert _build_command(p)[0] == "cmd"

    @pytest.mark.windows_only
    def test_no_extension_raises_on_windows(self, tmp_path: Path) -> None:
        p = tmp_path / "s"
        p.touch()
        with pytest.raises(ScriptNotFoundError, match="extension"):
            _build_command(p)


# ---------------------------------------------------------------------------
# TestParamEncoding — cross-platform
# ---------------------------------------------------------------------------


class TestParamEncoding:
    def test_string_param(self) -> None:
        assert _build_env({"model_path": "/models/llama.gguf"})["CSL_PARAM_MODEL_PATH"] == (
            "/models/llama.gguf"
        )

    def test_int_param(self) -> None:
        assert _build_env({"context_size": 4096})["CSL_PARAM_CONTEXT_SIZE"] == "4096"

    def test_float_param(self) -> None:
        assert _build_env({"temperature": 0.7})["CSL_PARAM_TEMPERATURE"] == "0.7"

    def test_bool_true_param(self) -> None:
        assert _build_env({"verbose": True})["CSL_PARAM_VERBOSE"] == "True"

    def test_bool_false_param(self) -> None:
        assert _build_env({"verbose": False})["CSL_PARAM_VERBOSE"] == "False"

    def test_param_name_uppercased(self) -> None:
        assert "CSL_PARAM_GPU_LAYERS" in _build_env({"gpu_layers": 16})

    def test_multiple_params(self) -> None:
        env = _build_env({"a": "x", "b": 1, "c": True})
        assert env["CSL_PARAM_A"] == "x"
        assert env["CSL_PARAM_B"] == "1"
        assert env["CSL_PARAM_C"] == "True"

    def test_existing_env_preserved(self) -> None:
        assert "PATH" in _build_env({})

    def test_empty_params_adds_nothing(self) -> None:
        assert not any(k.startswith("CSL_PARAM_") for k in _build_env({}))


# ---------------------------------------------------------------------------
# TestLinuxExecution — linux_only
# ---------------------------------------------------------------------------


@pytest.mark.linux_only
class TestLinuxExecution:
    def _sh(
        self,
        approvals: ApprovalsManager,
        dirs: dict[str, Path],
        name: str,
        content: str,
    ) -> None:
        _approve_script(approvals, dirs, name, content, ".sh")

    def test_stdout_captured(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._sh(approvals, dirs, "s", "#!/bin/bash\necho hello")
        assert run_script("s", {}, approvals, dirs["scripts"]).stdout.strip() == "hello"

    def test_stderr_captured(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._sh(approvals, dirs, "s", "#!/bin/bash\necho error >&2")
        assert run_script("s", {}, approvals, dirs["scripts"]).stderr.strip() == "error"

    def test_exit_code_zero(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._sh(approvals, dirs, "s", "#!/bin/bash\nexit 0")
        assert run_script("s", {}, approvals, dirs["scripts"]).exit_code == 0

    def test_exit_code_nonzero(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._sh(approvals, dirs, "s", "#!/bin/bash\nexit 42")
        assert run_script("s", {}, approvals, dirs["scripts"]).exit_code == 42

    def test_string_param_via_env(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._sh(approvals, dirs, "s", '#!/bin/bash\necho "$CSL_PARAM_NAME"')
        assert run_script("s", {"name": "world"}, approvals, dirs["scripts"]).stdout.strip() == (
            "world"
        )

    def test_int_param_as_string(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._sh(approvals, dirs, "s", '#!/bin/bash\necho "$CSL_PARAM_N"')
        assert run_script("s", {"n": 42}, approvals, dirs["scripts"]).stdout.strip() == "42"

    def test_multiple_params(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._sh(approvals, dirs, "s", '#!/bin/bash\necho "$CSL_PARAM_A $CSL_PARAM_B"')
        result = run_script("s", {"a": "hello", "b": "world"}, approvals, dirs["scripts"])
        assert result.stdout.strip() == "hello world"

    def test_timed_out_flag(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._sh(approvals, dirs, "s", "#!/bin/bash\nsleep 60")
        result = run_script("s", {}, approvals, dirs["scripts"], timeout=0.1)
        assert result.timed_out is True
        assert result.exit_code == -1

    def test_no_timeout_flag_not_set(
        self, approvals: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
        self._sh(approvals, dirs, "s", "#!/bin/bash\necho ok")
        assert run_script("s", {}, approvals, dirs["scripts"]).timed_out is False

    def test_returns_script_result(
        self, approvals: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
        self._sh(approvals, dirs, "s", "#!/bin/bash\necho hi")
        assert isinstance(run_script("s", {}, approvals, dirs["scripts"]), ScriptResult)

    def test_bare_script_no_extension(
        self, approvals: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
        """Script stored without extension is executed via bash using shebang."""
        _approve_script(approvals, dirs, "bare", "#!/bin/bash\necho bare", "")
        assert run_script("bare", {}, approvals, dirs["scripts"]).stdout.strip() == "bare"


# ---------------------------------------------------------------------------
# TestWindowsExecutionPS1 — windows_only
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
class TestWindowsExecutionPS1:
    def _ps1(
        self,
        approvals: ApprovalsManager,
        dirs: dict[str, Path],
        name: str,
        content: str,
    ) -> None:
        _approve_script(approvals, dirs, name, content, ".ps1")

    def test_stdout_captured(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._ps1(approvals, dirs, "s", 'Write-Host "hello"')
        assert "hello" in run_script("s", {}, approvals, dirs["scripts"]).stdout

    def test_exit_code_zero(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._ps1(approvals, dirs, "s", "exit 0")
        assert run_script("s", {}, approvals, dirs["scripts"]).exit_code == 0

    def test_exit_code_nonzero(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._ps1(approvals, dirs, "s", "exit 42")
        assert run_script("s", {}, approvals, dirs["scripts"]).exit_code == 42

    def test_param_via_env_var(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._ps1(approvals, dirs, "s", "Write-Host $env:CSL_PARAM_NAME")
        assert "world" in run_script("s", {"name": "world"}, approvals, dirs["scripts"]).stdout

    def test_timed_out_flag(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._ps1(approvals, dirs, "s", "Start-Sleep -Seconds 60")
        result = run_script("s", {}, approvals, dirs["scripts"], timeout=0.5)
        assert result.timed_out is True
        assert result.exit_code == -1


# ---------------------------------------------------------------------------
# TestWindowsExecutionBatch — windows_only
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
class TestWindowsExecutionBatch:
    def _bat(
        self,
        approvals: ApprovalsManager,
        dirs: dict[str, Path],
        name: str,
        content: str,
    ) -> None:
        _approve_script(approvals, dirs, name, content, ".bat")

    def test_stdout_captured(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._bat(approvals, dirs, "s", "@echo off\necho hello")
        assert "hello" in run_script("s", {}, approvals, dirs["scripts"]).stdout

    def test_exit_code_zero(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._bat(approvals, dirs, "s", "@echo off\nexit /b 0")
        assert run_script("s", {}, approvals, dirs["scripts"]).exit_code == 0

    def test_exit_code_nonzero(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._bat(approvals, dirs, "s", "@echo off\nexit /b 7")
        assert run_script("s", {}, approvals, dirs["scripts"]).exit_code == 7

    def test_param_via_env_var(self, approvals: ApprovalsManager, dirs: dict[str, Path]) -> None:
        self._bat(approvals, dirs, "s", "@echo off\necho %CSL_PARAM_NAME%")
        assert "world" in run_script("s", {"name": "world"}, approvals, dirs["scripts"]).stdout
