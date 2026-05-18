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

"""csl-agent setup — SSH daemon readiness checks and auto-fixes."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from control_station_lite.shared.platform_info import IS_LINUX, IS_MACOS, IS_WINDOWS

if TYPE_CHECKING:
    from control_station_lite.agent.config import AgentConfig

__all__ = ["ReadinessIssue", "_windows_is_admin", "check_readiness", "setup_system"]


@dataclass
class ReadinessIssue:
    """A single prerequisite check result."""

    severity: str  # "error" | "warning"
    description: str
    fix_hint: str


# ---------------------------------------------------------------------------
# Windows admin helper — lives here because readiness checks need it
# ---------------------------------------------------------------------------


def _windows_is_admin() -> bool:
    """Return True if the current process has Windows Administrator privileges."""
    # sys.platform used here (not IS_WINDOWS) so mypy can narrow platform stubs.
    if sys.platform == "win32":
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return False


# ---------------------------------------------------------------------------
# Per-platform readiness checks
# ---------------------------------------------------------------------------


def _sshd_running_linux() -> bool:
    for service in ("ssh", "sshd"):
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            capture_output=True,
        )
        if result.returncode == 0:
            return True
    result = subprocess.run(["pgrep", "-x", "sshd"], capture_output=True)
    return result.returncode == 0


def _check_readiness_linux() -> list[ReadinessIssue]:
    issues: list[ReadinessIssue] = []
    if not shutil.which("sshd"):
        issues.append(
            ReadinessIssue(
                severity="error",
                description="OpenSSH server (sshd) is not installed.",
                fix_hint=(
                    "Install it, e.g.:\n"
                    "  sudo apt install openssh-server   # Debian/Ubuntu\n"
                    "  sudo dnf install openssh-server   # Fedora/RHEL\n"
                    "  sudo pacman -S openssh            # Arch"
                ),
            )
        )
        return issues
    if not _sshd_running_linux():
        issues.append(
            ReadinessIssue(
                severity="warning",
                description="SSH daemon is installed but not running.",
                fix_hint="sudo systemctl enable --now ssh",
            )
        )
    return issues


_SSHD_EXE_WINDOWS = Path("C:/Windows/System32/OpenSSH/sshd.exe")


def _sshd_exe_windows() -> Path:
    return _SSHD_EXE_WINDOWS


def _sshd_running_windows() -> bool:
    result = subprocess.run(["sc", "query", "sshd"], capture_output=True, text=True)
    return "RUNNING" in result.stdout


def _check_readiness_windows(cfg: AgentConfig | None = None) -> list[ReadinessIssue]:
    issues: list[ReadinessIssue] = []
    if not _sshd_exe_windows().exists():
        issues.append(
            ReadinessIssue(
                severity="error",
                description="OpenSSH Server is not installed.",
                fix_hint=(
                    "Install via Settings > System > Optional Features > Add a feature "
                    "> OpenSSH Server,\n"
                    "  or in an admin PowerShell:\n"
                    "  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0"
                ),
            )
        )
        return issues
    if not _sshd_running_windows():
        issues.append(
            ReadinessIssue(
                severity="warning",
                description="OpenSSH Server is installed but the sshd service is not running.",
                fix_hint=(
                    "Start and enable it in an admin terminal:\n"
                    "  sc start sshd\n"
                    "  sc config sshd start= auto"
                ),
            )
        )
    if _windows_is_admin():
        if cfg is not None:
            admin_ak_dir = cfg.advanced.windows_admin_authorized_keys_path.parent
        else:
            from control_station_lite.agent.config import AdvancedSection

            admin_ak_dir = AdvancedSection().windows_admin_authorized_keys_path.parent
        if not admin_ak_dir.exists():
            issues.append(
                ReadinessIssue(
                    severity="warning",
                    description=f"{admin_ak_dir} does not exist.",
                    fix_hint=(
                        "Usually created by the OpenSSH Server installer — try reinstalling it."
                    ),
                )
            )
    return issues


def _sshd_running_macos() -> bool:
    result = subprocess.run(["launchctl", "list", "com.openssh.sshd"], capture_output=True)
    if result.returncode == 0:
        return True
    result = subprocess.run(["pgrep", "-x", "sshd"], capture_output=True)
    return result.returncode == 0


def _check_readiness_macos() -> list[ReadinessIssue]:
    issues: list[ReadinessIssue] = []
    if not _sshd_running_macos():
        issues.append(
            ReadinessIssue(
                severity="warning",
                description="Remote Login (SSH) is not enabled.",
                fix_hint=(
                    "Enable it:\n"
                    "  sudo systemsetup -setremotelogin on\n"
                    "  or: System Settings > General > Sharing > Remote Login"
                ),
            )
        )
    return issues


def check_readiness(cfg: AgentConfig | None = None) -> list[ReadinessIssue]:
    """Run platform-specific prerequisite checks for csl-agent operation."""
    if IS_WINDOWS:
        return _check_readiness_windows(cfg)
    if IS_MACOS:
        return _check_readiness_macos()
    if IS_LINUX:
        return _check_readiness_linux()
    return []


# ---------------------------------------------------------------------------
# Auto-fix
# ---------------------------------------------------------------------------


def _setup_linux() -> None:
    print("Attempting to enable and start SSH daemon...", file=sys.stderr)
    for service in ("ssh", "sshd"):
        result = subprocess.run(
            ["sudo", "systemctl", "enable", "--now", service],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"  ssh daemon enabled and started (service: {service!r}).", file=sys.stderr)
            return
    print(
        "  Could not start sshd automatically (sudo may be required).\n"
        "  Run manually: sudo systemctl enable --now ssh",
        file=sys.stderr,
    )


def _setup_windows() -> None:
    print("Attempting to start OpenSSH Server service...", file=sys.stderr)
    result = subprocess.run(["sc", "start", "sshd"], capture_output=True, text=True)
    if result.returncode == 0:
        subprocess.run(["sc", "config", "sshd", "start=", "auto"], capture_output=True)
        print("  sshd service started and set to automatic.", file=sys.stderr)
    else:
        print(
            "  Could not start sshd (admin privileges may be required).\n"
            "  Run in an admin terminal: sc start sshd && sc config sshd start= auto",
            file=sys.stderr,
        )


def _setup_macos() -> None:
    print("Attempting to enable Remote Login (SSH)...", file=sys.stderr)
    result = subprocess.run(
        ["sudo", "systemsetup", "-setremotelogin", "on"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("  Remote Login enabled.", file=sys.stderr)
    else:
        print(
            "  Could not enable Remote Login automatically (sudo may be required).\n"
            "  Run manually: sudo systemsetup -setremotelogin on\n"
            "  or: System Settings > General > Sharing > Remote Login",
            file=sys.stderr,
        )


def setup_system() -> None:
    """Attempt to apply automatic fixes for known readiness issues."""
    if IS_WINDOWS:
        _setup_windows()
    elif IS_MACOS:
        _setup_macos()
    else:
        _setup_linux()


# ---------------------------------------------------------------------------
# setup subcommand handler
# ---------------------------------------------------------------------------


def _print_issues(issues: list[ReadinessIssue]) -> None:
    for issue in issues:
        tag = "[ERROR]" if issue.severity == "error" else "[WARN] "
        print(f"{tag} {issue.description}", file=sys.stderr)
        for line in issue.fix_hint.splitlines():
            print(f"         {line}", file=sys.stderr)


def cmd_setup() -> None:
    """Implement ``csl-agent setup``."""
    from control_station_lite.agent.config import load_config

    cfg = load_config()
    issues = check_readiness(cfg)
    if not issues:
        print("All prerequisite checks passed — system is ready.")
        return

    print("Prerequisite check results:")
    _print_issues(issues)

    print("\nAttempting automatic fixes...")
    setup_system()

    remaining = check_readiness(cfg)
    if not remaining:
        print("\nAll issues resolved.")
    else:
        print("\nThe following issues could not be fixed automatically:")
        _print_issues(remaining)
        print(
            "\nPlease apply the fixes above manually, then re-run 'csl-agent setup'.",
            file=sys.stderr,
        )
        import sys as _sys

        _sys.exit(1)
