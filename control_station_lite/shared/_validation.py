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

import copy
import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

_M = TypeVar("_M", bound=BaseModel)

logger = logging.getLogger(__name__)


def validate_stripping_unknowns(
    model: type[_M],
    raw: dict[str, object],
    *,
    log_prefix: str = "unknown field will be ignored",
) -> _M:
    """Validate *raw* against *model*, warning on unknown fields instead of crashing.

    Unknown fields (extra_forbidden errors) are logged as warnings and stripped;
    the cleaned dict is then re-validated to produce a model instance.  All other
    validation errors raise the original ValidationError unchanged.

    The returned model is a sanitized version of the input — unknown keys are
    absent from the result even if they were present in *raw*.
    """
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors()
        extra = [e for e in errors if e["type"] == "extra_forbidden"]
        real = [e for e in errors if e["type"] != "extra_forbidden"]

        for e in extra:
            loc = " -> ".join(str(p) for p in e["loc"])
            logger.warning("%s: %s", log_prefix, loc)

        if real:
            raise

        # All errors were extra-field violations.  Strip them and re-validate to
        # obtain a model instance — model_validate either returns a model or raises,
        # never both, so we cannot skip this second call.
        cleaned = copy.deepcopy(raw)
        for e in extra:
            _delete_at_path(cleaned, e["loc"])
        return model.model_validate(cleaned)


def _delete_at_path(obj: object, path: tuple[str | int, ...]) -> None:
    """Delete the value at *path* from a nested dict/list structure in-place."""
    for part in path[:-1]:
        obj = obj[part]  # type: ignore[index]
    last = path[-1]
    if isinstance(obj, dict):
        obj.pop(last, None)
