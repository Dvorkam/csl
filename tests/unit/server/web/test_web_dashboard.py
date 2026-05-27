"""Unit tests for the dashboard and ping-badge web routes."""

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_station_lite.server.auth.jwt import create_access_token
from control_station_lite.server.auth.password import hash_password
from control_station_lite.server.db.models import Base, Machine, User, UserMachine
from control_station_lite.server.db.session import get_session
from control_station_lite.server.main import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _jwt_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = tmp_path / "jwt.key"
    key_file.write_bytes(os.urandom(64))
    master_file = tmp_path / "master.key"
    import base64

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
async def regular_user(db_session: AsyncSession) -> User:
    u = User(
        username="bob",
        password_hash=hash_password("pw"),
        role="user",
        disabled=False,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    u = User(
        username="admin",
        password_hash=hash_password("pw"),
        role="admin",
        disabled=False,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest.fixture
async def machine(db_session: AsyncSession) -> Machine:
    from datetime import datetime

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
    await db_session.commit()
    await db_session.refresh(m)
    return m


@pytest.fixture
def client(db_session: AsyncSession) -> TestClient:
    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


def _auth_cookies(user: User) -> dict[str, str]:
    return {"csl_access": create_access_token(user.id, user.role)}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_returns_200_for_authenticated_user(
    client: TestClient, regular_user: User
) -> None:
    resp = client.get("/", cookies=_auth_cookies(regular_user))
    assert resp.status_code == 200


def test_dashboard_contains_machine_name_for_admin(
    client: TestClient, admin_user: User, machine: Machine
) -> None:
    resp = client.get("/", cookies=_auth_cookies(admin_user))
    assert b"testbox" in resp.content


def test_dashboard_shows_empty_state_when_no_machines(
    client: TestClient, regular_user: User
) -> None:
    resp = client.get("/", cookies=_auth_cookies(regular_user))
    assert b"No machines available" in resp.content


def test_dashboard_only_shows_bookmarked_machines_for_regular_user(
    client: TestClient,
    regular_user: User,
    admin_user: User,
    machine: Machine,
    db_session: AsyncSession,
) -> None:
    import asyncio

    # machine is not bookmarked by regular_user — should not appear
    resp = client.get("/", cookies=_auth_cookies(regular_user))
    assert b"testbox" not in resp.content

    # bookmark it
    async def _bookmark() -> None:
        db_session.add(UserMachine(user_id=regular_user.id, machine_id=machine.id))
        await db_session.commit()

    asyncio.get_event_loop().run_until_complete(_bookmark())
    resp = client.get("/", cookies=_auth_cookies(regular_user))
    assert b"testbox" in resp.content


def test_dashboard_shows_admin_links_for_admin(client: TestClient, admin_user: User) -> None:
    resp = client.get("/", cookies=_auth_cookies(admin_user))
    assert b"/admin/scripts" in resp.content


def test_dashboard_does_not_show_admin_links_for_regular_user(
    client: TestClient, regular_user: User
) -> None:
    resp = client.get("/", cookies=_auth_cookies(regular_user))
    assert b"/admin/scripts" not in resp.content


# ---------------------------------------------------------------------------
# Ping badge (mocked SSH)
# ---------------------------------------------------------------------------


def test_ping_badge_returns_200_for_authorized_user(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncssh

    import control_station_lite.server.web.dashboard as dash_mod

    # Bypass crypto — return a dummy key bytes
    monkeypatch.setattr(dash_mod, "decrypt", lambda *_: b"fake-key")

    class _FakeConn:
        async def __aenter__(self) -> "_FakeConn":
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

    monkeypatch.setattr(asyncssh, "connect", lambda *a, **kw: _FakeConn())
    monkeypatch.setattr(asyncssh, "import_private_key", lambda _: None)
    resp = client.get(f"/machines/{machine.id}/ping-badge", cookies=_auth_cookies(admin_user))
    assert resp.status_code == 200


def test_ping_badge_shows_offline_on_ssh_failure(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncssh

    import control_station_lite.server.web.dashboard as dash_mod

    monkeypatch.setattr(dash_mod, "decrypt", lambda *_: b"fake-key")
    monkeypatch.setattr(asyncssh, "import_private_key", lambda _: None)

    class _FailConn:
        async def __aenter__(self) -> "_FailConn":
            raise OSError("refused")

        async def __aexit__(self, *_: object) -> None:
            pass

    monkeypatch.setattr(asyncssh, "connect", lambda *a, **kw: _FailConn())
    resp = client.get(f"/machines/{machine.id}/ping-badge", cookies=_auth_cookies(admin_user))
    assert resp.status_code == 200
    assert b"Offline" in resp.content


def test_ping_badge_404_machine_still_returns_offline_badge(
    client: TestClient, admin_user: User
) -> None:
    resp = client.get("/machines/9999/ping-badge", cookies=_auth_cookies(admin_user))
    assert resp.status_code == 200
    assert b"Offline" in resp.content
