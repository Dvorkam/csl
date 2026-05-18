"""Manual test: systemd user service installation on Linux.

Covers: install_service(), the generated unit file content, daemon-reload.

Run as:  python tests/manual/linux/check_service_install.py

WRITES to ~/.config/systemd/user/csl-agent.service and runs
`systemctl --user daemon-reload`.  Offers to remove the unit file at the end.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3]))

from control_station_lite.agent.service_installer import install_service  # noqa: E402
from control_station_lite.shared.platform_info import IS_LINUX  # noqa: E402

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"

_results: list[bool] = []
_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / "csl-agent.service"


def ok(label: str, detail: str = "") -> None:
    print(f"  {_GREEN}[ OK ]{_RESET} {label}")
    if detail:
        print(f"        {detail}")
    _results.append(True)


def fail(label: str, detail: str = "") -> None:
    print(f"  {_RED}[FAIL]{_RESET} {label}")
    if detail:
        print(f"        {detail}")
    _results.append(False)


def info(msg: str) -> None:
    print(f"  {_YELLOW}[INFO]{_RESET} {msg}")


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


# ---------------------------------------------------------------------------

print("=" * 60)
print("Manual test: systemd user service installation (Linux)")
print("=" * 60)

if not IS_LINUX:
    print(f"\n{_RED}This script must run on Linux.{_RESET}")
    sys.exit(1)

info(f"Unit file will be written to: {_UNIT_PATH}")

# Pre-existing unit check
if _UNIT_PATH.exists():
    info("Unit file already exists — will overwrite (idempotent install).")

# 1. Install
section("1. install_service()")
try:
    install_service()
    ok("install_service() completed without exception")
except Exception as exc:
    fail(f"install_service() raised: {exc}")
    sys.exit(1)

# 2. Unit file exists
section("2. Unit file content")
if _UNIT_PATH.exists():
    ok(f"unit file created at {_UNIT_PATH}")
else:
    fail(f"unit file NOT found at {_UNIT_PATH}")

content = _UNIT_PATH.read_text(encoding="utf-8") if _UNIT_PATH.exists() else ""

checks = {
    "Restart=no": "service is on-demand only (must NOT auto-restart)",
    "python": "unit file references a Python executable",
    "control_station_lite.agent": "unit file invokes the agent module",
    "[Service]": "unit file has [Service] section",
    "[Unit]": "unit file has [Unit] section",
}
for needle, description in checks.items():
    if needle in content:
        ok(f"contains {needle!r}", description)
    else:
        fail(f"missing {needle!r}", description)

# 3. daemon-reload was called (verify systemd knows about the unit)
section("3. systemd awareness")
result = subprocess.run(
    ["systemctl", "--user", "list-unit-files", "csl-agent.service"],
    capture_output=True,
    text=True,
)
if "csl-agent.service" in result.stdout:
    ok("systemctl --user lists csl-agent.service after daemon-reload")
else:
    fail(
        "csl-agent.service not visible to systemctl",
        "daemon-reload may not have been called, or ran with wrong scope",
    )

# 4. Unit is NOT enabled (on-demand only — must not auto-start at login)
section("4. Unit is NOT enabled (on-demand only)")
status = subprocess.run(
    ["systemctl", "--user", "is-enabled", "csl-agent.service"],
    capture_output=True,
    text=True,
)
state = status.stdout.strip()
if state in ("disabled", "static"):
    ok(f"unit state is {state!r} — will not start automatically at login")
else:
    fail(
        f"unit state is {state!r}, expected 'disabled' or 'static'",
        "the service must be on-demand only (no --user enable)",
    )

# ---------------------------------------------------------------------------
print(f"\n{'=' * 60}")
passed = sum(_results)
total = len(_results)
colour = _GREEN if passed == total else _RED
print(f"{colour}Results: {passed}/{total} passed{_RESET}")

print()
answer = input(f"Remove {_UNIT_PATH} and reload? [y/N] ").strip().lower()
if answer == "y":
    _UNIT_PATH.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print("Cleaned up.")

sys.exit(0 if passed == total else 1)
