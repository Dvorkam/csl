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

"""csl-agent init — first-time setup on a target machine."""

from __future__ import annotations

import argparse
import base64
import hashlib
import logging
import socket
import sys
from pathlib import Path

from control_station_lite.agent.config import default_config_path, load_config
from control_station_lite.agent.paths import CslPaths
from control_station_lite.agent.service_installer import install_service
from control_station_lite.shared.platform_info import IS_MACOS, IS_WINDOWS
from control_station_lite.shared.registration import encode_bundle

from .cmd_setup import ReadinessIssue, _print_issues, _windows_is_admin, check_readiness

__all__ = [
    "_WINDOWS_ADMIN_AK_PATH",
    "_append_authorized_keys",
    "_generate_keypair",
    "_platform_name",
    "_set_admin_ak_acl",
    "_ssh_fingerprint",
    "_windows_is_admin",
    "_write_approvals",
    "_write_config",
    "cmd_init",
]

logger = logging.getLogger(__name__)

# On Windows, accounts in the Administrators group use a system-wide file.
_WINDOWS_ADMIN_AK_PATH = Path("C:/ProgramData/ssh/administrators_authorized_keys")


# ---------------------------------------------------------------------------
# SSH key helpers
# ---------------------------------------------------------------------------


def _generate_keypair(keys_dir: Path) -> tuple[str, str, bytes]:
    """Generate an Ed25519 SSH keypair in *keys_dir* (idempotent).

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
        public_raw = private_key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)
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
    """Compute the SHA-256 fingerprint of an OpenSSH public key."""
    b64_part = public_openssh.decode().split()[1]
    wire_bytes = base64.b64decode(b64_part)
    digest = hashlib.sha256(wire_bytes).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


# ---------------------------------------------------------------------------
# authorized_keys helper
# ---------------------------------------------------------------------------


def _set_admin_ak_acl(path: Path) -> None:
    """Restrict *path* to SYSTEM + Administrators only via ``icacls``."""
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


def _append_authorized_keys(
    public_openssh: bytes,
    windows_admin_ak_path: Path | None = None,
) -> None:
    """Append *public_openssh* to the appropriate ``authorized_keys`` (idempotent).

    Writes to ``C:\\ProgramData\\ssh\\administrators_authorized_keys`` (or the
    config override) when running as a Windows Administrator; otherwise to
    ``~/.ssh/authorized_keys``.
    """
    use_admin_path = IS_WINDOWS and _windows_is_admin()

    if use_admin_path:
        ak_path = (
            windows_admin_ak_path if windows_admin_ak_path is not None else _WINDOWS_ADMIN_AK_PATH
        )
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
    """Write ``config.yaml`` with platform defaults. Always overwrites."""
    import yaml

    from control_station_lite.agent.config import AdvancedSection, AgentSection

    agent_defaults = AgentSection()
    advanced_defaults = AdvancedSection()
    base = CslPaths.platform_base()
    data = {
        "agent": {
            "listen_port": port,
            "idle_timeout_seconds": agent_defaults.idle_timeout_seconds,
            "lifecycle_check_interval_seconds": agent_defaults.lifecycle_check_interval_seconds,
            "log_tail_lines": agent_defaults.log_tail_lines,
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
        "advanced": {
            "windows_admin_authorized_keys_path": str(
                advanced_defaults.windows_admin_authorized_keys_path
            ),
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
# init command handler
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> None:
    """Implement ``csl-agent init``."""
    port: int = args.port
    base = CslPaths.platform_base()
    keys_dir = base / "keys"
    cfg = load_config()

    # 0 — prerequisite checks (non-blocking)
    issues: list[ReadinessIssue] = check_readiness(cfg)
    if issues:
        print("Prerequisite check results:", file=sys.stderr)
        _print_issues(issues)
        if any(i.severity == "error" for i in issues):
            print(
                "\nErrors found — run 'csl-agent setup' to attempt automatic fixes.",
                file=sys.stderr,
            )
        print("", file=sys.stderr)

    # 1 — directories
    paths = CslPaths.from_base(base)
    paths.ensure_dirs()
    keys_dir.mkdir(parents=True, exist_ok=True)
    logger.info("directories ready under %s", base)

    # 2 — SSH keypair
    private_pem, fingerprint, public_openssh = _generate_keypair(keys_dir)

    # 3 — authorized_keys
    _append_authorized_keys(
        public_openssh,
        windows_admin_ak_path=cfg.advanced.windows_admin_authorized_keys_path,
    )

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
