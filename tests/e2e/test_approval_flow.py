# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end: approval flow without the control station.

Exercises the full HTTP API path:
  POST /scripts/{name}/stage   → state becomes pending
  GET  /scripts/{name}/state   → confirms pending
  (approve via ApprovalsManager directly — same as `csl-agent approvals approve`)
  GET  /scripts/{name}/state   → confirms approved
  POST /jobs                   → executes the script; returns completed

All filesystem operations are redirected to tmp_path; no real ~/.csl is touched.

Linux tests use a plain bash script (no extension; resolved by find_script via the
empty-extension fallback and run with bash).  Windows tests stage the script under a
.ps1 name so that find_script resolves the extension and build_command picks powershell.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from control_station_lite.agent.config import AgentConfig, AgentSection
from control_station_lite.agent.main import app

_SCRIPT_CONTENT = "#!/bin/bash\necho hello\n"
_SCRIPT_MD5 = hashlib.md5(_SCRIPT_CONTENT.encode()).hexdigest()

_PS1_CONTENT = 'Write-Output "hello"\n'
_PS1_MD5 = hashlib.md5(_PS1_CONTENT.encode()).hexdigest()


@pytest.fixture
def agent(tmp_path: Path) -> TestClient:
    """In-process agent with isolated filesystem."""
    cfg = AgentConfig(agent=AgentSection(csl_dir=tmp_path / ".csl"))
    with patch("control_station_lite.agent.main.load_config", return_value=cfg):
        with TestClient(app) as client:
            yield client


@pytest.mark.linux_only
class TestApprovalFlow:
    def test_initial_state_is_absent(self, agent: TestClient) -> None:
        resp = agent.get("/scripts/greet/state")
        assert resp.status_code == 200
        assert resp.json()["state"] == "absent"

    def test_stage_moves_to_pending(self, agent: TestClient) -> None:
        resp = agent.post(
            "/scripts/greet/stage",
            json={"content": _SCRIPT_CONTENT, "md5": _SCRIPT_MD5},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "pending"

    def test_state_is_pending_after_stage(self, agent: TestClient) -> None:
        agent.post(
            "/scripts/greet/stage",
            json={"content": _SCRIPT_CONTENT, "md5": _SCRIPT_MD5},
        )
        resp = agent.get("/scripts/greet/state")
        assert resp.json()["state"] == "pending"

    def test_job_rejected_while_pending(self, agent: TestClient) -> None:
        agent.post(
            "/scripts/greet/stage",
            json={"content": _SCRIPT_CONTENT, "md5": _SCRIPT_MD5},
        )
        resp = agent.post(
            "/jobs",
            json={"job_uuid": str(uuid.uuid4()), "script_name": "greet"},
        )
        assert resp.status_code == 403

    def test_approve_and_run(self, agent: TestClient) -> None:
        agent.post(
            "/scripts/greet/stage",
            json={"content": _SCRIPT_CONTENT, "md5": _SCRIPT_MD5},
        )
        agent.app.state.approvals.approve("greet")

        resp = agent.get("/scripts/greet/state")
        assert resp.json()["state"] == "approved"

        job_uuid = str(uuid.uuid4())
        resp = agent.post(
            "/jobs",
            json={"job_uuid": job_uuid, "script_name": "greet"},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["job_uuid"] == job_uuid
        assert data["script_name"] == "greet"
        assert data["status"] == "completed"
        assert data["exit_code"] == 0
        assert data["persistent"] is False

    def test_update_pending_blocks_run(self, agent: TestClient) -> None:
        agent.post(
            "/scripts/greet/stage",
            json={"content": _SCRIPT_CONTENT, "md5": _SCRIPT_MD5},
        )
        agent.app.state.approvals.approve("greet")

        new_content = "#!/bin/bash\necho updated\n"
        new_md5 = hashlib.md5(new_content.encode()).hexdigest()
        resp = agent.post(
            "/scripts/greet/stage",
            json={"content": new_content, "md5": new_md5},
        )
        assert resp.json()["state"] == "update_pending"

        resp = agent.post(
            "/jobs",
            json={"job_uuid": str(uuid.uuid4()), "script_name": "greet"},
        )
        assert resp.status_code == 403

    def test_auto_approve_runs_immediately(self, agent: TestClient) -> None:
        agent.app.state.approvals._auto_approve.add("auto_script")

        resp = agent.post(
            "/scripts/auto_script/stage",
            json={"content": _SCRIPT_CONTENT, "md5": _SCRIPT_MD5},
        )
        assert resp.json()["state"] == "approved"

        resp = agent.post(
            "/jobs",
            json={"job_uuid": str(uuid.uuid4()), "script_name": "auto_script"},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "completed"


# ---------------------------------------------------------------------------
# Windows — identical flow; script name carries .ps1 extension so that
# find_script resolves it and build_command selects powershell.
# ---------------------------------------------------------------------------

_PS1_NAME = "greet.ps1"
_PS1_AUTO_NAME = "auto_script.ps1"


@pytest.mark.windows_only
class TestApprovalFlowWindows:
    def test_initial_state_is_absent(self, agent: TestClient) -> None:
        resp = agent.get(f"/scripts/{_PS1_NAME}/state")
        assert resp.status_code == 200
        assert resp.json()["state"] == "absent"

    def test_stage_moves_to_pending(self, agent: TestClient) -> None:
        resp = agent.post(
            f"/scripts/{_PS1_NAME}/stage",
            json={"content": _PS1_CONTENT, "md5": _PS1_MD5},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "pending"

    def test_state_is_pending_after_stage(self, agent: TestClient) -> None:
        agent.post(
            f"/scripts/{_PS1_NAME}/stage",
            json={"content": _PS1_CONTENT, "md5": _PS1_MD5},
        )
        resp = agent.get(f"/scripts/{_PS1_NAME}/state")
        assert resp.json()["state"] == "pending"

    def test_job_rejected_while_pending(self, agent: TestClient) -> None:
        agent.post(
            f"/scripts/{_PS1_NAME}/stage",
            json={"content": _PS1_CONTENT, "md5": _PS1_MD5},
        )
        resp = agent.post(
            "/jobs",
            json={"job_uuid": str(uuid.uuid4()), "script_name": _PS1_NAME},
        )
        assert resp.status_code == 403

    def test_approve_and_run(self, agent: TestClient) -> None:
        agent.post(
            f"/scripts/{_PS1_NAME}/stage",
            json={"content": _PS1_CONTENT, "md5": _PS1_MD5},
        )
        agent.app.state.approvals.approve(_PS1_NAME)

        resp = agent.get(f"/scripts/{_PS1_NAME}/state")
        assert resp.json()["state"] == "approved"

        job_uuid = str(uuid.uuid4())
        resp = agent.post(
            "/jobs",
            json={"job_uuid": job_uuid, "script_name": _PS1_NAME},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["job_uuid"] == job_uuid
        assert data["script_name"] == _PS1_NAME
        assert data["status"] == "completed"
        assert data["exit_code"] == 0
        assert data["persistent"] is False

    def test_update_pending_blocks_run(self, agent: TestClient) -> None:
        agent.post(
            f"/scripts/{_PS1_NAME}/stage",
            json={"content": _PS1_CONTENT, "md5": _PS1_MD5},
        )
        agent.app.state.approvals.approve(_PS1_NAME)

        new_content = 'Write-Output "updated"\n'
        new_md5 = hashlib.md5(new_content.encode()).hexdigest()
        resp = agent.post(
            f"/scripts/{_PS1_NAME}/stage",
            json={"content": new_content, "md5": new_md5},
        )
        assert resp.json()["state"] == "update_pending"

        resp = agent.post(
            "/jobs",
            json={"job_uuid": str(uuid.uuid4()), "script_name": _PS1_NAME},
        )
        assert resp.status_code == 403

    def test_auto_approve_runs_immediately(self, agent: TestClient) -> None:
        agent.app.state.approvals._auto_approve.add(_PS1_AUTO_NAME)

        resp = agent.post(
            f"/scripts/{_PS1_AUTO_NAME}/stage",
            json={"content": _PS1_CONTENT, "md5": _PS1_MD5},
        )
        assert resp.json()["state"] == "approved"

        resp = agent.post(
            "/jobs",
            json={"job_uuid": str(uuid.uuid4()), "script_name": _PS1_AUTO_NAME},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "completed"
