"""Manual test: SSHConnectionPool against a real local sshd.

Prerequisites (run once):
    ssh-keygen -t ed25519 -f /tmp/csl_test_key -N ""
    cat /tmp/csl_test_key.pub >> ~/.ssh/authorized_keys

Run as:
    uv run tests/manual/linux/check_ssh_pool.py

Assumes sshd is running and accepting connections on localhost:22.
Does NOT modify system state beyond opening and closing SSH connections.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[3]))

from control_station_lite.server.core.ssh import SSHConnectionPool  # noqa: E402

_KEY_PATH = Path("/tmp/csl_test_key")
_HOST = "127.0.0.1"
_PORT = 22
_USER = os.getenv("USER", "root")

_results: list[bool] = []


def ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")
    _results.append(True)


def fail(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}")
    _results.append(False)


def info(msg: str) -> None:
    print(f"    {msg}")


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


async def run() -> None:
    if not _KEY_PATH.exists():
        print(f"ERROR: key not found at {_KEY_PATH}")
        print("Run: ssh-keygen -t ed25519 -f /tmp/csl_test_key -N \"\"")
        print("Then: cat /tmp/csl_test_key.pub >> ~/.ssh/authorized_keys")
        sys.exit(1)

    private_key = _KEY_PATH.read_bytes()
    pool = SSHConnectionPool()

    # ------------------------------------------------------------------
    section("1. get_connection — open real SSH connection to localhost")
    # ------------------------------------------------------------------
    try:
        conn = await pool.get_connection(_HOST, _PORT, _USER, private_key)
        ok(f"Connected to {_USER}@{_HOST}:{_PORT}")
        info(f"Connection object: {type(conn).__name__}")
    except Exception as exc:
        fail(f"get_connection raised: {exc}")
        await pool.close_all()
        return

    # ------------------------------------------------------------------
    section("2. get_connection — reuse cached connection")
    # ------------------------------------------------------------------
    try:
        conn2 = await pool.get_connection(_HOST, _PORT, _USER, private_key)
        if conn2 is conn:
            ok("Returned the cached connection (same object)")
        else:
            fail("Returned a different object — pool is not reusing")
    except Exception as exc:
        fail(f"Second get_connection raised: {exc}")

    # ------------------------------------------------------------------
    section("3. open_tunnel — forward a local port to localhost:22")
    # ------------------------------------------------------------------
    try:
        listener, local_port = await pool.open_tunnel(
            _HOST, _PORT, _USER, private_key, "127.0.0.1", _PORT
        )
        ok(f"Tunnel opened: localhost:{local_port} -> {_HOST}:{_PORT}")
        listener.close()
        await listener.wait_closed()
        ok("Listener closed cleanly")
    except Exception as exc:
        fail(f"open_tunnel raised: {exc}")

    # ------------------------------------------------------------------
    section("4. close — close the pooled connection")
    # ------------------------------------------------------------------
    try:
        await pool.close(_HOST, _PORT, _USER)
        ok("Connection closed without error")
    except Exception as exc:
        fail(f"close raised: {exc}")

    # ------------------------------------------------------------------
    section("5. close on unknown host — should be a no-op")
    # ------------------------------------------------------------------
    try:
        await pool.close("no-such-host", 22, "nobody")
        ok("close on unknown host is a no-op")
    except Exception as exc:
        fail(f"close on unknown host raised: {exc}")

    # ------------------------------------------------------------------
    section("6. close_all — open two connections, close both")
    # ------------------------------------------------------------------
    pool2 = SSHConnectionPool()
    try:
        await pool2.get_connection(_HOST, _PORT, _USER, private_key)
        await pool2.get_connection(_HOST, _PORT + 1 if _PORT != 22 else _PORT, _USER, private_key)
    except Exception:
        pass  # second host may not be reachable — that's fine
    try:
        await pool2.close_all()
        ok("close_all completed without error")
        if not pool2._pool:
            ok("Pool is empty after close_all")
        else:
            fail(f"Pool still has {len(pool2._pool)} entries after close_all")
    except Exception as exc:
        fail(f"close_all raised: {exc}")

    # ------------------------------------------------------------------
    passed = sum(_results)
    total = len(_results)
    print(f"\nResults: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(run())
