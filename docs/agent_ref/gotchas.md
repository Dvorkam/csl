# Known gotchas

## Agent lifecycle

The agent self-terminates when idle. Tests that start a real agent process must either disable
the lifecycle background task or set `idle_timeout_seconds` to a very high value, otherwise the
agent exits mid-test.

## Windows — process group kill

Killing a persistent process on Windows must use `CREATE_NEW_PROCESS_GROUP` at spawn time and
`os.kill(pid, signal.CTRL_BREAK_EVENT)` to kill. The Unix path uses `os.killpg`. Both paths live
in `agent/process_manager.py`.

## Windows — SSH stdio

OpenSSH on Windows handles stdio for service-start commands slightly differently from Linux.
The integration tests cover both; check them before changing the agent-start flow.

## SQLite + async

Use the `aiosqlite` driver (already in `[server]` extras). Configure `journal_mode=WAL` on the
engine to allow concurrent reads without blocking writes.

## approvals.json / filesystem drift

`ApprovalsManager` treats `approvals.json` as authoritative. If a user manually deletes
an approved script file without clearing its JSON entry, the state machine will still
report `approved` and `script_runner` will fail when it tries to execute it.  The reverse
(file present, JSON entry deleted) leaves an orphaned file that can never be run.

A future reconciliation pass on agent startup should cross-check JSON state against
filesystem reality — similar to how `state.py` reattaches to or marks dead persistent
processes.  Until that pass exists, treat manual edits to `~/.csl/` as unsupported.

## schemathesis 4.x API

The public API changed significantly in schemathesis 4.x:
- Load ASGI apps via `schemathesis.openapi.from_asgi("/openapi.json", app)` — not `schemathesis.from_asgi(...)`.
- Call a case via `case.call(app=app)` — not `case.call_asgi(...)` or `case.call_wsgi(...)`.
- Custom checks live in `schemathesis.checks`, not `schemathesis.specs.openapi.checks`.
- Check signature: `(ctx: CheckContext, response: Response, case: Case) -> bool | None`.

## `importlib.metadata.version()` in tests

`importlib.metadata.version("control-station-lite")` raises `PackageNotFoundError` in test
environments where the package is not installed as an editable install (`uv sync` should handle
this, but it can fail in CI if the install step is skipped). Any module that calls this at import
time will break test collection. Always guard with a try/except and fall back to `"0.0.0-dev"`.

## First PR on a fresh repo

GitHub sets the first-pushed branch as the repository default. When there is no `main` yet:

1. `git checkout --orphan main && git commit --allow-empty -m "Initial commit"`
2. `git push origin main`
3. `gh repo edit <owner>/<repo> --default-branch main`
4. Force-reset `main` to the first real commit on the feature branch so they share history:
   `git checkout main && git reset --hard <sha> && git push origin main --force`
5. Now the PR can be opened normally against `main`.
