"""Integration tests for Phase 4 — machine management API.

SSH and network calls are mocked; real crypto and in-memory SQLite are used.
"""

import base64
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_station_lite.server.api.machines import _host_key_fingerprint, _ssh_connection_test
from control_station_lite.server.auth.jwt import create_access_token
from control_station_lite.server.auth.password import hash_password
from control_station_lite.server.core.crypto import encrypt
from control_station_lite.server.db.models import Base, Machine, User
from control_station_lite.server.db.session import get_session
from control_station_lite.server.main import app
from control_station_lite.shared.registration import RegistrationBundle, encode_bundle

_BUNDLE_KWARGS = {
    "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n",
    "key_fingerprint": "SHA256:abc123",
    "agent_port": 47731,
    "scripts_dir": "/home/user/.csl/scripts",
    "hostname_hint": "my-pc",
    "platform": "linux",
    "ssh_user": "alice",
    "api_token": "agent-token-xyz",
}


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
async def machine(db_session: AsyncSession) -> Machine:
    """A pre-stored machine that bypasses the registration connection test."""
    from control_station_lite.server.config import get_settings

    master_key = get_settings().read_master_key()
    key_enc = encrypt(_BUNDLE_KWARGS["private_key"].encode(), master_key)
    m = Machine(
        name="test-machine",
        ssh_host="192.168.1.100",
        ssh_port=22,
        ssh_user="testuser",
        ssh_key_encrypted=key_enc,
        key_fingerprint=_BUNDLE_KWARGS["key_fingerprint"],
        agent_port=_BUNDLE_KWARGS["agent_port"],
        scripts_dir=_BUNDLE_KWARGS["scripts_dir"],
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


# A valid OpenSSH ed25519 host-key line, as _ssh_connection_test returns on success.
_SAMPLE_HOST_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMBCOclB5ZfmeQZkZ3042IRzIRL+4bLlhcHNhsVq5Qkr"
)


def _no_op_connection_test() -> MagicMock:
    """Patch _ssh_connection_test to succeed and return a captured host key."""
    return patch(
        "control_station_lite.server.api.machines._ssh_connection_test",
        new_callable=AsyncMock,
        return_value=_SAMPLE_HOST_KEY,
    )


# ---------------------------------------------------------------------------
# Host-key capture in _ssh_connection_test (TOFU)
# ---------------------------------------------------------------------------


class TestSshConnectionTestCapturesHostKey:
    async def test_returns_host_key_line(self) -> None:
        bundle = RegistrationBundle.decode(encode_bundle(**_BUNDLE_KWARGS))

        host_key_obj = MagicMock()
        host_key_obj.export_public_key.return_value = (_SAMPLE_HOST_KEY + "\n").encode()
        run_result = MagicMock()
        run_result.stdout = "identity:\n  key_fingerprint: SHA256:abc123\n"

        conn = MagicMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        conn.get_server_host_key = MagicMock(return_value=host_key_obj)
        conn.run = AsyncMock(return_value=run_result)

        with (
            patch("control_station_lite.server.api.machines.asyncssh.connect", return_value=conn),
            patch(
                "control_station_lite.server.api.machines.asyncssh.import_private_key",
                return_value=MagicMock(),
            ),
        ):
            host_key = await _ssh_connection_test(bundle, "alice", "192.168.1.100", 22)

        assert host_key == _SAMPLE_HOST_KEY

    async def test_raises_when_host_key_unavailable(self) -> None:
        bundle = RegistrationBundle.decode(encode_bundle(**_BUNDLE_KWARGS))
        conn = MagicMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        conn.get_server_host_key = MagicMock(return_value=None)

        with (
            patch("control_station_lite.server.api.machines.asyncssh.connect", return_value=conn),
            patch(
                "control_station_lite.server.api.machines.asyncssh.import_private_key",
                return_value=MagicMock(),
            ),
            pytest.raises(ValueError, match="host key"),
        ):
            await _ssh_connection_test(bundle, "alice", "192.168.1.100", 22)


# ---------------------------------------------------------------------------
# 4.1 — Registration bundle (encode/decode covered in test_registration.py)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 4.2 POST /api/machines
# ---------------------------------------------------------------------------


class TestRegisterMachine:
    def test_invalid_bundle_returns_400(self, client: TestClient, admin_user: User) -> None:
        resp = client.post(
            "/api/machines",
            json={"bundle": "!!!not-base64!!!", "name": "x", "ssh_host": "h"},
            headers=_admin_h(admin_user),
        )
        assert resp.status_code == 400

    def test_connection_test_failure_returns_422(
        self, client: TestClient, admin_user: User
    ) -> None:
        bundle = encode_bundle(**_BUNDLE_KWARGS)
        with patch(
            "control_station_lite.server.api.machines._ssh_connection_test",
            new_callable=AsyncMock,
            side_effect=OSError("Connection refused"),
        ):
            resp = client.post(
                "/api/machines",
                json={"bundle": bundle, "name": "my-machine", "ssh_host": "192.168.1.100"},
                headers=_admin_h(admin_user),
            )
        assert resp.status_code == 422

    def test_fingerprint_mismatch_returns_422(self, client: TestClient, admin_user: User) -> None:
        bundle = encode_bundle(**_BUNDLE_KWARGS)
        with patch(
            "control_station_lite.server.api.machines._ssh_connection_test",
            new_callable=AsyncMock,
            side_effect=ValueError("key fingerprint mismatch"),
        ):
            resp = client.post(
                "/api/machines",
                json={"bundle": bundle, "name": "my-machine", "ssh_host": "192.168.1.100"},
                headers=_admin_h(admin_user),
            )
        assert resp.status_code == 422

    def test_ssh_user_defaults_to_bundle_value(self, client: TestClient, admin_user: User) -> None:
        bundle = encode_bundle(**_BUNDLE_KWARGS)
        with _no_op_connection_test():
            resp = client.post(
                "/api/machines",
                json={"bundle": bundle, "name": "my-machine", "ssh_host": "192.168.1.100"},
                headers=_admin_h(admin_user),
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "my-machine"
        assert data["platform"] == "linux"
        assert data["key_fingerprint"] == _BUNDLE_KWARGS["key_fingerprint"]
        assert data["agent_port"] == _BUNDLE_KWARGS["agent_port"]
        assert data["ssh_user"] == _BUNDLE_KWARGS["ssh_user"]  # "alice" from bundle

    def test_response_includes_host_key_fingerprint(
        self, client: TestClient, admin_user: User
    ) -> None:
        bundle = encode_bundle(**_BUNDLE_KWARGS)
        with _no_op_connection_test():
            resp = client.post(
                "/api/machines",
                json={"bundle": bundle, "name": "pinned", "ssh_host": "192.168.1.100"},
                headers=_admin_h(admin_user),
            )
        assert resp.status_code == 201
        assert resp.json()["ssh_host_key_fingerprint"] == _host_key_fingerprint(_SAMPLE_HOST_KEY)
        # The raw key line is captured but not serialised in the response.
        assert "ssh_host_key" not in resp.json()

    def test_ssh_user_override_takes_precedence(self, client: TestClient, admin_user: User) -> None:
        bundle = encode_bundle(**_BUNDLE_KWARGS)
        with _no_op_connection_test():
            resp = client.post(
                "/api/machines",
                json={
                    "bundle": bundle,
                    "name": "my-machine",
                    "ssh_host": "192.168.1.100",
                    "ssh_user": "root",
                },
                headers=_admin_h(admin_user),
            )
        assert resp.status_code == 201
        assert resp.json()["ssh_user"] == "root"

    def test_duplicate_name_returns_409(
        self, client: TestClient, admin_user: User, machine: Machine
    ) -> None:
        bundle = encode_bundle(**_BUNDLE_KWARGS)
        with _no_op_connection_test():
            resp = client.post(
                "/api/machines",
                json={"bundle": bundle, "name": "test-machine", "ssh_host": "192.168.1.101"},
                headers=_admin_h(admin_user),
            )
        assert resp.status_code == 409

    def test_non_admin_returns_403(self, client: TestClient, regular_user: User) -> None:
        resp = client.post(
            "/api/machines",
            json={"bundle": "x", "name": "x", "ssh_host": "h"},
            headers=_user_h(regular_user),
        )
        assert resp.status_code == 403

    def test_optional_mac_address_stored(self, client: TestClient, admin_user: User) -> None:
        bundle = encode_bundle(**_BUNDLE_KWARGS)
        with _no_op_connection_test():
            resp = client.post(
                "/api/machines",
                json={
                    "bundle": bundle,
                    "name": "mac-machine",
                    "ssh_host": "192.168.1.100",
                    "mac_address": "AA:BB:CC:DD:EE:FF",
                },
                headers=_admin_h(admin_user),
            )
        assert resp.status_code == 201
        assert resp.json()["mac_address"] == "AA:BB:CC:DD:EE:FF"


# ---------------------------------------------------------------------------
# 4.2 GET /api/machines and GET /api/machines/{id}
# ---------------------------------------------------------------------------


class TestListAndGetMachine:
    def test_admin_sees_all_machines(
        self, client: TestClient, admin_user: User, machine: Machine
    ) -> None:
        resp = client.get("/api/machines", headers=_admin_h(admin_user))
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["id"] == machine.id

    def test_user_sees_only_bookmarked(
        self,
        client: TestClient,
        regular_user: User,
        admin_user: User,
        machine: Machine,
        db_session: AsyncSession,
    ) -> None:
        # Initially no bookmarks — user sees empty list
        resp = client.get("/api/machines", headers=_user_h(regular_user))
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_machine_admin_no_bookmark_needed(
        self, client: TestClient, admin_user: User, machine: Machine
    ) -> None:
        resp = client.get(f"/api/machines/{machine.id}", headers=_admin_h(admin_user))
        assert resp.status_code == 200
        assert resp.json()["name"] == "test-machine"

    def test_get_machine_user_without_bookmark_returns_403(
        self, client: TestClient, regular_user: User, machine: Machine
    ) -> None:
        resp = client.get(f"/api/machines/{machine.id}", headers=_user_h(regular_user))
        assert resp.status_code == 403

    def test_get_machine_not_found_returns_404(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/api/machines/9999", headers=_admin_h(admin_user))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 4.2 DELETE /api/machines/{id}
# ---------------------------------------------------------------------------


class TestDeleteMachine:
    def test_delete_returns_204(
        self, client: TestClient, admin_user: User, machine: Machine
    ) -> None:
        resp = client.delete(f"/api/machines/{machine.id}", headers=_admin_h(admin_user))
        assert resp.status_code == 204

    def test_delete_missing_returns_404(self, client: TestClient, admin_user: User) -> None:
        resp = client.delete("/api/machines/9999", headers=_admin_h(admin_user))
        assert resp.status_code == 404

    def test_non_admin_delete_returns_403(
        self, client: TestClient, regular_user: User, machine: Machine
    ) -> None:
        resp = client.delete(f"/api/machines/{machine.id}", headers=_user_h(regular_user))
        assert resp.status_code == 403

    def test_deleted_machine_no_longer_in_list(
        self, client: TestClient, admin_user: User, machine: Machine
    ) -> None:
        client.delete(f"/api/machines/{machine.id}", headers=_admin_h(admin_user))
        resp = client.get("/api/machines", headers=_admin_h(admin_user))
        assert resp.json() == []


# ---------------------------------------------------------------------------
# 4.2 Bookmark endpoints
# ---------------------------------------------------------------------------


class TestBookmarks:
    def test_add_bookmark_grants_access(
        self,
        client: TestClient,
        regular_user: User,
        admin_user: User,
        machine: Machine,
    ) -> None:
        # Before bookmark — 403
        assert (
            client.get(f"/api/machines/{machine.id}", headers=_user_h(regular_user)).status_code
            == 403
        )
        # Add bookmark
        resp = client.post(f"/api/machines/{machine.id}/bookmark", headers=_user_h(regular_user))
        assert resp.status_code == 204
        # After bookmark — 200
        assert (
            client.get(f"/api/machines/{machine.id}", headers=_user_h(regular_user)).status_code
            == 200
        )

    def test_add_bookmark_idempotent(
        self, client: TestClient, regular_user: User, machine: Machine
    ) -> None:
        client.post(f"/api/machines/{machine.id}/bookmark", headers=_user_h(regular_user))
        resp = client.post(f"/api/machines/{machine.id}/bookmark", headers=_user_h(regular_user))
        assert resp.status_code == 204

    def test_remove_bookmark_revokes_access(
        self,
        client: TestClient,
        regular_user: User,
        machine: Machine,
    ) -> None:
        client.post(f"/api/machines/{machine.id}/bookmark", headers=_user_h(regular_user))
        client.delete(f"/api/machines/{machine.id}/bookmark", headers=_user_h(regular_user))
        assert (
            client.get(f"/api/machines/{machine.id}", headers=_user_h(regular_user)).status_code
            == 403
        )

    def test_bookmark_missing_machine_returns_404(
        self, client: TestClient, regular_user: User
    ) -> None:
        resp = client.post("/api/machines/9999/bookmark", headers=_user_h(regular_user))
        assert resp.status_code == 404

    def test_bookmarked_machine_appears_in_user_list(
        self,
        client: TestClient,
        regular_user: User,
        machine: Machine,
    ) -> None:
        client.post(f"/api/machines/{machine.id}/bookmark", headers=_user_h(regular_user))
        resp = client.get("/api/machines", headers=_user_h(regular_user))
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["id"] == machine.id


# ---------------------------------------------------------------------------
# 4.3 GET /api/machines/{id}/ping
# ---------------------------------------------------------------------------


def _mock_asyncssh_connect_success() -> MagicMock:
    mock_conn = MagicMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "control_station_lite.server.api.machines.asyncssh.connect",
        return_value=mock_conn,
    )


class TestPingMachine:
    def test_reachable_returns_true_with_latency(
        self, client: TestClient, admin_user: User, machine: Machine
    ) -> None:
        with (
            _mock_asyncssh_connect_success(),
            patch(
                "control_station_lite.server.api.machines.asyncssh.import_private_key",
                return_value=MagicMock(),
            ),
        ):
            resp = client.get(f"/api/machines/{machine.id}/ping", headers=_admin_h(admin_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data["reachable"] is True
        assert data["latency_ms"] is not None

    def test_unreachable_returns_false(
        self, client: TestClient, admin_user: User, machine: Machine
    ) -> None:
        with (
            patch(
                "control_station_lite.server.api.machines.asyncssh.connect",
                side_effect=OSError("refused"),
            ),
            patch(
                "control_station_lite.server.api.machines.asyncssh.import_private_key",
                return_value=MagicMock(),
            ),
        ):
            resp = client.get(f"/api/machines/{machine.id}/ping", headers=_admin_h(admin_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data["reachable"] is False
        assert data["latency_ms"] is None

    def test_not_found_returns_404(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/api/machines/9999/ping", headers=_admin_h(admin_user))
        assert resp.status_code == 404

    def test_user_without_bookmark_returns_403(
        self, client: TestClient, regular_user: User, machine: Machine
    ) -> None:
        resp = client.get(f"/api/machines/{machine.id}/ping", headers=_user_h(regular_user))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 4.4 GET /api/machines/{id}/agent-status
# ---------------------------------------------------------------------------


def _mock_pool_and_http(agent_health: dict | None) -> tuple[MagicMock, MagicMock]:
    mock_listener = MagicMock()
    mock_listener.close = MagicMock()
    mock_listener.wait_closed = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.open_tunnel = AsyncMock(return_value=(mock_listener, 12345))

    mock_resp = MagicMock()
    mock_resp.status_code = 200 if agent_health else 503
    if agent_health:
        mock_resp.json.return_value = agent_health

    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=False)
    mock_http.get = AsyncMock(return_value=mock_resp)

    return mock_pool, mock_http


class TestAgentStatus:
    def test_running_agent_returns_health(
        self, client: TestClient, admin_user: User, machine: Machine
    ) -> None:
        health_payload = {"version": "0.1.0", "running_persistent_jobs": 0, "idle_seconds": 5.0}
        mock_pool, mock_http = _mock_pool_and_http(health_payload)
        with (
            patch("control_station_lite.server.api.machines.get_ssh_pool", return_value=mock_pool),
            patch(
                "control_station_lite.server.api.machines.httpx.AsyncClient", return_value=mock_http
            ),
        ):
            resp = client.get(
                f"/api/machines/{machine.id}/agent-status", headers=_admin_h(admin_user)
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True
        assert data["health"]["version"] == "0.1.0"

    def test_agent_down_returns_not_running(
        self, client: TestClient, admin_user: User, machine: Machine
    ) -> None:
        mock_pool, mock_http = _mock_pool_and_http(None)
        with (
            patch("control_station_lite.server.api.machines.get_ssh_pool", return_value=mock_pool),
            patch(
                "control_station_lite.server.api.machines.httpx.AsyncClient", return_value=mock_http
            ),
        ):
            resp = client.get(
                f"/api/machines/{machine.id}/agent-status", headers=_admin_h(admin_user)
            )
        assert resp.status_code == 200
        assert resp.json()["running"] is False

    def test_ssh_failure_returns_not_running(
        self, client: TestClient, admin_user: User, machine: Machine
    ) -> None:
        mock_pool = MagicMock()
        mock_pool.open_tunnel = AsyncMock(side_effect=OSError("no route"))
        with patch("control_station_lite.server.api.machines.get_ssh_pool", return_value=mock_pool):
            resp = client.get(
                f"/api/machines/{machine.id}/agent-status", headers=_admin_h(admin_user)
            )
        assert resp.status_code == 200
        assert resp.json()["running"] is False

    def test_not_found_returns_404(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/api/machines/9999/agent-status", headers=_admin_h(admin_user))
        assert resp.status_code == 404

    def test_user_without_bookmark_returns_403(
        self, client: TestClient, regular_user: User, machine: Machine
    ) -> None:
        resp = client.get(f"/api/machines/{machine.id}/agent-status", headers=_user_h(regular_user))
        assert resp.status_code == 403
