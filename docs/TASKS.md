# Implementation tasks — control-station-lite

Tasks are grouped into phases. Phases are roughly sequential, but where a phase has no dependency on a prior phase's complete output (e.g. frontend can start once basic API is up), they may overlap.

Each task should ship with:
- Tests where the boundary is testable in isolation.
- Updated `ARCHITECTURE.md` if any decision differs from this document.
- Audit-log instrumentation where the action mutates state.

---

## Phase 0 — Project setup

- [x] **0.1** Initialize repository; add `pyproject.toml` with `[server]` and `[agent]` extras as defined in `ARCHITECTURE.md` §9.1.
- [x] **0.2** Configure dev tooling: `ruff` (lint + format), `pytest`, `mypy` (strict on `shared/`, relaxed elsewhere), `pre-commit`.
- [x] **0.3** Create top-level package skeleton (`server/`, `agent/`, `shared/`) with empty modules matching §6.1.
- [x] **0.4** CI pipeline (GitHub Actions or equivalent): lint, type-check, tests on push.

---

## Phase 1 — Agent foundation

The agent is testable in isolation (no control station needed). Build it first to derisk the cross-platform process management work and the approval state machine.

- [x] **1.1** Define `shared/models.py` Pydantic models: `JobRequest`, `JobStatusResponse`, `LogChunk`, `AgentHealth`, `ScriptDescriptor`, `ApprovalState`, `StageScriptRequest`, `StageScriptResponse`. (`JobStatus` and `ApprovalState` are `StrEnum`s; `JobStatusResponse` is the Pydantic model for the API response.)
- [x] **1.2** Implement `shared/script_meta.py`: parse `*.meta.yaml`, validate against the schema (§4.1), produce typed param descriptors. Unknown fields are rejected (`extra="forbid"`) but handled gracefully: `parse_meta_yaml` catches `extra_forbidden` validation errors, logs them as warnings via `logging`, strips the offending keys, and re-validates — so a YAML typo never crashes the server. Real validation errors (bad types, missing required fields, etc.) still raise `ScriptMetaError`.
- [x] **1.3** Implement `agent/config.py`: load `config.yaml` from the platform's app-data directory, fall back to sensible defaults. Includes `approval_policy.auto_approve` list.
- [x] **1.4** Implement `agent/main.py`: minimal FastAPI app with `/healthz`, `/jobs`, `/scripts/{name}/state`, and `/scripts/{name}/stage` endpoints. Bind to `127.0.0.1` only — refuse to bind to any other address.
- [x] **1.5** Implement `agent/approvals.py`: full state machine over `absent → pending → approved`, `approved → update_pending → approved | rejected`, etc. Persists to `approvals.json` atomically (write to temp + rename). All transitions audited to a local log file.
- [x] **1.6** Implement `agent/script_runner.py`: execute one-off scripts with parameters passed as `CSL_PARAM_*` environment variables. **Refuse to run any script not in `approved` state.** Capture stdout, stderr, exit code. Cross-platform (test on at least Linux and Windows).
- [x] **1.7** Implement `agent/process_manager.py`: start persistent processes (also approval-gated), track them, allow kill, return status. Use `subprocess.Popen` with platform-appropriate process group handling so kill cleans up children.
- [x] **1.8** Implement `agent/log_stream.py`: persistent jobs write stdout/stderr to `logs/{job_uuid}.log`. SSE endpoint `/jobs/{id}/stream` tails the file and pushes new lines. Multiple subscribers supported.
- [x] **1.9** Implement `agent/state.py`: serialize/deserialize `running.json`. On startup, reattach to processes whose PIDs are still alive; mark the rest as terminated.
- [x] **1.10** Implement `agent/lifecycle.py`: background task counts idle seconds; triggers shutdown when `running_persistent == 0 and idle > timeout`.
- [x] **1.11** Implement `agent/service_installer.py`:
  - Linux: write `~/.config/systemd/user/csl-agent.service` with `Restart=no`, run `systemctl --user daemon-reload`. **Do not** `--user enable` — the service must be on-demand only.
  - Windows: register a Task Scheduler task via `schtasks /create`, demand-only trigger, action `pythonw -m control_station_lite.agent`.
  - macOS: write a `launchd` user agent plist into `~/Library/LaunchAgents/`, load with `launchctl load`.
- [x] **1.12** Implement `agent/cli.py` and `csl-agent init` command:
  - Create `~/.csl/` folder structure including `scripts.pending/`.
  - Generate SSH keypair (Ed25519).
  - Append public key to `~/.ssh/authorized_keys` (create file with correct permissions if absent; idempotent).
  - Write `config.yaml` and an empty `approvals.json`.
  - Install the user-level service via `service_installer`.
  - Emit registration bundle (base64-encoded JSON containing private key, key fingerprint, agent port, scripts dir, hostname hint, platform).
- [x] **1.13** Implement `csl-agent approvals` subcommands: `list`, `show <name>`, `diff <name>`, `approve <name>`, `reject <name>`, `clear <name>`.
- [x] **1.14** Implement `csl-agent policy` subcommands: `auto-approve <name>`, `manual <name>`, `show`.
- [x] **1.15** Cross-platform tests for `csl-agent init`, service installation, and approval CLI flows (Linux, Windows). Verify Windows path handling, line endings, and `authorized_keys` permissions where applicable.
- [ ] **1.16** Test: end-to-end approval flow without the control station — stage a script via direct API call to a locally-running agent, list pending, approve, run.

---

## Phase 2 — Control station foundation

- [ ] **2.1** Implement `server/config.py` (pydantic-settings): load from env / file. Fail loudly if required secrets are missing.
- [ ] **2.2** Implement `server/db/models.py` per §5.1.
- [ ] **2.3** Set up Alembic; generate initial migration matching the models.
- [ ] **2.4** Implement `server/main.py` FastAPI app skeleton with `/healthz`.
- [ ] **2.5** Implement `server/core/crypto.py`: AES-256-GCM encrypt/decrypt using master key. Per-record nonce, stored alongside ciphertext.
- [ ] **2.6** Implement `server/core/ssh.py`: async SSH connection pool. Open connection, hold open for short keep-alive window, support local port forwarding. Built on `asyncssh`.
- [ ] **2.7** Implement `server/core/agent_client.py`: given a `Machine` record, ensure agent is running (issue platform-appropriate service-start command if `/healthz` does not respond, then poll with backoff), establish tunnel, expose a typed client (`get_health`, `get_script_state`, `stage_script`, `submit_job`, `kill_job`, `stream_logs`).

---

## Phase 3 — Authentication and authorization

- [ ] **3.1** Implement `server/auth/password.py`: bcrypt hash/verify with sane work factor.
- [ ] **3.2** Implement `server/auth/jwt.py`: issue and verify access and refresh tokens per §7.1.
- [ ] **3.3** Implement `server/auth/dependencies.py`: FastAPI dependencies `current_user`, `require_admin`.
- [ ] **3.4** Implement `server/api/auth.py`: `POST /api/auth/login`, `POST /api/auth/refresh`, `POST /api/auth/logout`. Refresh token issued as `HttpOnly Secure SameSite=Strict` cookie.
- [ ] **3.5** Implement refresh-token revocation: on rotation, mark prior token revoked; on logout, mark current token revoked.
- [ ] **3.6** CLI command to create an initial admin user (used by the bootstrap script).
- [ ] **3.7** Integration tests covering: login, refresh, rotation, revocation, expired access token, expired refresh token, replay of revoked token.

---

## Phase 4 — Machine management

- [ ] **4.1** Implement `shared/registration.py`: encode/decode registration bundle format. Round-trip tests.
- [ ] **4.2** Implement `server/api/machines.py`:
  - `POST /api/machines` (admin): accept registration bundle, name, SSH endpoint; decrypt, validate, store. Performs a one-time connection test (SSH in, read agent config, verify fingerprint). Atomic: failure leaves no partial state.
  - `GET /api/machines`: list for current user (filtered to bookmarked).
  - `GET /api/machines/{id}`: detail including current reachability and (if agent is running) list of running persistent jobs.
  - `DELETE /api/machines/{id}` (admin).
  - `POST /api/machines/{id}/bookmark` / `DELETE /api/machines/{id}/bookmark`.
- [ ] **4.3** Reachability check endpoint that does not spawn an agent: SSH ping only. Separate from agent status.
- [ ] **4.4** Status check that *does* talk to a running agent (without spawning one): tunnel + agent `/healthz` only if agent is already up.

---

## Phase 5 — Script library

- [ ] **5.1** Implement `server/core/script_registry.py`: CRUD over the `scripts` table; on edit, recompute MD5. Validates the metadata YAML at write time.
- [ ] **5.2** Implement `server/api/scripts.py`:
  - `GET /api/scripts`: list (any authenticated user).
  - `GET /api/scripts/{name}`: detail with metadata.
  - `POST /api/scripts` (admin): create.
  - `PUT /api/scripts/{name}` (admin): update.
  - `DELETE /api/scripts/{name}` (admin).
- [ ] **5.3** Implement `server/core/script_sync.py`:
  - For a (machine, script) pair: query the agent for current state via `GET /scripts/{name}/state`.
  - Map agent response to one of: `approved` (md5 matches), `approved_stale` (approved md5 doesn't match canonical — needs update), `pending`, `update_pending`, `rejected`, `absent`.
  - If the script needs to be staged (`absent` or canonical differs from approved): call `POST /scripts/{name}/stage` with the new content.
  - **The agent decides whether staging results in immediate approval (via auto-approve policy) or `pending` state. The control station never bypasses approval.**
  - Update `script_target_state` cache from each response.
- [ ] **5.4** Tests covering: stage when absent, stage when update needed, agent reports rejected, agent reports already-approved, auto-approved scripts complete in one round-trip.

---

## Phase 6 — Script execution and jobs

- [ ] **6.1** Implement `server/api/jobs.py`:
  - `POST /api/machines/{id}/jobs`: submit a job. Body includes `script_name`, `params`. Returns `job_uuid` on success. On approval-related failures, returns a structured error: `pending_approval (new)`, `pending_approval (update)`, or `rejected`, with the current agent state so the UI can render meaningfully.
  - `GET /api/jobs/{job_uuid}`: status.
  - `GET /api/jobs/{job_uuid}/stream`: SSE proxy to agent log stream.
  - `POST /api/jobs/{job_uuid}/kill`: kill a persistent job.
  - `GET /api/jobs`: history with filters.
- [ ] **6.2** Wire script-sync into the submit-job path: every submission triggers a state check + (if needed) stage attempt before forwarding to the agent's `POST /jobs`. Submissions where the result is still not `approved` after staging return a structured pending-approval error rather than running.
- [ ] **6.3** Job status reconciliation: periodic background task on the control station polls agents that have known running jobs; updates `jobs` table when state changes.
- [ ] **6.4** End-to-end test: stage a script, approve it via `csl-agent approvals approve`, submit, observe completion.
- [ ] **6.5** End-to-end test: submit a persistent script (pre-approved), stream logs, kill it.
- [ ] **6.6** End-to-end test: submit a script that's been rejected on the target — verify the control station surfaces the rejection clearly and does not retry.

---

## Phase 7 — Built-in actions

- [ ] **7.1** Implement `server/core/magic_packet.py`: build and broadcast Magic Packet from a MAC address. Configurable broadcast address.
- [ ] **7.2** Implement `server/api/builtin.py`:
  - `POST /api/machines/{id}/builtin/wol`.
- [ ] **7.3** Audit-log integration.

---

## Phase 8 — Frontend (Jinja2 + HTMX)

- [ ] **8.1** Base layout: nav, login state, flash messages.
- [ ] **8.2** Login page.
- [ ] **8.3** Dashboard: machine list with HTMX-driven reachability polling.
- [ ] **8.4** Machine detail page: status panel, running-jobs list, available-scripts list.
- [ ] **8.5** Script run dialog: dynamically rendered form from script metadata (string / int / float / bool / choice / path); client-side validation; submit; redirect to job view for persistent jobs. Handles `pending_approval` and `rejected` responses by surfacing the agent state, not a generic error.
- [ ] **8.6** Live log viewer: SSE-based, auto-scrolling, with kill button for persistent jobs.
- [ ] **8.7** Approval-state badges on machine detail page: each script in the available-scripts list shows its current state (`approved` / `pending` / `update_pending` / `rejected` / `absent`) with appropriate styling. Hover reveals approved/pending MD5s. A "Re-stage" button is available for `pending` and `update_pending` to prompt the target owner to recheck.
- [ ] **8.8** Admin: script library page (list, edit, delete).
- [ ] **8.9** Admin: script editor (monaco-style textarea is sufficient; no full IDE).
- [ ] **8.10** Admin: machine management page.
- [ ] **8.11** Admin: user management page.
- [ ] **8.12** Admin: audit log viewer with filters.

---

## Phase 9 — Audit log

- [ ] **9.1** Implement `server/api/audit.py` with admin-only read access.
- [ ] **9.2** Audit decorator / helper used by all state-mutating endpoints. Capture: user, action, target, result, structured details.
- [ ] **9.3** Verify coverage: every `POST`, `PUT`, `DELETE`, every login/logout, every job submit/kill produces an entry.

---

## Phase 10 — Packaging and distribution

- [ ] **10.1** `deploy/Dockerfile` for the control station.
- [ ] **10.2** `deploy/nginx.conf` template with TLS config, rate limiting on `/api/auth/*`, sane request size limits.
- [ ] **10.3** `deploy/docker-compose.yml` wiring app + nginx, with documented volumes.
- [ ] **10.4** `deploy/control-station.service` systemd unit.
- [ ] **10.5** `scripts/setup.sh` bootstrap per §9.4. Idempotent: rerun must not destroy data.
- [ ] **10.6** Build and publish PyPI release pipeline. Verify `pip install control-station-lite[agent]` works in a clean container on Linux and Windows.
- [ ] **10.7** End-to-end smoke test from a clean NAS: run `setup.sh`, register one Linux target, one Windows target, run scripts on each.

---

## Phase 11 — Built-in script catalogue

Ship as default scripts that target owners can opt into. Each is a `.sh` (Linux/macOS) and/or `.ps1` (Windows) plus `.meta.yaml`.

- [ ] **11.1** `sleep_machine` — put target to sleep.
- [ ] **11.2** `restart_machine` — restart target.
- [ ] **11.3** `start_steam`.
- [ ] **11.4** `start_llama_server` — persistent; parameters for model path, context size, GPU layers.
- [ ] **11.5** Add SPDX license headers to all `.sh` and `.ps1` scripts. Extend the `insert-license` pre-commit hook with a second entry targeting `\.sh$` and `\.ps1$` (both use `#` comment style).

(Wake-on-LAN is built-in, not a script — see Phase 7.)

---

## Phase 12 — Documentation

- [ ] **12.1** End-user docs: how to add a machine, how to run scripts, how to interpret status.
- [ ] **12.2** Target-owner docs: how to install the agent, how the approval workflow works, the `csl-agent approvals` and `csl-agent policy` CLI reference, how to audit and revoke access, where everything lives on disk.
- [ ] **12.3** Admin docs: how to write a script, metadata reference, troubleshooting.
- [ ] **12.4** Operator docs: how to back up the SQLite DB, rotate the master key, replace TLS certs.

---

## Out-of-scope (not now, maybe later)

These are explicitly deferred. They are listed here so we don't reinvent the discussion later.

- Multi-target orchestration (run script across many machines at once).
- Scheduling (run script at time X).
- Webhooks / external triggers.
- Hardware-backed secret storage on the control station.
- A proper SPA frontend.
- Generic UDP/TCP packet sender.
- Tailscale-native authentication integration.

---

## Definition of done for v0.1.0

- Linux NAS hosts the control station, reached via browser over HTTPS.
- One Linux target and one Windows target can be registered.
- On each target, `csl-agent init` installs the user-level service correctly.
- The control station can start the agent on demand via the platform-appropriate service command, with no detachment hacks.
- Wake-on-LAN works against both targets.
- A one-off shell script can be staged, approved via `csl-agent approvals approve`, and run, with the parameter form rendered from YAML.
- A persistent script (llama.cpp) runs after approval, streams logs, and can be killed.
- Updating a script on the control station puts the corresponding target script into `update_pending` and blocks further runs until re-approved.
- A rejected script on the target produces a clear UI state and is not retried.
- Refresh-token rotation and revocation work.
- All actions appear in the audit log.
- `pip install`, `setup.sh`, and `systemctl status control-station` all behave as documented.
