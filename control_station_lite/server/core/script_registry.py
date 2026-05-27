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

"""CRUD helpers for the scripts table.

All functions accept an :class:`AsyncSession` and are transaction-aware:
callers are responsible for ``commit``/``rollback``.  MD5 is always
recomputed from *content* — callers must not supply it separately.
"""

import hashlib
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.db.models import Script, ScriptTargetState
from control_station_lite.shared.script_meta import ScriptMetaError, parse_meta_yaml

__all__ = [
    "ScriptRegistryError",
    "create_script",
    "delete_script",
    "get_script",
    "get_script_or_raise",
    "list_scripts",
    "update_script",
]


class ScriptRegistryError(ValueError):
    """Raised for business-rule violations (duplicate name, bad metadata, etc.)."""


def _compute_md5(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()


def _validate_meta(meta_yaml: str | None) -> None:
    if meta_yaml is None:
        return
    try:
        parse_meta_yaml(meta_yaml)
    except ScriptMetaError as exc:
        raise ScriptRegistryError(f"invalid meta YAML: {exc}") from exc


def _is_persistent(meta_yaml: str | None) -> bool:
    if not meta_yaml:
        return False
    try:
        return parse_meta_yaml(meta_yaml).persistent
    except ScriptMetaError:
        return False


async def create_script(
    *,
    name: str,
    content: str,
    meta_yaml: str | None,
    user_id: int,
    session: AsyncSession,
) -> Script:
    """Insert a new script row.

    Raises :class:`ScriptRegistryError` on duplicate name or invalid metadata.
    """
    existing = await session.execute(select(Script).where(Script.name == name))
    if existing.scalar_one_or_none() is not None:
        raise ScriptRegistryError(f"script {name!r} already exists")
    _validate_meta(meta_yaml)
    script = Script(
        name=name,
        content=content,
        meta_yaml=meta_yaml,
        md5=_compute_md5(content),
        persistent=_is_persistent(meta_yaml),
        updated_at=datetime.utcnow(),
        updated_by=user_id,
    )
    session.add(script)
    return script


async def get_script(name: str, session: AsyncSession) -> Script | None:
    result = await session.execute(select(Script).where(Script.name == name))
    return result.scalar_one_or_none()


async def get_script_or_raise(name: str, session: AsyncSession) -> Script:
    script = await get_script(name, session)
    if script is None:
        raise ScriptRegistryError(f"script {name!r} not found")
    return script


async def list_scripts(session: AsyncSession) -> list[Script]:
    result = await session.execute(select(Script).order_by(Script.name))
    return list(result.scalars().all())


async def update_script(
    *,
    name: str,
    content: str,
    meta_yaml: str | None,
    user_id: int,
    session: AsyncSession,
) -> Script:
    """Update content and/or metadata; recomputes MD5.

    Raises :class:`ScriptRegistryError` if the script does not exist or if
    the new metadata YAML is invalid.
    """
    script = await get_script_or_raise(name, session)
    _validate_meta(meta_yaml)
    script.content = content
    script.meta_yaml = meta_yaml
    script.md5 = _compute_md5(content)
    script.persistent = _is_persistent(meta_yaml)
    script.updated_at = datetime.utcnow()
    script.updated_by = user_id
    return script


async def delete_script(name: str, session: AsyncSession) -> None:
    """Delete a script and its per-machine state cache rows.

    Raises :class:`ScriptRegistryError` if the script does not exist.
    """
    script = await get_script_or_raise(name, session)
    await session.execute(delete(ScriptTargetState).where(ScriptTargetState.script_id == script.id))
    await session.delete(script)
