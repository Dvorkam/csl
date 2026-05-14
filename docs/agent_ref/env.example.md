# Environment — template

Copy this file to `docs/agent_ref/env.md` and fill in values for your machine.
`env.md` is gitignored and never committed.

---

## Machine

- **OS:** <!-- e.g. Fedora 43, Ubuntu 24.04, macOS 15, Windows 11 -->
- **Shell:** <!-- bash / zsh / powershell -->
- **Virtualization:** <!-- bare-metal / WSL2 / VM / container -->
- **Package manager:** <!-- dnf / apt / brew / winget + install syntax -->

## Installed tools

- **git:** <!-- version; note if PATH setup was non-standard -->
- **uv:** <!-- version + location -->
- **GitHub CLI (gh):** <!-- version; `gh auth status` to verify -->
- **Docker:** <!-- version, or "not installed" -->
- **pre-commit:** <!-- managed by uv — `uv run pre-commit` -->

## Notes

<!-- Machine-specific quirks: proxy settings, path overrides, sudo behaviour,
     WSL mount points, known version conflicts, etc. -->
