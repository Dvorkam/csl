"""Manual test: SSH daemon readiness checks on Linux.

Covers: check_readiness(), _sshd_running_linux(), _check_readiness_linux(),
        setup_system() (print path only — does NOT run sudo commands).

Run as:  python tests/manual/linux/check_readiness.py

Read-only — does not modify system state.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3]))

from control_station_lite.agent.cli import (  # noqa: E402
    _check_readiness_linux,
    _sshd_running_linux,
    check_readiness,
)
from control_station_lite.shared.platform_info import IS_LINUX  # noqa: E402

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"

_results: list[bool] = []


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
print("Manual test: SSH daemon readiness checks (Linux)")
print("=" * 60)

if not IS_LINUX:
    print(f"\n{_RED}This script must run on Linux.{_RESET}")
    sys.exit(1)

# 1. sshd binary
section("1. sshd binary installed")
sshd_path = shutil.which("sshd")
if sshd_path:
    ok("sshd binary found", sshd_path)
else:
    fail("sshd not found in PATH", "Install with: sudo apt/dnf/pacman install openssh-server")

# 2. sshd running
section("2. sshd service running")
is_running = _sshd_running_linux()
if is_running:
    ok("sshd is running")
else:
    fail("sshd is NOT running", "Start with: sudo systemctl start ssh")
    info("check_readiness() will report a warning for this")

# 3. check_readiness() output matches actual state
section("3. check_readiness() output")
issues = check_readiness()
errors = [i for i in issues if i.severity == "error"]
warnings = [i for i in issues if i.severity == "warning"]

info(f"issues returned: {len(issues)}  (errors={len(errors)}, warnings={len(warnings)})")
for issue in issues:
    tag = "ERROR" if issue.severity == "error" else "WARN "
    info(f"  [{tag}] {issue.description}")

if sshd_path is None:
    # binary missing → should be one error
    if errors and "not installed" in errors[0].description.lower():
        ok("error reported matches missing binary")
    else:
        fail("expected an 'not installed' error, got something else")
elif not is_running:
    # installed but stopped → should be one warning
    if warnings and "not running" in warnings[0].description.lower():
        ok("warning reported matches stopped service")
    else:
        fail("expected a 'not running' warning, got something else")
else:
    # everything healthy → no issues
    if not issues:
        ok("no issues reported — system is ready")
    else:
        fail(f"unexpected issues when sshd is healthy: {[i.description for i in issues]}")

# 4. fix_hint content sanity
section("4. fix_hint content")
dummy_issues = (
    _check_readiness_linux.__wrapped__() if hasattr(_check_readiness_linux, "__wrapped__") else []
)  # noqa: SIM108
# Directly verify a known broken state by faking the conditions
import unittest.mock as mock  # noqa: E402

with mock.patch("control_station_lite.agent.cli.shutil.which", return_value=None):
    missing_issues = _check_readiness_linux()
if missing_issues and "apt" in missing_issues[0].fix_hint and "dnf" in missing_issues[0].fix_hint:
    ok("fix_hint for missing sshd mentions apt and dnf")
else:
    fail(
        "fix_hint for missing sshd does not mention expected package managers",
        f"hint: {missing_issues[0].fix_hint if missing_issues else '(none)'}",
    )

with (
    mock.patch("control_station_lite.agent.cli.shutil.which", return_value="/usr/sbin/sshd"),
    mock.patch("control_station_lite.agent.cli._sshd_running_linux", return_value=False),
):
    stopped_issues = _check_readiness_linux()
if stopped_issues and "systemctl" in stopped_issues[0].fix_hint:
    ok("fix_hint for stopped sshd mentions systemctl")
else:
    fail(
        "fix_hint for stopped sshd does not mention systemctl",
        f"hint: {stopped_issues[0].fix_hint if stopped_issues else '(none)'}",
    )

# ---------------------------------------------------------------------------
print(f"\n{'=' * 60}")
passed = sum(_results)
total = len(_results)
colour = _GREEN if passed == total else _RED
print(f"{colour}Results: {passed}/{total} passed{_RESET}")
sys.exit(0 if passed == total else 1)
