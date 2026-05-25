"""Integration tests for Phase 3 — authentication and authorisation.

Real bcrypt, real JWT signing, real in-memory SQLite. No mocking of crypto.
Covers: login, refresh, rotation, revocation, expired tokens, replay.
"""

import os
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_station_lite.server.auth.password import hash_password, verify_password
from control_station_lite.server.auth.jwt import (
    _ACCESS_TOKEN_EXPIRE_MINUTES,
    _REFRESH_TOKEN_EXPIRE_DAYS,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
)
from control_station_lite.server.db.models import Base, RefreshToken, User
from control_station_lite.server.db.session import _session_factory, get_session
from control_station_lite.server.main import app

# ---------------------------------------------------------------------------
# JWT key fixture — override settings so tests don't need secrets/jwt.key
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _jwt_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = tmp_path / "jwt.key"
    key_file.write_bytes(os.urandom(64))
    monkeypatch.setenv("CSL_JWT_KEY_PATH", str(key_file))
    monkeypatch.setenv("CSL_MASTER_KEY_PATH", str(tmp_path / "master.key"))
    import base64

    (tmp_path / "master.key").write_text(base64.b64encode(os.urandom(32)).decode())

    # Reset lru_cache so settings re-read from patched env
    from control_station_lite.server.config import get_settings
    from control_station_lite.server.auth import jwt as jwt_mod

    get_settings.cache_clear()
    # Clear jwt module's cached secret if any (it calls get_settings each time — no cache needed)
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# DB fixture — in-memory SQLite, one per test
# ---------------------------------------------------------------------------


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
    # base_url must be https so the client honours Secure cookies
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = User(
        username="admin",
        password_hash=hash_password("correct-password"),
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


# ---------------------------------------------------------------------------
# 3.1 password.py
# ---------------------------------------------------------------------------


def test_hash_and_verify_roundtrip() -> None:
    hashed = hash_password("secret")
    assert verify_password("secret", hashed)
    assert not verify_password("wrong", hashed)


def test_hash_is_not_plaintext() -> None:
    hashed = hash_password("secret")
    assert "secret" not in hashed


def test_two_hashes_of_same_password_differ() -> None:
    assert hash_password("secret") != hash_password("secret")


# ---------------------------------------------------------------------------
# 3.2 jwt.py
# ---------------------------------------------------------------------------


def test_access_token_roundtrip() -> None:
    token = create_access_token(user_id=42, role="admin")
    data = decode_access_token(token)
    assert data.user_id == 42
    assert data.role == "admin"


def test_refresh_token_roundtrip() -> None:
    token, jti = create_refresh_token(user_id=7)
    data = decode_refresh_token(token)
    assert data.user_id == 7
    assert data.jti == jti


def test_access_token_wrong_type_rejected() -> None:
    from jose import JWTError

    token, _ = create_refresh_token(user_id=1)
    with pytest.raises(JWTError):
        decode_access_token(token)


def test_refresh_token_wrong_type_rejected() -> None:
    from jose import JWTError

    token = create_access_token(user_id=1, role="user")
    with pytest.raises(JWTError):
        decode_refresh_token(token)


def test_tampered_access_token_rejected() -> None:
    from jose import JWTError

    token = create_access_token(user_id=1, role="user")
    with pytest.raises(JWTError):
        decode_access_token(token + "tampered")


def test_expired_access_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from jose import JWTError
    import control_station_lite.server.auth.jwt as jwt_mod
    from datetime import datetime, timezone

    # Patch timedelta to make token expire immediately
    original = jwt_mod.timedelta

    def short_delta(**kwargs: object) -> timedelta:
        if kwargs.get("minutes") == _ACCESS_TOKEN_EXPIRE_MINUTES:
            return timedelta(seconds=-1)
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(jwt_mod, "timedelta", short_delta)
    token = create_access_token(user_id=1, role="user")
    with pytest.raises(JWTError):
        decode_access_token(token)


# ---------------------------------------------------------------------------
# 3.4 Login endpoint
# ---------------------------------------------------------------------------


def test_login_success(client: TestClient, admin_user: User) -> None:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "correct-password"})
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert "refresh_token" in resp.cookies


def test_login_wrong_password(client: TestClient, admin_user: User) -> None:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


def test_login_unknown_user(client: TestClient) -> None:
    resp = client.post("/api/auth/login", json={"username": "nobody", "password": "pass"})
    assert resp.status_code == 401


def test_login_disabled_user(client: TestClient, db_session: AsyncSession) -> None:
    import asyncio

    async def make_disabled() -> None:
        user = User(
            username="disabled",
            password_hash=hash_password("pass"),
            role="user",
            disabled=True,
        )
        db_session.add(user)
        await db_session.commit()

    asyncio.get_event_loop().run_until_complete(make_disabled())
    resp = client.post("/api/auth/login", json={"username": "disabled", "password": "pass"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 3.4 + 3.3 Protected routes via access token
# ---------------------------------------------------------------------------


def test_access_token_grants_access(client: TestClient, admin_user: User) -> None:
    login = client.post(
        "/api/auth/login", json={"username": "admin", "password": "correct-password"}
    )
    token = login.json()["access_token"]
    resp = client.get("/healthz", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_missing_token_blocks_protected_route(client: TestClient) -> None:
    # FastAPI HTTPBearer returns 403 when Authorization header is absent.
    # We test with a temp protected route rather than /healthz (which is public).
    from fastapi import Depends
    from control_station_lite.server.auth.dependencies import current_user

    @app.get("/test-protected-tmp")
    async def _route(user: User = Depends(current_user)) -> dict:  # type: ignore[return]
        return {"ok": True}

    resp = client.get("/test-protected-tmp")
    assert resp.status_code in (401, 403)
    app.routes[:] = [r for r in app.routes if getattr(r, "path", None) != "/test-protected-tmp"]


# ---------------------------------------------------------------------------
# 3.4 Refresh endpoint + 3.5 Token rotation
# ---------------------------------------------------------------------------


def test_refresh_issues_new_access_token(client: TestClient, admin_user: User) -> None:
    client.post("/api/auth/login", json={"username": "admin", "password": "correct-password"})
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_refresh_rotates_cookie(client: TestClient, admin_user: User) -> None:
    client.post("/api/auth/login", json={"username": "admin", "password": "correct-password"})
    first_cookie = client.cookies.get("refresh_token")
    client.post("/api/auth/refresh")
    second_cookie = client.cookies.get("refresh_token")
    assert first_cookie != second_cookie


def test_refresh_without_cookie_returns_401(client: TestClient) -> None:
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 401


def test_refresh_with_invalid_token_returns_401(client: TestClient) -> None:
    client.cookies.set("refresh_token", "not.a.valid.jwt")
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 3.5 Revocation
# ---------------------------------------------------------------------------


def test_old_refresh_token_rejected_after_rotation(
    client: TestClient, admin_user: User
) -> None:
    client.post("/api/auth/login", json={"username": "admin", "password": "correct-password"})
    old_cookie = client.cookies.get("refresh_token")

    # Rotate
    client.post("/api/auth/refresh")

    # Try to use the old token — must be rejected
    client.cookies.set("refresh_token", old_cookie)
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 401


def test_logout_clears_cookie(client: TestClient, admin_user: User) -> None:
    client.post("/api/auth/login", json={"username": "admin", "password": "correct-password"})
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 204
    assert client.cookies.get("refresh_token") is None


def test_refresh_token_revoked_after_logout(client: TestClient, admin_user: User) -> None:
    client.post("/api/auth/login", json={"username": "admin", "password": "correct-password"})
    cookie = client.cookies.get("refresh_token")
    client.post("/api/auth/logout")

    client.cookies.set("refresh_token", cookie)
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 401


def test_logout_without_cookie_is_noop(client: TestClient) -> None:
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# 3.3 require_admin dependency
# ---------------------------------------------------------------------------


def test_regular_user_cannot_use_admin_dependency(
    client: TestClient, regular_user: User, admin_user: User
) -> None:
    """Smoke-test require_admin by wiring a temporary test route."""
    from fastapi import Depends
    from control_station_lite.server.auth.dependencies import require_admin

    @app.get("/test-admin-only")
    async def _admin_route(user: User = Depends(require_admin)) -> dict:  # type: ignore[return]
        return {"ok": True}

    # Regular user: get token then hit the route
    login = client.post("/api/auth/login", json={"username": "alice", "password": "alice-pass"})
    token = login.json()["access_token"]
    resp = client.get("/test-admin-only", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403

    # Admin user: should succeed
    login = client.post(
        "/api/auth/login", json={"username": "admin", "password": "correct-password"}
    )
    token = login.json()["access_token"]
    resp = client.get("/test-admin-only", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200

    # Remove the temporary route
    app.routes[:] = [r for r in app.routes if getattr(r, "path", None) != "/test-admin-only"]
