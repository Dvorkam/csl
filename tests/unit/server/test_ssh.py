"""Unit tests for server/core/ssh.py (SSHConnectionPool)."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from control_station_lite.server.core.ssh import SSHConnectionPool, get_ssh_pool

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_HOST = "192.168.1.10"
_PORT = 22
_USER = "alice"
_KEY = os.urandom(32)  # not a real key — asyncssh.connect is mocked


def _make_conn(closing: bool = False) -> MagicMock:
    conn = MagicMock()
    conn.is_closed.return_value = closing
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock()
    conn.forward_local_port = AsyncMock()
    return conn


def _make_listener(port: int = 54321) -> MagicMock:
    listener = MagicMock()
    listener.get_port.return_value = port
    listener.close = MagicMock()
    listener.wait_closed = AsyncMock()
    return listener


@pytest.fixture
def pool() -> SSHConnectionPool:
    return SSHConnectionPool()


# Patch both asyncssh entry points used by ssh.py
@pytest.fixture
def mock_connect(pool: SSHConnectionPool):
    conn = _make_conn()
    with (
        patch(
            "control_station_lite.server.core.ssh.asyncssh.connect", AsyncMock(return_value=conn)
        ) as p,
        patch(
            "control_station_lite.server.core.ssh.asyncssh.import_private_key",
            return_value=MagicMock(),
        ),
    ):
        yield p, conn


# ---------------------------------------------------------------------------
# get_connection
# ---------------------------------------------------------------------------


async def test_get_connection_opens_new(mock_connect, pool: SSHConnectionPool) -> None:
    mock_fn, conn = mock_connect
    result = await pool.get_connection(_HOST, _PORT, _USER, _KEY)
    assert result is conn
    mock_fn.assert_called_once()


async def test_get_connection_reuses_existing(mock_connect, pool: SSHConnectionPool) -> None:
    mock_fn, conn = mock_connect
    first = await pool.get_connection(_HOST, _PORT, _USER, _KEY)
    second = await pool.get_connection(_HOST, _PORT, _USER, _KEY)
    assert first is second
    assert mock_fn.call_count == 1  # only one real connect


async def test_get_connection_separate_per_host(mock_connect, pool: SSHConnectionPool) -> None:
    mock_fn, _ = mock_connect
    mock_fn.side_effect = [_make_conn(), _make_conn()]
    with patch(
        "control_station_lite.server.core.ssh.asyncssh.import_private_key", return_value=MagicMock()
    ):
        c1 = await pool.get_connection("host-a", _PORT, _USER, _KEY)
        c2 = await pool.get_connection("host-b", _PORT, _USER, _KEY)
    assert c1 is not c2
    assert mock_fn.call_count == 2


async def test_get_connection_replaces_closed_conn(pool: SSHConnectionPool) -> None:
    stale = _make_conn(closing=True)
    fresh = _make_conn(closing=False)
    with (
        patch(
            "control_station_lite.server.core.ssh.asyncssh.connect",
            AsyncMock(side_effect=[stale, fresh]),
        ),
        patch(
            "control_station_lite.server.core.ssh.asyncssh.import_private_key",
            return_value=MagicMock(),
        ),
    ):
        # Seed pool with a closing connection
        await pool.get_connection(_HOST, _PORT, _USER, _KEY)
        stale.is_closed.return_value = True
        result = await pool.get_connection(_HOST, _PORT, _USER, _KEY)
    assert result is fresh


async def test_get_connection_passes_host_port_user(mock_connect, pool: SSHConnectionPool) -> None:
    mock_fn, _ = mock_connect
    await pool.get_connection(_HOST, _PORT, _USER, _KEY)
    _, kwargs = mock_fn.call_args
    assert mock_fn.call_args[0][0] == _HOST
    assert kwargs["port"] == _PORT
    assert kwargs["username"] == _USER


async def test_get_connection_known_hosts_none(mock_connect, pool: SSHConnectionPool) -> None:
    mock_fn, _ = mock_connect
    await pool.get_connection(_HOST, _PORT, _USER, _KEY)
    _, kwargs = mock_fn.call_args
    assert kwargs["known_hosts"] is None


# ---------------------------------------------------------------------------
# open_tunnel
# ---------------------------------------------------------------------------


async def test_open_tunnel_returns_listener_and_port(pool: SSHConnectionPool) -> None:
    conn = _make_conn()
    listener = _make_listener(port=12345)
    conn.forward_local_port.return_value = listener
    with (
        patch(
            "control_station_lite.server.core.ssh.asyncssh.connect", AsyncMock(return_value=conn)
        ),
        patch(
            "control_station_lite.server.core.ssh.asyncssh.import_private_key",
            return_value=MagicMock(),
        ),
    ):
        returned_listener, port = await pool.open_tunnel(
            _HOST, _PORT, _USER, _KEY, "127.0.0.1", 36717
        )
    assert returned_listener is listener
    assert port == 12345


async def test_open_tunnel_tracks_listener_for_close(pool: SSHConnectionPool) -> None:
    conn = _make_conn()
    listener = _make_listener()
    conn.forward_local_port.return_value = listener
    with (
        patch(
            "control_station_lite.server.core.ssh.asyncssh.connect", AsyncMock(return_value=conn)
        ),
        patch(
            "control_station_lite.server.core.ssh.asyncssh.import_private_key",
            return_value=MagicMock(),
        ),
    ):
        await pool.open_tunnel(_HOST, _PORT, _USER, _KEY, "127.0.0.1", 36717)
        await pool.close(_HOST, _PORT, _USER)
    listener.close.assert_called_once()
    listener.wait_closed.assert_called_once()


async def test_open_tunnel_forwards_to_correct_remote(pool: SSHConnectionPool) -> None:
    conn = _make_conn()
    conn.forward_local_port.return_value = _make_listener()
    with (
        patch(
            "control_station_lite.server.core.ssh.asyncssh.connect", AsyncMock(return_value=conn)
        ),
        patch(
            "control_station_lite.server.core.ssh.asyncssh.import_private_key",
            return_value=MagicMock(),
        ),
    ):
        await pool.open_tunnel(_HOST, _PORT, _USER, _KEY, "127.0.0.1", 36717)
    conn.forward_local_port.assert_called_once_with("127.0.0.1", 0, "127.0.0.1", 36717)


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


async def test_close_closes_connection(mock_connect, pool: SSHConnectionPool) -> None:
    _, conn = mock_connect
    await pool.get_connection(_HOST, _PORT, _USER, _KEY)
    await pool.close(_HOST, _PORT, _USER)
    conn.close.assert_called_once()
    conn.wait_closed.assert_called_once()


async def test_close_removes_from_pool(mock_connect, pool: SSHConnectionPool) -> None:
    mock_fn, _ = mock_connect
    await pool.get_connection(_HOST, _PORT, _USER, _KEY)
    await pool.close(_HOST, _PORT, _USER)
    # After close, next get_connection must open a new one
    await pool.get_connection(_HOST, _PORT, _USER, _KEY)
    assert mock_fn.call_count == 2


async def test_close_unknown_host_is_noop(pool: SSHConnectionPool) -> None:
    # Should not raise
    await pool.close("unknown-host", 22, "nobody")


# ---------------------------------------------------------------------------
# close_all
# ---------------------------------------------------------------------------


async def test_close_all_closes_every_connection(pool: SSHConnectionPool) -> None:
    conn_a, conn_b = _make_conn(), _make_conn()
    with (
        patch(
            "control_station_lite.server.core.ssh.asyncssh.connect",
            AsyncMock(side_effect=[conn_a, conn_b]),
        ),
        patch(
            "control_station_lite.server.core.ssh.asyncssh.import_private_key",
            return_value=MagicMock(),
        ),
    ):
        await pool.get_connection("host-a", _PORT, _USER, _KEY)
        await pool.get_connection("host-b", _PORT, _USER, _KEY)
        await pool.close_all()
    conn_a.close.assert_called_once()
    conn_b.close.assert_called_once()


async def test_close_all_empties_pool(pool: SSHConnectionPool) -> None:
    conn = _make_conn()
    with (
        patch(
            "control_station_lite.server.core.ssh.asyncssh.connect", AsyncMock(return_value=conn)
        ),
        patch(
            "control_station_lite.server.core.ssh.asyncssh.import_private_key",
            return_value=MagicMock(),
        ),
    ):
        await pool.get_connection(_HOST, _PORT, _USER, _KEY)
        await pool.close_all()
    assert pool._pool == {}


# ---------------------------------------------------------------------------
# get_ssh_pool singleton
# ---------------------------------------------------------------------------


def test_get_ssh_pool_returns_same_instance() -> None:
    get_ssh_pool.cache_clear()
    try:
        assert get_ssh_pool() is get_ssh_pool()
    finally:
        get_ssh_pool.cache_clear()
