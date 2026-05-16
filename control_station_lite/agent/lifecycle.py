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

import asyncio
import logging
import os
import signal
import threading
import time
from typing import Protocol

__all__ = ["IdleTracker"]


class _HasRunningCount(Protocol):
    def running_count(self) -> int: ...


def _trigger_shutdown() -> None:
    """Send SIGTERM to ourselves so uvicorn's graceful-shutdown path fires."""
    os.kill(os.getpid(), signal.SIGTERM)


logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 10


class IdleTracker:
    """Tracks request activity and triggers agent self-shutdown when idle.

    The tracker records the monotonic timestamp of the last client request.
    A background asyncio task periodically checks whether the shutdown
    condition is met: no running persistent jobs AND idle time exceeds the
    configured timeout.

    Usage in a FastAPI lifespan::

        tracker = IdleTracker(timeout_seconds=cfg.agent.idle_timeout_seconds)
        app.state.tracker = tracker
        task = asyncio.create_task(tracker.run_loop(process_manager))
        yield
        task.cancel()
    """

    def __init__(self, timeout_seconds: int) -> None:
        self._timeout = timeout_seconds
        self._last_activity = time.monotonic()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_activity(self) -> None:
        """Reset the idle clock. Call this on every incoming HTTP request."""
        with self._lock:
            self._last_activity = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        """Seconds elapsed since the last recorded client request."""
        with self._lock:
            return time.monotonic() - self._last_activity

    def shutdown_due(self, running_persistent: int) -> bool:
        """Return True when the shutdown condition is met.

        The agent self-terminates when there are no running persistent jobs
        and the idle timer has exceeded the configured timeout.
        """
        return running_persistent == 0 and self.idle_seconds > self._timeout

    async def run_loop(
        self,
        process_manager: _HasRunningCount,
        check_interval: float = _CHECK_INTERVAL_SECONDS,
    ) -> None:
        """Background task: poll the shutdown condition and signal self if met.

        *process_manager* is any object with a ``running_count() -> int``
        method (i.e. ``ProcessManager``).  Uses ``_HasRunningCount`` Protocol
        to avoid a circular import.

        Shutdown is triggered by raising ``SIGTERM`` in the current process so
        that uvicorn's graceful-shutdown path fires exactly as it would for an
        operator-issued ``kill``.
        """
        logger.info(
            "lifecycle: idle shutdown enabled, timeout=%ds, check_interval=%ds",
            self._timeout,
            check_interval,
        )
        while True:
            await asyncio.sleep(check_interval)
            running = process_manager.running_count()
            idle = self.idle_seconds
            logger.debug(
                "lifecycle: running_persistent=%d idle=%.1fs timeout=%ds",
                running,
                idle,
                self._timeout,
            )
            if self.shutdown_due(running):
                logger.info(
                    "lifecycle: idle shutdown triggered"
                    " (running_persistent=%d idle=%.1fs timeout=%ds)",
                    running,
                    idle,
                    self._timeout,
                )
                _trigger_shutdown()
                return
