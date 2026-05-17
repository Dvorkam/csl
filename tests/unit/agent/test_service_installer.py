"""Tests for agent/service_installer.py.

Test organisation
-----------------
  TestContentGenerators   — platform-independent; run on any OS
  TestInstallLinux        — linux_only; mocks subprocess + filesystem
  TestInstallMacOS        — macos_only; mocks subprocess + filesystem
  TestInstallWindows      — windows_only; mocks subprocess + filesystem
  TestInstallServiceDispatch — cross-platform dispatch via platform patches
  TestAgentExecutable     — cross-platform + windows_only sections
"""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from control_station_lite.agent.service_installer import (
    ServiceInstallError,
    _agent_executable,
    _install_linux,
    _install_macos,
    _install_windows,
    _launchd_plist_content,
    _systemd_unit_content,
    _windows_task_xml,
    install_service,
    launchd_plist_path,
    systemd_unit_path,
)

# ---------------------------------------------------------------------------
# Content generators — no OS interaction, run on every platform
# ---------------------------------------------------------------------------


class TestContentGenerators:
    def test_systemd_unit_contains_exec_start(self) -> None:
        content = _systemd_unit_content("/usr/bin/python3")
        assert "ExecStart=/usr/bin/python3 -m control_station_lite.agent" in content

    def test_systemd_unit_restart_no(self) -> None:
        content = _systemd_unit_content("/usr/bin/python3")
        assert "Restart=no" in content

    def test_systemd_unit_no_install_section(self) -> None:
        content = _systemd_unit_content("/usr/bin/python3")
        assert "[Install]" not in content

    def test_systemd_unit_type_exec(self) -> None:
        content = _systemd_unit_content("/usr/bin/python3")
        assert "Type=exec" in content

    def test_launchd_plist_label(self) -> None:
        content = _launchd_plist_content("/usr/bin/python3")
        assert "com.controlstationlite.agent" in content

    def test_launchd_plist_run_at_load_false(self) -> None:
        content = _launchd_plist_content("/usr/bin/python3")
        # <key>RunAtLoad</key> must be followed by <false/>
        idx = content.find("<key>RunAtLoad</key>")
        assert idx != -1
        assert "<false/>" in content[idx : idx + 60]

    def test_launchd_plist_keep_alive_false(self) -> None:
        content = _launchd_plist_content("/usr/bin/python3")
        idx = content.find("<key>KeepAlive</key>")
        assert idx != -1
        assert "<false/>" in content[idx : idx + 60]

    def test_launchd_plist_executable(self) -> None:
        content = _launchd_plist_content("/path/to/python")
        assert "<string>/path/to/python</string>" in content

    def test_launchd_plist_module_args(self) -> None:
        content = _launchd_plist_content("/usr/bin/python3")
        assert "<string>-m</string>" in content
        assert "<string>control_station_lite.agent</string>" in content

    def test_windows_task_xml_no_triggers(self) -> None:
        content = _windows_task_xml(r"C:\Python\pythonw.exe")
        assert "<Triggers/>" in content

    def test_windows_task_xml_command(self) -> None:
        content = _windows_task_xml(r"C:\Python\pythonw.exe")
        assert r"<Command>C:\Python\pythonw.exe</Command>" in content

    def test_windows_task_xml_arguments(self) -> None:
        content = _windows_task_xml(r"C:\Python\pythonw.exe")
        assert "<Arguments>-m control_station_lite.agent</Arguments>" in content

    def test_windows_task_xml_multiple_instances_ignore(self) -> None:
        content = _windows_task_xml(r"C:\Python\pythonw.exe")
        assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in content


# ---------------------------------------------------------------------------
# Linux install
# ---------------------------------------------------------------------------


class TestInstallLinux:
    @pytest.mark.linux_only
    def test_writes_unit_file(self, tmp_path: Path) -> None:
        unit_path = tmp_path / "csl-agent.service"
        with (
            patch(
                "control_station_lite.agent.service_installer.systemd_unit_path",
                return_value=unit_path,
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _install_linux("/usr/bin/python3")

        assert unit_path.exists()
        content = unit_path.read_text(encoding="utf-8")
        assert "ExecStart=/usr/bin/python3" in content

    @pytest.mark.linux_only
    def test_calls_daemon_reload(self, tmp_path: Path) -> None:
        unit_path = tmp_path / "csl-agent.service"
        with (
            patch(
                "control_station_lite.agent.service_installer.systemd_unit_path",
                return_value=unit_path,
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _install_linux("/usr/bin/python3")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == ["systemctl", "--user", "daemon-reload"]

    @pytest.mark.linux_only
    def test_raises_on_daemon_reload_failure(self, tmp_path: Path) -> None:
        unit_path = tmp_path / "csl-agent.service"
        with (
            patch(
                "control_station_lite.agent.service_installer.systemd_unit_path",
                return_value=unit_path,
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr="Failed to connect")
            with pytest.raises(ServiceInstallError, match="daemon-reload failed"):
                _install_linux("/usr/bin/python3")

    @pytest.mark.linux_only
    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        unit_path = tmp_path / "nested" / "dir" / "csl-agent.service"
        with (
            patch(
                "control_station_lite.agent.service_installer.systemd_unit_path",
                return_value=unit_path,
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _install_linux("/usr/bin/python3")

        assert unit_path.exists()


# ---------------------------------------------------------------------------
# macOS install
# ---------------------------------------------------------------------------


class TestInstallMacOS:
    @pytest.mark.macos_only
    def test_writes_plist_file(self, tmp_path: Path) -> None:
        plist_path = tmp_path / "com.controlstationlite.agent.plist"
        with (
            patch(
                "control_station_lite.agent.service_installer.launchd_plist_path",
                return_value=plist_path,
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _install_macos("/usr/bin/python3")

        assert plist_path.exists()
        content = plist_path.read_text(encoding="utf-8")
        assert "com.controlstationlite.agent" in content

    @pytest.mark.macos_only
    def test_calls_launchctl_load(self, tmp_path: Path) -> None:
        plist_path = tmp_path / "com.controlstationlite.agent.plist"
        with (
            patch(
                "control_station_lite.agent.service_installer.launchd_plist_path",
                return_value=plist_path,
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _install_macos("/usr/bin/python3")

        load_call = mock_run.call_args_list[-1]
        assert load_call == call(
            ["launchctl", "load", str(plist_path)],
            capture_output=True,
            text=True,
        )

    @pytest.mark.macos_only
    def test_unloads_before_overwrite_when_plist_exists(self, tmp_path: Path) -> None:
        plist_path = tmp_path / "com.controlstationlite.agent.plist"
        plist_path.write_text("old content", encoding="utf-8")
        with (
            patch(
                "control_station_lite.agent.service_installer.launchd_plist_path",
                return_value=plist_path,
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _install_macos("/usr/bin/python3")

        # First call should be unload
        first_call_args = mock_run.call_args_list[0][0][0]
        assert first_call_args[:2] == ["launchctl", "unload"]

    @pytest.mark.macos_only
    def test_raises_on_load_failure(self, tmp_path: Path) -> None:
        plist_path = tmp_path / "com.controlstationlite.agent.plist"
        with (
            patch(
                "control_station_lite.agent.service_installer.launchd_plist_path",
                return_value=plist_path,
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr="service not found")
            with pytest.raises(ServiceInstallError, match="launchctl load failed"):
                _install_macos("/usr/bin/python3")


# ---------------------------------------------------------------------------
# Windows install
# ---------------------------------------------------------------------------


class TestInstallWindows:
    @pytest.mark.windows_only
    def test_calls_schtasks_create(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _install_windows(r"C:\Python\pythonw.exe")

        args = mock_run.call_args[0][0]
        assert args[0] == "schtasks"
        assert "/create" in args
        assert "/tn" in args
        assert "CSL-Agent" in args
        assert "/f" in args

    @pytest.mark.windows_only
    def test_temp_xml_file_cleaned_up(self) -> None:
        captured_tmp: list[Path] = []

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            for arg in cmd:
                p = Path(str(arg))
                if p.suffix == ".xml" and p.exists():
                    captured_tmp.append(p)
            return MagicMock(returncode=0, stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            _install_windows(r"C:\Python\pythonw.exe")

        # All temp files should be deleted after the call
        for p in captured_tmp:
            assert not p.exists(), f"temp file not cleaned up: {p}"

    @pytest.mark.windows_only
    def test_raises_on_schtasks_failure(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="Access denied")
            with pytest.raises(ServiceInstallError, match="schtasks /create failed"):
                _install_windows(r"C:\Python\pythonw.exe")


# ---------------------------------------------------------------------------
# install_service dispatch
# ---------------------------------------------------------------------------


class TestInstallServiceDispatch:
    def test_dispatches_to_linux_on_linux(self) -> None:
        with (
            patch("control_station_lite.agent.service_installer.IS_WINDOWS", False),
            patch("control_station_lite.agent.service_installer.IS_MACOS", False),
            patch("control_station_lite.agent.service_installer._install_linux") as mock_linux,
            patch(
                "control_station_lite.agent.service_installer._agent_executable",
                return_value="/usr/bin/python3",
            ),
        ):
            install_service()
        mock_linux.assert_called_once_with("/usr/bin/python3")

    def test_dispatches_to_macos_on_macos(self) -> None:
        with (
            patch("control_station_lite.agent.service_installer.IS_WINDOWS", False),
            patch("control_station_lite.agent.service_installer.IS_MACOS", True),
            patch("control_station_lite.agent.service_installer._install_macos") as mock_macos,
            patch(
                "control_station_lite.agent.service_installer._agent_executable",
                return_value="/usr/bin/python3",
            ),
        ):
            install_service()
        mock_macos.assert_called_once_with("/usr/bin/python3")

    def test_dispatches_to_windows_on_windows(self) -> None:
        with (
            patch("control_station_lite.agent.service_installer.IS_WINDOWS", True),
            patch("control_station_lite.agent.service_installer._install_windows") as mock_win,
            patch(
                "control_station_lite.agent.service_installer._agent_executable",
                return_value=r"C:\py\pythonw.exe",
            ),
        ):
            install_service()
        mock_win.assert_called_once_with(r"C:\py\pythonw.exe")


# ---------------------------------------------------------------------------
# _agent_executable
# ---------------------------------------------------------------------------


class TestAgentExecutable:
    def test_returns_string(self) -> None:
        result = _agent_executable()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_sys_executable_on_linux(self) -> None:
        import sys

        with (
            patch("control_station_lite.agent.service_installer.IS_WINDOWS", False),
        ):
            result = _agent_executable()
        assert result == sys.executable

    @pytest.mark.windows_only
    def test_prefers_pythonw_when_present(self, tmp_path: Path) -> None:
        fake_pythonw = tmp_path / "pythonw.exe"
        fake_pythonw.write_bytes(b"")
        fake_python = tmp_path / "python.exe"

        with patch("control_station_lite.agent.service_installer.IS_WINDOWS", True):
            with patch("sys.executable", str(fake_python)):
                result = _agent_executable()
        assert result == str(fake_pythonw)

    @pytest.mark.windows_only
    def test_falls_back_to_python_when_pythonw_absent(self, tmp_path: Path) -> None:
        fake_python = tmp_path / "python.exe"
        fake_python.write_bytes(b"")

        with patch("control_station_lite.agent.service_installer.IS_WINDOWS", True):
            with patch("sys.executable", str(fake_python)):
                result = _agent_executable()
        assert result == str(fake_python)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


class TestPathHelpers:
    def test_systemd_unit_path_ends_with_service(self) -> None:
        path = systemd_unit_path()
        assert path.suffix == ".service"
        assert path.name == "csl-agent.service"

    def test_systemd_unit_path_under_systemd_user(self) -> None:
        path = systemd_unit_path()
        assert "systemd" in path.parts
        assert "user" in path.parts

    def test_launchd_plist_path_ends_with_plist(self) -> None:
        path = launchd_plist_path()
        assert path.suffix == ".plist"
        assert "com.controlstationlite.agent" in path.name

    def test_launchd_plist_path_under_launchagents(self) -> None:
        path = launchd_plist_path()
        assert "LaunchAgents" in path.parts
