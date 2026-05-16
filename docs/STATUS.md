# Project status

Update this file at the end of every task (before committing) so the next thread can orient
itself without scanning `git log` or all of `TASKS.md`.

---

## Current task

**Task 1.11** — `agent/service_installer.py`  
Install user-level service on Linux (systemd --user), Windows (Task Scheduler), macOS (launchd).

## Up next

**Task 1.12** — `agent/cli.py` and `csl-agent init` command.

## Recently completed

| Task | Summary | PR / commit |
| --- | --- | --- |
| 1.10 | `agent/lifecycle.py`: `IdleTracker` (thread-safe idle clock, `record_activity`, `idle_seconds`, `shutdown_due`); `run_loop` async background task; `_HasRunningCount` Protocol to avoid circular import; `_ActivityMiddleware` in `main.py` resets clock on every request; `/healthz` now reports real `idle_seconds`; `asyncio.create_task` in lifespan with clean cancel on shutdown; 14 unit + integration tests | `feature/task-1.10-lifecycle` |
| 1.9 | `agent/state.py`: `JobEntry` Pydantic model; atomic `save_running_state` / `load_running_state`; `ProcessManager.save_state()` + `restore_state()`; `_ReattachedProcess` (Popen duck-type for recovered PIDs); `_pid_alive()` cross-platform (ctypes on Windows); lifespan calls restore; 27 new tests | `feature/task-1.9-state` |
| 1.8 | `agent/log_stream.py`: `tail_log` async generator with drain-on-exit; `sse_events` SSE envelope; `make_sse_response`; `GET /jobs/{uuid}/stream` endpoint; managers wired in lifespan; 18 unit tests | `feature/task-1.8-log-stream` |
| 1.7 | `agent/process_manager.py` + `agent/paths.py` (`CslPaths`): approval-gated persistent process start; SIGTERM→SIGKILL kill; `running_count()`; `CslPaths` centralises all agent paths with `platform_base()` cached; helpers made public in `script_runner.py`; 204 tests | `feature/task-1.7-process-manager` |
| 1.6 | `agent/script_runner.py`: cross-platform execution (.sh/.ps1/.bat); `CSL_PARAM_*` env vars; approval gate; platform markers in tests | `feature/task-1.6-script-runner` |
| 1.5 | `agent/approvals.py`: full state machine, atomic JSON write, audit log, thread-safe; 46 unit tests | `feature/task-1.5-approvals` |
| 1.4 | `agent/main.py`: FastAPI app; `/healthz` implemented; 3 stub endpoints (501); `127.0.0.1`-only binding; 10 unit + 4 contract tests | `feature/task-1.4-agent-main` |
| 1.3 | `agent/config.py`: platform-aware config loader; `csl_dir` as path root; shared `_validation.py` extracted; 72 unit tests | `feature/task-1.3-agent-config` |
| 1.2 | `shared/script_meta.py`: YAML parser + validator; unknown fields warned+stripped not crashed; 26 unit tests | `feature/task-1.2-script-meta` |
| 1.1 | `shared/models.py`: all 8 shared Pydantic models + enums; 18 unit tests, 100% coverage | `feature/task-1.1-shared-models` |
| Phase 0 (0.1–0.4) + licence + docs restructure | Repo init, pyproject.toml, dev tooling, package skeleton, CI, AGPL-3.0, agent_ref docs | [PR #1](https://github.com/Dvorkam/csl/pull/1) |
