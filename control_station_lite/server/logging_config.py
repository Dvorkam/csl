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

"""Structured JSON logging for the control station (ARCHITECTURE §10).

One JSON object per line to stdout, collected by Docker / the systemd journal.
The active request's correlation id (see ``middleware.py``) is attached to every
record via :data:`request_id_var`.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

# HTTP header carrying the correlation id, both inbound (client-supplied) and
# outbound (responses, and propagated to agent calls).
REQUEST_ID_HEADER = "X-Request-ID"

# Correlation id of the request currently being served, or None outside a
# request (background tasks, startup). Populated by RequestIdMiddleware.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# LogRecord attributes that are always present; anything else passed via
# ``logger.info(..., extra={...})`` is emitted as a top-level JSON field.
_RESERVED = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class JSONFormatter(logging.Formatter):
    """Render a :class:`logging.LogRecord` as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Surface structured extras the caller attached via ``extra={...}``.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger, writing to stdout.

    Call once at process start, before ``uvicorn.run(..., log_config=None)`` so
    uvicorn does not overwrite the handler. Idempotent: replaces existing root
    handlers rather than stacking another one.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Route uvicorn's own loggers through the root handler instead of their
    # private formatters, so access/error lines are JSON too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        ulog = logging.getLogger(name)
        for existing in list(ulog.handlers):
            ulog.removeHandler(existing)
        ulog.propagate = True
