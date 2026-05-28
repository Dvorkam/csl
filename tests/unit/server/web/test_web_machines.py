"""Unit tests for the machine-detail, run-dialog, job-detail web routes."""

import os
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_station_lite.server.auth.jwt import create_access_token
from control_station_lite.server.auth.password import hash_password
from control_station_lite.server.db.models import (
    Base,
    Job,
    Machine,
    Script,
    ScriptTargetState,
    User,
    UserMachine,
)
from control_station_lite.server.db.session import get_session
from control_station_lite.server.main import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _jwt_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

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
async def machine(db_session: AsyncSession) -> Machine:
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
async def script(db_session: AsyncSession, admin_user: User) -> Script:
    s = Script(
        name="hello",
        content="echo hello",
        meta_yaml="description: Says hello\nparams:\n  - name: target\n    type: string\n    required: true\n",
        md5="abc123",
        persistent=False,
        updated_at=datetime.utcnow(),
        updated_by=admin_user.id,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest.fixture
async def persistent_script(db_session: AsyncSession, admin_user: User) -> Script:
    s = Script(
        name="daemon",
        content="while true; do sleep 1; done",
        meta_yaml=None,
        md5="def456",
        persistent=True,
        updated_at=datetime.utcnow(),
        updated_by=admin_user.id,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


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


# ---------------------------------------------------------------------------
# Machine detail (8.4)
# ---------------------------------------------------------------------------


def test_machine_detail_200_for_admin(
    client: TestClient, admin_user: User, machine: Machine
) -> None:
    resp = client.get(f"/machines/{machine.id}", cookies=_auth(admin_user))
    assert resp.status_code == 200
    assert b"testbox" in resp.content


def test_machine_detail_200_for_bookmarked_user(
    client: TestClient, regular_user: User, machine: Machine, db_session: AsyncSession
) -> None:
    import asyncio

    async def _bm() -> None:
        db_session.add(UserMachine(user_id=regular_user.id, machine_id=machine.id))
        await db_session.commit()

    asyncio.get_event_loop().run_until_complete(_bm())
    resp = client.get(f"/machines/{machine.id}", cookies=_auth(regular_user))
    assert resp.status_code == 200


def test_machine_detail_403_for_unbookmarked_user(
    client: TestClient, regular_user: User, machine: Machine
) -> None:
    resp = client.get(f"/machines/{machine.id}", cookies=_auth(regular_user))
    assert resp.status_code == 403


def test_machine_detail_404_for_unknown_machine(
    client: TestClient, admin_user: User
) -> None:
    resp = client.get("/machines/9999", cookies=_auth(admin_user))
    assert resp.status_code == 404


def test_machine_detail_shows_script_name(
    client: TestClient, admin_user: User, machine: Machine, script: Script
) -> None:
    resp = client.get(f"/machines/{machine.id}", cookies=_auth(admin_user))
    assert b"hello" in resp.content


def test_machine_detail_shows_approval_state_from_cache(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    script: Script,
    db_session: AsyncSession,
) -> None:
    import asyncio

    async def _seed() -> None:
        db_session.add(
            ScriptTargetState(
                machine_id=machine.id,
                script_id=script.id,
                state="approved",
                approved_md5="abc123",
                pending_md5=None,
                last_refreshed_at=datetime.utcnow(),
            )
        )
        await db_session.commit()

    asyncio.get_event_loop().run_until_complete(_seed())
    resp = client.get(f"/machines/{machine.id}", cookies=_auth(admin_user))
    assert b"approved" in resp.content


def test_machine_detail_shows_running_jobs(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    persistent_script: Script,
    db_session: AsyncSession,
) -> None:
    import asyncio

    job_uuid = "aaaabbbb-0000-0000-0000-000000000001"

    async def _seed() -> None:
        db_session.add(
            Job(
                job_uuid=job_uuid,
                machine_id=machine.id,
                script_id=persistent_script.id,
                user_id=admin_user.id,
                params_json="{}",
                status="running",
                persistent=True,
                started_at=datetime.utcnow(),
            )
        )
        await db_session.commit()

    asyncio.get_event_loop().run_until_complete(_seed())
    resp = client.get(f"/machines/{machine.id}", cookies=_auth(admin_user))
    assert b"aaaabbbb" in resp.content


def test_machine_detail_redirect_when_unauthenticated(
    client: TestClient, machine: Machine
) -> None:
    resp = client.get(f"/machines/{machine.id}")
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Run form (8.5)
# ---------------------------------------------------------------------------


def test_run_form_200_for_admin(
    client: TestClient, admin_user: User, machine: Machine, script: Script
) -> None:
    resp = client.get(
        f"/machines/{machine.id}/scripts/{script.name}/run", cookies=_auth(admin_user)
    )
    assert resp.status_code == 200
    assert b"target" in resp.content  # param name in form


def test_run_form_renders_no_params_for_paramless_script(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    persistent_script: Script,
) -> None:
    resp = client.get(
        f"/machines/{machine.id}/scripts/{persistent_script.name}/run",
        cookies=_auth(admin_user),
    )
    assert resp.status_code == 200
    assert b"no parameters" in resp.content


def test_run_form_404_for_unknown_script(
    client: TestClient, admin_user: User, machine: Machine
) -> None:
    resp = client.get(
        f"/machines/{machine.id}/scripts/nonexistent/run", cookies=_auth(admin_user)
    )
    assert resp.status_code == 404


def test_run_form_403_for_unbookmarked_user(
    client: TestClient, regular_user: User, machine: Machine, script: Script
) -> None:
    resp = client.get(
        f"/machines/{machine.id}/scripts/{script.name}/run", cookies=_auth(regular_user)
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Run submit with mocked AgentClient (8.5)
# ---------------------------------------------------------------------------


def test_run_submit_redirects_to_job_on_success(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    script: Script,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import control_station_lite.server.web.machines as machines_mod
    from control_station_lite.shared.models import (
        ApprovalState,
        JobStatus,
        JobStatusResponse,
        ScriptDescriptor,
        StageScriptResponse,
    )

    monkeypatch.setattr(machines_mod, "decrypt", lambda *_: b"fake-key")
    monkeypatch.setattr(machines_mod, "get_ssh_pool", lambda: None)

    class _MockClient:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        async def __aenter__(self) -> "_MockClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def ensure_agent_running(self) -> None:
            pass

        async def get_script_state(self, name: str) -> ScriptDescriptor:
            return ScriptDescriptor(
                name=name,
                state=ApprovalState.approved,
                approved_md5="abc123",
                pending_md5=None,
            )

        async def stage_script(
            self, name: str, content: str, md5: str, meta_yaml: object
        ) -> StageScriptResponse:
            return StageScriptResponse(name=name, state=ApprovalState.approved)

        async def submit_job(self, request: object) -> JobStatusResponse:
            return JobStatusResponse(
                job_uuid=getattr(request, "job_uuid", "test-uuid"),
                script_name=script.name,
                status=JobStatus.running,
                persistent=False,
                started_at=datetime.utcnow(),
            )

    monkeypatch.setattr(machines_mod, "AgentClient", _MockClient)

    resp = client.post(
        f"/machines/{machine.id}/scripts/{script.name}/run",
        data={"target": "world"},
        cookies=_auth(admin_user),
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/jobs/")


def test_run_submit_shows_pending_error(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    script: Script,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import control_station_lite.server.web.machines as machines_mod
    from control_station_lite.shared.models import (
        ApprovalState,
        ScriptDescriptor,
        StageScriptResponse,
    )

    monkeypatch.setattr(machines_mod, "decrypt", lambda *_: b"fake-key")
    monkeypatch.setattr(machines_mod, "get_ssh_pool", lambda: None)

    class _MockClient:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        async def __aenter__(self) -> "_MockClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def ensure_agent_running(self) -> None:
            pass

        async def get_script_state(self, name: str) -> ScriptDescriptor:
            return ScriptDescriptor(
                name=name,
                state=ApprovalState.pending,
                approved_md5=None,
                pending_md5="abc123",
            )

        async def stage_script(
            self, name: str, content: str, md5: str, meta_yaml: object
        ) -> StageScriptResponse:
            return StageScriptResponse(name=name, state=ApprovalState.pending)

    monkeypatch.setattr(machines_mod, "AgentClient", _MockClient)

    resp = client.post(
        f"/machines/{machine.id}/scripts/{script.name}/run",
        data={"target": "world"},
        cookies=_auth(admin_user),
    )
    assert resp.status_code == 409
    assert b"pending" in resp.content


# ---------------------------------------------------------------------------
# Restage (8.7)
# ---------------------------------------------------------------------------


def test_restage_returns_badge_partial(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    script: Script,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import control_station_lite.server.web.machines as machines_mod
    from control_station_lite.shared.models import (
        ApprovalState,
        ScriptDescriptor,
        StageScriptResponse,
    )

    monkeypatch.setattr(machines_mod, "decrypt", lambda *_: b"fake-key")
    monkeypatch.setattr(machines_mod, "get_ssh_pool", lambda: None)

    class _MockClient:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        async def __aenter__(self) -> "_MockClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def stage_script(
            self, name: str, content: str, md5: str, meta_yaml: object
        ) -> StageScriptResponse:
            return StageScriptResponse(name=name, state=ApprovalState.pending)

        async def get_script_state(self, name: str) -> ScriptDescriptor:
            return ScriptDescriptor(
                name=name,
                state=ApprovalState.pending,
                approved_md5=None,
                pending_md5="abc123",
            )

    monkeypatch.setattr(machines_mod, "AgentClient", _MockClient)

    resp = client.post(
        f"/machines/{machine.id}/scripts/{script.name}/restage",
        cookies=_auth(admin_user),
    )
    assert resp.status_code == 200
    assert b"pending" in resp.content


def test_restage_404_for_unknown_script(
    client: TestClient, admin_user: User, machine: Machine
) -> None:
    resp = client.post(
        f"/machines/{machine.id}/scripts/nonexistent/restage",
        cookies=_auth(admin_user),
    )
    assert resp.status_code == 200
    assert b"absent" in resp.content


# ---------------------------------------------------------------------------
# Job detail (8.6)
# ---------------------------------------------------------------------------


async def _seed_job(
    db_session: AsyncSession,
    machine: Machine,
    script: Script,
    user: User,
    persistent: bool = False,
    status: str = "completed",
) -> Job:
    job_uuid = "ccccdddd-1111-1111-1111-000000000001"
    job = Job(
        job_uuid=job_uuid,
        machine_id=machine.id,
        script_id=script.id,
        user_id=user.id,
        params_json="{}",
        status=status,
        persistent=persistent,
        started_at=datetime.utcnow(),
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


def test_job_detail_returns_200(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    script: Script,
    db_session: AsyncSession,
) -> None:
    import asyncio

    job = asyncio.get_event_loop().run_until_complete(
        _seed_job(db_session, machine, script, admin_user)
    )
    resp = client.get(f"/jobs/{job.job_uuid}", cookies=_auth(admin_user))
    assert resp.status_code == 200
    assert b"ccccdddd" in resp.content


def test_job_detail_shows_script_name(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    script: Script,
    db_session: AsyncSession,
) -> None:
    import asyncio

    job = asyncio.get_event_loop().run_until_complete(
        _seed_job(db_session, machine, script, admin_user)
    )
    resp = client.get(f"/jobs/{job.job_uuid}", cookies=_auth(admin_user))
    assert b"hello" in resp.content


def test_job_detail_404_for_unknown_job(
    client: TestClient, admin_user: User
) -> None:
    resp = client.get("/jobs/no-such-uuid", cookies=_auth(admin_user))
    assert resp.status_code == 404


def test_job_detail_shows_kill_button_for_running_persistent_job(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    persistent_script: Script,
    db_session: AsyncSession,
) -> None:
    import asyncio

    job = asyncio.get_event_loop().run_until_complete(
        _seed_job(db_session, machine, persistent_script, admin_user, persistent=True, status="running")
    )
    resp = client.get(f"/jobs/{job.job_uuid}", cookies=_auth(admin_user))
    assert resp.status_code == 200
    assert b"kill-btn" in resp.content


def test_job_detail_no_kill_button_for_completed_job(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    script: Script,
    db_session: AsyncSession,
) -> None:
    import asyncio

    job = asyncio.get_event_loop().run_until_complete(
        _seed_job(db_session, machine, script, admin_user, status="completed")
    )
    resp = client.get(f"/jobs/{job.job_uuid}", cookies=_auth(admin_user))
    assert resp.status_code == 200
    assert b"kill-btn" not in resp.content


# ---------------------------------------------------------------------------
# Job stream (8.6) — SSE endpoint
# ---------------------------------------------------------------------------


def test_job_stream_returns_event_stream(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    script: Script,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import control_station_lite.server.web.machines as machines_mod

    monkeypatch.setattr(machines_mod, "decrypt", lambda *_: b"fake-key")
    monkeypatch.setattr(machines_mod, "get_ssh_pool", lambda: None)

    async def _fake_stream_logs(job_uuid: str):  # type: ignore[no-untyped-def]
        yield "line one"
        yield "line two"

    class _MockClient:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        async def __aenter__(self) -> "_MockClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        def stream_logs(self, job_uuid: str):  # type: ignore[no-untyped-def]
            return _fake_stream_logs(job_uuid)

    monkeypatch.setattr(machines_mod, "AgentClient", _MockClient)

    job = asyncio.get_event_loop().run_until_complete(
        _seed_job(db_session, machine, script, admin_user)
    )
    resp = client.get(f"/jobs/{job.job_uuid}/stream", cookies=_auth(admin_user))
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert b"line one" in resp.content


# ---------------------------------------------------------------------------
# Job kill (8.6)
# ---------------------------------------------------------------------------


def test_job_kill_returns_killed_badge(
    client: TestClient,
    admin_user: User,
    machine: Machine,
    persistent_script: Script,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import control_station_lite.server.web.machines as machines_mod

    monkeypatch.setattr(machines_mod, "decrypt", lambda *_: b"fake-key")
    monkeypatch.setattr(machines_mod, "get_ssh_pool", lambda: None)

    class _MockClient:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        async def __aenter__(self) -> "_MockClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def kill_job(self, job_uuid: str) -> None:
            pass

    monkeypatch.setattr(machines_mod, "AgentClient", _MockClient)

    job = asyncio.get_event_loop().run_until_complete(
        _seed_job(db_session, machine, persistent_script, admin_user, persistent=True, status="running")
    )
    resp = client.post(f"/jobs/{job.job_uuid}/kill", cookies=_auth(admin_user))
    assert resp.status_code == 200
    assert b"Killed" in resp.content
