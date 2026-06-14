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

## Why you'd want it

- **A single dashboard** for every machine on your LAN — wake them, run scripts, stream logs, kill jobs, all from a browser.
- **You stay in control of your own hardware.** Handing out a registration bundle is not handing out a shell. The control-station's SSH key is locked to a forced command; it can only talk to an approval-gated agent, never open a shell.
- **No attack surface at rest.** No agent port is ever exposed; everything rides an SSH tunnel. The agent isn't even running until something needs it.
- **Batteries included.** Wake-on-LAN, sleep/restart, Steam, a llama.cpp server — a starter catalogue ships in the box. Add your own with a shell script and a few lines of YAML.
- **Cross-platform targets** — Linux, Windows, and macOS.

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

## Get started

Pick the guide for what you're doing — each has the full step-by-step:

- **Run the control station** (NAS / home server) → [Operator guide](docs/guides/operator.md)
  ```bash
  sudo scripts/setup.sh        # idempotent bootstrap: data dirs, secrets, TLS, the stack
  ```
- **Make a machine controllable** (install the agent) → [Target-owner guide](docs/guides/target-owner.md)
  ```bash
  pip install control-station-lite[agent]
  csl-agent setup && csl-agent init     # prints a registration bundle for the admin
  ```
- **Use the web UI** → [User guide](docs/guides/user.md)
- **Manage scripts, users, machines** → [Admin guide](docs/guides/admin.md)

All guides live in **[docs/guides/](docs/guides/)**. For the full design, see
[docs/dev/ARCHITECTURE.md](docs/dev/ARCHITECTURE.md).

---

## License

[GNU Affero General Public License v3.0](LICENSE) or later, with an additional permission for distribution through app stores.

AGPL is chosen because this project is designed to run as a network service. Anyone who modifies it and operates it over a network must publish their changes under the same terms. The app store exception covers distribution through platforms (e.g. Apple App Store) whose terms would otherwise conflict.
