"""Unit tests for the stable error-code catalogue (Task 9.6)."""

from fastapi import FastAPI, status
from fastapi.testclient import TestClient
from pydantic import BaseModel

from control_station_lite.server.core.errors import (
    CslHTTPException,
    ErrorCode,
    install_error_handlers,
)


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    class Body(BaseModel):
        n: int

    @app.get("/boom")
    def boom() -> None:
        raise CslHTTPException(
            status_code=status.HTTP_409_CONFLICT,
            code=ErrorCode.APPROVAL_PENDING,
            detail="nope",
            extra={"agent_state": "pending"},
        )

    @app.post("/validate")
    def validate(body: Body) -> dict[str, int]:
        return {"n": body.n}

    return app


def test_csl_exception_returns_code_and_detail() -> None:
    with TestClient(_app()) as client:
        resp = client.get("/boom")
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"] == "nope"
    assert body["code"] == "approval.pending"
    assert body["agent_state"] == "pending"


def test_validation_error_carries_validation_code() -> None:
    with TestClient(_app()) as client:
        resp = client.post("/validate", json={"n": "not-an-int"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation.error"
    assert isinstance(body["detail"], list)


def test_error_codes_are_stable_strings() -> None:
    # Values are part of the API contract.
    assert ErrorCode.AUTH_INVALID_CREDENTIALS == "auth.invalid_credentials"
    assert ErrorCode.AGENT_UNREACHABLE == "agent.unreachable"
    assert str(ErrorCode.APPROVAL_REJECTED) == "approval.rejected"


def test_csl_exception_is_httpexception_subclass() -> None:
    from fastapi import HTTPException

    exc = CslHTTPException(400, ErrorCode.VALIDATION_ERROR, "x")
    assert isinstance(exc, HTTPException)
    assert exc.detail == "x"
    assert exc.extra == {}
