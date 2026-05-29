# control-station-lite

> A self-hosted dashboard for running scripts on your LAN machines — where the machine owner decides what can run.

| Component | Status |
|---|---|
| Agent — `csl-agent` | Alpha — install and use today |
| Control station + web UI | Alpha — functional, packaging in progress |

---

## The idea

You want to remotely run scripts on your home server, gaming PC, or NAS. SSH gives whoever holds the key unlimited access. **control-station-lite gives them a buzzer instead.**

- Every script is reviewed and explicitly approved by the target machine's owner before it can run.
- Script changes require re-approval — an update is not silently trusted.
- The machine owner can whitelist scripts they trust for automatic approval, and revoke that whitelist at any time.
- The agent is ephemeral: it starts on demand and shuts itself down when idle. No persistent daemon, no open port, no standing access.

**It runs on your hardware, in your network. No cloud, no accounts, no vendor lock-in.**

---

## How it works

```
  You (browser)          NAS (control station)          Target machine
       │                         │                             │
       │  Click "Run script"     │                             │
       │────────────────────────>│  SSH tunnel (port forward)  │
       │                         │────────────────────────────>│  starts agent
       │                         │                             │  checks approval
       │                         │  REST over SSH tunnel       │  runs script
       │  Live log stream        │<────────────────────────────│
       │<────────────────────────│                             │
```

The control station never gets a shell on the target — it only talks to the agent over a forwarded port inside an SSH tunnel. The agent holds the keys; the target owner holds the approval policy.

---

## Platform support

| Platform | Agent service | Notes |
|---|---|---|
| Linux | systemd user unit | `~/.config/systemd/user/csl-agent.service` |
| Windows | Task Scheduler | No triggers — demand-only |
| macOS | launchd | `~/Library/LaunchAgents/` |

---

## Agent quick-start

The agent runs on each target machine. Install it once; the control station starts it on demand over SSH.

### 1. Install

Requires Python 3.11+.

```bash
pip install control-station-lite[agent]
```

### 2. Check SSH prerequisites

```bash
csl-agent setup
```

Checks that an SSH server is installed and running, and attempts to fix common issues automatically.

### 3. Initialise

```bash
csl-agent init
```

This will:
- Create the agent directory structure (`~/.csl/`)
- Generate an Ed25519 SSH keypair
- Append the public key to your `authorized_keys`
- Write a default `config.yaml`
- Install the service (systemd / Task Scheduler / launchd)
- Print a **registration bundle** — a base64 blob you hand to the control station admin

Copy the registration bundle and give it to whoever manages the control station. That's the entire setup.

### 4. Approve scripts

Once connected, the control station will push scripts for your review. Manage them with:

```bash
csl-agent approvals list               # see all scripts and their state
csl-agent approvals show <name>        # read the script content
csl-agent approvals diff <name>        # diff an updated version against approved
csl-agent approvals approve <name>     # approve — script can now run
csl-agent approvals reject <name>      # reject — script will not run
csl-agent approvals clear <name>       # remove a script entirely
```

Auto-approve trusted scripts so they don't need manual review on every update:

```bash
csl-agent policy auto-approve sleep_machine
csl-agent policy show
csl-agent policy manual sleep_machine   # revoke auto-approval
```

---

## Control station quick-start

The control station is the always-on component (typically a NAS or home server) that hosts the web UI.

> **Note:** Docker/nginx packaging is in progress (Phase 10). For now, run it directly.

### 1. Install

```bash
pip install control-station-lite[server]
```

### 2. Create secrets

```bash
# JWT signing key
openssl rand -hex 64 > secrets/jwt.key

# Master encryption key (must be exactly 32 bytes, base64-encoded)
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())" > secrets/master.key
```

### 3. Configure

Set environment variables (or use a `.env` file):

```bash
export CSL_JWT_KEY_PATH=secrets/jwt.key
export CSL_MASTER_KEY_PATH=secrets/master.key
export CSL_DATABASE_URL=sqlite+aiosqlite:///data/csl.db   # default
```

### 4. Initialise the database and create an admin user

```bash
csl-server migrate          # runs Alembic migrations
csl-admin create-admin      # prompts for username + password
```

### 5. Start

```bash
csl-server
```

The web UI is available at `http://localhost:8000`.

---

## Web UI features

- **Dashboard** — machine list with live SSH reachability indicator
- **Machine detail** — approval state for every script, Wake-on-LAN, running persistent jobs
- **Script approval flow** — badge per script: `approved` / `pending` / `update_pending` / `rejected` / `approved_stale`; one-click stage/re-stage; auto-refresh on page open (single SSH tunnel per machine)
- **Script run dialog** — dynamic form built from script metadata (string / int / float / bool / choice params); approval errors surfaced inline
- **Live log viewer** — SSE-based streaming, auto-scrolling, kill button for persistent jobs
- **Job history** — filterable list of all past runs, links to log viewer
- **Admin panel** — script library editor, machine management, user management, audit log viewer

---

## Agent configuration

`csl-agent init` writes `~/.csl/config.yaml` with sensible defaults. Edit it to customise:

```yaml
agent:
  listen_port: 36717               # what the agent listens on (loopback only)
  idle_timeout_seconds: 600        # shut down after 10 min of no activity
  lifecycle_check_interval_seconds: 10
  log_tail_lines: 1000             # lines replayed when reconnecting to a log stream

approval_policy:
  auto_approve: []                 # script names trusted for automatic approval

advanced:
  # Windows only — override if your SSH install is non-standard
  windows_admin_authorized_keys_path: C:/ProgramData/ssh/administrators_authorized_keys
```

All paths support `~` expansion. Unknown fields are logged and ignored.

---

## License

[GNU Affero General Public License v3.0](LICENSE) or later, with an additional permission for distribution through app stores.

AGPL is chosen because this project is designed to run as a network service. Anyone who modifies it and operates it over a network must publish their changes under the same terms. The app store exception covers distribution through platforms (e.g. Apple App Store) whose terms would otherwise conflict.
