"""Integration tests for Phase 5 — script library API and script_sync.

5.2 — REST endpoints (via TestClient, real in-memory SQLite)
5.3 — script_sync logic (AgentClient mocked)
5.4 — state mapping scenarios
"""

import base64
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_station_lite.server.auth.jwt import create_access_token
from control_station_lite.server.auth.password import hash_password
from control_station_lite.server.core.script_registry import create_script
from control_station_lite.server.core.script_sync import APPROVED_STALE, sync_script
from control_station_lite.server.db.models import Base, Machine, Script, ScriptTargetState, User
from control_station_lite.server.db.session import get_session
from control_station_lite.server.main import app
from control_station_lite.shared.models import ApprovalState, ScriptDescriptor, StageScriptResponse

_CONTENT = "#!/bin/bash\necho hello\n"
_META = "description: greet\npersistent: false\n"


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
    async with factory() as s:
        yield s
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
        password_hash=hash_password("pass"),
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
        password_hash=hash_password("pass"),
        role="user",
        disabled=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def script(db_session: AsyncSession, admin_user: User) -> Script:
    s = await create_script(
        name="hello", content=_CONTENT, meta_yaml=_META, user_id=admin_user.id, session=db_session
    )
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest.fixture
async def machine(db_session: AsyncSession) -> Machine:
    from control_station_lite.server.config import get_settings
    from control_station_lite.server.core.crypto import encrypt

    master_key = get_settings().read_master_key()
    m = Machine(
        name="test-box",
        ssh_host="192.168.1.1",
        ssh_port=22,
        ssh_user="alice",
        ssh_key_encrypted=encrypt(b"fake-key", master_key),
        key_fingerprint="SHA256:fake",
        agent_port=47731,
        scripts_dir="/home/alice/.csl/scripts",
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


# ---------------------------------------------------------------------------
# 5.2 — GET /api/scripts
# ---------------------------------------------------------------------------


class TestListScriptsEndpoint:
    def test_returns_empty_list(self, client: TestClient, regular_user: User) -> None:
        resp = client.get("/api/scripts", headers=_user_h(regular_user))
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_created_scripts(
        self, client: TestClient, regular_user: User, script: Script
    ) -> None:
        resp = client.get("/api/scripts", headers=_user_h(regular_user))
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["name"] == "hello"

    def test_unauthenticated_returns_401(self, client: TestClient) -> None:
        resp = client.get("/api/scripts")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 5.2 — GET /api/scripts/{name}
# ---------------------------------------------------------------------------


class TestGetScriptEndpoint:
    def test_returns_script(self, client: TestClient, regular_user: User, script: Script) -> None:
        resp = client.get("/api/scripts/hello", headers=_user_h(regular_user))
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "hello"
        assert data["content"] == _CONTENT
        assert len(data["md5"]) == 32

    def test_missing_returns_404(self, client: TestClient, regular_user: User) -> None:
        resp = client.get("/api/scripts/nope", headers=_user_h(regular_user))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 5.2 — POST /api/scripts
# ---------------------------------------------------------------------------


class TestCreateScriptEndpoint:
    def test_admin_creates_script(self, client: TestClient, admin_user: User) -> None:
        resp = client.post(
            "/api/scripts",
            json={"name": "greet", "content": _CONTENT, "meta_yaml": _META},
            headers=_admin_h(admin_user),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "greet"
        assert data["persistent"] is False

    def test_persistent_flag_from_meta(self, client: TestClient, admin_user: User) -> None:
        resp = client.post(
            "/api/scripts",
            json={
                "name": "daemon",
                "content": "x",
                "meta_yaml": "description: d\npersistent: true\n",
            },
            headers=_admin_h(admin_user),
        )
        assert resp.status_code == 201
        assert resp.json()["persistent"] is True

    def test_duplicate_name_returns_409(
        self, client: TestClient, admin_user: User, script: Script
    ) -> None:
        resp = client.post(
            "/api/scripts",
            json={"name": "hello", "content": "x"},
            headers=_admin_h(admin_user),
        )
        assert resp.status_code == 409

    def test_invalid_meta_yaml_returns_409(self, client: TestClient, admin_user: User) -> None:
        resp = client.post(
            "/api/scripts",
            json={"name": "bad", "content": "x", "meta_yaml": ": {{{"},
            headers=_admin_h(admin_user),
        )
        assert resp.status_code == 409

    def test_non_admin_returns_403(self, client: TestClient, regular_user: User) -> None:
        resp = client.post(
            "/api/scripts",
            json={"name": "x", "content": "x"},
            headers=_user_h(regular_user),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 5.2 — PUT /api/scripts/{name}
# ---------------------------------------------------------------------------


class TestUpdateScriptEndpoint:
    def test_updates_content_and_md5(
        self, client: TestClient, admin_user: User, script: Script
    ) -> None:
        original_md5 = script.md5
        resp = client.put(
            "/api/scripts/hello",
            json={"content": "new content"},
            headers=_admin_h(admin_user),
        )
        assert resp.status_code == 200
        assert resp.json()["content"] == "new content"
        assert resp.json()["md5"] != original_md5

    def test_missing_returns_404(self, client: TestClient, admin_user: User) -> None:
        resp = client.put(
            "/api/scripts/ghost",
            json={"content": "x"},
            headers=_admin_h(admin_user),
        )
        assert resp.status_code == 404

    def test_invalid_meta_returns_422(
        self, client: TestClient, admin_user: User, script: Script
    ) -> None:
        resp = client.put(
            "/api/scripts/hello",
            json={"content": "x", "meta_yaml": ": {{{"},
            headers=_admin_h(admin_user),
        )
        assert resp.status_code == 422

    def test_non_admin_returns_403(
        self, client: TestClient, regular_user: User, script: Script
    ) -> None:
        resp = client.put(
            "/api/scripts/hello",
            json={"content": "x"},
            headers=_user_h(regular_user),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 5.2 — DELETE /api/scripts/{name}
# ---------------------------------------------------------------------------


class TestDeleteScriptEndpoint:
    def test_deletes_script(self, client: TestClient, admin_user: User, script: Script) -> None:
        resp = client.delete("/api/scripts/hello", headers=_admin_h(admin_user))
        assert resp.status_code == 204
        assert client.get("/api/scripts/hello", headers=_admin_h(admin_user)).status_code == 404

    def test_missing_returns_404(self, client: TestClient, admin_user: User) -> None:
        resp = client.delete("/api/scripts/ghost", headers=_admin_h(admin_user))
        assert resp.status_code == 404

    def test_non_admin_returns_403(
        self, client: TestClient, regular_user: User, script: Script
    ) -> None:
        resp = client.delete("/api/scripts/hello", headers=_user_h(regular_user))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 5.3 / 5.4 — script_sync state scenarios
# ---------------------------------------------------------------------------


def _mock_client(
    *,
    agent_state: ApprovalState,
    approved_md5: str | None = None,
    pending_md5: str | None = None,
    stage_result: ApprovalState = ApprovalState.pending,
) -> MagicMock:
    """Return a mock AgentClient preconfigured for a given scenario."""
    descriptor = ScriptDescriptor(
        name="hello",
        state=agent_state,
        approved_md5=approved_md5,
        pending_md5=pending_md5,
    )
    stage_response = StageScriptResponse(name="hello", state=stage_result)
    mock = MagicMock()
    mock.get_script_state = AsyncMock(return_value=descriptor)
    mock.stage_script = AsyncMock(return_value=stage_response)
    return mock


class TestSyncScript:
    async def test_absent_stages_and_returns_pending(
        self, db_session: AsyncSession, script: Script, machine: Machine
    ) -> None:
        client = _mock_client(agent_state=ApprovalState.absent, stage_result=ApprovalState.pending)
        result = await sync_script(machine, script, client, db_session)
        assert result == ApprovalState.pending
        client.stage_script.assert_awaited_once()

    async def test_absent_auto_approved_returns_approved(
        self, db_session: AsyncSession, script: Script, machine: Machine
    ) -> None:
        client = _mock_client(agent_state=ApprovalState.absent, stage_result=ApprovalState.approved)
        result = await sync_script(machine, script, client, db_session)
        assert result == ApprovalState.approved

    async def test_already_approved_matching_md5_no_stage(
        self, db_session: AsyncSession, script: Script, machine: Machine
    ) -> None:
        client = _mock_client(agent_state=ApprovalState.approved, approved_md5=script.md5)
        result = await sync_script(machine, script, client, db_session)
        assert result == ApprovalState.approved
        client.stage_script.assert_not_awaited()

    async def test_approved_stale_triggers_restage(
        self, db_session: AsyncSession, script: Script, machine: Machine
    ) -> None:
        client = _mock_client(
            agent_state=ApprovalState.approved,
            approved_md5="old-md5-differs",
            stage_result=ApprovalState.update_pending,
        )
        result = await sync_script(machine, script, client, db_session)
        assert result == ApprovalState.update_pending
        client.stage_script.assert_awaited_once()

    async def test_pending_not_staged_again(
        self, db_session: AsyncSession, script: Script, machine: Machine
    ) -> None:
        client = _mock_client(agent_state=ApprovalState.pending)
        result = await sync_script(machine, script, client, db_session)
        assert result == ApprovalState.pending
        client.stage_script.assert_not_awaited()

    async def test_update_pending_not_staged_again(
        self, db_session: AsyncSession, script: Script, machine: Machine
    ) -> None:
        client = _mock_client(agent_state=ApprovalState.update_pending)
        result = await sync_script(machine, script, client, db_session)
        assert result == ApprovalState.update_pending
        client.stage_script.assert_not_awaited()

    async def test_rejected_not_staged(
        self, db_session: AsyncSession, script: Script, machine: Machine
    ) -> None:
        client = _mock_client(agent_state=ApprovalState.rejected)
        result = await sync_script(machine, script, client, db_session)
        assert result == ApprovalState.rejected
        client.stage_script.assert_not_awaited()

    async def test_upserts_cache_row_on_first_sync(
        self, db_session: AsyncSession, script: Script, machine: Machine
    ) -> None:
        client = _mock_client(agent_state=ApprovalState.approved, approved_md5=script.md5)
        await sync_script(machine, script, client, db_session)
        from sqlalchemy import select

        row = (
            await db_session.execute(
                select(ScriptTargetState).where(
                    ScriptTargetState.machine_id == machine.id,
                    ScriptTargetState.script_id == script.id,
                )
            )
        ).scalar_one_or_none()
        assert row is not None
        assert row.state == ApprovalState.approved

    async def test_updates_existing_cache_row(
        self, db_session: AsyncSession, script: Script, machine: Machine
    ) -> None:
        # First sync: absent → pending
        client1 = _mock_client(agent_state=ApprovalState.absent, stage_result=ApprovalState.pending)
        await sync_script(machine, script, client1, db_session)
        await db_session.commit()

        # Second sync: now approved
        client2 = _mock_client(agent_state=ApprovalState.approved, approved_md5=script.md5)
        await sync_script(machine, script, client2, db_session)
        await db_session.commit()

        from sqlalchemy import select

        row = (
            await db_session.execute(
                select(ScriptTargetState).where(
                    ScriptTargetState.machine_id == machine.id,
                    ScriptTargetState.script_id == script.id,
                )
            )
        ).scalar_one()
        assert row.state == ApprovalState.approved

    async def test_approved_stale_state_constant(self) -> None:
        assert APPROVED_STALE == "approved_stale"
