import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from control_station_lite.agent.config import AgentConfig, AgentSection, IdentitySection
from control_station_lite.agent.main import _AGENT_HOST, app


@pytest.fixture(scope="module")
def client() -> TestClient:
    # Use as context manager so the lifespan (manager initialisation) runs.
    with TestClient(app) as c:
        return c


@pytest.fixture(scope="module")
def healthz_data(client: TestClient) -> dict:  # type: ignore[type-arg]
    return client.get("/healthz").json()


@pytest.fixture(scope="module")
def openapi_schema(client: TestClient) -> dict:  # type: ignore[type-arg]
    return client.get("/openapi.json").json()


class TestHealthz:
    def test_returns_200(self, client: TestClient) -> None:
        assert client.get("/healthz").status_code == 200

    def test_has_version(self, healthz_data: dict) -> None:  # type: ignore[type-arg]
        assert "version" in healthz_data

    def test_running_jobs_is_int(self, healthz_data: dict) -> None:  # type: ignore[type-arg]
        assert isinstance(healthz_data["running_persistent_jobs"], int)

    def test_idle_seconds_is_numeric(self, healthz_data: dict) -> None:  # type: ignore[type-arg]
        assert isinstance(healthz_data["idle_seconds"], float | int)


@pytest.fixture
def isolated_client(tmp_path: Path) -> TestClient:
    """Function-scoped client with isolated filesystem — no real ~/.csl side effects."""
    cfg = AgentConfig(agent=AgentSection(csl_dir=tmp_path / ".csl"))
    with patch("control_station_lite.agent.main.load_config", return_value=cfg):
        with TestClient(app) as c:
            yield c


_TOKEN = "s3cret-agent-token"


@pytest.fixture
def authed_client(tmp_path: Path) -> TestClient:
    """Client whose agent has an api_token configured (auth enforced)."""
    cfg = AgentConfig(
        agent=AgentSection(csl_dir=tmp_path / ".csl"),
        identity=IdentitySection(api_token=_TOKEN),
    )
    with patch("control_station_lite.agent.main.load_config", return_value=cfg):
        with TestClient(app) as c:
            yield c


class TestBearerTokenAuth:
    def test_missing_token_is_401(self, authed_client: TestClient) -> None:
        assert authed_client.get("/healthz").status_code == 401

    def test_wrong_token_is_401(self, authed_client: TestClient) -> None:
        resp = authed_client.get("/healthz", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    def test_malformed_header_is_401(self, authed_client: TestClient) -> None:
        resp = authed_client.get("/healthz", headers={"Authorization": _TOKEN})
        assert resp.status_code == 401

    def test_correct_token_is_200(self, authed_client: TestClient) -> None:
        resp = authed_client.get("/healthz", headers={"Authorization": f"Bearer {_TOKEN}"})
        assert resp.status_code == 200

    def test_enforced_on_non_health_endpoint(self, authed_client: TestClient) -> None:
        assert authed_client.get("/scripts/foo/state").status_code == 401

    def test_no_token_configured_allows_request(self, isolated_client: TestClient) -> None:
        # Legacy config with no api_token runs unauthenticated.
        assert isolated_client.get("/healthz").status_code == 200


class TestScriptStateEndpoint:
    def test_absent_for_unknown_script(self, isolated_client: TestClient) -> None:
        resp = isolated_client.get("/scripts/no_such_script/state")
        assert resp.status_code == 200
        assert resp.json()["state"] == "absent"

    def test_pending_after_stage(self, isolated_client: TestClient) -> None:
        content = "#!/bin/bash\necho hi\n"
        md5 = hashlib.md5(content.encode()).hexdigest()
        isolated_client.post("/scripts/greet/stage", json={"content": content, "md5": md5})
        resp = isolated_client.get("/scripts/greet/state")
        assert resp.json()["state"] == "pending"


class TestStageEndpoint:
    def test_returns_pending_for_new_script(self, isolated_client: TestClient) -> None:
        content = "#!/bin/bash\necho hi\n"
        md5 = hashlib.md5(content.encode()).hexdigest()
        resp = isolated_client.post(
            "/scripts/myscript/stage", json={"content": content, "md5": md5}
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "pending"

    def test_returns_409_when_rejected(self, isolated_client: TestClient) -> None:
        content = "#!/bin/bash\necho hi\n"
        md5 = hashlib.md5(content.encode()).hexdigest()
        isolated_client.post("/scripts/bad/stage", json={"content": content, "md5": md5})
        isolated_client.app.state.approvals.reject("bad")
        resp = isolated_client.post("/scripts/bad/stage", json={"content": content, "md5": md5})
        assert resp.status_code == 409


def _stage_and_approve(client: TestClient, name: str, content: str) -> str:
    md5 = hashlib.md5(content.encode()).hexdigest()
    client.post(f"/scripts/{name}/stage", json={"content": content, "md5": md5})
    client.app.state.approvals.approve(name)
    return md5


class TestSubmitJobEndpoint:
    def test_returns_403_when_unapproved(self, isolated_client: TestClient) -> None:
        resp = isolated_client.post(
            "/jobs",
            json={"job_uuid": "abc-123", "script_name": "not_approved"},
        )
        assert resp.status_code == 403

    def test_expected_md5_mismatch_returns_409(self, isolated_client: TestClient) -> None:
        _stage_and_approve(isolated_client, "drifter", "#!/bin/bash\necho ok\n")
        resp = isolated_client.post(
            "/jobs",
            json={"job_uuid": "u2", "script_name": "drifter", "expected_md5": "deadbeef"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["approval_error"] == "md5_mismatch"

    def test_unexpected_params_returns_422(self, isolated_client: TestClient) -> None:
        # Script has no meta.yaml, so it accepts no parameters.
        _stage_and_approve(isolated_client, "noparams", "#!/bin/bash\necho ok\n")
        resp = isolated_client.post(
            "/jobs",
            json={"job_uuid": "u4", "script_name": "noparams", "params": {"x": "1"}},
        )
        assert resp.status_code == 422
        assert "validation_error" in resp.json()["detail"]

    def test_tampered_script_returns_409_integrity(self, isolated_client: TestClient) -> None:
        _stage_and_approve(isolated_client, "tamper", "#!/bin/bash\necho ok\n")
        paths = isolated_client.app.state.config.agent.to_csl_paths()
        (paths.scripts_dir / "tamper").write_text("malicious\n")
        resp = isolated_client.post(
            "/jobs",
            json={"job_uuid": "u3", "script_name": "tamper"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["approval_error"] == "integrity"

    @pytest.mark.linux_only
    def test_expected_md5_match_runs(self, isolated_client: TestClient) -> None:
        md5 = _stage_and_approve(isolated_client, "matcher", "#!/bin/bash\necho ok\n")
        resp = isolated_client.post(
            "/jobs",
            json={"job_uuid": "u1", "script_name": "matcher", "expected_md5": md5},
        )
        assert resp.status_code == 202

    @pytest.mark.linux_only
    def test_non_persistent_job_writes_log_file(
        self, isolated_client: TestClient, tmp_path: Path
    ) -> None:
        """Non-persistent jobs must write stdout/stderr to logs_dir/{uuid}.log.

        Regression test for the bug where run_script output was discarded and the
        job detail log viewer showed nothing.
        """
        content = "#!/bin/bash\necho hello-from-script\n"
        _stage_and_approve(isolated_client, "logger", content)
        job_uuid = "test-log-uuid-1234"
        resp = isolated_client.post(
            "/jobs",
            json={"job_uuid": job_uuid, "script_name": "logger", "persistent": False},
        )
        assert resp.status_code == 202
        cfg = isolated_client.app.state.config
        log_file = cfg.agent.to_csl_paths().logs_dir / f"{job_uuid}.log"
        assert log_file.exists(), "log file must be written for non-persistent jobs"
        assert "hello-from-script" in log_file.read_text()

    @pytest.mark.linux_only
    def test_non_persistent_log_streamable(
        self, isolated_client: TestClient, tmp_path: Path
    ) -> None:
        """stream endpoint must serve the log file written by a completed non-persistent job."""
        content = "#!/bin/bash\necho streamed-output\n"
        _stage_and_approve(isolated_client, "streamer", content)
        job_uuid = "stream-log-uuid-5678"
        isolated_client.post(
            "/jobs",
            json={"job_uuid": job_uuid, "script_name": "streamer", "persistent": False},
        )
        resp = isolated_client.get(f"/jobs/{job_uuid}/stream")
        assert resp.status_code == 200
        assert "streamed-output" in resp.text

    @pytest.mark.windows_only
    def test_non_persistent_job_writes_log_file_windows(
        self, isolated_client: TestClient, tmp_path: Path
    ) -> None:
        """Windows equivalent: non-persistent .ps1 jobs must write output to logs_dir/{uuid}.log."""
        content = "Write-Host hello-from-ps1\n"
        _stage_and_approve(isolated_client, "logger.ps1", content)
        job_uuid = "test-log-uuid-win-1234"
        resp = isolated_client.post(
            "/jobs",
            json={"job_uuid": job_uuid, "script_name": "logger.ps1", "persistent": False},
        )
        assert resp.status_code == 202
        cfg = isolated_client.app.state.config
        log_file = cfg.agent.to_csl_paths().logs_dir / f"{job_uuid}.log"
        assert log_file.exists(), "log file must be written for non-persistent jobs"
        assert "hello-from-ps1" in log_file.read_text()

    @pytest.mark.windows_only
    def test_non_persistent_log_streamable_windows(
        self, isolated_client: TestClient, tmp_path: Path
    ) -> None:
        """Windows equivalent: stream endpoint must serve the log of a completed non-persistent job."""  # noqa: E501
        content = "Write-Host streamed-output-ps1\n"
        _stage_and_approve(isolated_client, "streamer.ps1", content)
        job_uuid = "stream-log-uuid-win-5678"
        isolated_client.post(
            "/jobs",
            json={"job_uuid": job_uuid, "script_name": "streamer.ps1", "persistent": False},
        )
        resp = isolated_client.get(f"/jobs/{job_uuid}/stream")
        assert resp.status_code == 200
        assert "streamed-output-ps1" in resp.text


class TestKillJobEndpoint:
    def test_unknown_job_returns_404(self, isolated_client: TestClient) -> None:
        resp = isolated_client.delete("/jobs/no-such-job")
        assert resp.status_code == 404


class TestStreamJobLogs:
    def test_unknown_job_returns_404(self, client: TestClient) -> None:
        resp = client.get("/jobs/nonexistent-uuid/stream")
        assert resp.status_code == 404

    def test_404_body_mentions_job_uuid(self, client: TestClient) -> None:
        resp = client.get("/jobs/no-such-job/stream")
        assert "no-such-job" in resp.text


class TestLocalhostBinding:
    def test_agent_host_constant_is_loopback(self) -> None:
        assert _AGENT_HOST == "127.0.0.1"


# Every endpoint that exists on the agent must be listed here.
# If a new endpoint is added to main.py and this set is not updated, the test
# below will fail — forcing an explicit decision and test coverage.
_EXPECTED_ENDPOINTS: set[tuple[str, str]] = {
    ("GET", "/healthz"),
    ("GET", "/scripts/{name}/state"),
    ("POST", "/scripts/{name}/stage"),
    ("POST", "/jobs"),
    ("GET", "/jobs/{job_uuid}"),
    ("DELETE", "/jobs/{job_uuid}"),
    ("GET", "/jobs/{job_uuid}/stream"),
}


class TestOpenApiSchema:
    def test_schema_reachable(self, client: TestClient) -> None:
        assert client.get("/openapi.json").status_code == 200

    def test_endpoint_set_matches_exactly(self, openapi_schema: dict) -> None:  # type: ignore[type-arg]
        actual = {
            (method.upper(), path)
            for path, methods in openapi_schema["paths"].items()
            for method in methods
        }
        assert actual == _EXPECTED_ENDPOINTS


class TestScriptNameValidation:
    """Reserved/unsafe script names are rejected with 422, never a 5xx.

    Regression for a Windows-only crash: a fuzzed reserved device name (e.g.
    NUL) reached the filesystem and raised PermissionError (a 500) from the
    stage endpoint. The name is now validated as a safe cross-platform filename.
    """

    # Note: ".." is normalised away by URL handling before routing, so it can't
    # reach the endpoint via the path — the validator still covers it for
    # non-URL callers (see tests/unit/shared/test_script_name.py).
    @pytest.mark.parametrize("name", ["NUL", "con", "COM1", "trailing."])
    def test_stage_rejects_unsafe_name(self, isolated_client: TestClient, name: str) -> None:
        resp = isolated_client.post(
            f"/scripts/{name}/stage",
            json={"content": "echo hi\n", "md5": hashlib.md5(b"echo hi\n").hexdigest()},
        )
        assert resp.status_code == 422

    @pytest.mark.parametrize("name", ["NUL", "con", "COM1"])
    def test_state_rejects_unsafe_name(self, isolated_client: TestClient, name: str) -> None:
        assert isolated_client.get(f"/scripts/{name}/state").status_code == 422

    def test_safe_name_still_works(self, isolated_client: TestClient) -> None:
        resp = isolated_client.post(
            "/scripts/greet.sh/stage",
            json={"content": "echo hi\n", "md5": hashlib.md5(b"echo hi\n").hexdigest()},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "pending"
