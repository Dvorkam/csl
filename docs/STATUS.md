# Project status

Update this file at the end of every task (before committing) so the next thread can orient
itself without scanning `git log` or all of `TASKS.md`.

---

## Current task

**Task 1.1** — `shared/models.py`  
Define Pydantic models shared by server and agent: `JobRequest`, `JobStatus`, `LogChunk`,
`AgentHealth`, `ScriptDescriptor`, `ApprovalState`, `StageScriptRequest`, `StageScriptResponse`.  
Branch: `feature/task-1.1-shared-models`

## Up next

**Task 1.2** — `shared/script_meta.py`  
Parse `*.meta.yaml` files, validate against §4.1 schema, produce typed param descriptors.
Reject unknown fields strictly.

## Recently completed

| Task | Summary | PR / commit |
| --- | --- | --- |
| Phase 0 (0.1–0.4) + licence + docs restructure | Repo init, pyproject.toml, dev tooling, package skeleton, CI, AGPL-3.0, agent_ref docs | [PR #1](https://github.com/Dvorkam/csl/pull/1) |
