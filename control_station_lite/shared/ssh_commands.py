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

"""Canonical SSH commands the control station may run on a target.

The control station never opens an interactive shell on a target; it only
issues a small, fixed set of exec commands over SSH (service-start and a
one-time config read at registration). The target restricts its dedicated key
to ``command="csl-agent ssh-gateway"`` (see ``agent.cli.cmd_ssh_gateway``),
which executes a request only if it exactly matches one of the strings below.

Both sides import from this module so the server (which sends the commands) and
the gateway (which allowlists them) cannot drift apart.
"""

from __future__ import annotations

from control_station_lite.shared.platform_info import IS_MACOS, IS_WINDOWS

__all__ = [
    "CONFIG_READ_CMD",
    "WAKEUP_CMD",
    "allowed_commands",
    "current_platform_name",
]

# Platform-appropriate one-shot service-start commands. Each exits immediately
# after signalling the OS to start the agent; the OS owns the process.
WAKEUP_CMD: dict[str, str] = {
    "linux": "systemctl --user start csl-agent",
    "windows": 'schtasks /run /tn "CSL-Agent"',
    "macos": 'launchctl kickstart "gui/$UID/com.controlstationlite.agent"',
}

# One-time config read performed during machine registration to verify the
# agent's key fingerprint before a record is written.
CONFIG_READ_CMD: dict[str, str] = {
    "linux": "cat ~/.csl/config.yaml",
    "macos": "cat ~/.csl/config.yaml",
    "windows": (
        r'powershell -Command "Get-Content (Join-Path $env:USERPROFILE \".csl\config.yaml\")"'
    ),
}


def current_platform_name() -> str:
    """Return this machine's platform as ``linux`` / ``windows`` / ``macos``."""
    if IS_WINDOWS:
        return "windows"
    if IS_MACOS:
        return "macos"
    return "linux"


def allowed_commands(platform: str) -> frozenset[str]:
    """Return the exact command strings the gateway permits on *platform*."""
    commands: set[str] = set()
    if platform in WAKEUP_CMD:
        commands.add(WAKEUP_CMD[platform])
    if platform in CONFIG_READ_CMD:
        commands.add(CONFIG_READ_CMD[platform])
    return frozenset(commands)
