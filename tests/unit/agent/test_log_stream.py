"""Tests for agent/log_stream.py.

Structure:
  TestTailLog                 — cross-platform: core tailing logic
  TestSseEvents               — cross-platform: SSE envelope formatting
  TestMakeSseResponse         — cross-platform: response headers and media type
"""

import asyncio
from pathlib import Path

from control_station_lite.agent.log_stream import (
    _tail_start_offset,
    make_sse_response,
    sse_events,
    tail_log,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _collect(gen) -> list[str]:  # type: ignore[type-arg]
    """Drain an async generator into a list."""
    result = []
    async for item in gen:
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# TestTailStartOffset — cross-platform (unit test for the binary seek helper)
# ---------------------------------------------------------------------------


class TestTailStartOffset:
    def test_returns_zero_for_fewer_lines_than_requested(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_bytes(b"a\nb\n")
        assert _tail_start_offset(log, 10) == 0

    def test_returns_zero_for_empty_file(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_bytes(b"")
        assert _tail_start_offset(log, 5) == 0

    def test_exact_boundary_for_last_two_lines(self, tmp_path: Path) -> None:
        # "line0\nline1\nline2\n" — asking for 2 should start at "line1\n"
        log = tmp_path / "job.log"
        log.write_bytes(b"line0\nline1\nline2\n")
        offset = _tail_start_offset(log, 2)
        with open(log, "rb") as f:
            f.seek(offset)
            assert f.read() == b"line1\nline2\n"

    def test_offset_zero_when_requesting_all_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_bytes(b"a\nb\nc\n")
        assert _tail_start_offset(log, 3) == 0

    def test_works_across_chunk_boundary(self, tmp_path: Path) -> None:
        """Boundary newline split across two 8 192-byte chunks must be found."""
        # Write enough data to force multiple chunk reads.
        lines = [f"line{i:05d}" for i in range(2000)]
        content = "\n".join(lines) + "\n"
        log = tmp_path / "job.log"
        log.write_bytes(content.encode())
        offset = _tail_start_offset(log, 100)
        with open(log, "rb") as f:
            f.seek(offset)
            remaining = f.read().decode()
        yielded = [ln for ln in remaining.splitlines() if ln]
        assert len(yielded) == 100
        assert yielded[0] == "line01900"
        assert yielded[-1] == "line01999"


# ---------------------------------------------------------------------------
# TestTailLog — cross-platform
# ---------------------------------------------------------------------------


class TestTailLog:
    async def test_yields_existing_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("line1\nline2\nline3\n", encoding="utf-8")

        lines = await _collect(tail_log(log, is_done=lambda: True, poll_interval=0.01))
        assert lines == ["line1", "line2", "line3"]

    async def test_empty_file_is_done(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("", encoding="utf-8")

        lines = await _collect(tail_log(log, is_done=lambda: True, poll_interval=0.01))
        assert lines == []

    async def test_strips_trailing_newline(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("hello\n", encoding="utf-8")

        lines = await _collect(tail_log(log, is_done=lambda: True, poll_interval=0.01))
        assert lines == ["hello"]

    async def test_strips_crlf(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_bytes(b"hello\r\nworld\r\n")

        lines = await _collect(tail_log(log, is_done=lambda: True, poll_interval=0.01))
        assert lines == ["hello", "world"]

    async def test_preserves_line_content(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("  spaces  \ttabs\t\n", encoding="utf-8")

        lines = await _collect(tail_log(log, is_done=lambda: True, poll_interval=0.01))
        assert lines == ["  spaces  \ttabs\t"]

    async def test_tails_live_writes(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("", encoding="utf-8")

        done = asyncio.Event()

        async def writer() -> None:
            await asyncio.sleep(0.05)
            with open(log, "a", encoding="utf-8") as f:
                f.write("line1\n")
            await asyncio.sleep(0.05)
            with open(log, "a", encoding="utf-8") as f:
                f.write("line2\n")
            await asyncio.sleep(0.05)
            done.set()

        lines: list[str] = []

        async def reader() -> None:
            async for line in tail_log(log, is_done=done.is_set, poll_interval=0.01):
                lines.append(line)

        await asyncio.gather(writer(), reader())
        assert lines == ["line1", "line2"]

    async def test_drains_final_write_after_done(self, tmp_path: Path) -> None:
        """Lines written just before process exit must not be lost."""
        log = tmp_path / "job.log"
        log.write_text("", encoding="utf-8")

        done = asyncio.Event()

        async def writer() -> None:
            await asyncio.sleep(0.05)
            # Write and signal done almost simultaneously.
            with open(log, "a", encoding="utf-8") as f:
                f.write("final_line\n")
            done.set()

        lines: list[str] = []

        async def reader() -> None:
            async for line in tail_log(log, is_done=done.is_set, poll_interval=0.01):
                lines.append(line)

        await asyncio.gather(writer(), reader())
        assert "final_line" in lines

    async def test_multiple_subscribers_independent(self, tmp_path: Path) -> None:
        """Two concurrent readers each receive the full log independently."""
        log = tmp_path / "job.log"
        log.write_text("a\nb\nc\n", encoding="utf-8")

        lines_x: list[str] = []
        lines_y: list[str] = []

        async def read_x() -> None:
            async for line in tail_log(log, is_done=lambda: True, poll_interval=0.01):
                lines_x.append(line)

        async def read_y() -> None:
            async for line in tail_log(log, is_done=lambda: True, poll_interval=0.01):
                lines_y.append(line)

        await asyncio.gather(read_x(), read_y())
        assert lines_x == ["a", "b", "c"]
        assert lines_y == ["a", "b", "c"]

    async def test_stops_when_done_and_no_new_data(self, tmp_path: Path) -> None:
        """Generator terminates rather than hanging when is_done() is True."""
        log = tmp_path / "job.log"
        log.write_text("only\n", encoding="utf-8")

        lines = await _collect(tail_log(log, is_done=lambda: True, poll_interval=0.01))
        assert lines == ["only"]

    # ------------------------------------------------------------------
    # tail_lines parameter
    # ------------------------------------------------------------------

    async def test_tail_lines_minus_one_yields_all(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("".join(f"line{i}\n" for i in range(10)), encoding="utf-8")

        lines = await _collect(
            tail_log(log, is_done=lambda: True, poll_interval=0.01, tail_lines=-1)
        )
        assert lines == [f"line{i}" for i in range(10)]

    async def test_tail_lines_zero_skips_history(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("old_line\n", encoding="utf-8")

        # is_done immediately True → nothing new will arrive, so zero lines.
        lines = await _collect(
            tail_log(log, is_done=lambda: True, poll_interval=0.01, tail_lines=0)
        )
        assert lines == []

    async def test_tail_lines_limits_history(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("".join(f"line{i}\n" for i in range(20)), encoding="utf-8")

        lines = await _collect(
            tail_log(log, is_done=lambda: True, poll_interval=0.01, tail_lines=5)
        )
        assert lines == [f"line{i}" for i in range(15, 20)]

    async def test_tail_lines_larger_than_file_yields_all(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("a\nb\nc\n", encoding="utf-8")

        lines = await _collect(
            tail_log(log, is_done=lambda: True, poll_interval=0.01, tail_lines=100)
        )
        assert lines == ["a", "b", "c"]

    async def test_tail_lines_still_receives_live_output(self, tmp_path: Path) -> None:
        """tail_lines affects history only; live writes after connect are always received."""
        log = tmp_path / "job.log"
        log.write_text("".join(f"old{i}\n" for i in range(10)), encoding="utf-8")

        done = asyncio.Event()

        async def writer() -> None:
            await asyncio.sleep(0.05)
            with open(log, "a", encoding="utf-8") as f:
                f.write("new_line\n")
            await asyncio.sleep(0.05)
            done.set()

        lines: list[str] = []

        async def reader() -> None:
            async for line in tail_log(log, is_done=done.is_set, poll_interval=0.01, tail_lines=2):
                lines.append(line)

        await asyncio.gather(writer(), reader())
        # History: last 2 old lines; live: new_line.
        assert lines[:2] == ["old8", "old9"]
        assert "new_line" in lines


# ---------------------------------------------------------------------------
# TestSseEvents — cross-platform
# ---------------------------------------------------------------------------


class TestSseEvents:
    async def test_wraps_lines_in_data_envelope(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("hello\nworld\n", encoding="utf-8")

        events = await _collect(sse_events(log, is_done=lambda: True, poll_interval=0.01))
        assert "data: hello\n\n" in events
        assert "data: world\n\n" in events

    async def test_emits_done_event_at_end(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("", encoding="utf-8")

        events = await _collect(sse_events(log, is_done=lambda: True, poll_interval=0.01))
        assert events[-1] == "event: done\ndata: \n\n"

    async def test_done_event_is_last(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("a\nb\n", encoding="utf-8")

        events = await _collect(sse_events(log, is_done=lambda: True, poll_interval=0.01))
        assert events[-1].startswith("event: done")
        assert all(e.startswith("data:") for e in events[:-1])


# ---------------------------------------------------------------------------
# TestMakeSseResponse — cross-platform
# ---------------------------------------------------------------------------


class TestMakeSseResponse:
    def test_media_type_is_event_stream(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("", encoding="utf-8")

        resp = make_sse_response(log, is_done=lambda: True)
        assert resp.media_type == "text/event-stream"

    def test_cache_control_no_cache(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("", encoding="utf-8")

        resp = make_sse_response(log, is_done=lambda: True)
        assert resp.headers["cache-control"] == "no-cache"

    def test_nginx_buffering_disabled(self, tmp_path: Path) -> None:
        log = tmp_path / "job.log"
        log.write_text("", encoding="utf-8")

        resp = make_sse_response(log, is_done=lambda: True)
        assert resp.headers["x-accel-buffering"] == "no"
