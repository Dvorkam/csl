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

"""csl-agent command-line interface.

Entry point: ``csl-agent <subcommand>``

Subcommands implemented here:
  init        — first-time agent setup on a target machine
  setup       — check and fix SSH daemon prerequisites

Future subcommands (Tasks 1.13–1.14):
  approvals   list | show | diff | approve | reject | clear
  policy      auto-approve | manual | show
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import logging
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from control_station_lite.agent.config import default_config_path
from control_station_lite.agent.paths import CslPaths
from control_station_lite.agent.service_installer import install_service
from control_station_lite.shared.platform_info import IS_LINUX, IS_MACOS, IS_WINDOWS
from control_station_lite.shared.registration import encode_bundle

__all__ = ["ReadinessIssue", "check_readiness", "main", "setup_system"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System readiness checks
# ---------------------------------------------------------------------------


@dataclass
class ReadinessIssue:
    """A single prerequisite check result."""

    severity: str  # "error" | "warning"
    description: str
    fix_hint: str


def _sshd_running_linux() -> bool:
    """Return True if an SSH daemon process is currently running on Linux."""
    # Prefer systemctl (available on all systemd distros) but fall back to
    # pgrep so the check works on non-systemd systems too.
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
        return issues  # no point checking if sshd is running

    if not _sshd_running_linux():
        issues.append(
            ReadinessIssue(
                severity="warning",
                description="SSH daemon is installed but not running.",
                fix_hint="sudo systemctl enable --now ssh",
            )
        )
    return issues


def _sshd_exe_windows() -> Path:
    return Path("C:/Windows/System32/OpenSSH/sshd.exe")


def _sshd_running_windows() -> bool:
    result = subprocess.run(
        ["sc", "query", "sshd"],
        capture_output=True,
        text=True,
    )
    return "RUNNING" in result.stdout


def _check_readiness_windows() -> list[ReadinessIssue]:
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

    if _windows_is_admin() and not Path("C:/ProgramData/ssh").exists():
        issues.append(
            ReadinessIssue(
                severity="warning",
                description="C:\\ProgramData\\ssh\\ does not exist.",
                fix_hint="Usually created by the OpenSSH Server installer — try reinstalling it.",
            )
        )
    return issues


def _sshd_running_macos() -> bool:
    # macOS Remote Login is managed by launchd; the daemon name is com.openssh.sshd.
    result = subprocess.run(
        ["launchctl", "list", "com.openssh.sshd"],
        capture_output=True,
    )
    if result.returncode == 0:
        return True
    # Fall back to process check for older macOS versions.
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


def check_readiness() -> list[ReadinessIssue]:
    """Run platform-specific prerequisite checks for csl-agent operation.

    Returns a (possibly empty) list of :class:`ReadinessIssue` objects.
    Each issue carries a ``severity`` (``"error"`` or ``"warning"``), a
    human-readable ``description``, and a ``fix_hint`` explaining how to
    resolve it.

    *Errors* indicate conditions that will prevent the agent from working at
    all (e.g. sshd not installed).  *Warnings* indicate degraded-but-operable
    states (e.g. sshd installed but not currently running).
    """
    if IS_WINDOWS:
        return _check_readiness_windows()
    if IS_MACOS:
        return _check_readiness_macos()
    if IS_LINUX:
        return _check_readiness_linux()
    return []


# ---------------------------------------------------------------------------
# System setup (auto-fix)
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
    """Attempt to apply automatic fixes for known readiness issues.

    Best-effort: fixes that require package installation or elevated
    privileges print instructions for the user when they cannot run
    automatically.
    """
    if IS_WINDOWS:
        _setup_windows()
    elif IS_MACOS:
        _setup_macos()
    else:
        _setup_linux()


# ---------------------------------------------------------------------------
# SSH key helpers
# ---------------------------------------------------------------------------


def _generate_keypair(
    keys_dir: Path,
) -> tuple[str, str, bytes]:
    """Generate an Ed25519 SSH keypair in *keys_dir*.

    If a key already exists, load and return it instead (idempotent).

    The public key is written with a ``csl-agent@<hostname>`` comment so that
    entries in ``authorized_keys`` are identifiable when multiple machines or
    re-inits are in play.

    Returns:
        (private_key_pem, fingerprint, public_openssh_bytes_with_comment)
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    private_path = keys_dir / "csl_ed25519"
    public_path = keys_dir / "csl_ed25519.pub"

    if private_path.exists() and public_path.exists():
        logger.info("SSH keypair already exists — reusing %s", private_path)
        private_pem = private_path.read_text(encoding="utf-8")
        public_openssh = public_path.read_bytes()
    else:
        keys_dir.mkdir(parents=True, exist_ok=True)
        private_key = Ed25519PrivateKey.generate()

        private_pem_bytes = private_key.private_bytes(
            Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()
        )
        # Raw key bytes without comment — used only for fingerprint calculation.
        public_raw = private_key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)

        # Annotate with a unique-enough label so the authorized_keys entry is
        # identifiable (mirrors what ssh-keygen -C "label" produces).
        comment = f"csl-agent@{socket.gethostname()}"
        public_openssh = (public_raw.decode().strip() + f" {comment}\n").encode()

        private_path.write_bytes(private_pem_bytes)
        if sys.platform != "win32":
            private_path.chmod(0o600)
        public_path.write_bytes(public_openssh)

        private_pem = private_pem_bytes.decode()
        logger.info("generated Ed25519 keypair at %s", private_path)

    fingerprint = _ssh_fingerprint(public_openssh)
    return private_pem, fingerprint, public_openssh


def _ssh_fingerprint(public_openssh: bytes) -> str:
    """Compute the SHA-256 fingerprint of an OpenSSH public key.

    The fingerprint is computed over the SSH wire format (the base64 payload
    from the public-key line), matching the output of ``ssh-keygen -lf``.

    Returns a string like ``SHA256:abc123…``
    """
    # OpenSSH public key line: "<type> <base64> [comment]"
    b64_part = public_openssh.decode().split()[1]
    wire_bytes = base64.b64decode(b64_part)
    digest = hashlib.sha256(wire_bytes).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


# ---------------------------------------------------------------------------
# authorized_keys helpers
# ---------------------------------------------------------------------------

# On Windows, OpenSSH sshd reads authorized_keys from a different location for
# accounts that are members of the local Administrators group.  The standard
# %USERPROFILE%\.ssh\authorized_keys is silently ignored for those accounts.
_WINDOWS_ADMIN_AK_PATH = Path("C:/ProgramData/ssh/administrators_authorized_keys")


def _windows_is_admin() -> bool:
    """Return True if the current process has Windows Administrator privileges."""
    # sys.platform used here (not IS_WINDOWS) so mypy can narrow platform stubs:
    # ctypes.windll only exists in Windows stubs.
    if sys.platform == "win32":
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    return False


def _set_admin_ak_acl(path: Path) -> None:
    """Restrict *path* to SYSTEM + Administrators only via ``icacls``.

    OpenSSH sshd on Windows rejects ``administrators_authorized_keys`` if any
    non-admin principal has access.  Removing inheritance and granting only
    SYSTEM and BUILTIN\\Administrators satisfies the check.
    """
    import subprocess

    subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant",
            "NT AUTHORITY\\SYSTEM:(F)",
            "/grant",
            "BUILTIN\\Administrators:(F)",
        ],
        check=False,
        capture_output=True,
    )


def _append_authorized_keys(public_openssh: bytes) -> None:
    """Append *public_openssh* to the appropriate ``authorized_keys`` file (idempotent).

    **Normal path** (Linux, macOS, non-admin Windows): writes to
    ``~/.ssh/authorized_keys``, creating the file (mode 600) and directory
    (mode 700) if absent.

    **Windows Administrator path**: when the current user is a member of the
    local Administrators group, OpenSSH sshd reads from
    ``C:\\ProgramData\\ssh\\administrators_authorized_keys`` instead.  This
    function detects that case, writes to the admin path, and applies the
    required restrictive ACLs via ``icacls``.
    """
    use_admin_path = IS_WINDOWS and _windows_is_admin()

    if use_admin_path:
        ak_path = _WINDOWS_ADMIN_AK_PATH
        ak_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        ssh_dir = Path.home() / ".ssh"
        ssh_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            ssh_dir.chmod(0o700)
        ak_path = ssh_dir / "authorized_keys"

    pub_line = public_openssh.decode().strip()
    if ak_path.exists():
        existing = ak_path.read_text(encoding="utf-8")
        if pub_line in existing:
            logger.info("public key already in %s — skipping", ak_path.name)
            return
        with open(ak_path, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(f"{pub_line}\n")
    else:
        ak_path.write_text(f"{pub_line}\n", encoding="utf-8")
        if not use_admin_path and sys.platform != "win32":
            ak_path.chmod(0o600)

    if use_admin_path:
        _set_admin_ak_acl(ak_path)

    logger.info("public key written to %s", ak_path)


# ---------------------------------------------------------------------------
# Config / state file helpers
# ---------------------------------------------------------------------------


def _write_config(config_path: Path, fingerprint: str, port: int) -> None:
    """Write ``config.yaml`` with platform defaults.  Always overwrites."""
    import yaml

    base = CslPaths.platform_base()
    data = {
        "agent": {
            "listen_port": port,
            "idle_timeout_seconds": 600,
            "scripts_dir": str(base / "scripts"),
            "pending_dir": str(base / "scripts.pending"),
            "logs_dir": str(base / "logs"),
            "state_path": str(base / "agent" / "running.json"),
            "approvals_path": str(base / "agent" / "approvals.json"),
        },
        "identity": {
            "key_fingerprint": fingerprint,
            "hostname_hint": socket.gethostname(),
        },
        "approval_policy": {
            "auto_approve": [],
        },
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    logger.info("wrote config: %s", config_path)


def _write_approvals(approvals_path: Path) -> None:
    """Write an empty ``approvals.json`` if one does not already exist."""
    import json

    if approvals_path.exists():
        logger.info("approvals.json already exists — skipping")
        return
    approvals_path.parent.mkdir(parents=True, exist_ok=True)
    approvals_path.write_text(json.dumps({"scripts": {}}, indent=2), encoding="utf-8")
    logger.info("wrote empty approvals: %s", approvals_path)


# ---------------------------------------------------------------------------
# Platform string
# ---------------------------------------------------------------------------


def _platform_name() -> str:
    if IS_WINDOWS:
        return "windows"
    if IS_MACOS:
        return "macos"
    return "linux"


# ---------------------------------------------------------------------------
# init subcommand
# ---------------------------------------------------------------------------


def _print_issues(issues: list[ReadinessIssue]) -> None:
    for issue in issues:
        tag = "[ERROR]" if issue.severity == "error" else "[WARN] "
        print(f"{tag} {issue.description}", file=sys.stderr)
        for line in issue.fix_hint.splitlines():
            print(f"         {line}", file=sys.stderr)


def _cmd_init(args: argparse.Namespace) -> None:
    """Implement ``csl-agent init``."""
    port: int = args.port
    base = CslPaths.platform_base()
    keys_dir = base / "keys"

    # 0 — prerequisite checks (non-blocking: init proceeds even if sshd is down)
    issues = check_readiness()
    if issues:
        print("Prerequisite check results:", file=sys.stderr)
        _print_issues(issues)
        if any(i.severity == "error" for i in issues):
            print(
                "\nErrors found — run 'csl-agent setup' to attempt automatic fixes.",
                file=sys.stderr,
            )
        print("", file=sys.stderr)

    # 1 — directory structure
    paths = CslPaths.from_base(base)
    paths.ensure_dirs()
    keys_dir.mkdir(parents=True, exist_ok=True)
    logger.info("directories ready under %s", base)

    # 2 — SSH keypair
    private_pem, fingerprint, public_openssh = _generate_keypair(keys_dir)

    # 3 — authorized_keys
    _append_authorized_keys(public_openssh)

    # 4 — config.yaml
    _write_config(default_config_path(), fingerprint, port)

    # 5 — approvals.json
    _write_approvals(paths.approvals_path)

    # 6 — install service
    try:
        install_service()
    except Exception as exc:  # noqa: BLE001
        logger.warning("service installation failed (non-fatal): %s", exc)
        print(f"Warning: service installation failed — {exc}", file=sys.stderr)
        print(
            "You can install it manually later by running: "
            'python -c "from control_station_lite.agent.service_installer '
            'import install_service; install_service()"',
            file=sys.stderr,
        )

    # 7 — registration bundle
    bundle = encode_bundle(
        private_key=private_pem,
        key_fingerprint=fingerprint,
        agent_port=port,
        scripts_dir=str(paths.scripts_dir),
        hostname_hint=socket.gethostname(),
        platform=_platform_name(),
    )

    print("\n=== REGISTRATION BUNDLE (send this to the control station admin) ===")
    print(bundle)
    print("=" * 70)
    print(
        f"\nAgent will listen on 127.0.0.1:{port}"
        f"\nKey fingerprint: {fingerprint}"
        f"\nPlatform: {_platform_name()}"
    )


# ---------------------------------------------------------------------------
# setup subcommand
# ---------------------------------------------------------------------------


def _cmd_setup(_args: argparse.Namespace) -> None:
    """Implement ``csl-agent setup``."""
    issues = check_readiness()
    if not issues:
        print("All prerequisite checks passed — system is ready.")
        return

    print("Prerequisite check results:")
    _print_issues(issues)

    print("\nAttempting automatic fixes...")
    setup_system()

    remaining = check_readiness()
    if not remaining:
        print("\nAll issues resolved.")
    else:
        print("\nThe following issues could not be fixed automatically:")
        _print_issues(remaining)
        print(
            "\nPlease apply the fixes above manually, then re-run 'csl-agent setup'.",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for the csl-agent CLI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-8s %(message)s",
        stream=sys.stderr if sys.stderr is not None else None,
    )

    parser = argparse.ArgumentParser(
        prog="csl-agent",
        description="Control Station Lite — agent management",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # --- init ---
    init_p = subparsers.add_parser(
        "init",
        help="First-time setup: create directories, generate SSH keys, install service",
    )
    init_p.add_argument(
        "--port",
        type=int,
        default=47731,
        metavar="PORT",
        help="Port the agent will listen on (default: 47731)",
    )

    # --- setup ---
    subparsers.add_parser(
        "setup",
        help="Check SSH daemon prerequisites and attempt automatic fixes",
    )

    args = parser.parse_args()

    if args.command == "init":
        _cmd_init(args)
    elif args.command == "setup":
        _cmd_setup(args)
    else:
        parser.print_help()
        sys.exit(1)
