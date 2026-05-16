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

"""Install the csl-agent as a user-level on-demand service.

Platform mapping:
  Linux  → systemd --user unit  (~/.config/systemd/user/csl-agent.service)
  macOS  → launchd user agent   (~/Library/LaunchAgents/com.controlstationlite.agent.plist)
  Windows → Task Scheduler task  ("CSL-Agent", demand-only, no trigger)

The service is intentionally *not* enabled/started after installation.
It is started on demand by the control station via the platform-appropriate
one-shot command:
  Linux   : systemctl --user start csl-agent
  macOS   : launchctl kickstart gui/$UID/com.controlstationlite.agent
  Windows : schtasks /run /tn "CSL-Agent"
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from control_station_lite.shared.platform_info import IS_MACOS, IS_WINDOWS

__all__ = [
    "ServiceInstallError",
    "install_service",
    "systemd_unit_path",
    "launchd_plist_path",
]

logger = logging.getLogger(__name__)

# Stable identifiers used by the control station to start the agent.
_SYSTEMD_UNIT_NAME = "csl-agent"
_LAUNCHD_LABEL = "com.controlstationlite.agent"
_WINDOWS_TASK_NAME = "CSL-Agent"


class ServiceInstallError(RuntimeError):
    """Raised when service installation fails."""


# ---------------------------------------------------------------------------
# Public helpers — paths callers may need
# ---------------------------------------------------------------------------


def systemd_unit_path() -> Path:
    """Return the path where the systemd unit file will be written."""
    return Path.home() / ".config" / "systemd" / "user" / f"{_SYSTEMD_UNIT_NAME}.service"


def launchd_plist_path() -> Path:
    """Return the path where the launchd plist will be written."""
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


# ---------------------------------------------------------------------------
# Content generators (platform-independent — fully testable on any OS)
# ---------------------------------------------------------------------------


def _systemd_unit_content(executable: str) -> str:
    return (
        "[Unit]\n"
        "Description=CSL Agent — Control Station Lite on-demand agent\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=exec\n"
        f"ExecStart={executable} -m control_station_lite.agent\n"
        "Restart=no\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "# No Install section — service is on-demand only, do not enable it.\n"
        "# Start on demand: systemctl --user start csl-agent\n"
    )


def _launchd_plist_content(executable: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{_LAUNCHD_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{executable}</string>\n"
        "        <string>-m</string>\n"
        "        <string>control_station_lite.agent</string>\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <false/>\n"
        "    <key>KeepAlive</key>\n"
        "    <false/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _windows_task_xml(executable: str) -> str:
    """Return Task Scheduler XML for a demand-only task (no triggers)."""
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2"'
        ' xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>CSL Agent — Control Station Lite on-demand agent</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers/>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{executable}</Command>\n"
        "      <Arguments>-m control_station_lite.agent</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


# ---------------------------------------------------------------------------
# Platform installers
# ---------------------------------------------------------------------------


def _install_linux(executable: str) -> None:
    unit_path = systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(_systemd_unit_content(executable), encoding="utf-8")
    logger.info("wrote systemd unit: %s", unit_path)

    result = subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ServiceInstallError(
            f"systemctl --user daemon-reload failed (exit {result.returncode}):\n"
            f"{result.stderr.strip()}"
        )
    logger.info("systemd user daemon reloaded")


def _install_macos(executable: str) -> None:
    plist_path = launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # Unload any existing registration before overwriting the plist.
    if plist_path.exists():
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            capture_output=True,
        )

    plist_path.write_text(_launchd_plist_content(executable), encoding="utf-8")
    logger.info("wrote launchd plist: %s", plist_path)

    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ServiceInstallError(
            f"launchctl load failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )
    logger.info("launchd agent loaded: %s", _LAUNCHD_LABEL)


def _install_windows(executable: str) -> None:
    xml_content = _windows_task_xml(executable)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".xml",
        delete=False,
        encoding="utf-16",
    ) as tmp:
        tmp.write(xml_content)
        tmp_path = Path(tmp.name)

    try:
        result = subprocess.run(
            ["schtasks", "/create", "/tn", _WINDOWS_TASK_NAME, "/xml", str(tmp_path), "/f"],
            capture_output=True,
            text=True,
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    if result.returncode != 0:
        raise ServiceInstallError(
            f"schtasks /create failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )
    logger.info("Task Scheduler task registered: %s", _WINDOWS_TASK_NAME)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _agent_executable() -> str:
    """Return the Python interpreter path to use as the service command.

    On Windows, prefer ``pythonw.exe`` (no console window) when it is
    present alongside the current interpreter.  Fall back to the current
    ``sys.executable`` on all platforms.
    """
    if IS_WINDOWS:
        pythonw = Path(sys.executable).parent / "pythonw.exe"
        if pythonw.exists():
            return str(pythonw)
    return sys.executable


def install_service() -> None:
    """Install the csl-agent user-level service for the current platform.

    Idempotent: safe to call multiple times (overwrites existing files and
    re-registers the task/unit each time).

    Raises:
        ServiceInstallError: if the OS command to register/reload the service
            fails (file write errors are propagated as plain ``OSError``).
        NotImplementedError: if the current platform is not Linux, macOS, or
            Windows.
    """
    executable = _agent_executable()
    logger.info("installing csl-agent service using executable: %s", executable)

    if IS_WINDOWS:
        _install_windows(executable)
    elif IS_MACOS:
        _install_macos(executable)
    else:
        # Treat anything else as Linux/systemd.
        _install_linux(executable)
