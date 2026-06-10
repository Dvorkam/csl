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
from collections.abc import AsyncGenerator

import asyncssh
import httpx

from control_station_lite.server.core.ssh import SSHConnectionPool
from control_station_lite.server.db.models import Machine
from control_station_lite.shared.models import (
    AgentHealth,
    JobRequest,
    JobStatusResponse,
    ScriptDescriptor,
    StageScriptRequest,
    StageScriptResponse,
)
from control_station_lite.shared.ssh_commands import WAKEUP_CMD

logger = logging.getLogger(__name__)

# Delays between /healthz retries after the start command is issued (~5 s total).
_WAKEUP_BACKOFF = (0.5, 1.0, 1.5, 2.0)


class AgentClientError(Exception):
    """Raised for unrecoverable agent communication errors."""


class AgentNotReachableError(AgentClientError):
    """Raised when the agent did not become healthy after the start command."""


class AgentClient:
    """Typed HTTP client that talks to a csl-agent through an SSH tunnel.

    Usage::

        async with AgentClient(machine, private_key, ssh_pool) as client:
            await client.ensure_agent_running()
            health = await client.get_health()

    The tunnel is opened on ``__aenter__`` and closed on ``__aexit__``.
    The underlying SSH *connection* remains in the pool for reuse.

    Parameters
    ----------
    machine:
        ORM row for the target machine.
    private_key:
        Raw bytes of the already-decrypted Ed25519 private key.
    ssh_pool:
        Shared pool; ``AgentClient`` borrows a connection but does not own it.
    _http_client:
        Injected only in tests to bypass real TCP connections.
    """

    def __init__(
        self,
        machine: Machine,
        private_key: bytes,
        ssh_pool: SSHConnectionPool,
        *,
        _http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._machine = machine
        self._private_key = private_key
        self._pool = ssh_pool
        self._http_client_override = _http_client
        self._listener: asyncssh.SSHListener | None = None
        self._local_port: int | None = None
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "AgentClient":
        self._listener, self._local_port = await self._pool.open_tunnel(
            self._machine.ssh_host,
            self._machine.ssh_port,
            self._machine.ssh_user,
            self._private_key,
            "127.0.0.1",
            self._machine.agent_port,
        )
        if self._http_client_override is not None:
            self._http = self._http_client_override
        else:
            self._http = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{self._local_port}",
                timeout=30.0,
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._http is not None and self._http_client_override is None:
            await self._http.aclose()
            self._http = None
        if self._listener is not None:
            self._listener.close()
            await self._listener.wait_closed()
            self._listener = None

    # ------------------------------------------------------------------
    # Agent wakeup
    # ------------------------------------------------------------------

    async def ensure_agent_running(self) -> None:
        """Ping /healthz; start the agent if it does not respond; poll until up.

        Raises :class:`AgentNotReachableError` if the agent never becomes
        reachable within the backoff window (~5 s).
        """
        if await self._is_healthy():
            return

        cmd = WAKEUP_CMD.get(self._machine.platform)
        if cmd is None:
            raise AgentClientError(f"No start command for platform {self._machine.platform!r}")

        conn = await self._pool.get_connection(
            self._machine.ssh_host,
            self._machine.ssh_port,
            self._machine.ssh_user,
            self._private_key,
        )
        await conn.run(cmd, check=False)
        logger.info(
            "SSH: sent start command to %s (%s)", self._machine.name, self._machine.platform
        )

        for delay in _WAKEUP_BACKOFF:
            await asyncio.sleep(delay)
            if await self._is_healthy():
                logger.info("Agent on %r is now reachable", self._machine.name)
                return

        raise AgentNotReachableError(
            f"Agent on {self._machine.name!r} did not respond after start command"
        )

    async def _is_healthy(self) -> bool:
        assert self._http is not None, "AgentClient must be used as a context manager"
        try:
            resp = await self._http.get("/healthz", timeout=2.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ------------------------------------------------------------------
    # Typed API calls
    # ------------------------------------------------------------------

    async def get_health(self) -> AgentHealth:
        assert self._http is not None
        resp = await self._http.get("/healthz")
        resp.raise_for_status()
        return AgentHealth.model_validate(resp.json())

    async def get_script_state(self, name: str) -> ScriptDescriptor:
        assert self._http is not None
        resp = await self._http.get(f"/scripts/{name}/state")
        resp.raise_for_status()
        return ScriptDescriptor.model_validate(resp.json())

    async def stage_script(
        self, name: str, content: str, md5: str, meta_yaml: str | None
    ) -> StageScriptResponse:
        assert self._http is not None
        body = StageScriptRequest(content=content, md5=md5, meta_yaml=meta_yaml)
        resp = await self._http.post(f"/scripts/{name}/stage", json=body.model_dump())
        resp.raise_for_status()
        return StageScriptResponse.model_validate(resp.json())

    async def submit_job(self, request: JobRequest) -> JobStatusResponse:
        assert self._http is not None
        resp = await self._http.post("/jobs", json=request.model_dump())
        resp.raise_for_status()
        return JobStatusResponse.model_validate(resp.json())

    async def get_job_status(self, job_uuid: str) -> JobStatusResponse:
        assert self._http is not None
        resp = await self._http.get(f"/jobs/{job_uuid}")
        resp.raise_for_status()
        return JobStatusResponse.model_validate(resp.json())

    async def kill_job(self, job_uuid: str) -> None:
        assert self._http is not None
        resp = await self._http.delete(f"/jobs/{job_uuid}")
        resp.raise_for_status()

    async def stream_logs(self, job_uuid: str) -> AsyncGenerator[str, None]:
        """Yield SSE data lines from the agent's log stream for *job_uuid*.

        Each yielded string is the payload of one ``data:`` SSE line with
        leading/trailing whitespace stripped. The caller is responsible for
        JSON-decoding if the agent sends structured events.
        """
        assert self._http is not None
        async with self._http.stream("GET", f"/jobs/{job_uuid}/stream", timeout=None) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    yield line[5:].strip()
