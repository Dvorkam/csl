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

"""Audit-log helper.

A single place to record state-mutating actions so every endpoint writes
`AuditLog` rows with a consistent shape. The route-coverage guard test
(`tests/unit/server/test_audit_coverage.py`) asserts that every mutating route
calls :func:`record_audit`.
"""

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.db.models import AuditLog

logger = logging.getLogger(__name__)


async def record_audit(
    session: AsyncSession,
    *,
    action: str,
    target_type: str,
    target_id: str | int | None,
    result: str = "success",
    user_id: int | None = None,
    details: dict[str, Any] | None = None,
    commit: bool = False,
) -> None:
    """Add an :class:`AuditLog` row to *session*.

    By default the row is only flushed, so it joins the caller's transaction and
    is persisted by the caller's own ``session.commit()``. Pass ``commit=True``
    on failure paths that raise before any other commit, so the audit trail
    survives the error.

    *details* is JSON-serialised; keep it to plain, non-secret values.
    """
    entry = AuditLog(
        timestamp=datetime.utcnow(),
        user_id=user_id,
        action=action,
        target_type=target_type,
        target_id="" if target_id is None else str(target_id),
        result=result,
        details_json=json.dumps(details) if details else None,
    )
    session.add(entry)
    if commit:
        await session.commit()
    else:
        await session.flush()
    logger.info(
        "audit action=%s target=%s/%s result=%s user=%s",
        action,
        target_type,
        entry.target_id,
        result,
        user_id,
    )
