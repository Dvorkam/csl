# Code conventions

## Style

- `ruff` config in `pyproject.toml`; line length 100; run `uv run ruff format .` before committing.
- Type hints required on **all public functions**. `mypy` strict mode for `control_station_lite/shared/`, normal mode elsewhere.
- `uv run pre-commit run --all-files` must pass cleanly before every commit.

## License headers

Every `.py` file under `control_station_lite/` must start with the SPDX block from `docs/lics/SOURCE_HEADER.txt`. Enforced automatically by the `insert-license` pre-commit hook — it only inserts if the header is absent. Excluded: `tests/`, `__init__.py` files, generated migration files.

When Phase 11 shell scripts are added, extend the hook with a second entry for `.sh` / `.ps1` (see Task 11.5).

## Platform detection

Use `IS_LINUX`, `IS_WINDOWS`, `IS_MACOS` from `shared/platform_info` — never call
`platform.system()` inline. See `gotchas.md` for cross-platform test marker discipline.

## Logging vs warnings

Use `logging.getLogger(__name__)` everywhere in library and application code. The `warnings`
module is for deprecation notices in third-party libraries — not for operational messages. Both
the server and agent configure their own handlers at startup; modules just emit to the logger.

## Testing HTTP endpoints

- Use a **module-scoped** `TestClient` fixture to avoid re-creating the ASGI app per test.
- Fetch expensive responses (e.g. `/healthz`, `/openapi.json`) once in a **module-scoped**
  fixture and pass the parsed dict to individual tests — don't repeat the HTTP call in every
  test method.
- Every `agent/main.py` (and future `server/main.py`) must have a `_EXPECTED_ENDPOINTS` set:
  ```python
  _EXPECTED_ENDPOINTS: set[tuple[str, str]] = {
      ("GET", "/healthz"),
      ...
  }
  ```
  and a test that compares it against the OpenAPI schema exactly. Adding an endpoint without
  updating this set must cause a test failure.

## Configuration (config.yaml)

**What belongs in config vs hardcoded:**
- User-settable values (timeouts, ports, paths that vary by machine setup) → `config.yaml`.
- Platform installation constants (where software is installed, e.g. `sshd.exe` location) → module constant in source code. These almost never vary; making them configurable adds surface area without real benefit.

**Checklist for every new config field:**
1. Add the field to the appropriate `*Section` Pydantic model in `agent/config.py` with a default.
2. If it's a `Path`: add `expanduser()` in the section's `_expand_paths` model validator.
3. Update `_write_config()` in `cli/cmd_init.py` so `csl-agent init` writes the default into new config files.
4. Update the `config.yaml` example in `docs/dev/ARCHITECTURE.md` §6.2 (and the agent config reference in `docs/guides/target-owner.md`).
5. Add a test in `tests/unit/agent/test_config.py` asserting the default value.

**Data flow rule:** Command handlers (`cmd_init`, `cmd_setup`, …) call `load_config()` once at entry and pass the result down. Helper functions must not call `load_config()` internally — that hides dependencies and creates bootstrap ordering surprises.

**Caching:** Use `get_config()` (LRU-cached) in long-running server code. Use `load_config(path)` in one-shot CLI commands and all tests.

**Bootstrap note:** On first run there is no config file yet. `load_config()` returns pure code defaults in that case — that is correct and expected. Document this if it affects a feature.

## File size and refactoring

Refactor a module into a package when it exceeds ~400 lines **and** handles more than one distinct responsibility.

Rules when splitting:
- Split by responsibility, not by size alone.
- The new `__init__.py` exports only the public interface — never add re-exports just to avoid updating test imports. Fix the imports instead.
- After splitting: update all `import` and `patch()` paths in **tests and manual scripts**. Patch paths must point to the module where the name is **used**, not where it was originally defined.
- Manual test import paths break silently (import errors only surface at runtime) — update them immediately.

## Architecture constraints

- `server/main.py` wires routers only — no endpoints defined directly in `main.py`. Each API module owns its router.
- Pydantic models for **all data crossing a boundary** (HTTP, file, IPC). No bare `dict` in API signatures.
- Async-first on both server and agent. Never mix sync and async I/O in the same request path.
- No `print()` in library code. Use `logging` (server) or a structured logger (agent).
- Secrets are never logged and never committed. `.env` is gitignored; `.env.example` is committed.
