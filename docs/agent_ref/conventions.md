# Code conventions

## Style

- `ruff` config in `pyproject.toml`; line length 100; run `uv run ruff format .` before committing.
- Type hints required on **all public functions**. `mypy` strict mode for `control_station_lite/shared/`, normal mode elsewhere.
- `uv run pre-commit run --all-files` must pass cleanly before every commit.

## License headers

Every `.py` file under `control_station_lite/` must start with the SPDX block from `docs/lics/SOURCE_HEADER.txt`. Enforced automatically by the `insert-license` pre-commit hook — it only inserts if the header is absent. Excluded: `tests/`, `__init__.py` files, generated migration files.

When Phase 11 shell scripts are added, extend the hook with a second entry for `.sh` / `.ps1` (see Task 11.5).

## Architecture constraints

- `server/main.py` wires routers only — no endpoints defined directly in `main.py`. Each API module owns its router.
- Pydantic models for **all data crossing a boundary** (HTTP, file, IPC). No bare `dict` in API signatures.
- Async-first on both server and agent. Never mix sync and async I/O in the same request path.
- No `print()` in library code. Use `logging` (server) or a structured logger (agent).
- Secrets are never logged and never committed. `.env` is gitignored; `.env.example` is committed.
