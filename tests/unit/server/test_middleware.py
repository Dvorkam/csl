"""Unit tests for correlation-id middleware and propagation (Task 9.5)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from control_station_lite.server.logging_config import REQUEST_ID_HEADER, request_id_var
from control_station_lite.server.middleware import RequestIdMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/probe")
    def probe() -> dict[str, str | None]:
        # Read the contextvar from inside the endpoint to prove it is bound for
        # the whole downstream call, not just the middleware frame.
        return {"seen": request_id_var.get()}

    return app


def test_response_has_generated_request_id() -> None:
    with TestClient(_app()) as client:
        resp = client.get("/probe")
    rid = resp.headers[REQUEST_ID_HEADER]
    assert rid
    assert resp.json()["seen"] == rid


def test_inbound_request_id_is_preserved() -> None:
    with TestClient(_app()) as client:
        resp = client.get("/probe", headers={REQUEST_ID_HEADER: "trace-abc"})
    assert resp.headers[REQUEST_ID_HEADER] == "trace-abc"
    assert resp.json()["seen"] == "trace-abc"


def test_context_is_reset_between_requests() -> None:
    with TestClient(_app()) as client:
        r1 = client.get("/probe")
        r2 = client.get("/probe")
    assert r1.headers[REQUEST_ID_HEADER] != r2.headers[REQUEST_ID_HEADER]
    # Outside any request the contextvar is back to its default.
    assert request_id_var.get() is None
