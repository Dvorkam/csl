# AGENTS.md

Project conventions for AI coding agents and human contributors. Keep this file short.

**What this project is:** A self-hosted web service for controlling LAN machines through an approval-gated agent. See `docs/README.md` for the overview, `docs/ARCHITECTURE.md` for the design (authoritative), and `docs/TASKS.md` for the implementation backlog.

---

## Stack

- Python 3.11+, FastAPI on both sides (server and agent).
- `uv` for dependency management.
- SQLite + SQLAlchemy + Alembic on the server.
- Jinja2 + HTMX for the frontend (no build pipeline).
- Docker + Compose + systemd for deployment.

## Setup

```bash
uv sync --all-extras --all-groups
uv run pre-commit install
```

## Commands

Always invoke tools via `uv run` — don't rely on a globally activated venv.

| Task | Command |
| --- | --- |
| Run all tests | `uv run pytest` |
| Run a specific test | `uv run pytest tests/unit/agent/test_approvals.py` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Type-check | `uv run mypy control_station_lite` |
| All pre-commit checks | `uv run pre-commit run --all-files` |
| Start dev server | `uv run csl-server --reload` |
| Start agent locally | `uv run csl-agent` (after `csl-agent init`) |
| Generate migration | `uv run alembic revision --autogenerate -m "..."` |
| Apply migrations | `uv run alembic upgrade head` |

## Dependency groups (`pyproject.toml`)

- `[project.dependencies]` — minimal shared runtime deps (used by both server and agent).
- `[project.optional-dependencies].server` — server-only runtime deps.
- `[project.optional-dependencies].agent` — agent-only runtime deps (likely empty or near-empty).
- `[dependency-groups].dev` — `ruff`, `mypy`, `pre-commit`.
- `[dependency-groups].test` — `pytest`, `pytest-asyncio`, `pytest-cov`, `httpx`, `schemathesis`, `testcontainers` where useful.

Production installs do not pull dev or test groups. Target machines install with `pip install control-station-lite[agent]` (no server deps).

## Workflow: one task at a time

Tasks live in `docs/TASKS.md` and are numbered (e.g. `1.5`). For each task:

1. Read the task and any relevant section of `docs/ARCHITECTURE.md` it touches.
2. Create a branch: `feature/task-1.5-approval-state-machine`.
3. Implement the change.
4. Add tests per the testing rules below.
5. Run `uv run pre-commit run --all-files`. Fix anything it finds.
6. Run `uv run pytest`. All tests must pass.
7. Commit. Message format: `<type>(<scope>): <summary> [Task N.M]`. Examples:
   - `feat(agent): implement approval state machine [Task 1.5]`
   - `fix(server): handle expired refresh token edge case [Task 3.5]`
   - `docs: clarify approval flow in architecture [Task 3.3]`
8. Tick the box in `docs/TASKS.md` in the same commit.

One task = one commit by default. Split only if the task has clearly separable steps.

## Testing rules

- **Every public function gets a unit test.** Place under `tests/unit/` mirroring source layout.
- **Every API endpoint gets a contract test** driven by the FastAPI-generated OpenAPI spec. Use `schemathesis`. Place under `tests/contract/`.
- **Cross-component interactions get an integration test.** Examples: control station ↔ agent over a tunnel, agent ↔ subprocess management, control station ↔ SQLite. Place under `tests/integration/`.
- **End-to-end smoke tests** (`tests/e2e/`) cover happy-path scenarios across the full stack. Use `docker compose` in fixtures.
- **Cross-platform sensitive tests** must be marked: `@pytest.mark.linux_only`, `@pytest.mark.windows_only`, etc. CI runs them on the right matrix entries.

Coverage target: 85% line coverage on `control_station_lite/`, enforced in CI. Exclusions documented in `pyproject.toml`.

## Non-Python artifact validation

Treat these as first-class code. They get checked in CI and (where fast enough) in pre-commit.

| Artifact | Tool | Where |
| --- | --- | --- |
| `Dockerfile` | `hadolint` | pre-commit + CI |
| `docker-compose.yml` | `docker compose config` (syntax) + actual build in CI | CI |
| `nginx.conf` | `nginx -t` via the nginx image | CI |
| `*.service` (systemd) | `systemd-analyze verify` | CI |
| Shell scripts (`.sh`) | `shellcheck` | pre-commit + CI |
| Shell script behavior | `bats` | CI |
| YAML files | `yamllint` | pre-commit |

The bootstrap script (`scripts/setup.sh`) gets a dedicated bats test suite covering: fresh install, rerun (must be idempotent), upgrade path.

## Code conventions

- `ruff` config in `pyproject.toml`; line length 100; format on save.
- Type hints required on all public functions. `mypy` strict mode for `control_station_lite/shared/`, normal mode elsewhere.
- Pydantic models for all data crossing a boundary (HTTP, file, IPC). No bare dicts in API signatures.
- Async-first on both server and agent. Don't mix sync and async I/O within a request path.
- No `print()` in library code. Use `logging` (server) or structured logger (agent).
- Secrets never logged. Never committed. `.env` is gitignored; provide `.env.example`.

## Things that have already been decided — don't relitigate without updating docs

- The agent is the **only** channel of interaction with a target. No direct shell over SSH.
- Scripts require explicit approval on the target before they can run. No silent sync.
- Parameters pass as `CSL_PARAM_*` environment variables.
- SSH keys at rest are encrypted with AES-256-GCM, master key from `secrets/master.key`.
- Auto-approve is per-script whitelist only — no "trust on first use" or "auto-approve minor changes".

If you find a reason to change one of these, update `docs/ARCHITECTURE.md` in the same PR.

## CI

GitHub Actions, defined in `.github/workflows/`:

- `ci.yml` — runs on every PR. Matrix: `{ubuntu-latest, windows-latest} × python-3.11`. Steps: install, lint, type-check, unit + contract + integration tests, non-Python validations, coverage report.
- `e2e.yml` — runs on PR label `e2e` and on merges to `main`. Builds the Docker image, runs the e2e suite.
- `release.yml` — **deferred.** Will run on tag push, build/publish PyPI package and Docker image. Not in scope for v0.1.

## Gotchas

- **Agent lifecycle:** the agent self-terminates on idle. Tests that start an agent must either disable the lifecycle task or set `idle_timeout_seconds` very high.
- **Windows process groups:** killing a persistent process must use `CREATE_NEW_PROCESS_GROUP` and `os.kill(pid, signal.CTRL_BREAK_EVENT)`. The unix path uses `os.killpg`. See `agent/process_manager.py`.
- **SSH tunneling on Windows:** OpenSSH on Windows behaves slightly differently around stdio handling for the service-start command. Integration tests cover both.
- **SQLite + async:** use `aiosqlite` driver; configure `journal_mode=WAL` for concurrent reads.

---

If something here disagrees with `docs/ARCHITECTURE.md`, the architecture doc wins. Fix this file.
