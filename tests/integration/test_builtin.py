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

"""Integration tests for Phase 7 — built-in actions (Wake-on-LAN)."""

import base64
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_station_lite.server.auth.jwt import create_access_token
from control_station_lite.server.auth.password import hash_password
from control_station_lite.server.core.crypto import encrypt
from control_station_lite.server.db.models import AuditLog, Base, Machine, User
from control_station_lite.server.db.session import get_session
from control_station_lite.server.main import app

_MAC = "AA:BB:CC:DD:EE:FF"
_PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jwt_key = tmp_path / "jwt.key"
    jwt_key.write_bytes(os.urandom(64))
    master_key = tmp_path / "master.key"
    master_key.write_text(base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("CSL_JWT_KEY_PATH", str(jwt_key))
    monkeypatch.setenv("CSL_MASTER_KEY_PATH", str(master_key))
    from control_station_lite.server.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def client(db_session: AsyncSession) -> TestClient:
    async def _override() -> AsyncSession:
        yield db_session

    app.dependency_overrides[get_session] = _override
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = User(
        username="admin",
        password_hash=hash_password("admin-pass"),
        role="admin",
        disabled=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def regular_user(db_session: AsyncSession) -> User:
    user = User(
        username="alice",
        password_hash=hash_password("alice-pass"),
        role="user",
        disabled=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def machine_with_mac(db_session: AsyncSession) -> Machine:
    from control_station_lite.server.config import get_settings

    master_key = get_settings().read_master_key()
    key_enc = encrypt(_PRIVATE_KEY.encode(), master_key)
    m = Machine(
        name="wol-machine",
        ssh_host="192.168.1.50",
        ssh_port=22,
        ssh_user="testuser",
        ssh_key_encrypted=key_enc,
        key_fingerprint="SHA256:abc123",
        agent_port=36717,
        scripts_dir="/home/testuser/.csl/scripts",
        platform="linux",
        mac_address=_MAC,
        created_at=datetime.utcnow(),
    )
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)
    return m


@pytest.fixture
async def machine_no_mac(db_session: AsyncSession) -> Machine:
    from control_station_lite.server.config import get_settings

    master_key = get_settings().read_master_key()
    key_enc = encrypt(_PRIVATE_KEY.encode(), master_key)
    m = Machine(
        name="no-mac-machine",
        ssh_host="192.168.1.51",
        ssh_port=22,
        ssh_user="testuser",
        ssh_key_encrypted=key_enc,
        key_fingerprint="SHA256:xyz789",
        agent_port=36717,
        scripts_dir="/home/testuser/.csl/scripts",
        platform="linux",
        mac_address=None,
        created_at=datetime.utcnow(),
    )
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)
    return m


def _admin_h(u: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(u.id, 'admin')}"}


def _user_h(u: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(u.id, 'user')}"}


def _patch_broadcast() -> object:
    return patch("control_station_lite.server.api.builtin.broadcast")


# ---------------------------------------------------------------------------
# 7.2 POST /api/machines/{id}/builtin/wol
# ---------------------------------------------------------------------------


class TestWakeOnLan:
    def test_wol_success_returns_200(
        self, client: TestClient, admin_user: User, machine_with_mac: Machine
    ) -> None:
        with _patch_broadcast() as mock_broadcast:
            resp = client.post(
                f"/api/machines/{machine_with_mac.id}/builtin/wol",
                headers=_admin_h(admin_user),
            )
        assert resp.status_code == 200
        assert resp.json() == {"sent": True}
        mock_broadcast.assert_called_once_with(_MAC, "255.255.255.255", 9)

    def test_wol_custom_broadcast_addr_and_port(
        self, client: TestClient, admin_user: User, machine_with_mac: Machine
    ) -> None:
        with _patch_broadcast() as mock_broadcast:
            resp = client.post(
                f"/api/machines/{machine_with_mac.id}/builtin/wol",
                json={"broadcast_addr": "192.168.1.255", "port": 7},
                headers=_admin_h(admin_user),
            )
        assert resp.status_code == 200
        mock_broadcast.assert_called_once_with(_MAC, "192.168.1.255", 7)

    def test_wol_no_mac_returns_400(
        self, client: TestClient, admin_user: User, machine_no_mac: Machine
    ) -> None:
        resp = client.post(
            f"/api/machines/{machine_no_mac.id}/builtin/wol",
            headers=_admin_h(admin_user),
        )
        assert resp.status_code == 400
        assert "MAC address" in resp.json()["detail"]

    def test_wol_machine_not_found_returns_404(self, client: TestClient, admin_user: User) -> None:
        resp = client.post("/api/machines/9999/builtin/wol", headers=_admin_h(admin_user))
        assert resp.status_code == 404

    def test_wol_unauthenticated_returns_401(
        self, client: TestClient, machine_with_mac: Machine
    ) -> None:
        resp = client.post(f"/api/machines/{machine_with_mac.id}/builtin/wol")
        assert resp.status_code == 401

    def test_wol_user_without_bookmark_returns_403(
        self, client: TestClient, regular_user: User, machine_with_mac: Machine
    ) -> None:
        resp = client.post(
            f"/api/machines/{machine_with_mac.id}/builtin/wol",
            headers=_user_h(regular_user),
        )
        assert resp.status_code == 403

    def test_wol_bookmarked_user_succeeds(
        self,
        client: TestClient,
        regular_user: User,
        machine_with_mac: Machine,
        db_session: AsyncSession,
    ) -> None:
        client.post(
            f"/api/machines/{machine_with_mac.id}/bookmark",
            headers=_user_h(regular_user),
        )
        with _patch_broadcast():
            resp = client.post(
                f"/api/machines/{machine_with_mac.id}/builtin/wol",
                headers=_user_h(regular_user),
            )
        assert resp.status_code == 200

    def test_wol_broadcast_failure_returns_502(
        self, client: TestClient, admin_user: User, machine_with_mac: Machine
    ) -> None:
        with patch(
            "control_station_lite.server.api.builtin.broadcast",
            side_effect=OSError("network unreachable"),
        ):
            resp = client.post(
                f"/api/machines/{machine_with_mac.id}/builtin/wol",
                headers=_admin_h(admin_user),
            )
        assert resp.status_code == 502
        assert "network unreachable" in resp.json()["detail"]

    # 7.3 — Audit log integration

    def test_wol_success_writes_audit_log(
        self,
        client: TestClient,
        admin_user: User,
        machine_with_mac: Machine,
        db_session: AsyncSession,
    ) -> None:
        with _patch_broadcast():
            client.post(
                f"/api/machines/{machine_with_mac.id}/builtin/wol",
                headers=_admin_h(admin_user),
            )

        import asyncio

        async def _check() -> AuditLog:
            result = await db_session.execute(
                select(AuditLog).where(AuditLog.action == "machine.wol")
            )
            return result.scalar_one()

        log = asyncio.get_event_loop().run_until_complete(_check())
        assert log.result == "success"
        assert log.target_type == "machine"
        assert log.target_id == str(machine_with_mac.id)
        assert log.user_id == admin_user.id

    def test_wol_failure_writes_audit_log_with_failure(
        self,
        client: TestClient,
        admin_user: User,
        machine_with_mac: Machine,
        db_session: AsyncSession,
    ) -> None:
        with patch(
            "control_station_lite.server.api.builtin.broadcast",
            side_effect=OSError("network unreachable"),
        ):
            client.post(
                f"/api/machines/{machine_with_mac.id}/builtin/wol",
                headers=_admin_h(admin_user),
            )

        import asyncio

        async def _check() -> AuditLog:
            result = await db_session.execute(
                select(AuditLog).where(AuditLog.action == "machine.wol")
            )
            return result.scalar_one()

        log = asyncio.get_event_loop().run_until_complete(_check())
        assert log.result == "failure"
