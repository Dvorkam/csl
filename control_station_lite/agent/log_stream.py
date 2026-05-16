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
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from fastapi.responses import StreamingResponse

__all__ = ["make_sse_response", "sse_events", "tail_log"]

# Seconds between readline() retries when no new data is available.
_DEFAULT_POLL_INTERVAL: float = 0.1

# Default number of historical lines to replay when a client connects.
# -1 means all lines (no truncation).
_DEFAULT_TAIL_LINES: int = 1000

# Chunk size for the backwards-seek scan in _tail_start_offset().
_SEEK_CHUNK: int = 8192


def _tail_start_offset(log_path: Path, tail_lines: int) -> int:
    """Return the byte offset of the first byte of the last *tail_lines* lines.

    Scans the file backwards in ``_SEEK_CHUNK``-sized binary reads, counting
    ``\\n`` bytes.  I/O cost is ``O(tail_lines × avg_line_length)``, not
    ``O(file_size)``, making it safe for week-long log files.

    A trailing ``\\n`` (the line terminator of the final line) is not counted
    as a line separator — only the ``\\n`` bytes that lie *between* lines are
    counted.  This ensures that a file with *N* complete lines and a trailing
    newline returns *N* lines, not *N − 1*.

    Returns 0 if the file has fewer than *tail_lines* lines (stream from BOF).
    """
    newlines = 0
    with open(log_path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size == 0:
            return 0

        # Skip the trailing newline (line terminator, not a separator).
        f.seek(-1, 2)
        effective_end = size - 1 if f.read(1) == b"\n" else size

        pos = effective_end
        while pos > 0:
            read_size = min(_SEEK_CHUNK, pos)
            pos -= read_size
            f.seek(pos)
            buf = f.read(read_size)

            # Only inspect bytes within [0, effective_end).
            scan_len = min(len(buf), effective_end - pos)
            for i in range(scan_len - 1, -1, -1):
                if buf[i] == ord("\n"):
                    newlines += 1
                    if newlines == tail_lines:
                        return pos + i + 1  # byte after the boundary newline

    return 0  # fewer lines than requested — start from beginning


async def tail_log(
    log_path: Path,
    *,
    is_done: Callable[[], bool],
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    tail_lines: int = _DEFAULT_TAIL_LINES,
) -> AsyncGenerator[str, None]:
    """Yield lines from *log_path* as they are written.

    Historical content is replayed first (up to *tail_lines* lines from the
    end), then new lines are streamed live until the job finishes.

    *tail_lines* controls how many existing lines are replayed on connect:

    - ``-1``: replay all existing content (use with caution on long-running jobs).
    - ``0``: skip history entirely — live output only.
    - ``N > 0``: replay the last *N* lines via a backward binary seek, then
      continue live (default 1 000).  I/O is proportional to the replayed
      content, not the total file size.

    After *is_done()* returns ``True``, one final drain cycle captures any
    lines written between the last read and process exit.

    The file is opened in **binary mode** throughout so that ``seek()``
    offsets are byte-accurate on all platforms.  Lines are decoded as UTF-8
    and stripped of trailing ``\\r\\n``.  Multiple concurrent callers each
    hold an independent file handle and are safe to run in parallel.
    """
    with open(log_path, "rb") as f:
        if tail_lines == 0:
            # Skip history: jump to EOF so only live writes are seen.
            f.seek(0, 2)
        elif tail_lines > 0:
            # Seek to the start of the last tail_lines lines.
            f.seek(_tail_start_offset(log_path, tail_lines))
        # else tail_lines == -1: file pointer stays at 0 → replay everything.

        while True:
            raw = f.readline()
            if raw:
                yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
            elif is_done():
                # Sleep once to let the OS flush any buffered writes from the
                # exiting process, then drain whatever remains.
                await asyncio.sleep(poll_interval)
                raw = f.readline()
                if raw:
                    yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    # Loop back to drain further lines if any.
                    continue
                break
            else:
                await asyncio.sleep(poll_interval)


async def sse_events(
    log_path: Path,
    *,
    is_done: Callable[[], bool],
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    tail_lines: int = _DEFAULT_TAIL_LINES,
) -> AsyncGenerator[str, None]:
    """Wrap :func:`tail_log` in SSE ``data:`` envelopes.

    Each log line becomes one ``data: <line>\\n\\n`` event.  A final
    ``event: done\\ndata:\\n\\n`` event is emitted when the stream ends so
    clients can stop listening without waiting for a reconnect timeout.
    """
    async for line in tail_log(
        log_path, is_done=is_done, poll_interval=poll_interval, tail_lines=tail_lines
    ):
        yield f"data: {line}\n\n"
    yield "event: done\ndata: \n\n"


def make_sse_response(
    log_path: Path,
    *,
    is_done: Callable[[], bool],
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    tail_lines: int = _DEFAULT_TAIL_LINES,
) -> StreamingResponse:
    """Return a ``StreamingResponse`` that tails *log_path* as SSE events.

    The response sets headers that disable caching and nginx buffering so
    events reach the browser immediately.
    """
    return StreamingResponse(
        sse_events(log_path, is_done=is_done, poll_interval=poll_interval, tail_lines=tail_lines),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
