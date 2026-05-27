"""Integration tests for Phase 6 — jobs API.

6.1 — REST endpoints (via TestClient, real in-memory SQLite, mocked AgentClient)
6.2 — sync_script wired into submit path
6.3 — reconciler logic tested directly
"""

import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_station_lite.server.auth.jwt import create_access_token
from control_station_lite.server.auth.password import hash_password
from control_station_lite.server.core.crypto import encrypt
from control_station_lite.server.core.job_reconciler import reconcile_once
from control_station_lite.server.core.script_registry import create_script
from control_station_lite.server.db.models import Base, Job, Machine, Script, User
from control_station_lite.server.db.session import get_session
from control_station_lite.server.main import app
from control_station_lite.shared.models import (
    ApprovalState,
    JobStatus,
    JobStatusResponse,
    ScriptDescriptor,
    StageScriptResponse,
)

_CONTENT = "#!/bin/bash\necho hello\n"
_META = "description: greet\npersistent: false\n"
_PERSISTENT_META = "description: llama\npersistent: true\n"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jwt_key = tmp_path / "jwt.key"
    jwt_key.write_bytes(os.urandom(64))
    master_key = tmp_path / "master.key"
    import base64

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
    user = User(username="alice", password_hash=hash_password("pass"), role="user", disabled=False)
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
async def persistent_script(db_session: AsyncSession, admin_user: User) -> Script:
    s = await create_script(
        name="llama",
        content=_CONTENT,
        meta_yaml=_PERSISTENT_META,
        user_id=admin_user.id,
        session=db_session,
    )
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest.fixture
async def machine(db_session: AsyncSession) -> Machine:
    from control_station_lite.server.config import get_settings

    master_key = get_settings().read_master_key()
    m = Machine(
        name="box",
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


def _now() -> datetime:
    return datetime.utcnow()


def _job_resp(
    job_uuid: str = "uuid-1",
    s: JobStatus = JobStatus.completed,
    persistent: bool = False,
) -> JobStatusResponse:
    return JobStatusResponse(
        job_uuid=job_uuid,
        script_name="hello",
        status=s,
        persistent=persistent,
        started_at=_now(),
        ended_at=_now() if s != JobStatus.running else None,
        exit_code=0 if s == JobStatus.completed else None,
    )


def _agent_client_ctx(
    *,
    descriptor: ScriptDescriptor,
    stage_resp: StageScriptResponse | None = None,
    job_resp: JobStatusResponse | None = None,
    get_job_resp: JobStatusResponse | None = None,
) -> MagicMock:
    """Build a mock AgentClient context manager."""
    mock_client = MagicMock()
    mock_client.ensure_agent_running = AsyncMock()
    mock_client.get_script_state = AsyncMock(return_value=descriptor)
    mock_client.stage_script = AsyncMock(
        return_value=stage_resp or StageScriptResponse(name="hello", state=ApprovalState.pending)
    )
    mock_client.submit_job = AsyncMock(return_value=job_resp or _job_resp())
    mock_client.get_job_status = AsyncMock(
        return_value=get_job_resp or _job_resp(s=JobStatus.completed)
    )
    mock_client.kill_job = AsyncMock()

    # stream_logs must be an async generator function (not AsyncMock) so that
    # `async for line in client.stream_logs(uuid)` works without awaiting first.
    async def _stream_logs(*_args: object, **_kwargs: object):  # type: ignore[return]
        for item in ["line one", "line two"]:
            yield item

    mock_client.stream_logs = _stream_logs

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# 6.1 — Submit job
# ---------------------------------------------------------------------------


class TestSubmitJob:
    def test_approved_script_creates_job(
        self, client: TestClient, admin_user: User, machine: Machine, script: Script
    ) -> None:
        descriptor = ScriptDescriptor(
            name="hello", state=ApprovalState.approved, approved_md5=script.md5
        )
        ctx = _agent_client_ctx(descriptor=descriptor)
        with patch("control_station_lite.server.api.jobs.AgentClient", return_value=ctx):
            with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                resp = client.post(
                    f"/api/machines/{machine.id}/jobs",
                    headers=_admin_h(admin_user),
                    json={"script_name": "hello", "params": {}},
                )
        assert resp.status_code == 202
        data = resp.json()
        assert data["machine_id"] == machine.id
        assert data["script_id"] == script.id
        assert data["status"] in ("completed", "running", "failed")
        assert "job_uuid" in data

    def test_pending_script_returns_409(
        self, client: TestClient, admin_user: User, machine: Machine, script: Script
    ) -> None:
        descriptor = ScriptDescriptor(name="hello", state=ApprovalState.absent)
        stage_resp = StageScriptResponse(name="hello", state=ApprovalState.pending)
        ctx = _agent_client_ctx(descriptor=descriptor, stage_resp=stage_resp)
        with patch("control_station_lite.server.api.jobs.AgentClient", return_value=ctx):
            with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                resp = client.post(
                    f"/api/machines/{machine.id}/jobs",
                    headers=_admin_h(admin_user),
                    json={"script_name": "hello", "params": {}},
                )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["approval_error"] == "pending_approval (new)"
        assert detail["agent_state"] == "pending"

    def test_update_pending_returns_409(
        self, client: TestClient, admin_user: User, machine: Machine, script: Script
    ) -> None:
        descriptor = ScriptDescriptor(name="hello", state=ApprovalState.update_pending)
        ctx = _agent_client_ctx(descriptor=descriptor)
        with patch("control_station_lite.server.api.jobs.AgentClient", return_value=ctx):
            with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                resp = client.post(
                    f"/api/machines/{machine.id}/jobs",
                    headers=_admin_h(admin_user),
                    json={"script_name": "hello", "params": {}},
                )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["approval_error"] == "pending_approval (update)"

    def test_rejected_script_returns_409(
        self, client: TestClient, admin_user: User, machine: Machine, script: Script
    ) -> None:
        descriptor = ScriptDescriptor(name="hello", state=ApprovalState.rejected)
        ctx = _agent_client_ctx(descriptor=descriptor)
        with patch("control_station_lite.server.api.jobs.AgentClient", return_value=ctx):
            with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                resp = client.post(
                    f"/api/machines/{machine.id}/jobs",
                    headers=_admin_h(admin_user),
                    json={"script_name": "hello", "params": {}},
                )
        assert resp.status_code == 409
        assert resp.json()["detail"]["approval_error"] == "rejected"

    def test_unknown_script_returns_404(
        self, client: TestClient, admin_user: User, machine: Machine
    ) -> None:
        with (
            patch("control_station_lite.server.api.jobs.AgentClient"),
            patch("control_station_lite.server.api.jobs.get_ssh_pool"),
        ):
            resp = client.post(
                f"/api/machines/{machine.id}/jobs",
                headers=_admin_h(admin_user),
                json={"script_name": "no-such", "params": {}},
            )
        assert resp.status_code == 404

    def test_unknown_machine_returns_404(self, client: TestClient, admin_user: User) -> None:
        resp = client.post(
            "/api/machines/9999/jobs",
            headers=_admin_h(admin_user),
            json={"script_name": "hello", "params": {}},
        )
        assert resp.status_code == 404

    def test_unauthenticated_returns_401(self, client: TestClient, machine: Machine) -> None:
        resp = client.post(
            f"/api/machines/{machine.id}/jobs", json={"script_name": "hello", "params": {}}
        )
        assert resp.status_code == 401

    def test_params_passed_through(
        self, client: TestClient, admin_user: User, machine: Machine, script: Script
    ) -> None:
        descriptor = ScriptDescriptor(
            name="hello", state=ApprovalState.approved, approved_md5=script.md5
        )
        ctx = _agent_client_ctx(descriptor=descriptor)
        with patch("control_station_lite.server.api.jobs.AgentClient", return_value=ctx):
            with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                resp = client.post(
                    f"/api/machines/{machine.id}/jobs",
                    headers=_admin_h(admin_user),
                    json={"script_name": "hello", "params": {"target": "world"}},
                )
        assert resp.status_code == 202
        data = resp.json()
        assert json.loads(data["params_json"]) == {"target": "world"}


# ---------------------------------------------------------------------------
# 6.1 — GET /api/jobs/{uuid}
# ---------------------------------------------------------------------------


class TestGetJob:
    async def _make_job(
        self, db_session: AsyncSession, machine: Machine, script: Script, user: User
    ) -> Job:
        job = Job(
            job_uuid="test-uuid",
            machine_id=machine.id,
            script_id=script.id,
            user_id=user.id,
            params_json="{}",
            status=JobStatus.completed,
            persistent=False,
            started_at=_now(),
            ended_at=_now(),
            exit_code=0,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        return job

    def test_get_completed_job(
        self,
        client: TestClient,
        db_session: AsyncSession,
        admin_user: User,
        machine: Machine,
        script: Script,
    ) -> None:
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            self._make_job(db_session, machine, script, admin_user)
        )
        resp = client.get("/api/jobs/test-uuid", headers=_admin_h(admin_user))
        assert resp.status_code == 200
        assert resp.json()["job_uuid"] == "test-uuid"

    def test_not_found_returns_404(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/api/jobs/no-such-uuid", headers=_admin_h(admin_user))
        assert resp.status_code == 404

    def test_running_job_refreshes_from_agent(
        self,
        client: TestClient,
        db_session: AsyncSession,
        admin_user: User,
        machine: Machine,
        script: Script,
    ) -> None:
        import asyncio

        job = asyncio.get_event_loop().run_until_complete(
            self._make_job(db_session, machine, script, admin_user)
        )
        # Change the DB status to running
        asyncio.get_event_loop().run_until_complete(_set_status(db_session, job, JobStatus.running))

        # Mock the agent response to return completed
        completed = _job_resp(job_uuid="test-uuid", s=JobStatus.completed)
        ctx = _agent_client_ctx(
            descriptor=ScriptDescriptor(name="hello", state=ApprovalState.approved),
            get_job_resp=completed,
        )
        with patch("control_station_lite.server.api.jobs.AgentClient", return_value=ctx):
            with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                resp = client.get("/api/jobs/test-uuid", headers=_admin_h(admin_user))

        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"


async def _set_status(session: AsyncSession, job: Job, s: JobStatus) -> None:
    job.status = s
    await session.commit()


# ---------------------------------------------------------------------------
# 6.1 — Kill job
# ---------------------------------------------------------------------------


class TestKillJob:
    async def _make_persistent_job(
        self, db_session: AsyncSession, machine: Machine, script: Script, user: User
    ) -> Job:
        job = Job(
            job_uuid="persist-uuid",
            machine_id=machine.id,
            script_id=script.id,
            user_id=user.id,
            params_json="{}",
            status=JobStatus.running,
            persistent=True,
            started_at=_now(),
            ended_at=None,
            exit_code=None,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        return job

    def test_kill_persistent_job(
        self,
        client: TestClient,
        db_session: AsyncSession,
        admin_user: User,
        machine: Machine,
        script: Script,
    ) -> None:
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            self._make_persistent_job(db_session, machine, script, admin_user)
        )
        ctx = _agent_client_ctx(
            descriptor=ScriptDescriptor(name="hello", state=ApprovalState.approved)
        )
        with patch("control_station_lite.server.api.jobs.AgentClient", return_value=ctx):
            with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                resp = client.post("/api/jobs/persist-uuid/kill", headers=_admin_h(admin_user))

        assert resp.status_code == 204

    def test_kill_nonpersistent_returns_400(
        self,
        client: TestClient,
        db_session: AsyncSession,
        admin_user: User,
        machine: Machine,
        script: Script,
    ) -> None:
        import asyncio

        job = asyncio.get_event_loop().run_until_complete(
            _make_oneoff_job(db_session, machine, script, admin_user)
        )
        resp = client.post(f"/api/jobs/{job.job_uuid}/kill", headers=_admin_h(admin_user))
        assert resp.status_code == 400

    def test_kill_unknown_job_returns_404(self, client: TestClient, admin_user: User) -> None:
        resp = client.post("/api/jobs/no-such-uuid/kill", headers=_admin_h(admin_user))
        assert resp.status_code == 404


async def _make_oneoff_job(
    session: AsyncSession, machine: Machine, script: Script, user: User
) -> Job:
    job = Job(
        job_uuid="oneoff-uuid",
        machine_id=machine.id,
        script_id=script.id,
        user_id=user.id,
        params_json="{}",
        status=JobStatus.completed,
        persistent=False,
        started_at=_now(),
        ended_at=_now(),
        exit_code=0,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


# ---------------------------------------------------------------------------
# 6.1 — GET /api/jobs (history)
# ---------------------------------------------------------------------------


class TestListJobs:
    async def _seed_jobs(
        self, db_session: AsyncSession, machine: Machine, script: Script, user: User
    ) -> list[Job]:
        jobs = [
            Job(
                job_uuid=f"uuid-{i}",
                machine_id=machine.id,
                script_id=script.id,
                user_id=user.id,
                params_json="{}",
                status=JobStatus.completed if i % 2 == 0 else JobStatus.failed,
                persistent=False,
                started_at=_now(),
                ended_at=_now(),
                exit_code=0,
            )
            for i in range(4)
        ]
        for j in jobs:
            db_session.add(j)
        await db_session.commit()
        return jobs

    def test_list_all_jobs(
        self,
        client: TestClient,
        db_session: AsyncSession,
        admin_user: User,
        machine: Machine,
        script: Script,
    ) -> None:
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            self._seed_jobs(db_session, machine, script, admin_user)
        )
        resp = client.get("/api/jobs", headers=_admin_h(admin_user))
        assert resp.status_code == 200
        assert len(resp.json()) == 4

    def test_filter_by_machine_id(
        self,
        client: TestClient,
        db_session: AsyncSession,
        admin_user: User,
        machine: Machine,
        script: Script,
    ) -> None:
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            self._seed_jobs(db_session, machine, script, admin_user)
        )
        resp = client.get(f"/api/jobs?machine_id={machine.id}", headers=_admin_h(admin_user))
        assert resp.status_code == 200
        assert all(j["machine_id"] == machine.id for j in resp.json())

    def test_filter_by_status(
        self,
        client: TestClient,
        db_session: AsyncSession,
        admin_user: User,
        machine: Machine,
        script: Script,
    ) -> None:
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            self._seed_jobs(db_session, machine, script, admin_user)
        )
        resp = client.get("/api/jobs?status=completed", headers=_admin_h(admin_user))
        assert resp.status_code == 200
        assert all(j["status"] == "completed" for j in resp.json())
        assert len(resp.json()) == 2

    def test_filter_by_script_name(
        self,
        client: TestClient,
        db_session: AsyncSession,
        admin_user: User,
        machine: Machine,
        script: Script,
    ) -> None:
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            self._seed_jobs(db_session, machine, script, admin_user)
        )
        resp = client.get("/api/jobs?script_name=hello", headers=_admin_h(admin_user))
        assert resp.status_code == 200
        assert len(resp.json()) == 4

    def test_filter_by_unknown_script_returns_empty(
        self, client: TestClient, admin_user: User
    ) -> None:
        resp = client.get("/api/jobs?script_name=no-such", headers=_admin_h(admin_user))
        assert resp.status_code == 200
        assert resp.json() == []

    def test_limit_parameter(
        self,
        client: TestClient,
        db_session: AsyncSession,
        admin_user: User,
        machine: Machine,
        script: Script,
    ) -> None:
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            self._seed_jobs(db_session, machine, script, admin_user)
        )
        resp = client.get("/api/jobs?limit=2", headers=_admin_h(admin_user))
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_unauthenticated_returns_401(self, client: TestClient) -> None:
        resp = client.get("/api/jobs")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 6.1 — Stream logs
# ---------------------------------------------------------------------------


class TestStreamLogs:
    async def _make_running_job(
        self, db_session: AsyncSession, machine: Machine, script: Script, user: User
    ) -> Job:
        job = Job(
            job_uuid="stream-uuid",
            machine_id=machine.id,
            script_id=script.id,
            user_id=user.id,
            params_json="{}",
            status=JobStatus.running,
            persistent=True,
            started_at=_now(),
            ended_at=None,
            exit_code=None,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        return job

    def test_stream_returns_sse_media_type(
        self,
        client: TestClient,
        db_session: AsyncSession,
        admin_user: User,
        machine: Machine,
        script: Script,
    ) -> None:
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            self._make_running_job(db_session, machine, script, admin_user)
        )
        ctx = _agent_client_ctx(
            descriptor=ScriptDescriptor(name="hello", state=ApprovalState.approved)
        )
        with patch("control_station_lite.server.api.jobs.AgentClient", return_value=ctx):
            with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                with client.stream(
                    "GET",
                    "/api/jobs/stream-uuid/stream",
                    headers=_admin_h(admin_user),
                ) as resp:
                    assert resp.status_code == 200
                    assert "text/event-stream" in resp.headers["content-type"]

    def test_stream_not_found_returns_404(self, client: TestClient, admin_user: User) -> None:
        resp = client.get("/api/jobs/no-such-uuid/stream", headers=_admin_h(admin_user))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 6.3 — Reconciler
# ---------------------------------------------------------------------------


async def _make_reconciler_db(
    *,
    master_key: bytes,
    jobs: list[Job],
    machine_row: Machine,
) -> tuple[async_sessionmaker[AsyncSession], AsyncSession]:
    """Create an independent in-memory DB for reconciler tests.

    Returns (factory, session).  The machine row is inserted so the reconciler
    can look up the machine for the jobs.  The caller must close the engine.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Re-create the machine in the new DB (with a fresh key encrypted by master_key)
    async with factory() as s:
        new_machine = Machine(
            name=machine_row.name,
            ssh_host=machine_row.ssh_host,
            ssh_port=machine_row.ssh_port,
            ssh_user=machine_row.ssh_user,
            ssh_key_encrypted=machine_row.ssh_key_encrypted,
            key_fingerprint=machine_row.key_fingerprint,
            agent_port=machine_row.agent_port,
            scripts_dir=machine_row.scripts_dir,
            platform=machine_row.platform,
            mac_address=machine_row.mac_address,
            created_at=machine_row.created_at or datetime.utcnow(),
        )
        s.add(new_machine)
        await s.flush()
        for job in jobs:
            s.add(
                Job(
                    job_uuid=job.job_uuid,
                    machine_id=new_machine.id,
                    script_id=job.script_id,
                    user_id=job.user_id,
                    params_json=job.params_json,
                    status=job.status,
                    persistent=job.persistent,
                    started_at=job.started_at,
                    ended_at=job.ended_at,
                    exit_code=job.exit_code,
                )
            )
        await s.commit()
    return factory, engine


class TestReconciler:
    async def test_reconcile_updates_completed_job(
        self,
        machine: Machine,
        script: Script,
        admin_user: User,
    ) -> None:
        from control_station_lite.server.config import get_settings

        master_key = get_settings().read_master_key()

        job_template = Job(
            job_uuid="rec-uuid",
            machine_id=machine.id,
            script_id=script.id,
            user_id=admin_user.id,
            params_json="{}",
            status=JobStatus.running,
            persistent=True,
            started_at=_now(),
            ended_at=None,
            exit_code=None,
        )

        factory, engine = await _make_reconciler_db(
            master_key=master_key, jobs=[job_template], machine_row=machine
        )

        completed = _job_resp(job_uuid="rec-uuid", s=JobStatus.completed)
        ctx = _agent_client_ctx(
            descriptor=ScriptDescriptor(name="hello", state=ApprovalState.approved),
            get_job_resp=completed,
        )

        with patch("control_station_lite.server.core.job_reconciler.AgentClient", return_value=ctx):
            with patch("control_station_lite.server.core.job_reconciler.get_ssh_pool"):
                await reconcile_once(factory, master_key)

        async with factory() as s:
            result = await s.execute(select(Job).where(Job.job_uuid == "rec-uuid"))
            updated = result.scalar_one()
            assert updated.status == JobStatus.completed

        await engine.dispose()

    async def test_reconcile_skips_completed_jobs(
        self,
        machine: Machine,
        script: Script,
        admin_user: User,
    ) -> None:
        from control_station_lite.server.config import get_settings

        master_key = get_settings().read_master_key()

        job_template = Job(
            job_uuid="done-uuid",
            machine_id=machine.id,
            script_id=script.id,
            user_id=admin_user.id,
            params_json="{}",
            status=JobStatus.completed,
            persistent=False,
            started_at=_now(),
            ended_at=_now(),
            exit_code=0,
        )

        factory, engine = await _make_reconciler_db(
            master_key=master_key, jobs=[job_template], machine_row=machine
        )

        with patch("control_station_lite.server.core.job_reconciler.AgentClient") as MockClient:
            with patch("control_station_lite.server.core.job_reconciler.get_ssh_pool"):
                await reconcile_once(factory, master_key)
            MockClient.assert_not_called()

        await engine.dispose()
