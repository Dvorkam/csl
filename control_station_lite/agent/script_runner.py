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

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from control_station_lite.agent.approvals import ApprovalsManager
from control_station_lite.shared.models import ApprovalState
from control_station_lite.shared.platform_info import IS_WINDOWS

__all__ = [
    "ScriptIntegrityError",
    "ScriptNotApprovedError",
    "ScriptNotFoundError",
    "ScriptResult",
    "build_command",
    "build_env",
    "file_md5",
    "find_script",
    "run_script",
    "verify_script_integrity",
]

logger = logging.getLogger(__name__)
_audit = logging.getLogger("csl.agent.audit")

# Platform-specific ordered candidate extensions.  The first match wins.
_LINUX_EXTENSIONS = (".sh", ".bash", "")
_WINDOWS_EXTENSIONS = (".ps1", ".bat", ".cmd", "")


class ScriptNotApprovedError(RuntimeError):
    """Raised when a script is not in the approved state."""


class ScriptNotFoundError(FileNotFoundError):
    """Raised when no executable script file can be found for the given name."""


class ScriptIntegrityError(RuntimeError):
    """Raised when an on-disk script's MD5 does not match its approved MD5."""


@dataclass
class ScriptResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = field(default=False)


def run_script(
    name: str,
    params: dict[str, str | int | float | bool],
    approvals: ApprovalsManager,
    scripts_dir: Path,
    *,
    timeout: float | None = None,
) -> ScriptResult:
    """Execute the approved script *name* with *params* as ``CSL_PARAM_*`` env vars.

    Raises:
        ScriptNotApprovedError: if the script's current approval state is not ``approved``.
        ScriptNotFoundError: if no script file exists for *name* in *scripts_dir*.
    """
    descriptor = approvals.get_state(name)
    if descriptor.state != ApprovalState.approved:
        raise ScriptNotApprovedError(
            f"refusing to run '{name}': approval state is '{descriptor.state}' (must be approved)"
        )

    script_path = find_script(name, scripts_dir)
    verify_script_integrity(name, script_path, descriptor.approved_md5)
    command = build_command(script_path)
    env = build_env(params)

    logger.info("running script '%s' via %s", name, command[0])

    try:
        proc = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return ScriptResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("script '%s' timed out after %s seconds", name, timeout)
        return ScriptResult(
            exit_code=-1,
            stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
            stderr=exc.stderr or "" if isinstance(exc.stderr, str) else "",
            timed_out=True,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def file_md5(path: Path) -> str:
    """MD5 of a script file, newline-normalised to match the canonical MD5.

    Reading with universal newlines collapses any CRLF written on Windows back
    to ``\\n``, so the digest matches ``md5(content.encode())`` computed by the
    control station from the canonical LF content.
    """
    return hashlib.md5(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def verify_script_integrity(name: str, script_path: Path, approved_md5: str | None) -> None:
    """Refuse to run if the on-disk script no longer matches its approved MD5.

    Approval is bound to a specific MD5 (ARCHITECTURE §7.4): this enforces that
    binding at execution time, not only when the script was approved. A mismatch
    means the approved file changed on disk outside the approval flow.
    """
    if approved_md5 is None:
        return
    actual = file_md5(script_path)
    if actual != approved_md5:
        _audit.warning(
            "action=integrity_violation script=%s approved_md5=%s actual_md5=%s",
            name,
            approved_md5,
            actual,
        )
        raise ScriptIntegrityError(
            f"refusing to run '{name}': on-disk MD5 {actual} != approved {approved_md5}"
        )


def find_script(name: str, scripts_dir: Path) -> Path:
    """Locate the script file for *name*, trying platform-appropriate extensions."""
    extensions = _WINDOWS_EXTENSIONS if IS_WINDOWS else _LINUX_EXTENSIONS
    for ext in extensions:
        candidate = scripts_dir / f"{name}{ext}"
        if candidate.exists():
            return candidate
    raise ScriptNotFoundError(
        f"no script file found for '{name}' in {scripts_dir} (tried extensions: {extensions})"
    )


def build_command(script_path: Path) -> list[str]:
    """Return the shell command list to execute *script_path*."""
    suffix = script_path.suffix.lower()
    if suffix in (".sh", ".bash"):
        return ["bash", str(script_path)]
    if suffix == ".ps1":
        # -ExecutionPolicy Bypass is intentional: scripts are already human-approved via
        # `csl-agent approvals approve` before reaching this point, so the system execution
        # policy adds no additional security value and would silently block execution on
        # machines with a Restricted default policy.
        return [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-NoProfile",
            "-File",
            str(script_path),
        ]
    if suffix in (".bat", ".cmd"):
        return ["cmd", "/c", str(script_path)]
    # No extension: default to bash on Linux/macOS.  Windows requires an explicit extension.
    if IS_WINDOWS:
        raise ScriptNotFoundError(
            f"cannot determine interpreter for '{script_path}' on Windows"
            f" — script must have a .ps1, .bat, or .cmd extension"
        )
    return ["bash", str(script_path)]


def build_env(params: dict[str, str | int | float | bool]) -> dict[str, str]:
    """Build an env dict with current environment plus ``CSL_PARAM_*`` entries."""
    env = os.environ.copy()
    for key, value in params.items():
        env[f"CSL_PARAM_{key.upper()}"] = str(value)
    return env
