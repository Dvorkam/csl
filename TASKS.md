# Follow-up tasks

These are follow-up tasks from a repository review of `control-station-lite`.

## Security / authorization

- [ ] Add job-level authorization checks.
  - Ensure users can only view, stream, kill, and list jobs for machines they are allowed to access.
  - Current `submit_job` checks machine access, but job detail/log/kill/list endpoints should also enforce access via `job.machine_id`.
  - Add regression tests:
    - user cannot `GET /api/jobs/{job_uuid}` for another user’s machine
    - user cannot stream logs for another user’s machine
    - user cannot kill a persistent job on another user’s machine
    - job list/history is scoped to accessible machines for non-admin users

## Script library validation

- [ ] Reuse `validate_script_name()` on the server side.
  - Apply the same script-name validation used by the agent before creating/updating/calling scripts from the canonical server library.
  - Prevent invalid names from being stored in the DB and later failing at sync/run time.
  - Add tests for path separators, Windows reserved names, trailing dots, dots-only names, and overlong names.

## Content identity / integrity

- [ ] Replace MD5 script identity with SHA-256 or algorithm-prefixed digests.
  - Prefer `sha256:<hex>` or a generic `content_digest` field.
  - Keep backwards compatibility/migration path for existing `md5` fields if needed.
  - Update docs, DB schema/migrations, agent approval state, sync logic, and tests.
  - Rationale: MD5 works as a consistency marker here, but SHA-256 better matches the project’s security posture.

## Release hardening

- [ ] Run full CI checks in the release workflow before publishing.
  - Release tags should run lint, format check, mypy, and tests before TestPyPI/PyPI/GHCR publishing.
  - Avoid publishing a broken direct tag that bypassed PR CI.

## Parameter defaults

- [ ] Decide where optional parameter defaults are materialized.
  - Metadata currently requires defaults for non-required params.
  - Ensure either the UI/server sends default values explicitly, or the agent fills missing defaults before building `CSL_PARAM_*` environment variables.
  - Add tests documenting the chosen behavior.
