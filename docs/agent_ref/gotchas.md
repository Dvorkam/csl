# Known gotchas

## Editable install required for service testing in dev

`uv sync` does NOT install `control_station_lite` into site-packages — it only sets up the
project path for `uv run`. When systemd (or any process) invokes the venv Python directly, the
package is not importable and the service fails with `ModuleNotFoundError: No module named
'control_station_lite'`.

Fix: run `uv pip install -e .` once after cloning or after adding `deploy/` or other top-level
directories. This creates a `.pth` file in site-packages so the package is importable from any
working directory.

In production this is a non-issue: `pip install control-station-lite[agent]` installs the
package normally.

## Agent lifecycle

The agent self-terminates when idle. Tests that start a real agent process must either disable
the lifecycle background task or set `idle_timeout_seconds` to a very high value, otherwise the
agent exits mid-test.

## Windows — pythonw.exe has no stdout/stderr

When the agent runs under `pythonw.exe` (as started by Task Scheduler), `sys.stdout`
and `sys.stderr` are both `None`.  Uvicorn's default colour log formatter calls
`sys.stdout.isatty()` during startup and raises `AttributeError: 'NoneType' object has
no attribute 'isatty'`, preventing the server from starting.

Fix: in `agent/main.py`, check `sys.stderr is not None` before calling `logging.basicConfig`,
and pass `log_config=None` to `uvicorn.run()` so uvicorn skips its `dictConfig` entirely.
Any uvicorn log calls then flow through the root logger configured by `basicConfig`.

The same pattern applies to any library that touches stdout/stderr during import or
initialisation — guard with `if sys.stdout is not None` before any console interaction.

## Windows — process group kill

Killing a persistent process on Windows uses `CREATE_NEW_PROCESS_GROUP` (0x200) at spawn time
and `taskkill /F /T /PID <pid>` to kill the whole tree. The POSIX path uses `os.killpg` with
SIGTERM → SIGKILL escalation. Both paths live in `agent/process_manager.py`.

## Windows — `os.kill(pid, 0)` terminates the process

On POSIX, `os.kill(pid, 0)` checks process existence without delivering a signal. On Windows,
the Python implementation calls `TerminateProcess`, so **`os.kill(pid, 0)` kills the process on
Windows**. To check PID existence on Windows, use `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`
+ `GetExitCodeProcess` via ctypes and compare the result to `STILL_ACTIVE` (259). See
`_pid_alive()` in `process_manager.py`.

## POSIX zombies and `os.kill(pid, 0)`

`os.kill(pid, 0)` returns success for zombie processes (exited but not yet reaped). A
`_pid_alive`-style check must also verify the process is not a zombie via
`/proc/{pid}/status` (look for `State:\tZ`). In production this rarely matters because
init/systemd reaps orphaned children immediately; in tests where the test runner is the
parent it is a real hang risk — `_ReattachedProcess.wait()` will spin forever on a zombie.

## `sys.platform` vs `IS_WINDOWS` for mypy type narrowing

mypy specifically recognises `if sys.platform == "win32":` and narrows platform stubs accordingly:
`ctypes.windll` is valid only in the `win32` branch; `os.getpgid`, `os.killpg`, and
`signal.SIGKILL` are valid only in the `else` branch. A custom `IS_WINDOWS` constant does not
trigger this narrowing.

Rule: in any function that has a POSIX branch and a Windows branch **with different stubs**, use
`sys.platform == "win32"` as the guard. Use `IS_WINDOWS` everywhere else (runtime-only checks
where mypy doesn't need to narrow).

## Windows — SSH stdio

OpenSSH on Windows handles stdio for service-start commands slightly differently from Linux.
The integration tests cover both; check them before changing the agent-start flow.

## SQLite + async

Use the `aiosqlite` driver (already in `[server]` extras). Configure `journal_mode=WAL` on the
engine to allow concurrent reads without blocking writes.

## Cross-platform test markers and `_find_script`

`_find_script` resolves extensions at runtime using `IS_WINDOWS`: Linux tries `.sh`, `.bash`,
then bare; Windows tries `.ps1`, `.bat`, `.cmd`, then bare.  A test that creates a `.ps1` file
and calls `_find_script` will silently *not find it* on Linux (wrong extension list), then raise
on the assertion — the test appears to test cross-platform behaviour but is actually
Windows-only.

Rule: any test whose correctness depends on `_find_script` or `_build_command` output for a
specific extension must carry `@pytest.mark.linux_only` or `@pytest.mark.windows_only`.

Exception: bare-name scripts (no extension) are the last entry in **both** extension lists, so
tests for the bare-name case are genuinely cross-platform.

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

## `passlib` is incompatible with `bcrypt` ≥ 4 / 5

`passlib[bcrypt]` 1.7.4 (the last release, from 2020) fails with `bcrypt` ≥ 4 because
the newer library removed `bcrypt.__about__`. The symptom is a `ValueError: password
cannot be longer than 72 bytes` raised from `bcrypt.hashpw` via passlib's internal
dispatch even for short passwords — it is actually a version-detection failure, not a
real length violation.

Fix: use `bcrypt` directly; `hash_password` / `verify_password` in
`server/auth/password.py` wrap `bcrypt.hashpw` / `bcrypt.checkpw` with no passlib
layer. `types-passlib` has been removed from dev deps. `[server]` deps now list
`bcrypt>=4` instead of `passlib[bcrypt]`.

## Secure cookies and `TestClient` — use `base_url="https://testserver"`

`response.set_cookie(secure=True, ...)` cookies are only transmitted by an HTTP client
on HTTPS connections. `TestClient` defaults to `base_url="http://testserver"` (HTTP),
so cookies with `Secure` are set in the `Set-Cookie` header but never sent back on
subsequent requests — auth tests appear to work until the refresh/logout endpoints
return 401 because the cookie jar is empty.

Fix: construct the test client with `base_url="https://testserver"`. The underlying
`ASGITransport` ignores the scheme, so requests still reach the ASGI app normally;
only the cookie-jar logic changes.

```python
with TestClient(app, base_url="https://testserver") as client:
    ...
```

## Registration bundle does not carry `ssh_user`

`RegistrationBundle` contains `private_key`, `key_fingerprint`, `agent_port`,
`scripts_dir`, `hostname_hint`, and `platform` — but **not** the SSH username. The
username varies (root vs. a named user) and was excluded to keep the bundle minimal.

The `POST /api/machines` request body must therefore include `ssh_user` as a separate
field alongside `bundle`, `name`, `ssh_host`, and `ssh_port`. The admin supplies it
when registering the machine, not the target owner.

## First PR on a fresh repo

GitHub sets the first-pushed branch as the repository default. When there is no `main` yet:

1. `git checkout --orphan main && git commit --allow-empty -m "Initial commit"`
2. `git push origin main`
3. `gh repo edit <owner>/<repo> --default-branch main`
4. Force-reset `main` to the first real commit on the feature branch so they share history:
   `git checkout main && git reset --hard <sha> && git push origin main --force`
5. Now the PR can be opened normally against `main`.
