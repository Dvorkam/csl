# Target-owner guide

For people who own a machine they want to make controllable — a gaming PC, a
home server, a NAS, a workstation. You install the **agent** once, and from then
on **you** decide what the control station is allowed to run.

This is the most important guarantee in the project: handing someone a
registration bundle is *not* handing them a shell. They get a buzzer. Every
script is gated on your explicit approval, and that approval is bound to exact
script content.

---

## The mental model

- The agent listens **only** on `127.0.0.1` (loopback). It is never exposed to
  the network.
- The control station reaches it by opening an **SSH tunnel** and port-forwarding
  to that loopback port. The only inbound port you open is SSH (22).
- The dedicated SSH key the control station uses is **locked to a forced
  command** — it cannot get a shell, only talk to the agent.
- The agent is **on-demand**: it starts when the control station needs it and
  shuts itself down after an idle timeout. No standing daemon.
- Every request the control station makes must carry a **bearer token** that was
  generated on your machine. No token, no access.

You can verify every one of these claims yourself — see
[Auditing and revoking access](#auditing-and-revoking-access).

---

## Installing the agent

Requires Python 3.11+.

```bash
pip install control-station-lite[agent]
```

The `[agent]` profile pulls in only the agent runtime — no server dependencies.

### 1. Check SSH prerequisites

```bash
csl-agent setup
```

This checks that an SSH server is installed and running and attempts to fix
common issues automatically. The agent is reached over SSH, so a working sshd is
a prerequisite. On Windows this checks the OpenSSH server feature.

### 2. Initialise

```bash
csl-agent init            # optional: --port PORT (default 36717)
```

`csl-agent init` is idempotent — re-running it upgrades an existing install
without invalidating your registration. It does all of the following:

- Creates the agent directory tree under `~/.csl/` (see
  [Where everything lives](#where-everything-lives-on-disk)).
- Generates an **Ed25519** SSH keypair under `~/.csl/keys/` (the private key
  never leaves the machine).
- Appends a **restricted** entry to your `authorized_keys`:
  ```
  command="csl-agent ssh-gateway",restrict,port-forwarding,permitopen="127.0.0.1:<port>" ssh-ed25519 AAAA...
  ```
  `restrict` removes all interactive capabilities; `permitopen` limits port
  forwarding to the agent's loopback port; the forced `command` means the key can
  only run the gateway, never a shell. (On Windows the entry goes into the
  Administrator `administrators_authorized_keys` file when appropriate.)
- Generates a random **API bearer token** and stores it in `config.yaml`.
- Writes a default `config.yaml` and an empty `approvals.json`.
- Installs the on-demand user service (systemd user unit / Task Scheduler task /
  launchd agent — see the table below).
- Prints a **registration bundle** and the **key fingerprint**.

| Platform | Service mechanism | Location |
| --- | --- | --- |
| Linux | systemd **user** unit (no `enable` — demand-only) | `~/.config/systemd/user/csl-agent.service` |
| Windows | Task Scheduler task, no triggers (demand-only) | — |
| macOS | launchd user agent | `~/Library/LaunchAgents/` |

### 3. Hand over the registration bundle

`init` prints a base64 blob labelled **REGISTRATION BUNDLE** plus a **key
fingerprint**. Give the bundle to whoever runs the control station. Communicate
the fingerprint over a **separate** channel so they can confirm it out-of-band at
registration time (the control station also pins your SSH host key TOFU-style and
shows its fingerprint for the same reason).

The bundle contains the private key, key fingerprint, agent port, scripts
directory, a hostname hint, the platform, the API token, and the agent version.
Treat it as a secret in transit.

> If you re-run `init` after a breaking change (e.g. a new bundle format), the
> admin must re-register the machine with the new bundle.

That's the entire setup. Nothing else runs until you approve it.

---

## The approval workflow

When the control station wants to run a script you haven't approved, it
**stages** the script into `~/.csl/scripts.pending/` and the script enters one of
these states. Approval is always *your* decision, made from the agent CLI on the
machine itself.

| State | What it means |
| --- | --- |
| `absent` | Not present on this machine. |
| `pending` | Staged, awaiting your first approval. |
| `approved` | You approved this exact content; it can run. |
| `update_pending` | You approved an older version; a changed version is staged. The old version is blocked until you re-approve. |
| `rejected` | You refused it. It will not run and will not be retried. |

Approval is bound to the script's **MD5**. If the admin edits a script, your
prior approval no longer applies and you'll be asked again — by design.

### `csl-agent approvals`

```bash
csl-agent approvals list               # all scripts + their states (with MD5 hints)
csl-agent approvals show <name>        # print the pending script content for review
csl-agent approvals diff <name>        # unified diff: approved vs pending (update_pending only)
csl-agent approvals approve <name>     # approve the pending version — it can now run
csl-agent approvals reject <name>      # refuse the pending version
csl-agent approvals clear <name>       # remove the script entirely, resetting to absent
csl-agent approvals purge [--dry-run]  # drop orphaned entries whose files are gone
```

A typical review:

```bash
csl-agent approvals list               # see what's pending
csl-agent approvals show backup_photos # read exactly what it would run
csl-agent approvals approve backup_photos
```

For an update, diff it against what you already trusted before deciding:

```bash
csl-agent approvals diff backup_photos
csl-agent approvals approve backup_photos   # or reject
```

### `csl-agent policy` — auto-approve

For scripts you fully trust and don't want to re-review on every edit, add them
to the auto-approve policy. Auto-approved scripts are approved automatically on
both first install **and** updates.

```bash
csl-agent policy show                       # current auto-approve list
csl-agent policy auto-approve sleep_machine # trust this script's future versions
csl-agent policy manual sleep_machine       # revoke auto-approval — back to manual review
```

Auto-approve is a deliberate trade-off: convenience for a script whose author you
trust, at the cost of not seeing each change. The control station can never add
to this list — only you can, from this CLI.

---

## Auditing and revoking access

You retain full control. To inspect or cut off the control station at any time:

**See exactly what the control-station key is allowed to do.** Look at the forced
command in your `authorized_keys`:

```bash
grep csl-agent ~/.ssh/authorized_keys
```

The `command="csl-agent ssh-gateway",restrict,...` prefix proves the key cannot
open a shell — it can only invoke the gateway, which itself executes only an
exact-match allowlist (the platform service-start command and a one-time config
read at registration). Anything else exits non-zero.

**Revoke access entirely.** Remove the control-station key line from
`authorized_keys`. The control station can no longer reach the agent at all.

```bash
# delete the line containing the ssh-gateway forced command
```

**Block individual scripts without cutting off the machine:**

```bash
csl-agent approvals reject <name>      # refuse a specific script
csl-agent approvals clear <name>       # remove it; control station must re-stage + you re-approve
csl-agent policy manual <name>         # stop auto-approving a script's updates
```

**Stop the agent.** It shuts down on its own after the idle timeout, but you can
stop the service immediately (e.g. `systemctl --user stop csl-agent` on Linux).
It will start again on demand only if the control-station key still exists.

---

## Where everything lives on disk

```
~/.csl/                          # %USERPROFILE%\.csl\ on Windows
├── scripts/                     # approved, runnable scripts
│   ├── <name>.sh / .ps1
│   └── <name>.meta.yaml
├── scripts.pending/             # staged, awaiting your approval
├── logs/
│   └── {job_uuid}.log           # per-job output
├── agent/
│   ├── running.json             # persistent-process tracking
│   └── approvals.json           # authoritative approval state (you own this)
├── keys/
│   ├── csl_ed25519              # private key — never transmitted
│   └── csl_ed25519.pub
└── config.yaml
```

`approvals.json` is the source of truth for what is approved on this machine. The
control station keeps a cached copy of these states but cannot change them — only
the agent (driven by your CLI commands and policy) writes this file.

---

## Configuration reference

`csl-agent init` writes `~/.csl/config.yaml` with sensible defaults. Edit it to
customise. All paths support `~` expansion; unknown fields are logged and
ignored.

```yaml
agent:
  listen_port: 36717                  # loopback port the agent listens on
  idle_timeout_seconds: 600           # shut down after 10 min with no running jobs
  lifecycle_check_interval_seconds: 10 # how often the idle-shutdown loop wakes
  log_tail_lines: 1000                # lines replayed when reconnecting to a log stream
  scripts_dir: ~/.csl/scripts
  pending_dir: ~/.csl/scripts.pending
  logs_dir: ~/.csl/logs
  state_path: ~/.csl/agent/running.json
  approvals_path: ~/.csl/agent/approvals.json

identity:
  key_fingerprint: "SHA256:..."       # written by init
  hostname_hint: "my-gaming-pc"
  api_token: "..."                    # bearer token the control station must present

approval_policy:
  auto_approve: []                    # script names trusted for automatic approval
                                      # manage via `csl-agent policy ...`, not by hand

advanced:
  # Windows only — override if your SSH install is non-standard
  windows_admin_authorized_keys_path: C:/ProgramData/ssh/administrators_authorized_keys
```

The idle timeout governs spin-down: with no persistent job running, the agent
exits after `idle_timeout_seconds`. Raise it if you find the agent restarting too
often during interactive sessions; lower it to minimise the time a process is
resident.
