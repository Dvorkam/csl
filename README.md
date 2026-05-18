# control-station-lite

> A self-hosted dashboard for running scripts on your LAN machines — where the machine owner decides what can run.

**Status:** Early development. The agent (target machine side) is functional today. The control station (web UI + server) is under active development.

| Component | Status |
|---|---|
| Agent — `csl-agent` | Alpha — install and use today |
| Control station + web UI | In development |

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

The agent runs on each target machine. Install it once; the control station will start it on demand over SSH.

### 1. Install

Requires Python 3.11+.

```bash
pip install control-station-lite[agent]
```

### 2. Check SSH prerequisites

```bash
csl-agent setup
```

This checks that an SSH server is installed and running, and attempts to fix common issues automatically.

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

## Configuration

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

All paths support `~` expansion. Unknown fields are logged and ignored (safe to add comments).

---

## Control station

The control station is the NAS-side component: a web UI and REST API that coordinates script management, user access, and communication with agents. It is currently under active development.

Planned deployment: Docker Compose on a NAS or always-on Linux host, fronted by nginx for TLS. A single bootstrap script handles first-time setup.

Planned features for v0.1:
- Web UI with per-machine dashboards
- Script library with per-user parameter forms
- Persistent process management with live log streaming
- Wake-on-LAN
- Audit log
- User/admin roles with JWT auth

---

## License

[GNU Affero General Public License v3.0](LICENSE) or later, with an additional permission for distribution through app stores.

AGPL is chosen because this project is designed to run as a network service. Anyone who modifies it and operates it over a network must publish their changes under the same terms. The app store exception covers distribution through platforms (e.g. Apple App Store) whose terms would otherwise conflict.
