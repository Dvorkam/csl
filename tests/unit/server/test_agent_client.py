"""Unit tests for server/core/agent_client.py."""

import json
import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from control_station_lite.server.core.agent_client import (
    AgentApprovalError,
    AgentClient,
    AgentClientError,
    AgentNotReachableError,
    AgentValidationError,
)
from control_station_lite.server.core.ssh import SSHConnectionPool
from control_station_lite.server.db.models import Machine
from control_station_lite.shared.models import (
    AgentHealth,
    ApprovalState,
    JobRequest,
    JobStatus,
    JobStatusResponse,
    ScriptDescriptor,
    StageScriptResponse,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _machine(platform: str = "linux") -> Machine:
    m = Machine(
        name="test-pc",
        ssh_host="192.168.1.10",
        ssh_port=22,
        ssh_user="alice",
        ssh_key_encrypted=b"\x00",
        key_fingerprint="SHA256:abc",
        agent_port=36717,
        scripts_dir="/home/alice/.csl/scripts",
        platform=platform,
    )
    return m


def _pool(local_port: int = 54321) -> SSHConnectionPool:
    pool = MagicMock(spec=SSHConnectionPool)
    listener = MagicMock()
    listener.close = MagicMock()
    listener.wait_closed = AsyncMock()
    listener.get_port.return_value = local_port
    pool.open_tunnel = AsyncMock(return_value=(listener, local_port))
    pool.get_connection = AsyncMock(return_value=MagicMock())
    return pool


def _http(responses: dict[str, tuple[int, object]]) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient backed by a MockTransport.

    *responses* maps ``"METHOD /path"`` to ``(status_code, body)``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        if key in responses:
            code, body = responses[key]
            content = json.dumps(body).encode() if not isinstance(body, bytes) else body
            return httpx.Response(code, content=content)
        return httpx.Response(404, content=b'{"detail":"not found"}')

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:54321")


_HEALTH_JSON = {"version": "0.1.1", "running_persistent_jobs": 0, "idle_seconds": 12.5}
_DESCRIPTOR_JSON = {
    "name": "hello",
    "state": "approved",
    "persistent": False,
    "approved_md5": "abc123",
    "pending_md5": None,
}
_STAGE_JSON = {"name": "hello", "state": "pending"}
_JOB_STATUS_JSON = {
    "job_uuid": "uuid-1",
    "script_name": "hello",
    "status": "running",
    "persistent": False,
    "started_at": datetime(2026, 1, 1, 10, 0).isoformat(),
    "ended_at": None,
    "exit_code": None,
}


@pytest.fixture
def client_ctx():
    """Return an AgentClient already entered as a context manager."""
    pool = _pool()
    http = _http(
        {
            "GET /healthz": (200, _HEALTH_JSON),
            "GET /scripts/hello/state": (200, _DESCRIPTOR_JSON),
            "POST /scripts/hello/stage": (200, _STAGE_JSON),
            "POST /jobs": (200, _JOB_STATUS_JSON),
            "DELETE /jobs/uuid-1": (204, {}),
        }
    )
    machine = _machine()
    return AgentClient(machine, os.urandom(32), pool, _http_client=http)


# ---------------------------------------------------------------------------
# Context manager lifecycle
# ---------------------------------------------------------------------------


async def test_enter_opens_tunnel() -> None:
    pool = _pool()
    async with AgentClient(_machine(), os.urandom(32), pool, _http_client=_http({})):
        pass
    pool.open_tunnel.assert_called_once()


def test_auth_headers_empty_when_no_token() -> None:
    client = AgentClient(_machine(), os.urandom(32), _pool())
    assert client._auth_headers() == {}


def test_auth_headers_bearer_when_token_set() -> None:
    machine = _machine()
    machine.agent_token_encrypted = b"ciphertext"
    client = AgentClient(machine, os.urandom(32), _pool())
    with (
        patch("control_station_lite.server.core.agent_client.get_settings"),
        patch("control_station_lite.server.core.agent_client.decrypt", return_value=b"my-token"),
    ):
        assert client._auth_headers() == {"Authorization": "Bearer my-token"}


def test_auth_headers_include_correlation_id() -> None:
    from control_station_lite.server.logging_config import REQUEST_ID_HEADER, request_id_var

    client = AgentClient(_machine(), os.urandom(32), _pool())
    token = request_id_var.set("trace-xyz")
    try:
        headers = client._auth_headers()
    finally:
        request_id_var.reset(token)
    assert headers == {REQUEST_ID_HEADER: "trace-xyz"}


async def test_exit_closes_listener() -> None:
    pool = _pool()
    listener = pool.open_tunnel.return_value[0]
    async with AgentClient(_machine(), os.urandom(32), pool, _http_client=_http({})):
        pass
    listener.close.assert_called_once()
    listener.wait_closed.assert_called_once()


async def test_exit_does_not_close_injected_http_client() -> None:
    """Injected clients are owned by the caller — we must not close them."""
    pool = _pool()
    http = _http({})
    async with AgentClient(_machine(), os.urandom(32), pool, _http_client=http):
        pass
    assert not http.is_closed


# ---------------------------------------------------------------------------
# ensure_agent_running
# ---------------------------------------------------------------------------


async def test_ensure_agent_running_returns_if_already_healthy() -> None:
    pool = _pool()
    http = _http({"GET /healthz": (200, _HEALTH_JSON)})
    async with AgentClient(_machine(), os.urandom(32), pool, _http_client=http) as c:
        await c.ensure_agent_running()
    pool.get_connection.assert_not_called()  # no start command needed


async def test_ensure_agent_running_issues_start_command() -> None:
    pool = _pool()
    ssh_conn = pool.get_connection.return_value
    ssh_conn.run = AsyncMock()
    call_count = 0

    def health_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # Fail first ping; succeed on retry
        if call_count == 1:
            return httpx.Response(500)
        return httpx.Response(200, content=json.dumps(_HEALTH_JSON).encode())

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(health_handler),
        base_url="http://127.0.0.1:54321",
    )
    with patch("control_station_lite.server.core.agent_client.asyncio.sleep", AsyncMock()):
        async with AgentClient(_machine("linux"), os.urandom(32), pool, _http_client=http) as c:
            await c.ensure_agent_running()

    ssh_conn.run.assert_called_once()
    cmd = ssh_conn.run.call_args[0][0]
    assert "systemctl" in cmd


async def test_ensure_agent_running_windows_command() -> None:
    pool = _pool()
    ssh_conn = pool.get_connection.return_value
    ssh_conn.run = AsyncMock()
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(500)
        return httpx.Response(200, content=json.dumps(_HEALTH_JSON).encode())

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:54321"
    )
    with patch("control_station_lite.server.core.agent_client.asyncio.sleep", AsyncMock()):
        async with AgentClient(_machine("windows"), os.urandom(32), pool, _http_client=http) as c:
            await c.ensure_agent_running()

    cmd = ssh_conn.run.call_args[0][0]
    assert "schtasks" in cmd


async def test_ensure_agent_running_raises_if_never_healthy() -> None:
    pool = _pool()
    ssh_conn = pool.get_connection.return_value
    ssh_conn.run = AsyncMock()
    http = _http({"GET /healthz": (500, {})})
    with patch("control_station_lite.server.core.agent_client.asyncio.sleep", AsyncMock()):
        async with AgentClient(_machine(), os.urandom(32), pool, _http_client=http) as c:
            with pytest.raises(AgentNotReachableError):
                await c.ensure_agent_running()


async def test_ensure_agent_running_unknown_platform_raises() -> None:
    pool = _pool()
    http = _http({"GET /healthz": (500, {})})
    async with AgentClient(_machine("freebsd"), os.urandom(32), pool, _http_client=http) as c:
        with pytest.raises(AgentClientError, match="freebsd"):
            await c.ensure_agent_running()


# ---------------------------------------------------------------------------
# Typed API methods
# ---------------------------------------------------------------------------


async def test_get_health(client_ctx: AgentClient) -> None:
    async with client_ctx as c:
        result = await c.get_health()
    assert isinstance(result, AgentHealth)
    assert result.version == "0.1.1"
    assert result.running_persistent_jobs == 0


async def test_get_script_state(client_ctx: AgentClient) -> None:
    async with client_ctx as c:
        result = await c.get_script_state("hello")
    assert isinstance(result, ScriptDescriptor)
    assert result.name == "hello"
    assert result.state == ApprovalState.approved


async def test_stage_script(client_ctx: AgentClient) -> None:
    async with client_ctx as c:
        result = await c.stage_script("hello", "#!/bin/bash\necho hi", "abc123", None)
    assert isinstance(result, StageScriptResponse)
    assert result.state == ApprovalState.pending


async def test_submit_job(client_ctx: AgentClient) -> None:
    req = JobRequest(job_uuid="uuid-1", script_name="hello")
    async with client_ctx as c:
        result = await c.submit_job(req)
    assert isinstance(result, JobStatusResponse)
    assert result.status == JobStatus.running


async def test_submit_job_409_raises_approval_error() -> None:
    pool = _pool()
    detail = {"approval_error": "md5_mismatch", "agent_state": "approved", "detail": "drift"}
    http = _http({"POST /jobs": (409, {"detail": detail})})
    async with AgentClient(_machine(), os.urandom(32), pool, _http_client=http) as c:
        with pytest.raises(AgentApprovalError) as exc_info:
            await c.submit_job(JobRequest(job_uuid="uuid-1", script_name="hello"))
    assert exc_info.value.approval_error == "md5_mismatch"
    assert exc_info.value.agent_state == "approved"


async def test_submit_job_422_raises_validation_error() -> None:
    pool = _pool()
    http = _http({"POST /jobs": (422, {"detail": {"validation_error": "unknown parameter 'x'"}})})
    async with AgentClient(_machine(), os.urandom(32), pool, _http_client=http) as c:
        with pytest.raises(AgentValidationError, match="unknown parameter"):
            await c.submit_job(JobRequest(job_uuid="uuid-1", script_name="hello"))


async def test_kill_job(client_ctx: AgentClient) -> None:
    async with client_ctx as c:
        await c.kill_job("uuid-1")  # should not raise


async def test_kill_job_http_error_propagates() -> None:
    pool = _pool()
    http = _http({"DELETE /jobs/uuid-missing": (404, {"detail": "not found"})})
    async with AgentClient(_machine(), os.urandom(32), pool, _http_client=http) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.kill_job("uuid-missing")


# ---------------------------------------------------------------------------
# stream_logs
# ---------------------------------------------------------------------------


async def test_stream_logs_yields_data_lines() -> None:
    pool = _pool()
    sse_body = b"data: line one\n\ndata: line two\n\nignored\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_body)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:54321"
    )
    async with AgentClient(_machine(), os.urandom(32), pool, _http_client=http) as c:
        lines = [line async for line in c.stream_logs("uuid-1")]

    assert lines == ["line one", "line two"]


async def test_stream_logs_skips_non_data_lines() -> None:
    pool = _pool()
    sse_body = b"event: log\ndata: hello\n\ncomment\ndata: world\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_body)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:54321"
    )
    async with AgentClient(_machine(), os.urandom(32), pool, _http_client=http) as c:
        lines = [line async for line in c.stream_logs("uuid-1")]

    assert lines == ["hello", "world"]
