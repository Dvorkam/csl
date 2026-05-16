import pytest
from fastapi.testclient import TestClient

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


class TestStubEndpoints:
    def test_get_script_state_returns_501(self, client: TestClient) -> None:
        assert client.get("/scripts/sleep_machine/state").status_code == 501

    def test_stage_script_returns_501(self, client: TestClient) -> None:
        response = client.post(
            "/scripts/sleep_machine/stage",
            json={"content": "#!/bin/bash\necho hi", "md5": "deadbeef"},
        )
        assert response.status_code == 501

    def test_submit_job_returns_501(self, client: TestClient) -> None:
        response = client.post(
            "/jobs",
            json={"job_uuid": "abc-123", "script_name": "sleep_machine"},
        )
        assert response.status_code == 501


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
