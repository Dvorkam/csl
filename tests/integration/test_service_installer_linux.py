"""Integration tests for the Linux service installer.

These tests exercise the real unit-file generation and validate it with
``systemd-analyze verify``.  No D-Bus or user session is required — the
verify sub-command performs static analysis only.

All tests are guarded with ``linux_only`` so they are skipped on Windows and
macOS CI runners.
"""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from control_station_lite.agent.service_installer import _install_linux, _systemd_unit_content


@pytest.mark.linux_only
class TestSystemdUnitFileValidity:
    def test_systemd_analyze_available(self) -> None:
        """Confirm the tool we depend on is present."""
        assert shutil.which("systemd-analyze") is not None, (
            "systemd-analyze not found — cannot validate unit files"
        )

    def test_unit_file_passes_systemd_analyze_verify(self, tmp_path: Path) -> None:
        """The generated unit file must pass static validation."""
        import sys

        unit_file = tmp_path / "csl-agent.service"
        unit_file.write_text(_systemd_unit_content(sys.executable), encoding="utf-8")

        result = subprocess.run(
            ["systemd-analyze", "verify", str(unit_file)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"systemd-analyze verify failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_install_linux_writes_file_to_expected_path(self, tmp_path: Path) -> None:
        """_install_linux writes to the path returned by systemd_unit_path()."""
        import sys

        unit_path = tmp_path / "csl-agent.service"
        with (
            patch(
                "control_station_lite.agent.service_installer.systemd_unit_path",
                return_value=unit_path,
            ),
            patch("subprocess.run") as mock_run,
        ):
            from unittest.mock import MagicMock

            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _install_linux(sys.executable)

        assert unit_path.exists()
        content = unit_path.read_text(encoding="utf-8")
        assert sys.executable in content

    def test_installed_unit_file_passes_verify(self, tmp_path: Path) -> None:
        """The file written by _install_linux() passes systemd-analyze verify."""
        import sys

        unit_path = tmp_path / "csl-agent.service"
        with (
            patch(
                "control_station_lite.agent.service_installer.systemd_unit_path",
                return_value=unit_path,
            ),
            patch("subprocess.run") as mock_run,
        ):
            from unittest.mock import MagicMock

            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _install_linux(sys.executable)

        result = subprocess.run(
            ["systemd-analyze", "verify", str(unit_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"systemd-analyze verify failed on the installed file:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
