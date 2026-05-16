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

import json
import logging
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

__all__ = ["JobEntry", "load_running_state", "save_running_state"]

logger = logging.getLogger(__name__)


class JobEntry(BaseModel):
    """One persistent job entry as persisted in ``running.json``."""

    script_name: str
    pid: int
    log_path: Path
    started_at: datetime


class _RunningState(BaseModel):
    jobs: dict[str, JobEntry] = Field(default_factory=dict)


def save_running_state(state_path: Path, jobs: dict[str, JobEntry]) -> None:
    """Atomically write *jobs* to *state_path* (``running.json``).

    Only running jobs should be passed; the file always reflects the set of
    jobs that were alive at the time of the last write.
    """
    state = _RunningState(jobs=jobs)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(state_path)
    logger.debug("saved running state: %d job(s)", len(jobs))


def load_running_state(state_path: Path) -> dict[str, JobEntry]:
    """Read ``running.json`` and return its job entries.

    Returns an empty dict if the file does not exist or cannot be parsed.
    Corrupt state is logged as a warning and treated as empty.
    """
    if not state_path.exists():
        return {}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        state = _RunningState.model_validate(raw)
        return state.jobs
    except Exception as exc:
        logger.warning("could not load running.json, starting fresh: %s", exc)
        return {}
