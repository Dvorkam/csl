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

"""Unit tests for admin web routes (8.8-8.12)."""

import asyncio
import base64
import os
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_station_lite.server.auth.jwt import create_access_token
from control_station_lite.server.auth.password import hash_password
from control_station_lite.server.db.models import AuditLog, Base, Machine, Script, User, UserMachine
from control_station_lite.server.db.session import get_session
from control_station_lite.server.main import app

# ---------------------------------------------------------------------------
# Fixtures (mirror pattern from test_web_machines.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _jwt_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = tmp_path / "jwt.key"
    key_file.write_bytes(os.urandom(64))
    master_file = tmp_path / "master.key"
    master_file.write_text(base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("CSL_JWT_KEY_PATH", str(key_file))
    monkeypatch.setenv("CSL_MASTER_KEY_PATH", str(master_file))
    from control_station_lite.server.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    u = User(username="admin", password_hash=hash_password("pw"), role="admin", disabled=False)
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest.fixture
async def regular_user(db_session: AsyncSession) -> User:
    u = User(username="bob", password_hash=hash_password("pw"), role="user", disabled=False)
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest.fixture
def client(db_session: AsyncSession) -> TestClient:
    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


def _auth(user: User) -> dict[str, str]:
    return {"csl_access": create_access_token(user.id, user.role)}


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 8.8 — Script library list
# ---------------------------------------------------------------------------


class TestAdminScriptList:
    def test_200_for_admin(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/admin/scripts", cookies=_auth(admin_user))
        assert resp.status_code == 200
        assert b"Script Library" in resp.content

    def test_403_for_regular_user(self, client: TestClient, regular_user: User) -> None:
        resp = client.get("/admin/scripts", cookies=_auth(regular_user))
        assert resp.status_code == 403

    def test_redirects_without_auth(self, client: TestClient) -> None:
        resp = client.get("/admin/scripts")
        assert resp.status_code == 302
        assert "/login" in resp.headers["location"]

    def test_shows_existing_script(
        self, client: TestClient, admin_user: User, db_session: AsyncSession
    ) -> None:
        async def _seed() -> None:
            db_session.add(
                Script(
                    name="myscript",
                    content="echo hi",
                    meta_yaml=None,
                    md5="abc",
                    persistent=False,
                    updated_at=datetime.utcnow(),
                    updated_by=admin_user.id,
                )
            )
            await db_session.commit()

        _run(_seed())
        resp = client.get("/admin/scripts", cookies=_auth(admin_user))
        assert b"myscript" in resp.content


# ---------------------------------------------------------------------------
# 8.9 — Script editor (create / edit / delete)
# ---------------------------------------------------------------------------


class TestAdminScriptCreate:
    def test_new_form_200(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/admin/scripts/new", cookies=_auth(admin_user))
        assert resp.status_code == 200
        assert b"New Script" in resp.content

    def test_create_redirects_on_success(self, client: TestClient, admin_user: User) -> None:
        resp = client.post(
            "/admin/scripts/new",
            data={"name": "greet", "content": "echo hello", "meta_yaml": ""},
            cookies=_auth(admin_user),
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/scripts"

    def test_create_422_on_duplicate(
        self, client: TestClient, admin_user: User, db_session: AsyncSession
    ) -> None:
        async def _seed() -> None:
            db_session.add(
                Script(
                    name="dup",
                    content="x",
                    meta_yaml=None,
                    md5="x",
                    persistent=False,
                    updated_at=datetime.utcnow(),
                    updated_by=admin_user.id,
                )
            )
            await db_session.commit()

        _run(_seed())
        resp = client.post(
            "/admin/scripts/new",
            data={"name": "dup", "content": "y", "meta_yaml": ""},
            cookies=_auth(admin_user),
        )
        assert resp.status_code == 422
        assert b"already exists" in resp.content

    def test_create_422_on_invalid_meta(self, client: TestClient, admin_user: User) -> None:
        resp = client.post(
            "/admin/scripts/new",
            data={"name": "badmeta", "content": "x", "meta_yaml": "params: not-a-list"},
            cookies=_auth(admin_user),
        )
        assert resp.status_code == 422


class TestAdminScriptEdit:
    def _seed_script(self, db_session: AsyncSession, admin_user: User, name: str = "hello") -> None:
        async def _inner() -> None:
            db_session.add(
                Script(
                    name=name,
                    content="echo hi",
                    meta_yaml=None,
                    md5="abc",
                    persistent=False,
                    updated_at=datetime.utcnow(),
                    updated_by=admin_user.id,
                )
            )
            await db_session.commit()

        _run(_inner())

    def test_edit_form_200(
        self, client: TestClient, admin_user: User, db_session: AsyncSession
    ) -> None:
        self._seed_script(db_session, admin_user)
        resp = client.get("/admin/scripts/hello/edit", cookies=_auth(admin_user))
        assert resp.status_code == 200
        assert b"hello" in resp.content

    def test_edit_form_redirects_for_missing(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/admin/scripts/no_such/edit", cookies=_auth(admin_user))
        assert resp.status_code == 303

    def test_edit_submit_redirects_on_success(
        self, client: TestClient, admin_user: User, db_session: AsyncSession
    ) -> None:
        self._seed_script(db_session, admin_user)
        resp = client.post(
            "/admin/scripts/hello/edit",
            data={"content": "echo updated", "meta_yaml": ""},
            cookies=_auth(admin_user),
        )
        assert resp.status_code == 303

    def test_delete_redirects_on_success(
        self, client: TestClient, admin_user: User, db_session: AsyncSession
    ) -> None:
        self._seed_script(db_session, admin_user, name="todelete")
        resp = client.post(
            "/admin/scripts/todelete/delete",
            cookies=_auth(admin_user),
        )
        assert resp.status_code == 303

    def test_delete_unknown_still_redirects(self, client: TestClient, admin_user: User) -> None:
        resp = client.post("/admin/scripts/no_such/delete", cookies=_auth(admin_user))
        assert resp.status_code == 303


# ---------------------------------------------------------------------------
# 8.10 — Machine management
# ---------------------------------------------------------------------------


def _seed_machine(db_session: AsyncSession) -> Machine:
    m = Machine(
        name="testbox",
        ssh_host="192.168.1.10",
        ssh_port=22,
        ssh_user="ubuntu",
        ssh_key_encrypted=b"\x00" * 32,
        key_fingerprint="aa:bb",
        agent_port=36717,
        scripts_dir="/home/ubuntu/.csl/scripts",
        platform="linux",
        mac_address=None,
        created_at=datetime.utcnow(),
    )
    db_session.add(m)

    async def _commit() -> None:
        await db_session.commit()
        await db_session.refresh(m)

    _run(_commit())
    return m


class TestAdminMachineList:
    def test_200_for_admin(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/admin/machines", cookies=_auth(admin_user))
        assert resp.status_code == 200
        assert b"Machine Management" in resp.content

    def test_403_for_regular_user(self, client: TestClient, regular_user: User) -> None:
        resp = client.get("/admin/machines", cookies=_auth(regular_user))
        assert resp.status_code == 403

    def test_shows_machine_name(
        self, client: TestClient, admin_user: User, db_session: AsyncSession
    ) -> None:
        _seed_machine(db_session)
        resp = client.get("/admin/machines", cookies=_auth(admin_user))
        assert b"testbox" in resp.content


class TestAdminMachineDelete:
    def test_delete_redirects(
        self, client: TestClient, admin_user: User, db_session: AsyncSession
    ) -> None:
        m = _seed_machine(db_session)
        resp = client.post(f"/admin/machines/{m.id}/delete", cookies=_auth(admin_user))
        assert resp.status_code == 303

    def test_delete_removes_bookmarks(
        self, client: TestClient, admin_user: User, regular_user: User, db_session: AsyncSession
    ) -> None:
        m = _seed_machine(db_session)

        async def _bm() -> None:
            db_session.add(UserMachine(user_id=regular_user.id, machine_id=m.id))
            await db_session.commit()

        _run(_bm())
        resp = client.post(f"/admin/machines/{m.id}/delete", cookies=_auth(admin_user))
        assert resp.status_code == 303

    def test_delete_unknown_machine(self, client: TestClient, admin_user: User) -> None:
        resp = client.post("/admin/machines/9999/delete", cookies=_auth(admin_user))
        assert resp.status_code == 303

    def test_list_has_register_button(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/admin/machines", cookies=_auth(admin_user))
        assert b"/admin/machines/new" in resp.content


def _fake_machine() -> Machine:
    """A populated (uncommitted) Machine for the success-path mock."""
    return Machine(
        id=1,
        name="gaming-pc",
        ssh_host="10.0.0.5",
        ssh_port=22,
        ssh_user="me",
        ssh_key_encrypted=b"enc",
        key_fingerprint="SHA256:abc",
        ssh_host_key="ssh-ed25519 " + base64.b64encode(b"\x00" * 32).decode(),
        agent_token_encrypted=b"tok",
        agent_port=36717,
        scripts_dir="/home/me/.csl/scripts",
        platform="linux",
        mac_address=None,
        created_at=datetime.utcnow(),
    )


class TestAdminMachineRegister:
    def test_form_200_for_admin(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/admin/machines/new", cookies=_auth(admin_user))
        assert resp.status_code == 200
        assert b"Register a machine" in resp.content

    def test_form_403_for_regular_user(self, client: TestClient, regular_user: User) -> None:
        resp = client.get("/admin/machines/new", cookies=_auth(regular_user))
        assert resp.status_code == 403

    def test_form_redirects_without_auth(self, client: TestClient) -> None:
        resp = client.get("/admin/machines/new")
        assert resp.status_code == 302
        assert "/login" in resp.headers["location"]

    def test_submit_403_for_regular_user(self, client: TestClient, regular_user: User) -> None:
        resp = client.post(
            "/admin/machines/new",
            data={"bundle": "x", "name": "n", "ssh_host": "h"},
            cookies=_auth(regular_user),
        )
        assert resp.status_code == 403

    def test_submit_success_redirects_with_fingerprint_flash(
        self, client: TestClient, admin_user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        mock = AsyncMock(return_value=_fake_machine())
        monkeypatch.setattr(
            "control_station_lite.server.web.admin.register_machine_from_input", mock
        )
        resp = client.post(
            "/admin/machines/new",
            data={
                "bundle": "validbundle",
                "name": "gaming-pc",
                "ssh_host": "10.0.0.5",
                "ssh_port": "22",
                "ssh_user": "me",
                "mac_address": "",
            },
            cookies=_auth(admin_user),
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/machines"
        # flash carries the host-key fingerprint for out-of-band confirmation
        set_cookie = resp.headers.get("set-cookie", "")
        assert "_flash" in set_cookie
        assert "SHA256" in set_cookie
        # the parsed form values reached the registration helper
        assert mock.await_count == 1
        reg = mock.await_args.args[0]
        assert reg.name == "gaming-pc"
        assert reg.ssh_host == "10.0.0.5"
        assert reg.ssh_user == "me"
        assert reg.mac_address is None  # empty string normalised to None

    def test_submit_duplicate_name_rerenders_with_error(
        self, client: TestClient, admin_user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from fastapi import HTTPException

        mock = AsyncMock(
            side_effect=HTTPException(
                status_code=409, detail="Machine with name 'dup' already exists"
            )
        )
        monkeypatch.setattr(
            "control_station_lite.server.web.admin.register_machine_from_input", mock
        )
        resp = client.post(
            "/admin/machines/new",
            data={"bundle": "b", "name": "dup", "ssh_host": "h"},
            cookies=_auth(admin_user),
        )
        assert resp.status_code == 422
        assert b"already exists" in resp.content
        assert b"dup" in resp.content  # entered name preserved in the re-rendered form

    def test_submit_invalid_bundle_shows_error(self, client: TestClient, admin_user: User) -> None:
        # No mock: exercises the real RegistrationBundle.decode failure path.
        resp = client.post(
            "/admin/machines/new",
            data={"bundle": "not-a-valid-bundle", "name": "x", "ssh_host": "h"},
            cookies=_auth(admin_user),
        )
        assert resp.status_code == 422
        assert b"flash-error" in resp.content


# ---------------------------------------------------------------------------
# 8.11 — User management
# ---------------------------------------------------------------------------


class TestAdminUserList:
    def test_200_for_admin(self, client: TestClient, admin_user: User, regular_user: User) -> None:
        resp = client.get("/admin/users", cookies=_auth(admin_user))
        assert resp.status_code == 200
        assert b"admin" in resp.content
        assert b"bob" in resp.content

    def test_403_for_regular_user(self, client: TestClient, regular_user: User) -> None:
        resp = client.get("/admin/users", cookies=_auth(regular_user))
        assert resp.status_code == 403


class TestAdminUserToggle:
    def test_toggle_disables_user(
        self, client: TestClient, admin_user: User, regular_user: User
    ) -> None:
        resp = client.post(f"/admin/users/{regular_user.id}/toggle", cookies=_auth(admin_user))
        assert resp.status_code == 303

    def test_cannot_disable_self(self, client: TestClient, admin_user: User) -> None:
        resp = client.post(f"/admin/users/{admin_user.id}/toggle", cookies=_auth(admin_user))
        assert resp.status_code == 303
        # Flash error set — page still redirects, not an HTTP error code

    def test_toggle_unknown_user(self, client: TestClient, admin_user: User) -> None:
        resp = client.post("/admin/users/9999/toggle", cookies=_auth(admin_user))
        assert resp.status_code == 303


class TestAdminUserRole:
    def test_set_role_to_admin(
        self, client: TestClient, admin_user: User, regular_user: User
    ) -> None:
        resp = client.post(
            f"/admin/users/{regular_user.id}/role",
            data={"role": "admin"},
            cookies=_auth(admin_user),
        )
        assert resp.status_code == 303

    def test_invalid_role_rejected(
        self, client: TestClient, admin_user: User, regular_user: User
    ) -> None:
        resp = client.post(
            f"/admin/users/{regular_user.id}/role",
            data={"role": "superuser"},
            cookies=_auth(admin_user),
        )
        assert resp.status_code == 303  # redirects with flash error

    def test_cannot_demote_self(self, client: TestClient, admin_user: User) -> None:
        resp = client.post(
            f"/admin/users/{admin_user.id}/role",
            data={"role": "user"},
            cookies=_auth(admin_user),
        )
        assert resp.status_code == 303


# ---------------------------------------------------------------------------
# 8.12 — Audit log viewer
# ---------------------------------------------------------------------------


def _seed_audit(db_session: AsyncSession, admin_user: User) -> None:
    async def _inner() -> None:
        db_session.add(
            AuditLog(
                timestamp=datetime.utcnow(),
                user_id=admin_user.id,
                action="login",
                target_type="user",
                target_id=str(admin_user.id),
                result="ok",
                details_json=None,
            )
        )
        await db_session.commit()

    _run(_inner())


class TestAdminAuditLog:
    def test_200_for_admin(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/admin/audit", cookies=_auth(admin_user))
        assert resp.status_code == 200
        assert b"Audit Log" in resp.content

    def test_403_for_regular_user(self, client: TestClient, regular_user: User) -> None:
        resp = client.get("/admin/audit", cookies=_auth(regular_user))
        assert resp.status_code == 403

    def test_shows_audit_entry(
        self, client: TestClient, admin_user: User, db_session: AsyncSession
    ) -> None:
        _seed_audit(db_session, admin_user)
        resp = client.get("/admin/audit", cookies=_auth(admin_user))
        assert b"login" in resp.content

    def test_filter_by_action(
        self, client: TestClient, admin_user: User, db_session: AsyncSession
    ) -> None:
        _seed_audit(db_session, admin_user)
        resp = client.get("/admin/audit?action=login", cookies=_auth(admin_user))
        assert resp.status_code == 200
        assert b"login" in resp.content

    def test_filter_no_match_shows_empty(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/admin/audit?action=nonexistent_action", cookies=_auth(admin_user))
        assert resp.status_code == 200
        assert b"No audit log entries" in resp.content

    def test_pagination_page_param(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/admin/audit?page=2", cookies=_auth(admin_user))
        assert resp.status_code == 200


class TestFlashOnRedirect:
    """Regression: flash must be set on the RETURNED response, not an injected
    Response param (which FastAPI discards when a Response is returned)."""

    def test_redirect_action_sets_flash_cookie(self, client: TestClient, admin_user: User) -> None:
        # The not-found delete path returns a redirect with an error flash.
        resp = client.post("/admin/scripts/no_such/delete", cookies=_auth(admin_user))
        assert resp.status_code == 303
        set_cookie = resp.headers.get("set-cookie", "")
        assert "_flash" in set_cookie
        assert "error|" in set_cookie
        assert "not found" in set_cookie

    def test_get_page_clears_flash_cookie(self, client: TestClient, admin_user: User) -> None:
        # A page that displays a flash must also clear it (one-shot).
        resp = client.get(
            "/admin/scripts",
            cookies={**_auth(admin_user), "_flash": "success|done"},
        )
        assert resp.status_code == 200
        set_cookie = resp.headers.get("set-cookie", "").lower()
        assert "_flash" in set_cookie
        assert "max-age=0" in set_cookie
