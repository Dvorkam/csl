"""Unit tests for GET /healthz and _EXPECTED_ENDPOINTS registry."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.db.session import get_session
from control_station_lite.server.main import _EXPECTED_ENDPOINTS, app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_HEALTHY_SESSION_CALLS: list[AsyncMock] = []


async def _healthy_session() -> AsyncGenerator[AsyncSession, None]:
    """Fake session whose execute() succeeds (SELECT 1 returns fine)."""
    mock = AsyncMock(spec=AsyncSession)
    mock.execute.return_value = MagicMock()
    yield mock  # type: ignore[misc]


async def _broken_session() -> AsyncGenerator[AsyncSession, None]:
    """Fake session whose execute() raises to simulate DB down."""
    mock = AsyncMock(spec=AsyncSession)
    mock.execute.side_effect = Exception("connection refused")
    yield mock  # type: ignore[misc]


@pytest.fixture(scope="module")
def client() -> TestClient:
    app.dependency_overrides[get_session] = _healthy_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture(scope="module")
def client_db_down() -> TestClient:
    app.dependency_overrides[get_session] = _broken_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture(scope="module")
def openapi(client: TestClient) -> dict:  # type: ignore[type-arg]
    return client.get("/openapi.json").json()


# ---------------------------------------------------------------------------
# /healthz — happy path
# ---------------------------------------------------------------------------


def test_healthz_status_200(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200


def test_healthz_status_ok(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"


def test_healthz_db_ok(client: TestClient) -> None:
    assert client.get("/healthz").json()["db"] == "ok"


def test_healthz_version_present(client: TestClient) -> None:
    assert "version" in client.get("/healthz").json()


def test_healthz_version_matches_package_version_helper(client: TestClient) -> None:
    from control_station_lite.server.api.health import _package_version

    assert client.get("/healthz").json()["version"] == _package_version()


# ---------------------------------------------------------------------------
# /healthz — degraded (DB unreachable)
# ---------------------------------------------------------------------------


def test_healthz_db_down_returns_200(client_db_down: TestClient) -> None:
    # Health endpoint always returns 200; body signals degraded state.
    assert client_db_down.get("/healthz").status_code == 200


def test_healthz_db_down_status_degraded(client_db_down: TestClient) -> None:
    assert client_db_down.get("/healthz").json()["status"] == "degraded"


def test_healthz_db_down_db_error(client_db_down: TestClient) -> None:
    assert client_db_down.get("/healthz").json()["db"] == "error"


def test_healthz_db_down_version_still_present(client_db_down: TestClient) -> None:
    assert "version" in client_db_down.get("/healthz").json()


# ---------------------------------------------------------------------------
# _EXPECTED_ENDPOINTS vs OpenAPI schema
# ---------------------------------------------------------------------------


def test_expected_endpoints_matches_openapi(openapi: dict) -> None:
    _http_methods = {"GET", "POST", "PUT", "DELETE", "PATCH"}
    actual = {
        (method.upper(), path)
        for path, methods in openapi.get("paths", {}).items()
        for method in methods
        if method.upper() in _http_methods
    }
    assert actual == _EXPECTED_ENDPOINTS
