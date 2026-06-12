# SPDX-License-Identifier: AGPL-3.0-or-later
#
# control-station-lite
# Copyright (C) 2026 Michal Dvořák
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version, with an additional permission for
# distribution through app stores (see LICENSE).

import asyncio
import logging
from dataclasses import dataclass, field
from functools import lru_cache

import asyncssh

logger = logging.getLogger(__name__)

# (host, port, username)
_ConnKey = tuple[str, int, str]


def build_known_hosts(host_key: str | None) -> object | None:
    """Return an asyncssh ``known_hosts`` value that pins *host_key*.

    *host_key* is a stored OpenSSH public-key line (e.g. ``ssh-ed25519 AAAA…``).
    The ``*`` host pattern pins the key itself regardless of how the host
    resolves, which is exactly the property we want: the server we dialled must
    present this key or the connection fails. Returns ``None`` (trust-on-first-
    use) only for legacy machines with no pinned key.
    """
    if not host_key:
        return None
    return asyncssh.import_known_hosts(f"* {host_key.strip()}\n")


@dataclass
class _Entry:
    conn: asyncssh.SSHClientConnection
    listeners: list[asyncssh.SSHListener] = field(default_factory=list)


class SSHConnectionPool:
    """One persistent SSH connection per target, keyed by (host, port, username).

    Reconnects transparently when a cached connection has been closed (e.g.
    the remote agent shut down or the network dropped). Tunnels opened on a
    connection are tracked so they can be closed cleanly via :meth:`close`.

    Note: callers pass the machine's pinned ``host_key`` so the server's host
    key is validated on connect (fail closed on mismatch). Only legacy machines
    registered before host-key pinning pass ``host_key=None`` (trust-on-first-
    use), which should be resolved by re-registering them.
    """

    def __init__(self) -> None:
        self._pool: dict[_ConnKey, _Entry] = {}
        self._lock = asyncio.Lock()

    async def get_connection(
        self,
        host: str,
        port: int,
        username: str,
        private_key: bytes,
        *,
        host_key: str | None = None,
        connect_timeout: float = 10.0,
    ) -> asyncssh.SSHClientConnection:
        """Return an open connection, opening a new one if necessary.

        When *host_key* is supplied the server's host key is validated against
        it; a mismatch raises :class:`asyncssh.Error` (fail closed).
        """
        key = (host, port, username)
        async with self._lock:
            entry = self._pool.get(key)
            if entry is not None and not entry.conn.is_closed():
                return entry.conn
            conn = await asyncssh.connect(
                host,
                port=port,
                username=username,
                client_keys=[asyncssh.import_private_key(private_key)],
                known_hosts=build_known_hosts(host_key),
                connect_timeout=connect_timeout,
            )
            self._pool[key] = _Entry(conn=conn)
            logger.debug("SSH: opened connection to %s@%s:%d", username, host, port)
            return conn

    async def open_tunnel(
        self,
        host: str,
        port: int,
        username: str,
        private_key: bytes,
        remote_host: str,
        remote_port: int,
        *,
        host_key: str | None = None,
        local_host: str = "127.0.0.1",
    ) -> tuple[asyncssh.SSHListener, int]:
        """Forward a random local port to *remote_host*:*remote_port* over SSH.

        Returns ``(listener, local_port)``.  The caller should close the
        listener when the tunnel is no longer needed; :meth:`close` also
        closes all listeners associated with a connection.
        """
        conn = await self.get_connection(host, port, username, private_key, host_key=host_key)
        listener = await conn.forward_local_port(local_host, 0, remote_host, remote_port)
        local_port = listener.get_port()
        key = (host, port, username)
        async with self._lock:
            if key in self._pool:
                self._pool[key].listeners.append(listener)
        logger.debug(
            "SSH: tunnel %s:%d -> %s:%d (local %d)",
            host,
            port,
            remote_host,
            remote_port,
            local_port,
        )
        return listener, local_port

    async def close(self, host: str, port: int, username: str) -> None:
        """Close the connection and all its tunnels for the given target."""
        key = (host, port, username)
        async with self._lock:
            entry = self._pool.pop(key, None)
        if entry is None:
            return
        for listener in entry.listeners:
            listener.close()
            await listener.wait_closed()
        entry.conn.close()
        await entry.conn.wait_closed()
        logger.debug("SSH: closed connection to %s@%s:%d", username, host, port)

    async def close_all(self) -> None:
        """Close every pooled connection. Call during application shutdown."""
        async with self._lock:
            keys = list(self._pool.keys())
        for key in keys:
            await self.close(*key)


@lru_cache(maxsize=1)
def get_ssh_pool() -> SSHConnectionPool:
    return SSHConnectionPool()
