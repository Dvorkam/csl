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

"""Integration tests for Phase 9 — audit instrumentation and the read API."""

import asyncio
import base64
import os
from datetime import datetime
from pathlib import Path

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

_PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n"


@pytest.fixture(autouse=True)
def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
async def machine(db_session: AsyncSession) -> Machine:
    from control_station_lite.server.config import get_settings

    master_key = get_settings().read_master_key()
    m = Machine(
        name="m1",
        ssh_host="192.168.1.50",
        ssh_port=22,
        ssh_user="testuser",
        ssh_key_encrypted=encrypt(_PRIVATE_KEY.encode(), master_key),
        key_fingerprint="SHA256:abc123",
        agent_port=36717,
        scripts_dir="/home/testuser/.csl/scripts",
        platform="linux",
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


def _audit_rows(session: AsyncSession, action: str) -> list[AuditLog]:
    async def _q() -> list[AuditLog]:
        res = await session.execute(select(AuditLog).where(AuditLog.action == action))
        return list(res.scalars().all())

    return asyncio.get_event_loop().run_until_complete(_q())


# ---------------------------------------------------------------------------
# 9.2 — instrumentation
# ---------------------------------------------------------------------------


class TestInstrumentation:
    def test_login_success_audited(
        self, client: TestClient, admin_user: User, db_session: AsyncSession
    ) -> None:
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin-pass"})
        assert resp.status_code == 200
        rows = _audit_rows(db_session, "auth.login")
        assert len(rows) == 1
        assert rows[0].result == "success"
        assert rows[0].user_id == admin_user.id

    def test_login_failure_audited(
        self, client: TestClient, admin_user: User, db_session: AsyncSession
    ) -> None:
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 401
        # Stable error code (Task 9.6) surfaced alongside the human-readable detail.
        assert resp.json()["code"] == "auth.invalid_credentials"
        rows = _audit_rows(db_session, "auth.login")
        assert len(rows) == 1
        assert rows[0].result == "failure"

    def test_login_unknown_user_audited_without_user_id(
        self, client: TestClient, db_session: AsyncSession
    ) -> None:
        resp = client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
        assert resp.status_code == 401
        rows = _audit_rows(db_session, "auth.login")
        assert len(rows) == 1
        assert rows[0].result == "failure"
        assert rows[0].user_id is None

    def test_script_create_update_delete_audited(
        self, client: TestClient, admin_user: User, db_session: AsyncSession
    ) -> None:
        h = _admin_h(admin_user)
        assert (
            client.post(
                "/api/scripts", json={"name": "s1", "content": "echo hi"}, headers=h
            ).status_code
            == 201
        )
        assert (
            client.put("/api/scripts/s1", json={"content": "echo bye"}, headers=h).status_code
            == 200
        )
        assert client.delete("/api/scripts/s1", headers=h).status_code == 204

        assert len(_audit_rows(db_session, "script.create")) == 1
        assert len(_audit_rows(db_session, "script.update")) == 1
        assert len(_audit_rows(db_session, "script.delete")) == 1

    def test_bookmark_audited(
        self,
        client: TestClient,
        regular_user: User,
        machine: Machine,
        db_session: AsyncSession,
    ) -> None:
        h = _user_h(regular_user)
        assert client.post(f"/api/machines/{machine.id}/bookmark", headers=h).status_code == 204
        assert client.delete(f"/api/machines/{machine.id}/bookmark", headers=h).status_code == 204
        assert len(_audit_rows(db_session, "machine.bookmark")) == 1
        assert len(_audit_rows(db_session, "machine.unbookmark")) == 1


# ---------------------------------------------------------------------------
# 9.1 — read API
# ---------------------------------------------------------------------------


class TestAuditReadApi:
    def _seed(self, client: TestClient, admin_user: User) -> None:
        h = _admin_h(admin_user)
        for i in range(3):
            client.post("/api/scripts", json={"name": f"s{i}", "content": "echo"}, headers=h)

    def test_requires_admin(self, client: TestClient, regular_user: User) -> None:
        resp = client.get("/api/audit", headers=_user_h(regular_user))
        assert resp.status_code == 403

    def test_requires_auth(self, client: TestClient) -> None:
        assert client.get("/api/audit").status_code == 401

    def test_lists_entries(self, client: TestClient, admin_user: User) -> None:
        self._seed(client, admin_user)
        resp = client.get("/api/audit", headers=_admin_h(admin_user))
        assert resp.status_code == 200
        body = resp.json()
        actions = {item["action"] for item in body["items"]}
        assert "script.create" in actions

    def test_filter_by_target_type(self, client: TestClient, admin_user: User) -> None:
        self._seed(client, admin_user)
        resp = client.get("/api/audit?target_type=script", headers=_admin_h(admin_user))
        assert resp.status_code == 200
        assert all(i["target_type"] == "script" for i in resp.json()["items"])

    def test_filter_by_action_substring(self, client: TestClient, admin_user: User) -> None:
        self._seed(client, admin_user)
        resp = client.get("/api/audit?action=create", headers=_admin_h(admin_user))
        assert all("create" in i["action"] for i in resp.json()["items"])

    def test_filter_by_username(self, client: TestClient, admin_user: User) -> None:
        self._seed(client, admin_user)
        resp = client.get("/api/audit?username=admin", headers=_admin_h(admin_user))
        items = resp.json()["items"]
        assert items
        assert all(i["username"] == "admin" for i in items)

    def test_pagination_has_next(self, client: TestClient, admin_user: User) -> None:
        self._seed(client, admin_user)  # 3 script.create entries
        resp = client.get("/api/audit?limit=2", headers=_admin_h(admin_user))
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["has_next"] is True
        resp2 = client.get("/api/audit?limit=2&offset=2", headers=_admin_h(admin_user))
        assert resp2.json()["has_next"] is False

    def test_details_deserialised(self, client: TestClient, admin_user: User) -> None:
        self._seed(client, admin_user)
        resp = client.get("/api/audit?action=script.create", headers=_admin_h(admin_user))
        item = resp.json()["items"][0]
        assert "md5" in item["details"]
