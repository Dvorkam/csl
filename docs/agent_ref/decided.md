# Locked architectural decisions

These are settled. Don't relitigate without updating `docs/ARCHITECTURE.md` in the same PR.

- **Agent is the only channel.** No direct shell execution over SSH. Every action — trivial or not — goes through the agent's API.
- **Approval is mandatory.** Scripts require explicit target-owner approval before they can run. No silent sync, no auto-run-on-push.
- **Approval is content-bound.** Approval is tied to the script's MD5. Any change revokes the existing approval and requires re-approval.
- **Parameters via environment variables.** Passed as `CSL_PARAM_<NAME>` — avoids shell-quoting issues and works identically on Linux and Windows.
- **SSH keys encrypted at rest.** AES-256-GCM, per-record nonce, master key from `secrets/master.key`.
- **Auto-approve is whitelist-only.** No "trust on first use", no "auto-approve minor changes". The whitelist is per-script and stored in the agent's `config.yaml`.
- **License: AGPL-3.0-or-later** with app-store distribution exception. All source files carry `SPDX-License-Identifier: AGPL-3.0-or-later`. See `LICENSE` and `docs/lics/SOURCE_HEADER.txt`.
- **Docker build strategy: pinned release image + dev bind-mount override.** Production image has the package baked in at build time, pinned to the release version, tagged to match. `release.yml` builds and pushes it. Dev uses a bind-mount + editable install via a separate Dockerfile stage and a `docker-compose.override.yml`. The asymmetry between dev and prod is intentional and accepted. Two containers (`app` + `nginx`) in one prod compose file; nginx owns only edge concerns (TLS, rate limiting, request-size limits). No attempt to make the prod image work for dev or vice versa.
