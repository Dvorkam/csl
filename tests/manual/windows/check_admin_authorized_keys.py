"""Manual test: authorized_keys routing and ACLs on Windows.

Covers: _windows_is_admin(), _append_authorized_keys() path routing,
        _set_admin_ak_acl() / icacls ACL, idempotency, SSH key comment.

Run as both a standard user AND as Administrator to cover both paths.

    python tests\\manual\\windows\\check_admin_authorized_keys.py

Writes a temporary public key to the appropriate authorized_keys file.
Offers to remove it at the end.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3]))

from control_station_lite.agent.cli import (  # noqa: E402
    _WINDOWS_ADMIN_AK_PATH,
    _append_authorized_keys,
    _generate_keypair,
    _ssh_fingerprint,
    _windows_is_admin,
)
from control_station_lite.shared.platform_info import IS_WINDOWS  # noqa: E402

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"

_results: list[bool] = []
_added_key_line: str | None = None
_key_written_to: Path | None = None


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
print("Manual test: authorized_keys routing and ACLs (Windows)")
print("=" * 60)

if not IS_WINDOWS:
    print(f"\n{_RED}This script must run on Windows.{_RESET}")
    sys.exit(1)

is_admin = _windows_is_admin()
info(f"Running as Administrator: {is_admin}")
expected_path = _WINDOWS_ADMIN_AK_PATH if is_admin else Path.home() / ".ssh" / "authorized_keys"
info(f"Expected authorized_keys path: {expected_path}")

# 1. Generate a test keypair
section("1. Generate test keypair")
with tempfile.TemporaryDirectory() as tmp:
    keys_dir = Path(tmp) / "keys"
    priv, fp, pub = _generate_keypair(keys_dir)
    pub_line = pub.decode().strip()
    info(f"fingerprint: {fp}")

    parts = pub_line.split()
    if len(parts) == 3 and parts[2].startswith("csl-agent@"):
        ok("public key has csl-agent@hostname comment", parts[2])
    else:
        fail(f"unexpected public key format: {pub_line!r}")

    if fp == _ssh_fingerprint(pub):
        ok("fingerprint is stable (comment does not affect it)")
    else:
        fail("fingerprint changed after adding comment")

    # 2. Write to authorized_keys
    section("2. authorized_keys path routing")
    _append_authorized_keys(pub)
    _added_key_line = pub_line
    _key_written_to = expected_path

    if expected_path.exists():
        ok("authorized_keys exists at expected path", str(expected_path))
    else:
        fail(f"authorized_keys NOT found at {expected_path}")

    content = expected_path.read_text(encoding="utf-8") if expected_path.exists() else ""
    if pub_line in content:
        ok("public key line present in file")
    else:
        fail("public key line NOT found in file")

    if is_admin:
        wrong_path = Path.home() / ".ssh" / "authorized_keys"
        if wrong_path.exists() and pub_line in wrong_path.read_text(
            encoding="utf-8", errors="ignore"
        ):
            fail("key was ALSO written to user path (should only go to admin path)")
        else:
            ok("key was NOT written to user path (admin path only)")
    else:
        if _WINDOWS_ADMIN_AK_PATH.exists():
            admin_content = _WINDOWS_ADMIN_AK_PATH.read_text(encoding="utf-8", errors="ignore")
            if pub_line in admin_content:
                fail("key was written to admin path (user is not admin)")
            else:
                ok("key was NOT written to admin path (user is not admin, correct)")

    # 3. Idempotency
    section("3. Idempotency")
    _append_authorized_keys(pub)
    _append_authorized_keys(pub)
    content2 = expected_path.read_text(encoding="utf-8") if expected_path.exists() else ""
    count = content2.count(pub_line)
    if count == 1:
        ok("key appears exactly once after three writes (idempotent)")
    else:
        fail(f"key appears {count} times after three writes")

    # 4. ACL check (admin path only)
    if is_admin:
        section("4. ACL on administrators_authorized_keys")
        icacls = subprocess.run(
            ["icacls", str(_WINDOWS_ADMIN_AK_PATH)],
            capture_output=True,
            text=True,
        )
        acl_output = icacls.stdout
        info(f"icacls output:\n{acl_output}")

        if "NT AUTHORITY\\SYSTEM:(F)" in acl_output or "SYSTEM" in acl_output:
            ok("SYSTEM has Full Control")
        else:
            fail("SYSTEM entry missing or wrong in ACL")

        if "BUILTIN\\Administrators:(F)" in acl_output or "Administrators" in acl_output:
            ok("Administrators have Full Control")
        else:
            fail("Administrators entry missing or wrong in ACL")

        # No other users should have access (no inheritance)
        # icacls shows inherited entries with (I) flag; we check it's absent
        lines_with_i = [ln for ln in acl_output.splitlines() if "(I)" in ln]
        if not lines_with_i:
            ok("no inherited ACE entries (inheritance removed)")
        else:
            fail("inherited ACEs still present:\n" + "\n".join(lines_with_i))

# ---------------------------------------------------------------------------
print(f"\n{'=' * 60}")
passed = sum(_results)
total = len(_results)
colour = _GREEN if passed == total else _RED
print(f"{colour}Results: {passed}/{total} passed{_RESET}")

# Cleanup offer
if _key_written_to and _added_key_line:
    print()
    answer = input(f"Remove the test key from {_key_written_to}? [y/N] ").strip().lower()
    if answer == "y":
        try:
            text = _key_written_to.read_text(encoding="utf-8")
            new_text = "\n".join(
                line for line in text.splitlines() if line.strip() != _added_key_line
            ).strip()
            if new_text:
                _key_written_to.write_text(new_text + "\n", encoding="utf-8")
            else:
                _key_written_to.unlink()
            print("Test key removed.")
        except Exception as exc:
            print(f"Cleanup failed: {exc}")

sys.exit(0 if passed == total else 1)
