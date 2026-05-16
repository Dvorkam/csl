# Workflow reference

## One task at a time

Tasks live in `docs/TASKS.md` and are numbered (e.g. `1.5`). For each task:

1. Read the task and any relevant section of `docs/ARCHITECTURE.md` it touches.
2. Create a branch: `feature/task-1.5-short-description`.
3. Implement the change.
4. Add tests per the testing rules below.
5. Run `uv run pre-commit run --all-files`. Fix anything it finds.
6. Run `uv run pytest`. All tests must pass.
7. Commit. Message format: `<type>(<scope>): <summary> [Task N.M]`. Examples:
   - `feat(agent): implement approval state machine [Task 1.5]`
   - `fix(server): handle expired refresh token edge case [Task 3.5]`
   - `docs: clarify approval flow in architecture [Task 3.3]`
8. Tick the box in `docs/TASKS.md` in the same commit.
9. Update `docs/STATUS.md` (current task → completed, set next task as current).

One task = one commit by default. Split only if the task has clearly separable steps.

After each task (or logical group), open a PR:

```
gh pr create --base main --title "..." --body "..."
```

**Always target `main`** unless the user explicitly names a different base branch.
The project used a temporary feature branch as base during bootstrapping; that is over.

---

## Testing rules

- **Every public function gets a unit test.** Place under `tests/unit/` mirroring source layout.
- **Every API endpoint gets a contract test** driven by the FastAPI OpenAPI spec. Use `schemathesis`. Place under `tests/contract/`.
- **Cross-component interactions get an integration test** (control station ↔ agent, agent ↔ subprocess, control station ↔ SQLite). Place under `tests/integration/`.
- **End-to-end smoke tests** (`tests/e2e/`) cover happy-path scenarios across the full stack. Use `docker compose` in fixtures.
- **Cross-platform sensitive tests** must be marked `@pytest.mark.linux_only` / `@pytest.mark.windows_only`. CI runs them on the right matrix entries.

Coverage target: **85% line coverage** on `control_station_lite/`, enforced in CI. Exclusions documented in `pyproject.toml`.

---

## Non-Python artifact validation

| Artifact | Tool | Where |
| --- | --- | --- |
| `Dockerfile` | `hadolint` | pre-commit + CI |
| `docker-compose.yml` | `docker compose config` + actual build | CI |
| `nginx.conf` | `nginx -t` via nginx image | CI |
| `*.service` (systemd) | `systemd-analyze verify` | CI |
| Shell scripts (`.sh`) | `shellcheck` | pre-commit + CI |
| Shell script behaviour | `bats` | CI |
| YAML files | `yamllint` | pre-commit |

`scripts/setup.sh` gets a dedicated bats suite: fresh install, rerun (idempotent), upgrade path.

---

## CI

Workflows live in `.github/workflows/`:

- **`ci.yml`** — every PR. Matrix: `{ubuntu-latest, windows-latest} × python-3.11`. Steps: install, lint, type-check, unit + contract + integration tests, coverage report.
- **`e2e.yml`** — PR label `e2e` or merge to `main`. Builds Docker image, runs `tests/e2e/` with a real agent subprocess on the runner.
- **`release.yml`** — deferred. Tag push → build/publish PyPI + Docker. Not in scope for v0.1.
