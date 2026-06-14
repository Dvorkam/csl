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

"""Built-in script catalogue: discovery and idempotent seeding (Phase 11).

Built-in scripts ship inside the package under ``server/builtin_scripts/`` as
platform files (``<name>.sh`` / ``<name>.ps1``) plus a shared per-name metadata
file (``<name>.meta.yaml``). Each platform file becomes one row in the ``scripts``
table — its name carries the extension so the agent resolves the right
interpreter — and both variants of a cross-platform script share the same
metadata.

Seeding is **create-if-absent**: a script that already exists in the library is
left untouched, so re-running never clobbers an admin's edits. Shipping a newer
version of a built-in therefore does not auto-update an existing install.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.core.script_registry import create_script
from control_station_lite.server.db.models import Script

__all__ = [
    "BUILTIN_SCRIPTS_DIR",
    "BuiltinScript",
    "SeedResult",
    "iter_builtin_scripts",
    "seed_builtin_scripts",
]

logger = logging.getLogger(__name__)

BUILTIN_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "builtin_scripts"

# Platform script extensions (the agent picks the interpreter from these).
_SCRIPT_EXTENSIONS = (".sh", ".ps1")


@dataclass(frozen=True)
class BuiltinScript:
    """One seedable script: ``name`` carries its extension (e.g. ``sleep_machine.ps1``)."""

    name: str
    content: str
    meta_yaml: str | None


@dataclass(frozen=True)
class SeedResult:
    """Outcome of a seeding run."""

    created: list[str]
    skipped: list[str]


def iter_builtin_scripts(directory: Path | None = None) -> list[BuiltinScript]:
    """Discover packaged built-in scripts, sorted by name.

    A script file ``<name>.<ext>`` is paired with the shared metadata file
    ``<name>.meta.yaml`` when present (``<name>`` is the stem without the
    platform extension).
    """
    base = directory or BUILTIN_SCRIPTS_DIR
    scripts: list[BuiltinScript] = []
    for path in sorted(base.iterdir() if base.is_dir() else []):
        if path.suffix not in _SCRIPT_EXTENSIONS or not path.is_file():
            continue
        meta_path = base / f"{path.stem}.meta.yaml"
        meta_yaml = meta_path.read_text(encoding="utf-8") if meta_path.is_file() else None
        scripts.append(
            BuiltinScript(
                name=path.name,
                content=path.read_text(encoding="utf-8"),
                meta_yaml=meta_yaml,
            )
        )
    return scripts


async def seed_builtin_scripts(
    session: AsyncSession,
    *,
    user_id: int,
    directory: Path | None = None,
) -> SeedResult:
    """Insert any missing built-in scripts; never modify existing rows.

    The caller owns the transaction (this flushes but does not commit). Returns
    the names created and the names skipped because they already exist.
    """
    created: list[str] = []
    skipped: list[str] = []
    for builtin in iter_builtin_scripts(directory):
        existing = await session.execute(select(Script.id).where(Script.name == builtin.name))
        if existing.scalar_one_or_none() is not None:
            skipped.append(builtin.name)
            continue
        await create_script(
            name=builtin.name,
            content=builtin.content,
            meta_yaml=builtin.meta_yaml,
            user_id=user_id,
            session=session,
        )
        created.append(builtin.name)
    logger.info(
        "seeded built-in scripts: %d created, %d already present",
        len(created),
        len(skipped),
    )
    return SeedResult(created=created, skipped=skipped)
