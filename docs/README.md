# control-station-lite

A lightweight, self-hosted web service for controlling machines on a LAN. Provides a web UI and REST API for executing shell scripts on remote machines, managing persistent processes, and performing basic network operations such as Wake-on-LAN.

Designed for personal infrastructure and small home labs. Intended to be reached over a private network tunnel (e.g. Tailscale) and run on a NAS or similar always-on host, with a security model that does not require target machines to grant the control station permanent unconditional access.

---

## Goals

- A single web UI and API surface to operate multiple LAN machines.
- Target machine owners retain full sovereignty: every script is reviewed and explicitly approved by the target owner before it can run. Approval is tied to specific script content; any change requires re-approval (unless the target owner has whitelisted the script for auto-approval).
- The agent on each target is the **only** thing the control station ever interacts with. There is no direct shell execution path over SSH; the agent is the entire protocol surface.
- Minimal setup burden on target machines.
- Cross-platform target support (Linux, Windows, macOS).
- No coupling to commercial services, paid tiers, or specific network providers.
- "Lite" in scope: deliberately simpler than full configuration management.

## Non-goals

- Replacing configuration management tools (Ansible, SaltStack, Puppet).
- Multi-tenant or SaaS deployment.
- Generic UDP/TCP packet senders. Concrete use cases (Wake-on-LAN) are implemented as first-class built-in actions instead.
- Browser-based remote desktop or interactive shell.
- Orchestration across machines (each script targets a single machine).

## Architecture summary

Two-process distributed system:

- **Control station** — FastAPI application running on the NAS. Fronted by nginx (TLS termination, rate limiting). Persists state in SQLite. Hosts the web UI and the REST API. Holds the canonical copy of all scripts.
- **Agent** — Small FastAPI process running on each target machine. Installed once as a user-level service (systemd user unit on Linux, Task Scheduler task on Windows). Started on demand by the control station via a single short-lived SSH command. Listens only on `localhost`. Terminates itself when no persistent process is running and the idle timer expires. **The agent is the sole channel through which the control station interacts with the target — there is no direct shell execution.**

All communication between the control station and an agent travels through an SSH tunnel. No agent port is ever exposed on the network. The only inbound port required on a target machine is SSH (22).

See `ARCHITECTURE.md` for the full design.

## Initial feature set

- User and admin roles with separate permission scopes.
- Authentication via JWT (short-lived access token, longer-lived refresh token in `HttpOnly` cookie).
- Per-user bookmarked machine list.
- Per-machine view with live status (reachable / not reachable, list of running persistent jobs, list of available scripts, approval state of each script).
- Script approval workflow: new and changed scripts must be approved by the target owner via the agent CLI before they can run. Per-script auto-approve policy supported.
- One-off script execution with parameter forms generated from script metadata.
- Persistent script execution with live log streaming, status polling, and kill capability.
- Admin: add, edit, delete scripts; manage users; manage machines.
- Built-in action: Wake-on-LAN (Magic Packet).
- Audit log of all actions (who ran what, when, against which machine).

## Initial built-in script catalogue

Shipped as default scripts that target owners can opt into:

- Wake-on-LAN (built-in, no script needed).
- Put machine to sleep.
- Start local LLM server via llama.cpp (persistent).
- Start Steam.
- Restart PC.

## Technology stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.11+ |
| Web framework (both sides) | FastAPI |
| Frontend | Jinja2 templates + HTMX + minimal vanilla JS |
| Database | SQLite via SQLAlchemy + Alembic migrations |
| Auth | `python-jose` (JWT), `passlib[bcrypt]` (passwords) |
| SSH | `asyncssh` (preferred, native async) or `paramiko` |
| Reverse proxy / TLS | nginx |
| Process management | Docker + Docker Compose, supervised by systemd |
| Packaging | PyPI (`control-station-lite`, `control-station-lite[agent]`) |

## Distribution model

The project ships in four cooperating forms:

1. **PyPI package** — two install profiles. `pip install control-station-lite` installs the full server (only needed on the NAS). `pip install control-station-lite[agent]` installs only the agent runtime (used on target machines).
2. **Docker image** — production deployment of the control station, including nginx, via `docker-compose`.
3. **systemd unit** — supervises the Docker stack on the NAS, ensures the service comes up at boot.
4. **Bootstrap shell script** — first-time setup on the NAS: verifies dependencies (Docker, systemd, Python), pulls the image, installs the systemd unit, generates initial admin credentials.

## Repository layout

```
control-station-lite/
├── control_station_lite/
│   ├── server/            # NAS-side application
│   ├── agent/             # Target-side application
│   └── shared/            # Models and parsers used by both
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── nginx.conf
│   └── control-station.service
├── scripts/
│   └── setup.sh           # NAS bootstrap
├── tests/
├── docs/
│   ├── ARCHITECTURE.md
│   └── TASKS.md
├── pyproject.toml
└── README.md
```

## License

TBD.
