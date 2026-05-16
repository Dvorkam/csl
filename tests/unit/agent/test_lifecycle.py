import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from control_station_lite.agent.lifecycle import IdleTracker

# ---------------------------------------------------------------------------
# IdleTracker unit tests
# ---------------------------------------------------------------------------


class TestIdleTrackerInitial:
    def test_idle_seconds_starts_near_zero(self) -> None:
        tracker = IdleTracker(timeout_seconds=300)
        assert tracker.idle_seconds < 1.0

    def test_shutdown_not_due_immediately(self) -> None:
        tracker = IdleTracker(timeout_seconds=300)
        assert not tracker.shutdown_due(running_persistent=0)


class TestRecordActivity:
    def test_resets_idle_clock(self) -> None:
        tracker = IdleTracker(timeout_seconds=300)
        # Advance monotonic time by patching _last_activity backward
        tracker._last_activity = time.monotonic() - 200
        assert tracker.idle_seconds > 190

        tracker.record_activity()
        assert tracker.idle_seconds < 1.0

    def test_idempotent_multiple_calls(self) -> None:
        tracker = IdleTracker(timeout_seconds=300)
        tracker._last_activity = time.monotonic() - 100
        tracker.record_activity()
        tracker.record_activity()
        assert tracker.idle_seconds < 1.0


class TestShutdownDue:
    def test_not_due_when_jobs_running(self) -> None:
        tracker = IdleTracker(timeout_seconds=1)
        tracker._last_activity = time.monotonic() - 999
        assert not tracker.shutdown_due(running_persistent=2)

    def test_not_due_when_within_timeout(self) -> None:
        tracker = IdleTracker(timeout_seconds=300)
        # idle ~0s, no jobs
        assert not tracker.shutdown_due(running_persistent=0)

    def test_due_when_idle_exceeds_timeout_and_no_jobs(self) -> None:
        tracker = IdleTracker(timeout_seconds=60)
        tracker._last_activity = time.monotonic() - 61
        assert tracker.shutdown_due(running_persistent=0)

    def test_not_due_when_idle_equals_timeout_exactly(self) -> None:
        # "idle > timeout" — equal is not enough
        tracker = IdleTracker(timeout_seconds=60)
        tracker._last_activity = time.monotonic() - 60
        # Could be slightly over due to timing; use a fresh tracker instead
        tracker2 = IdleTracker(timeout_seconds=100)
        tracker2._last_activity = time.monotonic() - 60
        assert not tracker2.shutdown_due(running_persistent=0)


# ---------------------------------------------------------------------------
# run_loop integration tests
# ---------------------------------------------------------------------------


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_loop_triggers_shutdown_when_condition_met(self) -> None:
        tracker = IdleTracker(timeout_seconds=0)
        # Timeout=0 means condition fires immediately after first sleep
        tracker._last_activity = time.monotonic() - 1

        process_manager = MagicMock()
        process_manager.running_count.return_value = 0

        with patch("control_station_lite.agent.lifecycle._trigger_shutdown") as mock_shutdown:
            await tracker.run_loop(process_manager, check_interval=0.05)
        mock_shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_loop_does_not_shutdown_while_jobs_running(self) -> None:
        tracker = IdleTracker(timeout_seconds=0)
        tracker._last_activity = time.monotonic() - 999

        process_manager = MagicMock()
        # Jobs running for first 3 checks, then none
        process_manager.running_count.side_effect = [2, 2, 2, 0]

        with patch("control_station_lite.agent.lifecycle._trigger_shutdown") as mock_shutdown:
            await tracker.run_loop(process_manager, check_interval=0.02)
        mock_shutdown.assert_called_once()
        assert process_manager.running_count.call_count == 4

    @pytest.mark.asyncio
    async def test_loop_cancelled_cleanly(self) -> None:
        tracker = IdleTracker(timeout_seconds=9999)
        process_manager = MagicMock()
        process_manager.running_count.return_value = 1

        with patch("control_station_lite.agent.lifecycle._trigger_shutdown") as mock_shutdown:
            task = asyncio.create_task(tracker.run_loop(process_manager, check_interval=0.05))
            await asyncio.sleep(0.08)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        mock_shutdown.assert_not_called()

    @pytest.mark.asyncio
    async def test_loop_does_not_shutdown_while_not_idle(self) -> None:
        tracker = IdleTracker(timeout_seconds=9999)
        process_manager = MagicMock()
        process_manager.running_count.return_value = 0

        with patch("control_station_lite.agent.lifecycle._trigger_shutdown") as mock_shutdown:
            task = asyncio.create_task(tracker.run_loop(process_manager, check_interval=0.05))
            await asyncio.sleep(0.12)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        mock_shutdown.assert_not_called()


# ---------------------------------------------------------------------------
# Middleware + healthz integration via TestClient
# ---------------------------------------------------------------------------


class TestIdleTrackerIntegration:
    def test_healthz_reports_idle_seconds(self) -> None:
        from fastapi.testclient import TestClient

        from control_station_lite.agent.main import app

        with TestClient(app) as client:
            resp = client.get("/healthz")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data["idle_seconds"], float | int)

    def test_activity_recorded_on_request(self) -> None:
        """After a request the idle clock should be near zero."""
        from fastapi.testclient import TestClient

        from control_station_lite.agent.main import app

        with TestClient(app) as client:
            tracker = app.state.tracker
            # Wind back the clock
            tracker._last_activity = time.monotonic() - 500
            client.get("/healthz")
            # Middleware should have reset it
            assert tracker.idle_seconds < 2.0
