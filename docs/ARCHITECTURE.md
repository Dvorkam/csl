# Architecture — control-station-lite

This document is the authoritative technical reference for the project. Pull requests that contradict it must update it.

---

## 1. System overview

```
┌──────────────────────────────────────────────┐         ┌────────────────────────────┐
│                NAS (control station)         │         │     Target machine          │
│                                              │         │                             │
│   Browser ──HTTPS──► [nginx] ──► [FastAPI]   │         │   ~/.csl/                   │
│                       TLS         server     │         │   ├── scripts/              │
│                                    │         │         │   ├── logs/                 │
│                                    ▼         │         │   └── agent/                │
│                                 [SQLite]     │  SSH    │       └── running.json      │
│                                              │ tunnel  │                             │
│                                  SSH client ─┼─────────┤── sshd ── csl-agent          │
│                                              │ :22     │              localhost:N    │
└──────────────────────────────────────────────┘         └─────────────────────────────┘
```

Two cooperating processes communicate exclusively over SSH. The control station holds canonical state and presents the user interface. The agent is the only component that runs scripts and tracks processes on a target machine.

---

## 2. Components

### 2.1 Control station (`control_station_lite.server`)

A FastAPI application. Responsibilities:

- Serve the web UI (Jinja2 + HTMX).
- Expose a JSON REST API for the same operations.
- Authenticate users (JWT).
- Maintain the canonical script library.
- Hold the per-machine connection details (SSH endpoint, key, agent port, paths).
- Drive SSH connections and tunnels to agents.
- Issue platform-specific service-start commands to spawn agents on demand.
- Proxy SSE log streams from agents to the browser.
- Implement built-in actions that do not require an agent (e.g. Magic Packet, which is a UDP broadcast issued by the control station itself).
- Track per-machine approval state for each script (mirror of authoritative state on agent).
- Persist users, machines, scripts, jobs, and the audit log in SQLite.

### 2.2 Agent (`control_station_lite.agent`)

A small FastAPI application. **The agent is the only process on the target machine that the control station ever interacts with. There is no direct shell execution path over SSH; every action — including trivial one-off scripts — goes through the agent's API.**

Responsibilities:

- Listen on `127.0.0.1:<port>` only.
- Receive job requests from the control station (via SSH tunnel).
- Maintain approval state for every script: a script can only run if its current content has been explicitly approved by the target owner (or matches a per-script auto-approve policy).
- Stage new and updated scripts in a `scripts.pending/` directory until approved.
- Execute scripts from the local approved scripts directory.
- Track persistent processes; serve their logs over SSE.
- Maintain `running.json` so persistent process state survives an agent restart.
- Self-terminate when idle: no active connections, no running persistent jobs, idle timer expired.
- Provide a CLI (`csl-agent approvals ...`) for the target owner to review and approve scripts.

Installed as a user-level service:

- **Linux:** systemd `--user` unit. Started by `systemctl --user start csl-agent`.
- **Windows:** Task Scheduler task. Started by `schtasks /run /tn "CSL-Agent"`.
- **macOS:** launchd user agent (`launchctl kickstart`).

The control station starts the agent by issuing the platform-appropriate one-shot SSH command, which exits immediately while the OS takes over process supervision.

### 2.3 Shared (`control_station_lite.shared`)

Pydantic models and parsers used by both sides:

- Job request/response models.
- Script metadata schema (`.meta.yaml`).
- Registration bundle format.
- Constants (default ports, paths).

### 2.4 nginx

In front of the control station. Responsibilities:

- TLS termination using user-supplied certificates (self-signed, Let's Encrypt, or otherwise).
- Rate limiting on auth endpoints.
- Request size limits.
- Static file serving.

### 2.5 SQLite

Single-file database, stored on a volume that survives container restarts. Tables defined in section 5.

---

## 3. Communication and lifecycle

### 3.1 Adding a machine (one-time setup)

Performed by the target owner and the control station admin together.

**Target side:**

1. Target owner installs the agent runtime: `pip install control-station-lite[agent]`.
2. Target owner runs `csl-agent init`. This command:
   - Creates the folder structure under `~/.csl/` (or platform equivalent).
   - Generates an SSH keypair dedicated to control station use.
   - Adds the public key to `~/.ssh/authorized_keys`, optionally with a `command=` restriction.
   - Writes the agent config to the platform's app-data directory (see section 6.2).
   - Prints a **registration bundle** once: the private key plus connection metadata, base64-encoded as a single string.
3. Target owner sends the registration bundle to the control station admin through any secure channel.

**Control station side:**

4. Admin opens the "Add Machine" form, pastes the registration bundle, supplies a friendly name and the target's SSH host/port.
5. Control station decodes the bundle, performs a one-time connection test (SSH in, read the agent config, verify key fingerprint matches), and stores the machine record in SQLite. The private key is encrypted at rest using a key derived from a master secret in the control station's environment.

### 3.2 Running a script on a machine

All script execution — one-off or persistent, trivial or complex — goes through the agent. There is no agentless path.

1. User requests action via the web UI.
2. Control station looks up the machine record (host, port, key, agent port, scripts dir, platform).
3. Control station opens an SSH connection with a local port forward to the agent (`-L localport:127.0.0.1:agentport`).
4. Control station pings the agent's `/healthz` through the tunnel.
5. If the agent does not respond within the connection timeout, the control station issues the platform-appropriate service-start command in a short-lived SSH exec session:
   - Linux: `systemctl --user start csl-agent`
   - Windows: `schtasks /run /tn "CSL-Agent"`
   - macOS: `launchctl kickstart gui/$UID/com.controlstationlite.agent`
   This SSH session exits immediately; the OS takes responsibility for the agent process. The control station then polls `/healthz` through the tunnel with exponential backoff (up to ~5 seconds total).
6. Once the agent is reachable, the control station resolves the script's approval state (see §3.3) and then submits the job: `POST /jobs` to the agent with script name, parameters, and a generated `job_uuid`.
7. Agent validates the request (script is approved, parameters match metadata), starts the process, and returns the job ID.
8. For persistent jobs, the UI subscribes to `/jobs/{id}/stream` (SSE), which the control station proxies from the agent.

### 3.3 Script lifecycle and approval

Scripts are canonical on the control station. The control station holds the master copy and tracks per-machine approval state. The agent is the authoritative source for whether a script is currently approved on that specific target: target owners can revoke or change approval state at any time without involving the control station.

**Per-script states (from the agent's point of view):**

| State | Meaning |
| --- | --- |
| `absent` | Agent has no copy of this script. |
| `pending` | A copy is staged in `scripts.pending/`, not yet approved to run. |
| `approved` | Script is in `scripts/` with a recorded approved MD5 matching its current content. |
| `update_pending` | Script is in `scripts/` (previously approved), but a new version is staged in `scripts.pending/` awaiting re-approval. |
| `rejected` | Target owner explicitly rejected this script. Will not be re-staged unless the target owner clears the rejection. |

**Sync and approval flow when the control station wants to run script X on machine M:**

1. Control station asks agent for current state of X.
2. If `approved` and MD5 matches what the control station expects → proceed to run.
3. If `approved` but MD5 differs → push new version to `scripts.pending/`; state becomes `update_pending`; agent returns `pending_approval (update)` error; UI surfaces the pending state and re-run is blocked until approval.
4. If `absent` → push to `scripts.pending/`; state becomes `pending`; agent returns `pending_approval (new)` error; UI shows pending state.
5. If `pending` or `update_pending` already → push only if the staged content differs from what's already pending; return same `pending_approval` error.
6. If `rejected` → agent returns `rejected` error. The control station does not retry. The target owner must clear the rejection via the agent CLI before any further attempt.

**Target owner workflow (on the target machine):**

```
csl-agent approvals list                    # show all scripts and their states
csl-agent approvals show <script_name>      # display content of pending script for review
csl-agent approvals diff <script_name>      # if updating, show diff vs approved version
csl-agent approvals approve <script_name>   # promote pending → approved
csl-agent approvals reject <script_name>    # mark as rejected
csl-agent approvals clear <script_name>     # remove the script entirely; clears rejection
csl-agent policy auto-approve <script_name> # whitelist for future auto-approval
csl-agent policy manual <script_name>       # remove from auto-approve whitelist
```

**Auto-approve policy:**

- Default: `manual` for everything.
- Per-script whitelist stored in `~/.csl/config.yaml` under `approval_policy.auto_approve` (list of script names).
- A whitelisted script auto-approves both initial install and subsequent updates.
- Removing a script from the whitelist does not retroactively revoke approval, but the next change will require manual approval again.
- There is **no** "trust on first use" or "auto-approve minor changes" mode by design — these defeat the security goal.

**Authoritative state location:**

- Agent owns `~/.csl/approvals.json` — the source of truth for what is approved on this target.
- Control station's `script_sync_state` table is a cache for UI display; it is updated from agent responses and may be briefly stale.

### 3.4 Agent lifecycle

The agent is ephemeral:

- Installed once as a user-level service by `csl-agent init`; never enabled to start at login.
- Started on demand by the control station via the platform-appropriate one-shot service-start command.
- Listens on `127.0.0.1` only. Port number stored in the target's agent config.
- Tracks two counters: number of running persistent jobs, and seconds since last client request.
- Self-terminates when: `running_persistent_jobs == 0` AND `idle_seconds > idle_timeout` (default 300s).
- On exit, the OS marks the service inactive but does not restart it (unit configured `Restart=no`; task triggers are demand-only).
- Re-spawnable at any time via the same start command. On startup, reads `running.json` to recover knowledge of persistent processes that may have outlived a previous agent instance.

Note: an agent restart while persistent jobs are running is recoverable for *tracking* but not for *log streaming*. Logs are written to disk by the agent; in-flight SSE subscribers will disconnect and need to reconnect, at which point streaming resumes from the current log file tail.

### 3.5 Built-in actions

A small set of actions do not require an agent on the target because they don't run anything *on* the target — they act *toward* it from the control station:

- **Wake-on-LAN**: builds a Magic Packet from the target's stored MAC address and broadcasts it on UDP/9 (or a user-configured port). The target need not even be online — that's the point.
- **SSH reachability ping**: tests whether the SSH endpoint accepts a connection. Does not start the agent.

Apart from these, everything goes through the agent.

---

## 4. Script metadata format

A script with no parameters needs only the script file itself.

A script with parameters must be accompanied by a YAML file of the same base name (`my_script.sh` + `my_script.meta.yaml`).

### 4.1 Schema

```yaml
description: |
  Multi-line description shown in the UI.
  Markdown supported.

persistent: false           # default false; true means it's a long-running job

tags:                       # optional, for UI grouping
  - llm
  - dev-tools

params:
  - name: model_path
    type: string            # string | int | float | bool | choice | path
    required: true
    help: "Filesystem path to the GGUF model file."

  - name: context_size
    type: int
    default: 4096
    min: 512
    max: 32768
    help: "Context window size."

  - name: gpu_layers
    type: choice
    choices: [0, 16, 32, "all"]
    default: "all"
    help: "Number of layers to offload to GPU."
```

### 4.2 Parameter passing

Parameters are passed as environment variables to the script, with names uppercased and prefixed `CSL_PARAM_`:

```bash
CSL_PARAM_MODEL_PATH=/path/to/model.gguf
CSL_PARAM_CONTEXT_SIZE=4096
CSL_PARAM_GPU_LAYERS=all
```

This avoids shell-quoting concerns inherent in positional arguments and works identically on Linux and Windows shells.

---

## 5. Data model

SQLite database, single file, managed by SQLAlchemy with Alembic migrations.

### 5.1 Tables

**users**

| Column | Type | Notes |
| --- | --- | --- |
| id | INTEGER PK | |
| username | TEXT UNIQUE | |
| password_hash | TEXT | bcrypt |
| role | TEXT | `user` or `admin` |
| created_at | TIMESTAMP | |
| disabled | BOOLEAN | |

**refresh_tokens**

| Column | Type | Notes |
| --- | --- | --- |
| id | INTEGER PK | |
| user_id | INTEGER FK | |
| token_hash | TEXT | hash of the refresh token JTI |
| issued_at | TIMESTAMP | |
| expires_at | TIMESTAMP | |
| revoked | BOOLEAN | |

**machines**

| Column | Type | Notes |
| --- | --- | --- |
| id | INTEGER PK | |
| name | TEXT UNIQUE | friendly name |
| ssh_host | TEXT | |
| ssh_port | INTEGER | |
| ssh_user | TEXT | |
| ssh_key_encrypted | BLOB | encrypted with master key |
| key_fingerprint | TEXT | from registration bundle |
| agent_port | INTEGER | from agent config |
| scripts_dir | TEXT | path on target |
| mac_address | TEXT | for Wake-on-LAN, optional |
| created_at | TIMESTAMP | |

**user_machines** (bookmark / access)

| Column | Type | Notes |
| --- | --- | --- |
| user_id | INTEGER FK | |
| machine_id | INTEGER FK | |
| (composite PK) | | |

**scripts**

| Column | Type | Notes |
| --- | --- | --- |
| id | INTEGER PK | |
| name | TEXT UNIQUE | filename without extension |
| content | TEXT | script body, canonical |
| meta_yaml | TEXT NULLABLE | metadata YAML, canonical |
| md5 | TEXT | MD5 of `content` |
| persistent | BOOLEAN | parsed from meta |
| updated_at | TIMESTAMP | |
| updated_by | INTEGER FK users.id | |

**script_target_state** (renamed from `script_sync_state`; cache of authoritative state held by agent)

| Column | Type | Notes |
| --- | --- | --- |
| machine_id | INTEGER FK | |
| script_id | INTEGER FK | |
| state | TEXT | `absent` `pending` `approved` `update_pending` `rejected` |
| approved_md5 | TEXT NULLABLE | MD5 currently approved on target, if any |
| pending_md5 | TEXT NULLABLE | MD5 of staged version awaiting approval, if any |
| last_refreshed_at | TIMESTAMP | when state was last fetched from agent |
| (composite PK on machine_id+script_id) | | |

**jobs**

| Column | Type | Notes |
| --- | --- | --- |
| id | INTEGER PK | |
| job_uuid | TEXT UNIQUE | UUID also known to agent |
| machine_id | INTEGER FK | |
| script_id | INTEGER FK NULLABLE | null for built-in actions |
| built_in_action | TEXT NULLABLE | e.g. `wol` |
| user_id | INTEGER FK | who ran it |
| params_json | TEXT | submitted parameter values |
| status | TEXT | `pending` `running` `completed` `failed` `killed` |
| persistent | BOOLEAN | |
| started_at | TIMESTAMP | |
| ended_at | TIMESTAMP NULLABLE | |
| exit_code | INTEGER NULLABLE | |
| log_path | TEXT NULLABLE | path on target, for retrieval |

**audit_log**

| Column | Type | Notes |
| --- | --- | --- |
| id | INTEGER PK | |
| timestamp | TIMESTAMP | |
| user_id | INTEGER FK NULLABLE | null for system events |
| action | TEXT | e.g. `script.run`, `machine.add`, `script.edit`, `user.login` |
| target_type | TEXT | `machine` `script` `user` `job` `system` |
| target_id | TEXT | string for flexibility |
| result | TEXT | `success` `failure` |
| details_json | TEXT NULLABLE | structured detail |

---

## 6. File and folder structures

### 6.1 Python package

```
control_station_lite/
├── __init__.py
├── server/
│   ├── __init__.py
│   ├── main.py                # FastAPI app entrypoint
│   ├── config.py              # Settings (pydantic-settings)
│   ├── api/
│   │   ├── auth.py
│   │   ├── machines.py
│   │   ├── scripts.py
│   │   ├── jobs.py
│   │   ├── builtin.py
│   │   ├── audit.py
│   │   └── admin.py
│   ├── auth/
│   │   ├── jwt.py
│   │   ├── password.py
│   │   └── dependencies.py    # FastAPI auth dependencies
│   ├── core/
│   │   ├── ssh.py             # SSH connection + tunnel management
│   │   ├── agent_client.py    # Talks to agent through tunnel
│   │   ├── script_registry.py
│   │   ├── script_sync.py     # MD5-based sync
│   │   ├── magic_packet.py
│   │   └── crypto.py          # Encrypt/decrypt stored SSH keys
│   ├── db/
│   │   ├── models.py
│   │   ├── session.py
│   │   └── migrations/        # Alembic
│   ├── templates/             # Jinja2
│   └── static/
├── agent/
│   ├── __init__.py
│   ├── __main__.py            # `python -m control_station_lite.agent`
│   ├── main.py                # Agent FastAPI app
│   ├── cli.py                 # `csl-agent init`, `csl-agent approvals ...`, `csl-agent policy ...`
│   ├── config.py
│   ├── service_installer.py   # systemd --user / Task Scheduler / launchd
│   ├── process_manager.py     # Run + track persistent processes
│   ├── script_runner.py       # One-off execution
│   ├── log_stream.py          # SSE for live logs
│   ├── lifecycle.py           # Idle shutdown
│   ├── state.py               # running.json
│   └── approvals.py           # approvals.json + state machine
└── shared/
    ├── __init__.py
    ├── models.py              # Pydantic models used both sides
    ├── script_meta.py         # YAML parsing + validation
    └── registration.py        # Bundle encode/decode
```

### 6.2 Target machine — `~/.csl/`

```
~/.csl/                                    # Linux/macOS
%USERPROFILE%\.csl\                        # Windows (home-dir equivalent of ~/.csl/)
├── scripts/                               # Approved scripts (runnable)
│   ├── start_llama.sh
│   ├── start_llama.meta.yaml
│   ├── sleep.sh
│   └── ...
├── scripts.pending/                       # Staged, awaiting approval
│   └── ...
├── logs/
│   └── {job_uuid}.log
├── agent/
│   ├── running.json                       # persistent-process tracking
│   └── approvals.json                     # authoritative approval state
├── keys/
│   ├── csl_ed25519                        # private key (host-side, never transmitted)
│   └── csl_ed25519.pub                    # public key (added to authorized_keys)
└── config.yaml
```

`config.yaml`:

```yaml
agent:
  listen_port: 47731
  idle_timeout_seconds: 300
  scripts_dir: ~/.csl/scripts
  pending_dir: ~/.csl/scripts.pending
  logs_dir: ~/.csl/logs
  state_path: ~/.csl/agent/running.json
  approvals_path: ~/.csl/agent/approvals.json

identity:
  key_fingerprint: "SHA256:..."
  hostname_hint: "my-gaming-pc"

approval_policy:
  # Scripts in this list are auto-approved for both first install and updates.
  # Empty by default. Add entries via `csl-agent policy auto-approve <name>`.
  auto_approve: []
```

`approvals.json` schema:

```json
{
  "scripts": {
    "start_llama": {
      "state": "approved",
      "approved_md5": "a1b2c3...",
      "approved_at": "2026-05-13T10:00:00Z",
      "approved_via": "cli"
    },
    "restart_machine": {
      "state": "update_pending",
      "approved_md5": "deadbeef...",
      "pending_md5": "cafe1234...",
      "approved_at": "2026-04-01T08:00:00Z"
    },
    "dangerous_script": {
      "state": "rejected",
      "rejected_at": "2026-05-10T12:00:00Z"
    }
  }
}
```

### 6.3 NAS — control station

```
/var/lib/control-station-lite/             # Docker volume mounted here
├── db/
│   └── control-station.sqlite
├── scripts/                                # Canonical scripts (mirrored from DB on edit)
│   └── ...
├── secrets/
│   ├── master.key                          # Encryption key for SSH keys at rest
│   └── jwt.key                             # JWT signing key
├── certs/
│   ├── server.crt
│   └── server.key
└── logs/
    └── control-station.log
```

---

## 7. Security model

### 7.1 Authentication

- Passwords hashed with bcrypt (`passlib`).
- JWT access tokens, 30-minute lifetime, signed with HS256 using a key from `secrets/jwt.key`.
- JWT refresh tokens, 14-day lifetime, stored in an `HttpOnly`, `Secure`, `SameSite=Strict` cookie. Each refresh token has a unique JTI; its hash is stored in `refresh_tokens` to support revocation.
- Access tokens carry `sub` (user id), `role`, and `exp`.
- Token rotation: a successful refresh issues a new access token AND a new refresh token; the old refresh token is marked revoked.
- Rate limiting on `/login` and `/refresh` (enforced at nginx).

### 7.2 Authorization

Two roles:

- `user`: can view bookmarked machines, run scripts, view their own job history.
- `admin`: everything user can do, plus add/edit/delete scripts, add/edit/remove machines, manage users, view full audit log.

Role checked via FastAPI dependency on every protected route. Admin-only routes also re-check at the data layer.

### 7.3 Transport security

- nginx terminates TLS using user-supplied certificates. The user is free to use self-signed, Let's Encrypt, or an internal CA. No coupling to any cert provider.
- All control-station-to-agent traffic travels through SSH-tunneled connections. The agent never listens on a network-routable interface.

### 7.4 Target machine autonomy

The target machine is a fully sovereign participant in the system. The target owner controls four layers of access independently:

- **Authentication:** the control station authenticates to a target using exactly one SSH key, dedicated to it. The target owner controls `authorized_keys` and can revoke at any time.
- **Protocol surface:** the only thing the control station's SSH key can reach is the agent service (started via systemd/Task Scheduler) and the agent's API. There is no general shell execution path. Tightening `authorized_keys` with a `command=` restriction is possible but not required for security; the agent's API is the only thing that's actually exposed.
- **Code:** no script can run without an explicit approval recorded in `~/.csl/agent/approvals.json`. Approval is bound to a specific MD5 — any change to a script revokes its approval and requires re-approval. The target owner reviews, approves, rejects, or removes scripts via `csl-agent approvals` commands.
- **Footprint:** the target owner can wipe `~/.csl/` to permanently remove the control station's footprint. They can also disable the systemd service / Task Scheduler task to make the agent unstartable.

This means three different actions are needed for an attacker who has compromised the control station to gain meaningful access to a given target: (1) hold the SSH key, (2) succeed in submitting a script to the agent, and (3) have that script be either pre-approved or whitelisted for auto-approval. A target owner who carefully reviews each approval has a strong veto even against a fully compromised control station.

### 7.5 SSH key storage on control station

- Private keys for each target are encrypted with a master key derived from `secrets/master.key` using AES-256-GCM with a per-record nonce.
- `secrets/master.key` is mounted into the container from the host filesystem; permissions restricted to the container's user.
- Keys are decrypted into memory only when needed for an SSH operation.

### 7.6 Threat model boundaries

In scope:

- Casual network attackers (handled by TLS + private network tunnel).
- Token theft (mitigated by short access token lifetime, HttpOnly cookie for refresh, revocation list).
- Compromise of one target (does not propagate; each target has its own key).
- **Compromise of the control station.** Materially reduced by the approval system: an attacker on the control station can attempt to push and run scripts, but cannot bypass the target owner's approval requirement. They are restricted to scripts already approved on a given target (and to actions on the auto-approve list). The blast radius depends entirely on how much the target owner has trusted.

Out of scope:

- Compromise of a target machine itself. If an attacker has local code execution on the target, they can subvert the agent and the approvals file. The control-station system can do no more than the target's own OS protections.
- Compromise of the control station combined with social-engineering the target owner into approving a malicious script.

Note that the approval system materially changes the threat picture compared to traditional remote execution tools. In a silent-sync model, a compromised control station gives an attacker arbitrary code execution on every target as the configured SSH user. With per-script approval bound to MD5, the attacker is restricted to the set of scripts already trusted by each individual target owner.

---

## 8. Frontend approach

- Server-rendered Jinja2 templates.
- HTMX for partial updates (machine list refresh, job status updates).
- A small amount of vanilla JavaScript for SSE log streaming and form interactions.
- No build pipeline. No node_modules. Templates and static assets ship as package data inside the PyPI distribution.

Pages:

- `/login` — login form.
- `/` — dashboard: list of bookmarked machines with reachability indicators.
- `/machines/{id}` — machine detail: status panel, running-jobs list, available-scripts list. Each script displays its approval state on this target (`approved` / `pending` / `update_pending` / `rejected` / `absent`). The list is sourced from `script_target_state` and refreshed on demand.
- `/machines/{id}/jobs/{job_id}` — live log viewer (SSE).
- `/machines/{id}/scripts/{name}/request-approval` — POST endpoint (HTMX): force a fresh re-stage attempt for a script that is `pending` or `update_pending`, useful if the target owner says they're ready to review.
- `/admin/scripts` — script library editor (admin only).
- `/admin/scripts/{name}` — script editor with metadata form.
- `/admin/machines` — machine management (admin only).
- `/admin/users` — user management (admin only).
- `/admin/audit` — audit log viewer (admin only).

---

## 9. Distribution and deployment

### 9.1 PyPI

Two install profiles:

```toml
# pyproject.toml (excerpt)
[project]
name = "control-station-lite"
dependencies = [
  # shared minimal deps
  "fastapi",
  "pydantic",
  "pydantic-settings",
  "pyyaml",
  "uvicorn",
]

[project.optional-dependencies]
server = [
  "sqlalchemy",
  "alembic",
  "passlib[bcrypt]",
  "python-jose[cryptography]",
  "asyncssh",
  "jinja2",
  "cryptography",
]
agent = [
  # agent needs only the shared deps above; this extra exists to document intent
]

[project.scripts]
csl-server = "control_station_lite.server.main:main"
csl-agent  = "control_station_lite.agent.cli:main"
```

Target machines install with `pip install control-station-lite[agent]`; the NAS installs with `pip install control-station-lite[server]`.

### 9.2 Docker

Two containers, one prod compose file, one dev override:

- `app` — the FastAPI server (uvicorn).
- `nginx` — edge concerns only: TLS termination, rate limiting, request-size limits.

Volumes mounted: `/var/lib/control-station-lite/{db,scripts,secrets,certs,logs}`.

**Production image** (`deploy/Dockerfile`): package baked in at build time, pinned to the release version. Built and pushed by `release.yml`, tagged to match the PyPI release. No runtime dependency on the source tree.

**Dev image** (`deploy/Dockerfile`, dev stage + `deploy/docker-compose.override.yml`): bind-mounts the source tree and does an editable install so code changes are reflected without rebuilding. The asymmetry between dev and prod is intentional.

### 9.3 systemd

`deploy/control-station.service` runs `docker compose up` and ensures the stack is restarted on failure / at boot. Installed under `/etc/systemd/system/` by the bootstrap script.

### 9.4 Bootstrap script

`scripts/setup.sh` (run once on the NAS):

1. Verify dependencies: Docker, `docker compose`, systemd, openssl. Install if missing where possible.
2. Create `/var/lib/control-station-lite/` with subdirectories.
3. Generate `secrets/master.key` and `secrets/jwt.key` if not present.
4. Generate self-signed cert into `certs/` (interactive prompt for hostname; user can replace later).
5. Pull the Docker image and stage `docker-compose.yml`, `nginx.conf`.
6. Install the systemd unit, enable and start it.
7. Run the initial admin-user-creation flow (prompt for username and password).
8. Print the URL the service is reachable on.

---

## 10. Error handling and observability

- Structured JSON logging from both server and agent, written to stdout (collected by Docker / systemd journal on the NAS side, captured to `logs/` on the agent side).
- Correlation IDs on every request, propagated through agent calls so a single user action can be traced end-to-end.
- Health endpoints: `/healthz` on both server and agent. The server's exposes basic info (version, DB reachable). The agent's reports running persistent jobs and idle time.
- All exceptions in FastAPI surfaced as structured error responses with stable error codes.

---

## 11. Versioning and compatibility

- Single PyPI version covers both server and agent. They must match. The control station refuses to register an agent whose major version differs.
- Database migrations are forward-only via Alembic. Downgrades are not supported.
- Script metadata format is versioned via a top-level `schema_version` field (default `1`).
