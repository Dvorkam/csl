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

import functools
import os
from dataclasses import dataclass
from pathlib import Path

from control_station_lite.shared.platform_info import IS_WINDOWS

__all__ = ["CslPaths"]


@dataclass(frozen=True)
class CslPaths:
    """All filesystem paths used by one agent installation.

    Pass a single ``CslPaths`` instance into managers instead of threading
    individual ``Path`` arguments through every constructor.

    Platform detection lives here so it is not duplicated across config.py,
    CLI code, and other call sites.  Use :meth:`platform_default` to get
    a fully-populated instance for the current OS without parsing any file.
    """

    scripts_dir: Path
    pending_dir: Path
    logs_dir: Path
    approvals_path: Path
    state_path: Path

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    @functools.cache
    def platform_base(cls) -> Path:
        """Return the platform-appropriate root directory for agent data.

        Result is cached — the platform does not change while the process runs.

        - Linux / macOS: ``~/.csl/``
        - Windows: ``%USERPROFILE%\\.csl\\``  (home-dir equivalent, mirrors Linux)
        """
        if IS_WINDOWS:
            userprofile = os.environ.get("USERPROFILE", "")
            return Path(userprofile) / ".csl"
        return Path.home() / ".csl"

    @classmethod
    def from_base(cls, base: Path) -> CslPaths:
        """Derive standard paths from a ``~/.csl/`` root directory."""
        return cls(
            scripts_dir=base / "scripts",
            pending_dir=base / "scripts.pending",
            logs_dir=base / "logs",
            approvals_path=base / "agent" / "approvals.json",
            state_path=base / "agent" / "running.json",
        )

    @classmethod
    @functools.cache
    def platform_default(cls) -> CslPaths:
        """Return a ``CslPaths`` rooted at the platform default base directory.

        Result is cached — equivalent to calling ``from_base(platform_base())``.
        """
        return cls.from_base(cls.platform_base())

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def ensure_dirs(self) -> None:
        """Create every directory that must exist before the agent starts."""
        for directory in (
            self.scripts_dir,
            self.pending_dir,
            self.logs_dir,
            self.approvals_path.parent,
            self.state_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)
