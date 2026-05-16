"""Manual test: authorized_keys writing on Linux.

Covers: _append_authorized_keys(), _generate_keypair(), SSH key comment,
        file permissions (600 / 700), idempotency.

Run as:  python tests/manual/linux/check_authorized_keys.py

Uses a temporary directory — does NOT touch the real ~/.ssh/authorized_keys.
"""

from __future__ import annotations

import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[3]))

from control_station_lite.agent.cli import (  # noqa: E402
    _append_authorized_keys,
    _generate_keypair,
    _ssh_fingerprint,
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
print("Manual test: authorized_keys writing (Linux)")
print("=" * 60)

if not IS_LINUX:
    print(f"\n{_RED}This script must run on Linux.{_RESET}")
    sys.exit(1)

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    keys_dir = tmp_path / "keys"

    # 1. Key generation
    section("1. Keypair generation")
    priv, fp, pub = _generate_keypair(keys_dir)

    if (keys_dir / "csl_ed25519").exists():
        ok("private key file created")
    else:
        fail("private key file missing")

    if (keys_dir / "csl_ed25519.pub").exists():
        ok("public key file created")
    else:
        fail("public key file missing")

    priv_mode = stat.S_IMODE((keys_dir / "csl_ed25519").stat().st_mode)
    if priv_mode == 0o600:
        ok("private key permissions are 600")
    else:
        fail(f"private key permissions are {oct(priv_mode)}, expected 0o600")

    parts = pub.decode().strip().split()
    if len(parts) == 3:
        ok("public key has comment field", f"comment: {parts[2]}")
    else:
        fail(f"public key has {len(parts)} fields, expected 3 (type key comment)")

    if parts[2].startswith("csl-agent@"):
        ok("comment is in csl-agent@hostname format")
    else:
        fail(f"unexpected comment format: {parts[2]!r}")

    # 2. Fingerprint
    section("2. Fingerprint")
    fp2 = _ssh_fingerprint(pub)
    if fp == fp2:
        ok("fingerprint is stable across calls", fp)
    else:
        fail(f"fingerprint mismatch: {fp!r} vs {fp2!r}")

    if fp.startswith("SHA256:"):
        ok("fingerprint has SHA256: prefix")
    else:
        fail(f"unexpected fingerprint format: {fp!r}")

    # 3. authorized_keys creation
    section("3. authorized_keys file creation")
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with mock.patch("pathlib.Path.home", return_value=fake_home):
        _append_authorized_keys(pub)

    ak = fake_home / ".ssh" / "authorized_keys"
    ssh_dir = fake_home / ".ssh"

    if ak.exists():
        ok("authorized_keys created")
    else:
        fail("authorized_keys NOT created")

    ak_mode = stat.S_IMODE(ak.stat().st_mode)
    if ak_mode == 0o600:
        ok("authorized_keys permissions are 600")
    else:
        fail(f"authorized_keys permissions are {oct(ak_mode)}, expected 0o600")

    ssh_mode = stat.S_IMODE(ssh_dir.stat().st_mode)
    if ssh_mode == 0o700:
        ok(".ssh/ directory permissions are 700")
    else:
        fail(f".ssh/ permissions are {oct(ssh_mode)}, expected 0o700")

    pub_line = pub.decode().strip()
    content = ak.read_text()
    if pub_line in content:
        ok("public key line present in authorized_keys")
    else:
        fail("public key line NOT found in authorized_keys")

    # 4. Idempotency
    section("4. Idempotency")
    with mock.patch("pathlib.Path.home", return_value=fake_home):
        _append_authorized_keys(pub)
        _append_authorized_keys(pub)

    content2 = ak.read_text()
    count = content2.count(pub_line)
    if count == 1:
        ok("key appears exactly once after three writes (idempotent)")
    else:
        fail(f"key appears {count} times in authorized_keys after three writes")

    # 5. Appending to existing file
    section("5. Appending alongside an existing key")
    fake_home2 = tmp_path / "home2"
    fake_home2.mkdir()
    ssh2 = fake_home2 / ".ssh"
    ssh2.mkdir(mode=0o700)
    ak2 = ssh2 / "authorized_keys"
    ak2.write_text("ssh-rsa AAAAB... existing@host\n")
    ak2.chmod(0o600)

    with mock.patch("pathlib.Path.home", return_value=fake_home2):
        _append_authorized_keys(pub)

    content3 = ak2.read_text()
    if "existing@host" in content3 and pub_line in content3:
        ok("new key appended without removing existing key")
    else:
        fail("existing key was overwritten or new key not added", f"content:\n{content3}")

# ---------------------------------------------------------------------------
print(f"\n{'=' * 60}")
passed = sum(_results)
total = len(_results)
colour = _GREEN if passed == total else _RED
print(f"{colour}Results: {passed}/{total} passed{_RESET}")
sys.exit(0 if passed == total else 1)
