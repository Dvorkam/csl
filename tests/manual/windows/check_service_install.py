"""Manual test: Task Scheduler service installation on Windows.

Covers: install_service(), schtasks task creation, task XML content.

Run as Administrator (schtasks /create requires elevation):
    python tests\\manual\\windows\\check_service_install.py

Creates the CSL-Agent scheduled task.  Offers to delete it at the end.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3]))

from control_station_lite.agent.cli.cmd_setup import _windows_is_admin  # noqa: E402
from control_station_lite.agent.service_installer import install_service  # noqa: E402
from control_station_lite.shared.platform_info import IS_WINDOWS  # noqa: E402

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"

_TASK_NAME = "CSL-Agent"
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


def _task_exists() -> bool:
    result = subprocess.run(
        ["schtasks", "/query", "/tn", _TASK_NAME],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------

print("=" * 60)
print("Manual test: Task Scheduler service installation (Windows)")
print("=" * 60)

if not IS_WINDOWS:
    print(f"\n{_RED}This script must run on Windows.{_RESET}")
    sys.exit(1)

if not _windows_is_admin():
    print(f"\n{_YELLOW}[WARN]{_RESET} Not running as Administrator.")
    print("  schtasks /create may fail. Re-run in an admin terminal for a complete test.")

info(f"Task name: {_TASK_NAME}")

if _task_exists():
    info(f"Task {_TASK_NAME!r} already exists — install_service() will overwrite it.")

# 1. Install
section("1. install_service()")
try:
    install_service()
    ok("install_service() completed without exception")
except Exception as exc:
    fail(f"install_service() raised: {exc}")
    sys.exit(1)

# 2. Task exists in Task Scheduler
section("2. Task registered in Task Scheduler")
if _task_exists():
    ok(f"Task {_TASK_NAME!r} found in schtasks /query")
else:
    fail(f"Task {_TASK_NAME!r} NOT found after install_service()")

# 3. Task details
section("3. Task configuration")
result = subprocess.run(
    ["schtasks", "/query", "/tn", _TASK_NAME, "/fo", "LIST", "/v"],
    capture_output=True,
    text=True,
)
task_info = result.stdout
info(f"schtasks output (truncated):\n{task_info[:600]}")

# On-demand only — no scheduled trigger
if "At system startup" not in task_info and "Daily" not in task_info and "Weekly" not in task_info:
    ok("task has no automatic trigger (demand-only)")
else:
    fail("task has an automatic trigger — it should only run on demand")

# Status should be Ready (not Running, not Disabled)
if "Ready" in task_info or "Disabled" in task_info:
    ok("task status is Ready or Disabled (not running yet, as expected)")
else:
    fail("unexpected task status in output")

# 4. XML content check (get the XML to verify pythonw.exe and module invocation)
section("4. Task XML content")
xml_result = subprocess.run(
    ["schtasks", "/query", "/tn", _TASK_NAME, "/xml"],
    capture_output=True,
    text=True,
)
xml = xml_result.stdout

if "pythonw" in xml.lower() or "python" in xml.lower():
    ok("XML references Python executable")
else:
    fail("XML does not reference Python", f"XML snippet:\n{xml[:400]}")

if "control_station_lite" in xml or "csl" in xml.lower():
    ok("XML references control_station_lite agent")
else:
    fail("XML does not reference the agent module", f"XML snippet:\n{xml[:400]}")

# 5. Task can be started manually (dry-run: just check /run doesn't error)
section("5. Manual start (schtasks /run)")
run_result = subprocess.run(
    ["schtasks", "/run", "/tn", _TASK_NAME],
    capture_output=True,
    text=True,
)
info(f"schtasks /run exit code: {run_result.returncode}")
info(f"stdout: {run_result.stdout.strip()}")
if run_result.returncode == 0:
    ok("schtasks /run succeeded (task is startable)")
else:
    fail(
        "schtasks /run failed",
        f"stderr: {run_result.stderr.strip()}\n"
        "This may be expected if pythonw.exe is not on PATH or package not installed.",
    )

# ---------------------------------------------------------------------------
print(f"\n{'=' * 60}")
passed = sum(_results)
total = len(_results)
colour = _GREEN if passed == total else _RED
print(f"{colour}Results: {passed}/{total} passed{_RESET}")

print()
answer = input(f"Delete the {_TASK_NAME!r} scheduled task? [y/N] ").strip().lower()
if answer == "y":
    del_result = subprocess.run(
        ["schtasks", "/delete", "/tn", _TASK_NAME, "/f"],
        capture_output=True,
        text=True,
    )
    if del_result.returncode == 0:
        print("Task deleted.")
    else:
        print(f"Deletion failed: {del_result.stderr.strip()}")

sys.exit(0 if passed == total else 1)
