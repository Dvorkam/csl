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

import time
from datetime import datetime

import asyncssh
import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.auth.dependencies import current_user, require_admin
from control_station_lite.server.config import get_settings
from control_station_lite.server.core.crypto import decrypt, encrypt
from control_station_lite.server.core.ssh import get_ssh_pool
from control_station_lite.server.db.models import Machine, User, UserMachine
from control_station_lite.server.db.session import get_session
from control_station_lite.shared.models import AgentHealth
from control_station_lite.shared.registration import RegistrationBundle

router = APIRouter(prefix="/api/machines", tags=["machines"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class RegisterMachineIn(BaseModel):
    bundle: str
    name: str
    ssh_host: str
    ssh_port: int = 22
    ssh_user: str
    mac_address: str | None = None


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
    """SSH exec command that prints the agent config.yaml on the remote machine."""
    if platform == "windows":
        return (
            r'powershell -Command "Get-Content (Join-Path $env:USERPROFILE \".csl\config.yaml\")"'
        )
    return "cat ~/.csl/config.yaml"


async def _ssh_connection_test(
    bundle: RegistrationBundle,
    ssh_user: str,
    ssh_host: str,
    ssh_port: int,
) -> None:
    """Open a one-shot SSH connection and verify the remote key fingerprint.

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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED, response_model=MachineOut)
async def register_machine(
    body: RegisterMachineIn,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Machine:
    try:
        bundle = RegistrationBundle.decode(body.bundle)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    existing = await session.execute(select(Machine).where(Machine.name == body.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Machine with name {body.name!r} already exists",
        )

    try:
        await _ssh_connection_test(bundle, body.ssh_user, body.ssh_host, body.ssh_port)
    except (OSError, asyncssh.Error, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Connection test failed: {exc}",
        ) from exc

    master_key = get_settings().read_master_key()
    encrypted_key = encrypt(bundle.private_key.encode(), master_key)

    machine = Machine(
        name=body.name,
        ssh_host=body.ssh_host,
        ssh_port=body.ssh_port,
        ssh_user=body.ssh_user,
        ssh_key_encrypted=encrypted_key,
        key_fingerprint=bundle.key_fingerprint,
        agent_port=bundle.agent_port,
        scripts_dir=bundle.scripts_dir,
        platform=bundle.platform,
        mac_address=body.mac_address,
        created_at=datetime.utcnow(),
    )
    session.add(machine)
    await session.commit()
    await session.refresh(machine)
    return machine


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
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    await _get_machine_or_404(machine_id, session)
    await session.execute(delete(UserMachine).where(UserMachine.machine_id == machine_id))
    result = await session.execute(select(Machine).where(Machine.id == machine_id))
    machine = result.scalar_one()
    await session.delete(machine)
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
            known_hosts=None,
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
        )
    except (OSError, asyncssh.Error):
        return AgentStatusOut(running=False)

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{local_port}", timeout=2.0
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
