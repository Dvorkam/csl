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

"""Machine management API: register, list, detail, delete, bookmark, ping, agent-status."""

import base64
import hashlib
import importlib.metadata
import logging
import re
import time
from datetime import datetime

import asyncssh
import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, computed_field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.auth.dependencies import current_user, require_admin
from control_station_lite.server.config import get_settings
from control_station_lite.server.core.audit import record_audit
from control_station_lite.server.core.crypto import decrypt, encrypt
from control_station_lite.server.core.errors import CslHTTPException, ErrorCode
from control_station_lite.server.core.ssh import build_known_hosts, get_ssh_pool
from control_station_lite.server.db.models import Machine, User, UserMachine
from control_station_lite.server.db.session import get_session
from control_station_lite.server.logging_config import REQUEST_ID_HEADER, request_id_var
from control_station_lite.shared.models import AgentHealth
from control_station_lite.shared.registration import RegistrationBundle
from control_station_lite.shared.ssh_commands import CONFIG_READ_CMD

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/machines", tags=["machines"])


def _server_version() -> str:
    try:
        return importlib.metadata.version("control-station-lite")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _major(version: str) -> int | None:
    """Leading integer of a version string, or None when it has none."""
    match = re.match(r"\s*(\d+)", version)
    return int(match.group(1)) if match else None


def _assert_version_compatible(agent_version: str) -> None:
    """Refuse registration when the agent's major version differs (§11).

    Server and agent ship as a single PyPI version and must match on the major.
    When either version lacks a parseable major (e.g. an agent run from a source
    checkout reports ``"unknown"``), we log and allow rather than block dev work.
    """
    server_version = _server_version()
    agent_major = _major(agent_version)
    server_major = _major(server_version)
    if agent_major is None or server_major is None:
        logger.warning(
            "registration version check skipped: unparseable version (agent=%r, server=%r)",
            agent_version,
            server_version,
        )
        return
    if agent_major != server_major:
        raise CslHTTPException(
            status_code=status.HTTP_409_CONFLICT,
            code=ErrorCode.VERSION_INCOMPATIBLE,
            detail=(
                f"agent version {agent_version!r} is incompatible with server "
                f"version {server_version!r}: major versions must match"
            ),
            extra={"agent_version": agent_version, "server_version": server_version},
        )


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class RegisterMachineIn(BaseModel):
    bundle: str
    name: str
    ssh_host: str
    ssh_port: int = 22
    ssh_user: str | None = None  # overrides bundle value; defaults to bundle.ssh_user
    mac_address: str | None = None


def _host_key_fingerprint(host_key_line: str | None) -> str | None:
    """SHA-256 fingerprint of a stored OpenSSH host-key line, or None."""
    if not host_key_line:
        return None
    parts = host_key_line.split()
    if len(parts) < 2:
        return None
    digest = hashlib.sha256(base64.b64decode(parts[1])).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class MachineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    ssh_host: str
    ssh_port: int
    ssh_user: str
    agent_port: int
    platform: str
    key_fingerprint: str
    mac_address: str | None
    created_at: datetime
    # Source key line is read from the ORM but not serialised; the admin
    # confirms the derived fingerprint out-of-band.
    ssh_host_key: str | None = Field(default=None, exclude=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ssh_host_key_fingerprint(self) -> str | None:
        return _host_key_fingerprint(self.ssh_host_key)


class PingOut(BaseModel):
    reachable: bool
    latency_ms: float | None = None


class AgentStatusOut(BaseModel):
    running: bool
    health: AgentHealth | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _remote_config_cmd(platform: str) -> str:
    """SSH exec command that prints the agent config.yaml on the remote machine.

    Must exactly match an allowlisted command in ``shared.ssh_commands`` so the
    target's ssh-gateway permits it.
    """
    return CONFIG_READ_CMD.get(platform, CONFIG_READ_CMD["linux"])


async def _ssh_connection_test(
    bundle: RegistrationBundle,
    ssh_user: str,
    ssh_host: str,
    ssh_port: int,
) -> str:
    """Open a one-shot SSH connection, verify the agent key, capture the host key.

    This is the single trust-on-first-use point: ``known_hosts=None`` is allowed
    here because there is no pinned key yet. The server's host key is captured
    and returned (OpenSSH public-key line) so the caller can pin it.

    Raises ValueError on fingerprint mismatch.
    Raises asyncssh.Error / OSError on connection failure.
    """
    private_key = asyncssh.import_private_key(bundle.private_key)
    async with asyncssh.connect(
        ssh_host,
        port=ssh_port,
        username=ssh_user,
        client_keys=[private_key],
        known_hosts=None,
        connect_timeout=15.0,
    ) as conn:
        server_host_key = conn.get_server_host_key()
        if server_host_key is None:
            raise ValueError("could not obtain the target's SSH host key")
        host_key_line = server_host_key.export_public_key().decode().strip()

        result = await conn.run(_remote_config_cmd(bundle.platform), check=False)
        raw_out = result.stdout or ""
        stdout: str = (
            raw_out if isinstance(raw_out, str) else raw_out.decode("utf-8", errors="replace")
        )
        try:
            config_data = yaml.safe_load(stdout) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"remote config.yaml is not valid YAML: {exc}") from exc
        if not isinstance(config_data, dict):
            raise ValueError("remote config.yaml is not a YAML mapping")
        identity = config_data.get("identity") or {}
        remote_fingerprint = identity.get("key_fingerprint")
        if remote_fingerprint != bundle.key_fingerprint:
            raise ValueError(
                f"key fingerprint mismatch: remote={remote_fingerprint!r}, "
                f"bundle={bundle.key_fingerprint!r}"
            )
        return host_key_line


async def _get_machine_or_404(machine_id: int, session: AsyncSession) -> Machine:
    result = await session.execute(select(Machine).where(Machine.id == machine_id))
    machine = result.scalar_one_or_none()
    if machine is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found")
    return machine


async def _assert_access(user: User, machine_id: int, session: AsyncSession) -> None:
    """Raise 403 if *user* has no bookmark on *machine_id* (admins are exempt)."""
    if user.role == "admin":
        return
    bm = await session.execute(
        select(UserMachine).where(
            UserMachine.user_id == user.id, UserMachine.machine_id == machine_id
        )
    )
    if bm.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")


def _decrypt_private_key(machine: Machine) -> bytes:
    master_key = get_settings().read_master_key()
    return decrypt(machine.ssh_key_encrypted, master_key)


def _agent_auth_headers(machine: Machine) -> dict[str, str]:
    """Bearer-token + correlation-id headers for direct agent calls."""
    headers: dict[str, str] = {}
    if machine.agent_token_encrypted:
        token = decrypt(machine.agent_token_encrypted, get_settings().read_master_key()).decode()
        headers["Authorization"] = f"Bearer {token}"
    request_id = request_id_var.get()
    if request_id is not None:
        headers[REQUEST_ID_HEADER] = request_id
    return headers


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def register_machine_from_input(
    body: RegisterMachineIn,
    admin_id: int,
    session: AsyncSession,
) -> Machine:
    """Decode a bundle, test the connection, and persist a new Machine.

    Shared by the JSON API endpoint and the admin web form. Raises
    ``HTTPException`` / ``CslHTTPException`` on failure (bundle decode, version
    mismatch, duplicate name, connection test); callers map these to either an
    HTTP response or a flash message.
    """
    try:
        bundle = RegistrationBundle.decode(body.bundle)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    _assert_version_compatible(bundle.agent_version)

    ssh_user = body.ssh_user or bundle.ssh_user

    existing = await session.execute(select(Machine).where(Machine.name == body.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Machine with name {body.name!r} already exists",
        )

    try:
        host_key_line = await _ssh_connection_test(bundle, ssh_user, body.ssh_host, body.ssh_port)
    except (OSError, asyncssh.Error, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Connection test failed: {exc}",
        ) from exc

    master_key = get_settings().read_master_key()
    encrypted_key = encrypt(bundle.private_key.encode(), master_key)
    encrypted_token = encrypt(bundle.api_token.encode(), master_key)

    machine = Machine(
        name=body.name,
        ssh_host=body.ssh_host,
        ssh_port=body.ssh_port,
        ssh_user=ssh_user,
        ssh_key_encrypted=encrypted_key,
        key_fingerprint=bundle.key_fingerprint,
        ssh_host_key=host_key_line,
        agent_token_encrypted=encrypted_token,
        agent_port=bundle.agent_port,
        scripts_dir=bundle.scripts_dir,
        platform=bundle.platform,
        mac_address=body.mac_address,
        created_at=datetime.utcnow(),
    )
    session.add(machine)
    await session.flush()
    await record_audit(
        session,
        action="machine.register",
        target_type="machine",
        target_id=machine.id,
        result="success",
        user_id=admin_id,
        details={"name": machine.name, "ssh_host": machine.ssh_host, "platform": machine.platform},
    )
    await session.commit()
    await session.refresh(machine)
    return machine


@router.post("", status_code=status.HTTP_201_CREATED, response_model=MachineOut)
async def register_machine(
    body: RegisterMachineIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Machine:
    return await register_machine_from_input(body, admin.id, session)


@router.get("", response_model=list[MachineOut])
async def list_machines(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Machine]:
    if user.role == "admin":
        result = await session.execute(select(Machine))
        return list(result.scalars().all())
    result = await session.execute(
        select(Machine)
        .join(UserMachine, UserMachine.machine_id == Machine.id)
        .where(UserMachine.user_id == user.id)
    )
    return list(result.scalars().all())


@router.get("/{machine_id}", response_model=MachineOut)
async def get_machine(
    machine_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Machine:
    machine = await _get_machine_or_404(machine_id, session)
    await _assert_access(user, machine_id, session)
    return machine


@router.delete("/{machine_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_machine(
    machine_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    await _get_machine_or_404(machine_id, session)
    await session.execute(delete(UserMachine).where(UserMachine.machine_id == machine_id))
    result = await session.execute(select(Machine).where(Machine.id == machine_id))
    machine = result.scalar_one()
    await session.delete(machine)
    await record_audit(
        session,
        action="machine.delete",
        target_type="machine",
        target_id=machine_id,
        result="success",
        user_id=admin.id,
        details={"name": machine.name},
    )
    await session.commit()


@router.post("/{machine_id}/bookmark", status_code=status.HTTP_204_NO_CONTENT)
async def add_bookmark(
    machine_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await _get_machine_or_404(machine_id, session)
    existing = await session.execute(
        select(UserMachine).where(
            UserMachine.user_id == user.id, UserMachine.machine_id == machine_id
        )
    )
    if existing.scalar_one_or_none() is None:
        session.add(UserMachine(user_id=user.id, machine_id=machine_id))
        await record_audit(
            session,
            action="machine.bookmark",
            target_type="machine",
            target_id=machine_id,
            result="success",
            user_id=user.id,
        )
        await session.commit()


@router.delete("/{machine_id}/bookmark", status_code=status.HTTP_204_NO_CONTENT)
async def remove_bookmark(
    machine_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await _get_machine_or_404(machine_id, session)
    await session.execute(
        delete(UserMachine).where(
            UserMachine.user_id == user.id, UserMachine.machine_id == machine_id
        )
    )
    await record_audit(
        session,
        action="machine.unbookmark",
        target_type="machine",
        target_id=machine_id,
        result="success",
        user_id=user.id,
    )
    await session.commit()


@router.get("/{machine_id}/ping", response_model=PingOut)
async def ping_machine(
    machine_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> PingOut:
    machine = await _get_machine_or_404(machine_id, session)
    await _assert_access(user, machine_id, session)

    private_key_bytes = _decrypt_private_key(machine)
    t0 = time.monotonic()
    try:
        async with asyncssh.connect(
            machine.ssh_host,
            port=machine.ssh_port,
            username=machine.ssh_user,
            client_keys=[asyncssh.import_private_key(private_key_bytes)],
            known_hosts=build_known_hosts(machine.ssh_host_key),
            connect_timeout=5.0,
        ):
            latency_ms = (time.monotonic() - t0) * 1000
            return PingOut(reachable=True, latency_ms=round(latency_ms, 2))
    except (OSError, asyncssh.Error):
        return PingOut(reachable=False)


@router.get("/{machine_id}/agent-status", response_model=AgentStatusOut)
async def agent_status(
    machine_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentStatusOut:
    machine = await _get_machine_or_404(machine_id, session)
    await _assert_access(user, machine_id, session)

    private_key_bytes = _decrypt_private_key(machine)
    pool = get_ssh_pool()
    try:
        listener, local_port = await pool.open_tunnel(
            machine.ssh_host,
            machine.ssh_port,
            machine.ssh_user,
            private_key_bytes,
            "127.0.0.1",
            machine.agent_port,
            host_key=machine.ssh_host_key,
        )
    except (OSError, asyncssh.Error):
        return AgentStatusOut(running=False)

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{local_port}",
            timeout=2.0,
            headers=_agent_auth_headers(machine),
        ) as http:
            resp = await http.get("/healthz")
            if resp.status_code == 200:
                health = AgentHealth.model_validate(resp.json())
                return AgentStatusOut(running=True, health=health)
            return AgentStatusOut(running=False)
    except httpx.HTTPError:
        return AgentStatusOut(running=False)
    finally:
        listener.close()
        await listener.wait_closed()
