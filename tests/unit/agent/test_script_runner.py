"""Tests for agent/script_runner.py.

Structure:
  TestApprovalEnforcement     — cross-platform: approval gate before any execution
  TestScriptDiscovery         — platform-split: find_script extension resolution
  TestCommandBuilding         — platform-split: build_command per extension type
  TestParamEncoding           — cross-platform: build_env param → CSL_PARAM_* conversion
  TestLinuxExecution          — linux_only: real .sh scripts executed via bash
  TestWindowsExecutionPS1     — windows_only: real .ps1 scripts via PowerShell
  TestWindowsExecutionBatch   — windows_only: real .bat/.cmd scripts via cmd
"""

import hashlib
from pathlib import Path

import pytest

from control_station_lite.agent.approvals import ApprovalsManager
from control_station_lite.agent.paths import CslPaths
from control_station_lite.agent.script_runner import (
    ParamValidationError,
    ScriptIntegrityError,
    ScriptNotApprovedError,
    ScriptNotFoundError,
    ScriptResult,
    build_command,
    build_env,
    file_md5,
    find_script,
    run_script,
    validate_params,
    verify_script_integrity,
)

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


def _approve_script(
    mgr: ApprovalsManager,
    paths: CslPaths,
    name: str,
    content: str,
    extension: str,
) -> None:
    """Stage and approve *content* as *name*, renaming the approved file to *name*+*extension*."""
    md5 = hashlib.md5(content.encode()).hexdigest()
    mgr.stage(name, content, md5)
    mgr.approve(name)
    # approve() moves pending/<name> → scripts/<name> (no extension).
    # Rename to the platform-appropriate extension so find_script can locate it.
    approved = paths.scripts_dir / name
    if approved.exists() and extension:
        approved.rename(paths.scripts_dir / f"{name}{extension}")


def _meta_body(params: dict[str, str]) -> str:
    body = "params:\n"
    for pname, ptype in params.items():
        body += f"  - name: {pname}\n    type: {ptype}\n    required: true\n"
    return body


def _write_meta(paths: CslPaths, name: str, params: dict[str, str]) -> None:
    """Write a meta.yaml declaring each *param name → type* as a required param."""
    (paths.scripts_dir / f"{name}.meta.yaml").write_text(_meta_body(params))


def _write_meta_dir(directory: Path, name: str, params: dict[str, str]) -> None:
    """Like :func:`_write_meta` but writing into a plain directory."""
    (directory / f"{name}.meta.yaml").write_text(_meta_body(params))


# ---------------------------------------------------------------------------
# TestApprovalEnforcement — cross-platform
# ---------------------------------------------------------------------------


class TestApprovalEnforcement:
    def test_absent_raises(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        with pytest.raises(ScriptNotApprovedError, match="absent"):
            run_script("sleep_machine", {}, approvals, paths.scripts_dir)

    def test_pending_raises(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        approvals.stage("s", "content", "md5")
        with pytest.raises(ScriptNotApprovedError, match="pending"):
            run_script("s", {}, approvals, paths.scripts_dir)

    def test_rejected_raises(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        approvals.stage("s", "content", "md5")
        approvals.reject("s")
        with pytest.raises(ScriptNotApprovedError, match="rejected"):
            run_script("s", {}, approvals, paths.scripts_dir)

    def test_approved_but_file_missing_raises(
        self, approvals: ApprovalsManager, paths: CslPaths
    ) -> None:
        """Approved in JSON but file deleted — exercises the JSON/filesystem drift case."""
        paths.scripts_dir.mkdir(parents=True, exist_ok=True)
        approvals.stage("s", "content", "md5")
        approvals.approve("s")
        (paths.scripts_dir / "s").unlink()
        with pytest.raises(ScriptNotFoundError):
            run_script("s", {}, approvals, paths.scripts_dir)


# ---------------------------------------------------------------------------
# TestIntegrityCheck — on-disk MD5 must match approved MD5 (task 8.5.4)
# ---------------------------------------------------------------------------


class TestIntegrityCheck:
    def test_file_md5_normalises_newlines(self, tmp_path: Path) -> None:
        p = tmp_path / "s"
        p.write_bytes(b"line1\r\nline2\n")
        assert file_md5(p) == hashlib.md5(b"line1\nline2\n").hexdigest()

    def test_verify_passes_on_match(self, tmp_path: Path) -> None:
        p = tmp_path / "s"
        p.write_text("echo hi\n")
        verify_script_integrity("s", p, hashlib.md5(b"echo hi\n").hexdigest())  # no raise

    def test_verify_skipped_when_approved_md5_none(self, tmp_path: Path) -> None:
        p = tmp_path / "s"
        p.write_text("anything\n")
        verify_script_integrity("s", p, None)  # no raise

    def test_verify_raises_on_mismatch(self, tmp_path: Path) -> None:
        p = tmp_path / "s"
        p.write_text("tampered\n")
        with pytest.raises(ScriptIntegrityError):
            verify_script_integrity("s", p, "deadbeef")

    def test_run_script_refuses_tampered_file(
        self, approvals: ApprovalsManager, paths: CslPaths
    ) -> None:
        _approve_script(approvals, paths, "s", "echo hi\n", "")
        # Tamper the approved file on disk, outside the approval flow.
        (paths.scripts_dir / "s").write_text("malicious\n")
        with pytest.raises(ScriptIntegrityError):
            run_script("s", {}, approvals, paths.scripts_dir)


# ---------------------------------------------------------------------------
# TestParamValidation — params must match meta.yaml (task 8.5.5)
# ---------------------------------------------------------------------------


class TestParamValidation:
    def test_no_meta_no_params_ok(self, tmp_path: Path) -> None:
        validate_params("s", {}, tmp_path)  # no raise

    def test_no_meta_with_params_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ParamValidationError, match="accepts no parameters"):
            validate_params("s", {"x": "1"}, tmp_path)

    def test_unknown_param_rejected(self, tmp_path: Path) -> None:
        _write_meta_dir(tmp_path, "s", {"known": "string"})
        with pytest.raises(ParamValidationError, match="unknown parameter 'extra'"):
            validate_params("s", {"known": "v", "extra": "x"}, tmp_path)

    def test_missing_required_rejected(self, tmp_path: Path) -> None:
        _write_meta_dir(tmp_path, "s", {"need": "string"})
        with pytest.raises(ParamValidationError, match="missing required parameter 'need'"):
            validate_params("s", {}, tmp_path)

    def test_wrong_type_rejected(self, tmp_path: Path) -> None:
        _write_meta_dir(tmp_path, "s", {"n": "int"})
        with pytest.raises(ParamValidationError, match="must be an integer"):
            validate_params("s", {"n": "not-a-number"}, tmp_path)

    def test_bool_not_accepted_as_int(self, tmp_path: Path) -> None:
        _write_meta_dir(tmp_path, "s", {"n": "int"})
        with pytest.raises(ParamValidationError, match="must be an integer"):
            validate_params("s", {"n": True}, tmp_path)

    def test_min_max_enforced(self, tmp_path: Path) -> None:
        (tmp_path / "s.meta.yaml").write_text(
            "params:\n  - name: n\n    type: int\n    required: true\n    min: 1\n    max: 10\n"
        )
        with pytest.raises(ParamValidationError, match=">= 1"):
            validate_params("s", {"n": 0}, tmp_path)
        with pytest.raises(ParamValidationError, match="<= 10"):
            validate_params("s", {"n": 11}, tmp_path)

    def test_choice_enforced(self, tmp_path: Path) -> None:
        (tmp_path / "s.meta.yaml").write_text(
            "params:\n  - name: mode\n    type: choice\n    required: true\n    choices: [a, b]\n"
        )
        validate_params("s", {"mode": "a"}, tmp_path)  # no raise
        with pytest.raises(ParamValidationError, match="must be one of"):
            validate_params("s", {"mode": "z"}, tmp_path)

    def test_valid_params_pass(self, tmp_path: Path) -> None:
        _write_meta_dir(tmp_path, "s", {"name": "string", "n": "int"})
        validate_params("s", {"name": "x", "n": 5}, tmp_path)  # no raise


# ---------------------------------------------------------------------------
# TestScriptDiscovery — platform-split
# ---------------------------------------------------------------------------


class TestScriptDiscovery:
    @pytest.mark.linux_only
    def test_sh_extension_found(self, tmp_path: Path) -> None:
        (tmp_path / "sleep_machine.sh").write_text("#!/bin/bash\n")
        assert find_script("sleep_machine", tmp_path).name == "sleep_machine.sh"

    @pytest.mark.windows_only
    def test_ps1_extension_found(self, tmp_path: Path) -> None:
        (tmp_path / "sleep_machine.ps1").write_text("Write-Host hi\n")
        assert find_script("sleep_machine", tmp_path).name == "sleep_machine.ps1"

    @pytest.mark.windows_only
    def test_bat_extension_found(self, tmp_path: Path) -> None:
        (tmp_path / "sleep_machine.bat").write_text("@echo off\n")
        assert find_script("sleep_machine", tmp_path).name == "sleep_machine.bat"

    def test_no_extension_found(self, tmp_path: Path) -> None:
        (tmp_path / "sleep_machine").write_text("#!/bin/bash\n")
        assert find_script("sleep_machine", tmp_path).name == "sleep_machine"

    def test_missing_raises_script_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(ScriptNotFoundError, match="sleep_machine"):
            find_script("sleep_machine", tmp_path)

    @pytest.mark.linux_only
    def test_sh_preferred_over_no_extension(self, tmp_path: Path) -> None:
        (tmp_path / "s.sh").write_text("#!/bin/bash\n")
        (tmp_path / "s").write_text("#!/bin/bash\n")
        assert find_script("s", tmp_path).suffix == ".sh"

    @pytest.mark.windows_only
    def test_ps1_preferred_over_bat(self, tmp_path: Path) -> None:
        (tmp_path / "s.ps1").write_text("")
        (tmp_path / "s.bat").write_text("")
        assert find_script("s", tmp_path).suffix == ".ps1"


# ---------------------------------------------------------------------------
# TestCommandBuilding — platform-split
# ---------------------------------------------------------------------------


class TestCommandBuilding:
    @pytest.mark.linux_only
    def test_sh_uses_bash(self, tmp_path: Path) -> None:
        p = tmp_path / "s.sh"
        p.touch()
        cmd = build_command(p)
        assert cmd[0] == "bash"
        assert str(p) in cmd

    @pytest.mark.linux_only
    def test_bash_extension_uses_bash(self, tmp_path: Path) -> None:
        p = tmp_path / "s.bash"
        p.touch()
        assert build_command(p)[0] == "bash"

    @pytest.mark.linux_only
    def test_no_extension_uses_bash_on_linux(self, tmp_path: Path) -> None:
        p = tmp_path / "s"
        p.touch()
        assert build_command(p)[0] == "bash"

    @pytest.mark.windows_only
    def test_ps1_uses_powershell(self, tmp_path: Path) -> None:
        p = tmp_path / "s.ps1"
        p.touch()
        cmd = build_command(p)
        assert cmd[0] == "powershell"
        assert "-File" in cmd
        assert str(p) in cmd

    @pytest.mark.windows_only
    def test_ps1_has_bypass_execution_policy(self, tmp_path: Path) -> None:
        p = tmp_path / "s.ps1"
        p.touch()
        cmd = build_command(p)
        assert "-ExecutionPolicy" in cmd
        assert "Bypass" in cmd

    @pytest.mark.windows_only
    def test_bat_uses_cmd(self, tmp_path: Path) -> None:
        p = tmp_path / "s.bat"
        p.touch()
        cmd = build_command(p)
        assert cmd[0] == "cmd"
        assert "/c" in cmd

    @pytest.mark.windows_only
    def test_cmd_extension_uses_cmd(self, tmp_path: Path) -> None:
        p = tmp_path / "s.cmd"
        p.touch()
        assert build_command(p)[0] == "cmd"

    @pytest.mark.windows_only
    def test_no_extension_raises_on_windows(self, tmp_path: Path) -> None:
        p = tmp_path / "s"
        p.touch()
        with pytest.raises(ScriptNotFoundError, match="extension"):
            build_command(p)


# ---------------------------------------------------------------------------
# TestParamEncoding — cross-platform
# ---------------------------------------------------------------------------


class TestParamEncoding:
    def test_string_param(self) -> None:
        assert build_env({"model_path": "/models/llama.gguf"})["CSL_PARAM_MODEL_PATH"] == (
            "/models/llama.gguf"
        )

    def test_int_param(self) -> None:
        assert build_env({"context_size": 4096})["CSL_PARAM_CONTEXT_SIZE"] == "4096"

    def test_float_param(self) -> None:
        assert build_env({"temperature": 0.7})["CSL_PARAM_TEMPERATURE"] == "0.7"

    def test_bool_true_param(self) -> None:
        assert build_env({"verbose": True})["CSL_PARAM_VERBOSE"] == "True"

    def test_bool_false_param(self) -> None:
        assert build_env({"verbose": False})["CSL_PARAM_VERBOSE"] == "False"

    def test_param_name_uppercased(self) -> None:
        assert "CSL_PARAM_GPU_LAYERS" in build_env({"gpu_layers": 16})

    def test_multiple_params(self) -> None:
        env = build_env({"a": "x", "b": 1, "c": True})
        assert env["CSL_PARAM_A"] == "x"
        assert env["CSL_PARAM_B"] == "1"
        assert env["CSL_PARAM_C"] == "True"

    def test_existing_env_preserved(self) -> None:
        assert "PATH" in build_env({})

    def test_empty_params_adds_nothing(self) -> None:
        assert not any(k.startswith("CSL_PARAM_") for k in build_env({}))


# ---------------------------------------------------------------------------
# TestLinuxExecution — linux_only
# ---------------------------------------------------------------------------


@pytest.mark.linux_only
class TestLinuxExecution:
    def _sh(
        self,
        approvals: ApprovalsManager,
        paths: CslPaths,
        name: str,
        content: str,
    ) -> None:
        _approve_script(approvals, paths, name, content, ".sh")

    def test_stdout_captured(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._sh(approvals, paths, "s", "#!/bin/bash\necho hello")
        assert run_script("s", {}, approvals, paths.scripts_dir).stdout.strip() == "hello"

    def test_stderr_captured(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._sh(approvals, paths, "s", "#!/bin/bash\necho error >&2")
        assert run_script("s", {}, approvals, paths.scripts_dir).stderr.strip() == "error"

    def test_exit_code_zero(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._sh(approvals, paths, "s", "#!/bin/bash\nexit 0")
        assert run_script("s", {}, approvals, paths.scripts_dir).exit_code == 0

    def test_exit_code_nonzero(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._sh(approvals, paths, "s", "#!/bin/bash\nexit 42")
        assert run_script("s", {}, approvals, paths.scripts_dir).exit_code == 42

    def test_string_param_via_env(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._sh(approvals, paths, "s", '#!/bin/bash\necho "$CSL_PARAM_NAME"')
        _write_meta(paths, "s", {"name": "string"})
        assert run_script("s", {"name": "world"}, approvals, paths.scripts_dir).stdout.strip() == (
            "world"
        )

    def test_int_param_as_string(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._sh(approvals, paths, "s", '#!/bin/bash\necho "$CSL_PARAM_N"')
        _write_meta(paths, "s", {"n": "int"})
        assert run_script("s", {"n": 42}, approvals, paths.scripts_dir).stdout.strip() == "42"

    def test_multiple_params(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._sh(approvals, paths, "s", '#!/bin/bash\necho "$CSL_PARAM_A $CSL_PARAM_B"')
        _write_meta(paths, "s", {"a": "string", "b": "string"})
        result = run_script("s", {"a": "hello", "b": "world"}, approvals, paths.scripts_dir)
        assert result.stdout.strip() == "hello world"

    def test_timed_out_flag(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._sh(approvals, paths, "s", "#!/bin/bash\nsleep 60")
        result = run_script("s", {}, approvals, paths.scripts_dir, timeout=0.1)
        assert result.timed_out is True
        assert result.exit_code == -1

    def test_no_timeout_flag_not_set(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._sh(approvals, paths, "s", "#!/bin/bash\necho ok")
        assert run_script("s", {}, approvals, paths.scripts_dir).timed_out is False

    def test_returns_script_result(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._sh(approvals, paths, "s", "#!/bin/bash\necho hi")
        assert isinstance(run_script("s", {}, approvals, paths.scripts_dir), ScriptResult)

    def test_bare_script_no_extension(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        """Script stored without extension is executed via bash using shebang."""
        _approve_script(approvals, paths, "bare", "#!/bin/bash\necho bare", "")
        assert run_script("bare", {}, approvals, paths.scripts_dir).stdout.strip() == "bare"


# ---------------------------------------------------------------------------
# TestWindowsExecutionPS1 — windows_only
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
class TestWindowsExecutionPS1:
    def _ps1(
        self,
        approvals: ApprovalsManager,
        paths: CslPaths,
        name: str,
        content: str,
    ) -> None:
        _approve_script(approvals, paths, name, content, ".ps1")

    def test_stdout_captured(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._ps1(approvals, paths, "s", 'Write-Host "hello"')
        assert "hello" in run_script("s", {}, approvals, paths.scripts_dir).stdout

    def test_exit_code_zero(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._ps1(approvals, paths, "s", "exit 0")
        assert run_script("s", {}, approvals, paths.scripts_dir).exit_code == 0

    def test_exit_code_nonzero(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._ps1(approvals, paths, "s", "exit 42")
        assert run_script("s", {}, approvals, paths.scripts_dir).exit_code == 42

    def test_param_via_env_var(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._ps1(approvals, paths, "s", "Write-Host $env:CSL_PARAM_NAME")
        _write_meta(paths, "s", {"name": "string"})
        assert "world" in run_script("s", {"name": "world"}, approvals, paths.scripts_dir).stdout

    def test_timed_out_flag(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._ps1(approvals, paths, "s", "Start-Sleep -Seconds 60")
        result = run_script("s", {}, approvals, paths.scripts_dir, timeout=0.5)
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
        paths: CslPaths,
        name: str,
        content: str,
    ) -> None:
        _approve_script(approvals, paths, name, content, ".bat")

    def test_stdout_captured(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._bat(approvals, paths, "s", "@echo off\necho hello")
        assert "hello" in run_script("s", {}, approvals, paths.scripts_dir).stdout

    def test_exit_code_zero(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._bat(approvals, paths, "s", "@echo off\nexit /b 0")
        assert run_script("s", {}, approvals, paths.scripts_dir).exit_code == 0

    def test_exit_code_nonzero(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._bat(approvals, paths, "s", "@echo off\nexit /b 7")
        assert run_script("s", {}, approvals, paths.scripts_dir).exit_code == 7

    def test_param_via_env_var(self, approvals: ApprovalsManager, paths: CslPaths) -> None:
        self._bat(approvals, paths, "s", "@echo off\necho %CSL_PARAM_NAME%")
        _write_meta(paths, "s", {"name": "string"})
        assert "world" in run_script("s", {"name": "world"}, approvals, paths.scripts_dir).stdout
