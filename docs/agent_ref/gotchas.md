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

## First PR on a fresh repo

GitHub sets the first-pushed branch as the repository default. When there is no `main` yet:

1. `git checkout --orphan main && git commit --allow-empty -m "Initial commit"`
2. `git push origin main`
3. `gh repo edit <owner>/<repo> --default-branch main`
4. Force-reset `main` to the first real commit on the feature branch so they share history:
   `git checkout main && git reset --hard <sha> && git push origin main --force`
5. Now the PR can be opened normally against `main`.
