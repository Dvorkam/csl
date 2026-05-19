# Project status

Update this file at the end of every task (before committing) so the next thread can orient
itself without scanning `git log` or all of `TASKS.md`.

---

## Current task

**Task 1.16** — End-to-end approval flow test without the control station.

## Up next

**Task 2.1** — `server/config.py` (pydantic-settings).

## Recently completed

| Task | Summary | PR / commit |
| --- | --- | --- |
| 1.15 | Cross-platform tests: `authorized_keys` permission bits (linux_only), CRLF-safe idempotency, Windows-simulated config paths and service-installer dispatch, multi-step approval CLI flow integration tests (absent→pending→approved→absent, update flow, policy add/remove); 27 new tests | `feature/task-1.15-cross-platform-tests` |
| (config) | Integrate hardcoded constants into `config.yaml`: added `lifecycle_check_interval_seconds` (default 10s), `log_tail_lines` (default 1000), and new `advanced` section with `windows_admin_authorized_keys_path`; wired into `main.py` and `cmd_init.py`; 5 new config tests | `feature/task-1.13-approvals-cli` |
| (refactor) | Refactored 971-line `agent/cli.py` into `agent/cli/` package: `_main.py`, `cmd_init.py`, `cmd_setup.py`, `cmd_approvals.py`, `cmd_policy.py`; clean one-way dependency graph; default port changed to 36717 | `feature/task-1.13-approvals-cli` |
| 1.14 | `csl-agent policy` subcommands: `show`, `auto-approve <name>`, `manual <name>`; reads and rewrites `config.yaml` in-place preserving all other settings; 5 unit tests | `feature/task-1.13-approvals-cli` |
| 1.13 | `csl-agent approvals` subcommands: `list` (table with state badges + md5 hints), `show <name>` (pending content), `diff <name>` (unified diff via difflib, update_pending only), `approve/reject/clear <name>` (delegates to ApprovalsManager); exits 1 on invalid state transitions; 20 unit tests | `feature/task-1.13-approvals-cli` |
| 1.12 | `agent/cli.py` + `shared/registration.py`: argparse CLI (`csl-agent init`); Ed25519 keypair generation via `cryptography`; idempotent `authorized_keys` append (700/600 perms); `config.yaml` + empty `approvals.json` write; `install_service()` call (non-fatal on failure); base64-JSON registration bundle; `RegistrationBundle.decode()` with full validation; `cryptography` moved to base deps; 45 unit tests | `feature/task-1.12-cli` |
| 1.11 | `agent/service_installer.py`: `install_service()` dispatches to `_install_linux` (writes `~/.config/systemd/user/csl-agent.service`, `Restart=no`, `daemon-reload`), `_install_macos` (writes `~/Library/LaunchAgents/com.controlstationlite.agent.plist`, `launchctl load`), `_install_windows` (XML task, `schtasks /create /f`, no triggers); `_agent_executable()` prefers `pythonw.exe` on Windows; `macos_only` marker added to conftest; 26 tests (9 skipped on Linux) | `feature/task-1.11-service-installer` |
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
