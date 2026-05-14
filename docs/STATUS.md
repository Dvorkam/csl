# Project status

Update this file at the end of every task (before committing) so the next thread can orient
itself without scanning `git log` or all of `TASKS.md`.

---

## Current task

**Task 1.2** — `shared/script_meta.py`  
Parse `*.meta.yaml` files, validate against §4.1 schema, produce typed param descriptors.
Reject unknown fields strictly.  
Branch: `feature/task-1.2-script-meta`

## Up next

**Task 1.3** — `agent/config.py`  
Load `config.yaml` from the platform's app-data directory, fall back to sensible defaults.
Includes `approval_policy.auto_approve` list.

## Recently completed

| Task | Summary | PR / commit |
| --- | --- | --- |
| 1.1 | `shared/models.py`: all 8 shared Pydantic models + enums; 18 unit tests, 100% coverage | `feature/task-1.1-shared-models` |
| Phase 0 (0.1–0.4) + licence + docs restructure | Repo init, pyproject.toml, dev tooling, package skeleton, CI, AGPL-3.0, agent_ref docs | [PR #1](https://github.com/Dvorkam/csl/pull/1) |
