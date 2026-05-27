"""Unit tests for web auth routes: GET/POST /login, POST /logout."""

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_station_lite.server.auth.password import hash_password
from control_station_lite.server.db.models import Base, User
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
async def user(db_session: AsyncSession) -> User:
    u = User(
        username="alice",
        password_hash=hash_password("secret"),
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
        password_hash=hash_password("adminpass"),
        role="admin",
        disabled=False,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest.fixture
def client(db_session: AsyncSession, user: User) -> TestClient:
    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


# ---------------------------------------------------------------------------
# GET /login
# ---------------------------------------------------------------------------


def test_login_page_returns_200(client: TestClient) -> None:
    resp = client.get("/login")
    assert resp.status_code == 200


def test_login_page_contains_form(client: TestClient) -> None:
    resp = client.get("/login")
    assert b'action="/login"' in resp.content
    assert b'name="username"' in resp.content
    assert b'name="password"' in resp.content


def test_login_page_redirects_if_already_authenticated(client: TestClient, user: User) -> None:
    from control_station_lite.server.auth.jwt import create_access_token

    token = create_access_token(user.id, user.role)
    resp = client.get("/login", cookies={"csl_access": token})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


# ---------------------------------------------------------------------------
# POST /login — success
# ---------------------------------------------------------------------------


def test_login_success_redirects_to_dashboard(client: TestClient, user: User) -> None:
    resp = client.post("/login", data={"username": "alice", "password": "secret"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


def test_login_success_sets_access_cookie(client: TestClient, user: User) -> None:
    resp = client.post("/login", data={"username": "alice", "password": "secret"})
    assert "csl_access" in resp.cookies


def test_login_success_sets_refresh_cookie(client: TestClient, user: User) -> None:
    resp = client.post("/login", data={"username": "alice", "password": "secret"})
    assert "refresh_token" in resp.cookies


# ---------------------------------------------------------------------------
# POST /login — failure
# ---------------------------------------------------------------------------


def test_login_wrong_password_returns_401(client: TestClient, user: User) -> None:
    resp = client.post("/login", data={"username": "alice", "password": "wrong"})
    assert resp.status_code == 401


def test_login_wrong_password_shows_error(client: TestClient, user: User) -> None:
    resp = client.post("/login", data={"username": "alice", "password": "wrong"})
    assert b"Invalid username or password" in resp.content


def test_login_unknown_user_returns_401(client: TestClient) -> None:
    resp = client.post("/login", data={"username": "nobody", "password": "x"})
    assert resp.status_code == 401


def test_login_disabled_user_returns_401(client: TestClient, db_session: AsyncSession) -> None:
    import asyncio

    from sqlalchemy import select

    async def _disable() -> None:
        result = await db_session.execute(select(User).where(User.username == "alice"))
        u = result.scalar_one()
        u.disabled = True
        await db_session.commit()

    asyncio.get_event_loop().run_until_complete(_disable())
    resp = client.post("/login", data={"username": "alice", "password": "secret"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /logout
# ---------------------------------------------------------------------------


def test_logout_redirects_to_login(client: TestClient, user: User) -> None:
    # Log in first to get cookies
    login = client.post("/login", data={"username": "alice", "password": "secret"})
    resp = client.post("/logout", cookies=login.cookies)
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]


def test_logout_clears_access_cookie(client: TestClient, user: User) -> None:
    login = client.post("/login", data={"username": "alice", "password": "secret"})
    resp = client.post("/logout", cookies=login.cookies)
    # Cookie should be deleted (set with max-age=0 or empty)
    assert resp.cookies.get("csl_access", "") == ""


# ---------------------------------------------------------------------------
# Unauthenticated access to protected routes
# ---------------------------------------------------------------------------


def test_dashboard_unauthenticated_redirects_to_login(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]
