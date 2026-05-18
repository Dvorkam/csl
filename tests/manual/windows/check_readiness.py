"""Manual test: SSH daemon readiness checks on Windows.

Covers: check_readiness(), _sshd_running_windows(), _check_readiness_windows(),
        _windows_is_admin(), _sshd_exe_windows().

Run as (standard or admin PowerShell / cmd):
    python tests\\manual\\windows\\check_readiness.py

Read-only — does not modify system state.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[3]))

from control_station_lite.agent.cli.cmd_setup import (  # noqa: E402
    _check_readiness_windows,
    _sshd_exe_windows,
    _sshd_running_windows,
    _windows_is_admin,
    check_readiness,
)
from control_station_lite.agent.config import load_config  # noqa: E402
from control_station_lite.shared.platform_info import IS_WINDOWS  # noqa: E402

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
print("Manual test: SSH daemon readiness checks (Windows)")
print("=" * 60)

if not IS_WINDOWS:
    print(f"\n{_RED}This script must run on Windows.{_RESET}")
    sys.exit(1)

# 0. Admin status
section("0. Running context")
is_admin = _windows_is_admin()
_cfg = load_config()
info(f"Running as Administrator: {is_admin}")
if is_admin:
    ak_path = _cfg.advanced.windows_admin_authorized_keys_path
    info(f"authorized_keys will go to {ak_path}")
else:
    info("authorized_keys will go to %USERPROFILE%\\.ssh\\authorized_keys")

# 1. OpenSSH Server installed
section("1. OpenSSH Server installed")
sshd_exe = _sshd_exe_windows()
if sshd_exe.exists():
    ok(f"sshd.exe found at {sshd_exe}")
else:
    fail(
        f"sshd.exe NOT found at {sshd_exe}",
        "Install: Settings > System > Optional Features > OpenSSH Server\n"
        "  or: Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0",
    )

# 2. sshd service running
section("2. sshd service state")
result = subprocess.run(["sc", "query", "sshd"], capture_output=True, text=True)
info(f"sc query sshd: {result.stdout.strip()[:120]}")
is_running = _sshd_running_windows()
if is_running:
    ok("sshd service is RUNNING")
else:
    fail(
        "sshd service is NOT running",
        "Start with: sc start sshd\n  To set automatic: sc config sshd start= auto",
    )

# 3. admin authorized_keys directory (only relevant for admin)
if is_admin:
    admin_ak = _cfg.advanced.windows_admin_authorized_keys_path
    admin_ssh = admin_ak.parent
    section(f"3. {admin_ssh} (admin-account prerequisite)")
    if admin_ssh.exists():
        ok(f"{admin_ssh} exists")
        if admin_ak.exists():
            info(f"{admin_ak.name} already exists ({admin_ak.stat().st_size} bytes)")
        else:
            info(f"{admin_ak.name} does not exist yet — will be created by init")
    else:
        fail(
            f"{admin_ssh} does not exist",
            "Usually created by the OpenSSH Server installer. Try reinstalling.",
        )

# 4. check_readiness() matches observed state
section("4. check_readiness() consistency")
issues = check_readiness()
errors = [i for i in issues if i.severity == "error"]
warnings = [i for i in issues if i.severity == "warning"]
info(f"issues: {len(issues)}  (errors={len(errors)}, warnings={len(warnings)})")
for issue in issues:
    info(f"  [{issue.severity.upper()[:4]}] {issue.description}")

if not sshd_exe.exists():
    if errors and "not installed" in errors[0].description.lower():
        ok("error reported matches missing sshd.exe")
    else:
        fail("expected an 'not installed' error, got something else")
elif not is_running:
    if warnings and "not running" in warnings[0].description.lower():
        ok("warning reported matches stopped service")
    else:
        fail("expected 'not running' warning, got something else")
else:
    # Healthy — only possible extra: missing ProgramData/ssh when admin
    remaining = [i for i in issues if "ProgramData" not in i.description]
    if not remaining:
        ok("no unexpected issues when sshd is healthy")
    else:
        fail(f"unexpected issues: {[i.description for i in remaining]}")

# 5. fix_hint content sanity
section("5. fix_hint content")
with mock.patch(
    "control_station_lite.agent.cli.cmd_setup._sshd_exe_windows",
    return_value=Path("C:/nonexistent/sshd.exe"),
):
    missing_issues = _check_readiness_windows()
if missing_issues and "Add-WindowsCapability" in missing_issues[0].fix_hint:
    ok("fix_hint for missing sshd mentions Add-WindowsCapability")
else:
    hint = missing_issues[0].fix_hint if missing_issues else "(none)"
    fail("fix_hint does not mention Add-WindowsCapability", f"hint: {hint}")

# ---------------------------------------------------------------------------
print(f"\n{'=' * 60}")
passed = sum(_results)
total = len(_results)
colour = _GREEN if passed == total else _RED
print(f"{colour}Results: {passed}/{total} passed{_RESET}")
sys.exit(0 if passed == total else 1)
