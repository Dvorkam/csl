"""Tests for agent/state.py.

Structure:
  TestSaveRunningState    — cross-platform: atomic write, content, empty dict
  TestLoadRunningState    — cross-platform: round-trip, missing file, corrupt JSON
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from control_station_lite.agent.state import JobEntry, load_running_state, save_running_state


def _entry(pid: int = 12345, script: str = "llama") -> JobEntry:
    return JobEntry(
        script_name=script,
        pid=pid,
        log_path=Path("/tmp/job.log"),
        started_at=datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# TestSaveRunningState
# ---------------------------------------------------------------------------


class TestSaveRunningState:
    def test_creates_file(self, tmp_path: Path) -> None:
        state_path = tmp_path / "agent" / "running.json"
        save_running_state(state_path, {"uuid-1": _entry()})
        assert state_path.exists()

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        state_path = tmp_path / "deep" / "nested" / "running.json"
        save_running_state(state_path, {})
        assert state_path.exists()

    def test_no_tmp_file_left_behind(self, tmp_path: Path) -> None:
        state_path = tmp_path / "running.json"
        save_running_state(state_path, {"u": _entry()})
        assert not state_path.with_suffix(".tmp").exists()

    def test_content_is_valid_json(self, tmp_path: Path) -> None:
        state_path = tmp_path / "running.json"
        save_running_state(state_path, {"u": _entry()})
        raw = json.loads(state_path.read_text())
        assert "jobs" in raw

    def test_empty_dict_writes_no_jobs(self, tmp_path: Path) -> None:
        state_path = tmp_path / "running.json"
        save_running_state(state_path, {})
        raw = json.loads(state_path.read_text())
        assert raw["jobs"] == {}

    def test_multiple_jobs_all_written(self, tmp_path: Path) -> None:
        state_path = tmp_path / "running.json"
        save_running_state(
            state_path,
            {"a": _entry(pid=1), "b": _entry(pid=2)},
        )
        raw = json.loads(state_path.read_text())
        assert set(raw["jobs"]) == {"a", "b"}

    def test_pid_and_script_name_round_trip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "running.json"
        save_running_state(state_path, {"u": _entry(pid=9999, script="my_script")})
        raw = json.loads(state_path.read_text())
        assert raw["jobs"]["u"]["pid"] == 9999
        assert raw["jobs"]["u"]["script_name"] == "my_script"


# ---------------------------------------------------------------------------
# TestLoadRunningState
# ---------------------------------------------------------------------------


class TestLoadRunningState:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_running_state(tmp_path / "no_such_file.json") == {}

    def test_corrupt_json_returns_empty(self, tmp_path: Path) -> None:
        state_path = tmp_path / "running.json"
        state_path.write_text("not valid json{{{")
        assert load_running_state(state_path) == {}

    def test_round_trip_preserves_job_uuid(self, tmp_path: Path) -> None:
        state_path = tmp_path / "running.json"
        save_running_state(state_path, {"my-uuid": _entry()})
        result = load_running_state(state_path)
        assert "my-uuid" in result

    def test_round_trip_preserves_pid(self, tmp_path: Path) -> None:
        state_path = tmp_path / "running.json"
        save_running_state(state_path, {"u": _entry(pid=4242)})
        assert load_running_state(state_path)["u"].pid == 4242

    def test_round_trip_preserves_script_name(self, tmp_path: Path) -> None:
        state_path = tmp_path / "running.json"
        save_running_state(state_path, {"u": _entry(script="llama_server")})
        assert load_running_state(state_path)["u"].script_name == "llama_server"

    def test_round_trip_preserves_log_path(self, tmp_path: Path) -> None:
        state_path = tmp_path / "running.json"
        entry = _entry()
        save_running_state(state_path, {"u": entry})
        assert load_running_state(state_path)["u"].log_path == entry.log_path

    def test_round_trip_preserves_started_at(self, tmp_path: Path) -> None:
        state_path = tmp_path / "running.json"
        entry = _entry()
        save_running_state(state_path, {"u": entry})
        assert load_running_state(state_path)["u"].started_at == entry.started_at

    def test_empty_jobs_round_trip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "running.json"
        save_running_state(state_path, {})
        assert load_running_state(state_path) == {}

    def test_multiple_jobs_round_trip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "running.json"
        save_running_state(
            state_path,
            {"a": _entry(pid=1), "b": _entry(pid=2), "c": _entry(pid=3)},
        )
        result = load_running_state(state_path)
        assert set(result) == {"a", "b", "c"}
        assert result["b"].pid == 2
