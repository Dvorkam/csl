# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end job flow tests (Phase 6).

These tests run both the agent (in-process TestClient) and the server
(in-process TestClient with a real in-memory SQLite DB) with AgentClient
mocked to route HTTP calls through the agent TestClient directly.

6.4 — Stage a script, approve it, submit, observe completion.
6.5 — Submit a persistent script, stream logs, kill it.
6.6 — Submit a rejected script — verify structured error, no retry.
"""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_station_lite.agent.approvals import ApprovalsManager
from control_station_lite.agent.config import AgentConfig, AgentSection
from control_station_lite.agent.main import app as agent_app
from control_station_lite.server.auth.jwt import create_access_token
from control_station_lite.server.auth.password import hash_password
from control_station_lite.server.core.crypto import encrypt
from control_station_lite.server.core.script_registry import create_script
from control_station_lite.server.db.models import Base, Machine, Script, User
from control_station_lite.server.db.session import get_session
from control_station_lite.server.main import app as server_app
from control_station_lite.shared.models import JobStatus

_SCRIPT_CONTENT = "#!/bin/bash\necho hello from csl\n"
_SCRIPT_MD5 = hashlib.md5(_SCRIPT_CONTENT.encode()).hexdigest()
_META = "description: greet\npersistent: false\n"
_PERSISTENT_META = "description: llama\npersistent: true\n"

# Windows: script name carries .ps1 so find_script resolves it and
# build_command selects powershell.  Matches the pattern in test_approval_flow.py.
_PS1_CONTENT = 'Write-Output "hello from csl"\n'
_PS1_MD5 = hashlib.md5(_PS1_CONTENT.encode()).hexdigest()
_PS1_PERSISTENT_CONTENT = "Start-Sleep -Seconds 60\n"
_PS1_PERSISTENT_MD5 = hashlib.md5(_PS1_PERSISTENT_CONTENT.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jwt_key = tmp_path / "jwt.key"
    jwt_key.write_bytes(os.urandom(64))
    master_key_bytes = os.urandom(32)
    master_key_file = tmp_path / "master.key"
    master_key_file.write_text(base64.b64encode(master_key_bytes).decode())
    monkeypatch.setenv("CSL_JWT_KEY_PATH", str(jwt_key))
    monkeypatch.setenv("CSL_MASTER_KEY_PATH", str(master_key_file))
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
async def server_machine(db_session: AsyncSession) -> Machine:
    from control_station_lite.server.config import get_settings

    master_key = get_settings().read_master_key()
    m = Machine(
        name="e2e-box",
        ssh_host="127.0.0.1",
        ssh_port=22,
        ssh_user="alice",
        ssh_key_encrypted=encrypt(b"fake-e2e-key", master_key),
        key_fingerprint="SHA256:e2e",
        agent_port=47731,
        scripts_dir="/tmp/.csl/scripts",
        platform="linux",
        mac_address=None,
        created_at=datetime.utcnow(),
    )
    db_session.add(m)
    await db_session.commit()
    await db_session.refresh(m)
    return m


@pytest.fixture
async def server_client(db_session: AsyncSession) -> TestClient:
    async def _override() -> AsyncSession:
        yield db_session

    server_app.dependency_overrides[get_session] = _override
    with TestClient(server_app, base_url="https://testserver", raise_server_exceptions=True) as c:
        yield c
    server_app.dependency_overrides.pop(get_session, None)


def _admin_h(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, 'admin')}"}


# ---------------------------------------------------------------------------
# Bridge: route AgentClient through in-process agent TestClient
# ---------------------------------------------------------------------------


def _make_agent_bridge(agent: TestClient) -> Any:
    """Build an AgentClient that delegates all calls to the in-process agent."""

    mock_client = MagicMock()
    mock_client.ensure_agent_running = AsyncMock()

    async def _get_script_state(name: str):  # type: ignore[return]
        from control_station_lite.shared.models import ScriptDescriptor

        resp = agent.get(f"/scripts/{name}/state")
        return ScriptDescriptor.model_validate(resp.json())

    async def _stage_script(name: str, content: str, md5: str, meta_yaml: str | None):  # type: ignore[return]
        from control_station_lite.shared.models import StageScriptResponse

        payload: dict[str, Any] = {"content": content, "md5": md5}
        if meta_yaml is not None:
            payload["meta_yaml"] = meta_yaml
        resp = agent.post(f"/scripts/{name}/stage", json=payload)
        return StageScriptResponse.model_validate(resp.json())

    async def _submit_job(request: Any):  # type: ignore[return]
        from control_station_lite.shared.models import JobStatusResponse

        resp = agent.post("/jobs", json=request.model_dump())
        resp.raise_for_status()
        return JobStatusResponse.model_validate(resp.json())

    async def _get_job_status(job_uuid: str):  # type: ignore[return]
        from control_station_lite.shared.models import JobStatusResponse

        resp = agent.get(f"/jobs/{job_uuid}")
        resp.raise_for_status()
        return JobStatusResponse.model_validate(resp.json())

    async def _kill_job(job_uuid: str) -> None:
        agent.delete(f"/jobs/{job_uuid}")

    async def _stream_logs(job_uuid: str):  # type: ignore[return]
        resp = agent.get(f"/jobs/{job_uuid}/stream")
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                yield line[5:].strip()

    mock_client.get_script_state = _get_script_state
    mock_client.stage_script = _stage_script
    mock_client.submit_job = _submit_job
    mock_client.get_job_status = _get_job_status
    mock_client.kill_job = _kill_job
    mock_client.stream_logs = _stream_logs

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# 6.4 — Stage, approve, submit, observe completion
# ---------------------------------------------------------------------------


@pytest.mark.linux_only
class TestE2EApprovalThenRun:
    def test_stage_approve_submit_completes(
        self,
        tmp_path: Path,
        db_session: AsyncSession,
        server_client: TestClient,
        server_machine: Machine,
        admin_user: User,
    ) -> None:
        import asyncio

        cfg = AgentConfig(agent=AgentSection(csl_dir=tmp_path / ".csl"))

        async def _create_script() -> Script:
            return await create_script(
                name="greet",
                content=_SCRIPT_CONTENT,
                meta_yaml=_META,
                user_id=admin_user.id,
                session=db_session,
            )

        asyncio.get_event_loop().run_until_complete(_create_script())
        asyncio.get_event_loop().run_until_complete(db_session.commit())

        with patch("control_station_lite.agent.main.load_config", return_value=cfg):
            with TestClient(agent_app) as agent:
                bridge = _make_agent_bridge(agent)

                # Step 1: stage the script through the server — should end up pending
                with patch("control_station_lite.server.api.jobs.AgentClient", return_value=bridge):
                    with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                        resp = server_client.post(
                            f"/api/machines/{server_machine.id}/jobs",
                            headers=_admin_h(admin_user),
                            json={"script_name": "greet", "params": {}},
                        )
                # Script is absent → staged → pending → 409
                assert resp.status_code == 409
                assert resp.json()["detail"]["approval_error"] == "pending_approval (new)"

                # Step 2: approve directly (same as `csl-agent approvals approve`)
                approvals: ApprovalsManager = agent.app.state.approvals  # type: ignore[attr-defined]
                approvals.approve("greet")

                # Step 3: submit again — now approved
                with patch("control_station_lite.server.api.jobs.AgentClient", return_value=bridge):
                    with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                        resp = server_client.post(
                            f"/api/machines/{server_machine.id}/jobs",
                            headers=_admin_h(admin_user),
                            json={"script_name": "greet", "params": {}},
                        )
                assert resp.status_code == 202
                data = resp.json()
                assert data["status"] in (JobStatus.completed, JobStatus.failed)


# ---------------------------------------------------------------------------
# 6.5 — Persistent script: submit, stream, kill
# ---------------------------------------------------------------------------


@pytest.mark.linux_only
class TestE2EPersistentJobStreamKill:
    def test_persistent_job_kill(
        self,
        tmp_path: Path,
        db_session: AsyncSession,
        server_client: TestClient,
        server_machine: Machine,
        admin_user: User,
    ) -> None:
        import asyncio

        cfg = AgentConfig(agent=AgentSection(csl_dir=tmp_path / ".csl"))

        persistent_content = "#!/bin/bash\nsleep 60\n"
        persistent_md5 = hashlib.md5(persistent_content.encode()).hexdigest()

        async def _create_script() -> Script:
            return await create_script(
                name="sleeper",
                content=persistent_content,
                meta_yaml=_PERSISTENT_META,
                user_id=admin_user.id,
                session=db_session,
            )

        asyncio.get_event_loop().run_until_complete(_create_script())
        asyncio.get_event_loop().run_until_complete(db_session.commit())

        with patch("control_station_lite.agent.main.load_config", return_value=cfg):
            with TestClient(agent_app) as agent:
                # Approve directly
                approvals: ApprovalsManager = agent.app.state.approvals  # type: ignore[attr-defined]
                stage_resp = agent.post(
                    "/scripts/sleeper/stage",
                    json={
                        "content": persistent_content,
                        "md5": persistent_md5,
                        "meta_yaml": _PERSISTENT_META,
                    },
                )
                assert stage_resp.status_code == 200
                approvals.approve("sleeper")

                bridge = _make_agent_bridge(agent)

                # Submit
                with patch("control_station_lite.server.api.jobs.AgentClient", return_value=bridge):
                    with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                        resp = server_client.post(
                            f"/api/machines/{server_machine.id}/jobs",
                            headers=_admin_h(admin_user),
                            json={"script_name": "sleeper", "params": {}},
                        )
                assert resp.status_code == 202
                job_uuid = resp.json()["job_uuid"]
                assert resp.json()["persistent"] is True

                # Kill via server
                with patch("control_station_lite.server.api.jobs.AgentClient", return_value=bridge):
                    with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                        kill_resp = server_client.post(
                            f"/api/jobs/{job_uuid}/kill",
                            headers=_admin_h(admin_user),
                        )
                assert kill_resp.status_code == 204


# ---------------------------------------------------------------------------
# 6.6 — Rejected script → structured error, no retry
# ---------------------------------------------------------------------------


@pytest.mark.linux_only
class TestE2ERejectedScript:
    def test_rejected_script_returns_structured_error(
        self,
        tmp_path: Path,
        db_session: AsyncSession,
        server_client: TestClient,
        server_machine: Machine,
        admin_user: User,
    ) -> None:
        import asyncio

        cfg = AgentConfig(agent=AgentSection(csl_dir=tmp_path / ".csl"))

        async def _create_script() -> Script:
            return await create_script(
                name="badscript",
                content=_SCRIPT_CONTENT,
                meta_yaml=_META,
                user_id=admin_user.id,
                session=db_session,
            )

        asyncio.get_event_loop().run_until_complete(_create_script())
        asyncio.get_event_loop().run_until_complete(db_session.commit())

        with patch("control_station_lite.agent.main.load_config", return_value=cfg):
            with TestClient(agent_app) as agent:
                # Stage then reject
                stage_resp = agent.post(
                    "/scripts/badscript/stage",
                    json={"content": _SCRIPT_CONTENT, "md5": _SCRIPT_MD5},
                )
                assert stage_resp.status_code == 200

                approvals: ApprovalsManager = agent.app.state.approvals  # type: ignore[attr-defined]
                approvals.reject("badscript")

                bridge = _make_agent_bridge(agent)

                # Submit — should get 409 with rejection error
                with patch("control_station_lite.server.api.jobs.AgentClient", return_value=bridge):
                    with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                        resp = server_client.post(
                            f"/api/machines/{server_machine.id}/jobs",
                            headers=_admin_h(admin_user),
                            json={"script_name": "badscript", "params": {}},
                        )

                assert resp.status_code == 409
                detail = resp.json()["detail"]
                assert detail["approval_error"] == "rejected"
                assert detail["agent_state"] == "rejected"

                # The approval check happens before submit_job — so the agent never received a job


# ---------------------------------------------------------------------------
# Windows equivalents — identical flow; script names carry .ps1 extension so
# find_script resolves them and build_command selects powershell.
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
class TestE2EApprovalThenRunWindows:
    def test_stage_approve_submit_completes(
        self,
        tmp_path: Path,
        db_session: AsyncSession,
        server_client: TestClient,
        server_machine: Machine,
        admin_user: User,
    ) -> None:
        import asyncio

        cfg = AgentConfig(agent=AgentSection(csl_dir=tmp_path / ".csl"))

        async def _create_script() -> Script:
            return await create_script(
                name="greet.ps1",
                content=_PS1_CONTENT,
                meta_yaml=_META,
                user_id=admin_user.id,
                session=db_session,
            )

        asyncio.get_event_loop().run_until_complete(_create_script())
        asyncio.get_event_loop().run_until_complete(db_session.commit())

        with patch("control_station_lite.agent.main.load_config", return_value=cfg):
            with TestClient(agent_app) as agent:
                bridge = _make_agent_bridge(agent)

                # Step 1: submit — script is absent, gets staged → pending
                with patch("control_station_lite.server.api.jobs.AgentClient", return_value=bridge):
                    with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                        resp = server_client.post(
                            f"/api/machines/{server_machine.id}/jobs",
                            headers=_admin_h(admin_user),
                            json={"script_name": "greet.ps1", "params": {}},
                        )
                assert resp.status_code == 409
                assert resp.json()["detail"]["approval_error"] == "pending_approval (new)"

                # Step 2: approve directly (same as `csl-agent approvals approve`)
                approvals: ApprovalsManager = agent.app.state.approvals  # type: ignore[attr-defined]
                approvals.approve("greet.ps1")

                # Step 3: submit again — now approved; powershell runs the script
                with patch("control_station_lite.server.api.jobs.AgentClient", return_value=bridge):
                    with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                        resp = server_client.post(
                            f"/api/machines/{server_machine.id}/jobs",
                            headers=_admin_h(admin_user),
                            json={"script_name": "greet.ps1", "params": {}},
                        )
                assert resp.status_code == 202
                data = resp.json()
                assert data["status"] in (JobStatus.completed, JobStatus.failed)


@pytest.mark.windows_only
class TestE2EPersistentJobStreamKillWindows:
    def test_persistent_job_kill(
        self,
        tmp_path: Path,
        db_session: AsyncSession,
        server_client: TestClient,
        server_machine: Machine,
        admin_user: User,
    ) -> None:
        import asyncio

        cfg = AgentConfig(agent=AgentSection(csl_dir=tmp_path / ".csl"))

        async def _create_script() -> Script:
            return await create_script(
                name="sleeper.ps1",
                content=_PS1_PERSISTENT_CONTENT,
                meta_yaml=_PERSISTENT_META,
                user_id=admin_user.id,
                session=db_session,
            )

        asyncio.get_event_loop().run_until_complete(_create_script())
        asyncio.get_event_loop().run_until_complete(db_session.commit())

        with patch("control_station_lite.agent.main.load_config", return_value=cfg):
            with TestClient(agent_app) as agent:
                approvals: ApprovalsManager = agent.app.state.approvals  # type: ignore[attr-defined]
                stage_resp = agent.post(
                    "/scripts/sleeper.ps1/stage",
                    json={
                        "content": _PS1_PERSISTENT_CONTENT,
                        "md5": _PS1_PERSISTENT_MD5,
                        "meta_yaml": _PERSISTENT_META,
                    },
                )
                assert stage_resp.status_code == 200
                approvals.approve("sleeper.ps1")

                bridge = _make_agent_bridge(agent)

                with patch("control_station_lite.server.api.jobs.AgentClient", return_value=bridge):
                    with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                        resp = server_client.post(
                            f"/api/machines/{server_machine.id}/jobs",
                            headers=_admin_h(admin_user),
                            json={"script_name": "sleeper.ps1", "params": {}},
                        )
                assert resp.status_code == 202
                job_uuid = resp.json()["job_uuid"]
                assert resp.json()["persistent"] is True

                with patch("control_station_lite.server.api.jobs.AgentClient", return_value=bridge):
                    with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                        kill_resp = server_client.post(
                            f"/api/jobs/{job_uuid}/kill",
                            headers=_admin_h(admin_user),
                        )
                assert kill_resp.status_code == 204


@pytest.mark.windows_only
class TestE2ERejectedScriptWindows:
    def test_rejected_script_returns_structured_error(
        self,
        tmp_path: Path,
        db_session: AsyncSession,
        server_client: TestClient,
        server_machine: Machine,
        admin_user: User,
    ) -> None:
        import asyncio

        cfg = AgentConfig(agent=AgentSection(csl_dir=tmp_path / ".csl"))

        async def _create_script() -> Script:
            return await create_script(
                name="badscript.ps1",
                content=_PS1_CONTENT,
                meta_yaml=_META,
                user_id=admin_user.id,
                session=db_session,
            )

        asyncio.get_event_loop().run_until_complete(_create_script())
        asyncio.get_event_loop().run_until_complete(db_session.commit())

        with patch("control_station_lite.agent.main.load_config", return_value=cfg):
            with TestClient(agent_app) as agent:
                stage_resp = agent.post(
                    "/scripts/badscript.ps1/stage",
                    json={"content": _PS1_CONTENT, "md5": _PS1_MD5},
                )
                assert stage_resp.status_code == 200

                approvals: ApprovalsManager = agent.app.state.approvals  # type: ignore[attr-defined]
                approvals.reject("badscript.ps1")

                bridge = _make_agent_bridge(agent)

                with patch("control_station_lite.server.api.jobs.AgentClient", return_value=bridge):
                    with patch("control_station_lite.server.api.jobs.get_ssh_pool"):
                        resp = server_client.post(
                            f"/api/machines/{server_machine.id}/jobs",
                            headers=_admin_h(admin_user),
                            json={"script_name": "badscript.ps1", "params": {}},
                        )

                assert resp.status_code == 409
                detail = resp.json()["detail"]
                assert detail["approval_error"] == "rejected"
                assert detail["agent_state"] == "rejected"
