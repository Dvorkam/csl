# AGENTS.md

Project conventions for AI coding agents and human contributors. Keep this file short.

**What this project is:** A self-hosted web service for controlling LAN machines through an
approval-gated agent. Key docs:

| Doc | Purpose |
| --- | --- |
| `docs/README.md` | Project overview and goals |
| `docs/ARCHITECTURE.md` | Full design — **authoritative**; wins over everything else |
| `docs/TASKS.md` | Numbered implementation backlog |
| `docs/STATUS.md` | Current task, next task, recent completions — read this first |
| `docs/agent_ref/env.md` | **This machine's** OS, tools, quirks (gitignored; copy from `env.example.md`) |
| `docs/agent_ref/workflow.md` | Branch naming, commit format, testing rules, CI layout |
| `docs/agent_ref/conventions.md` | Code style, type hints, SPDX headers, architecture constraints |
| `docs/agent_ref/gotchas.md` | Platform pitfalls (Windows process groups, SSH, SQLite, first-PR) |
| `docs/agent_ref/decided.md` | Locked architectural decisions |

---

## Stack

- Python 3.11+, FastAPI on both server and agent.
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

Always use `uv run` — don't rely on a globally activated venv.

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

## Dependency groups

- `[project.dependencies]` — shared runtime deps (server and agent).
- `[project.optional-dependencies].server` — server-only runtime deps.
- `[project.optional-dependencies].agent` — agent-only (currently empty; signals install intent).
- `[dependency-groups].dev` — `ruff`, `mypy`, `pre-commit`.
- `[dependency-groups].test` — `pytest`, `pytest-asyncio`, `pytest-cov`, `httpx`, `schemathesis`.

Target machines install with `pip install control-station-lite[agent]` — no server deps pulled in.
