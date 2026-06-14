"""Guard test: every state-mutating API route must be audit-instrumented.

Same spirit as the ``_EXPECTED_ENDPOINTS`` OpenAPI guard. Walks the FastAPI
route table and asserts that every mutating route (POST/PUT/DELETE) in the JSON
API calls :func:`record_audit`, unless it is on the explicit exempt allowlist.
"""

import inspect

from fastapi.routing import APIRoute

from control_station_lite.server.main import app

_MUTATING_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

# Routes that legitimately mutate state but are exempt from audit logging.
# Keep this list short and justified.
_EXEMPT: set[tuple[str, str]] = {
    # Token rotation: high-frequency, carries no user intent, and a successful
    # rotation is implied by the preceding login (which IS audited).
    ("POST", "/api/auth/refresh"),
}

# Marker the audit helper is called by; endpoints reference it by name.
_AUDIT_MARKER = "record_audit"


def _audit_source(endpoint: object) -> str:
    """Source of the endpoint plus any same-module helper it references by name.

    Endpoints may delegate the actual mutation (and its ``record_audit`` call) to
    a shared helper in the same module (e.g. ``register_machine`` →
    ``register_machine_from_input``). Following one level keeps the guard honest
    for that pattern without weakening it to module-wide scope.
    """
    src = inspect.getsource(endpoint)  # type: ignore[arg-type]
    module = inspect.getmodule(endpoint)
    if module is None:
        return src
    for name in dir(module):
        if name == getattr(endpoint, "__name__", None) or name not in src:
            continue
        obj = getattr(module, name)
        if inspect.isfunction(obj) and inspect.getmodule(obj) is module:
            try:
                src += "\n" + inspect.getsource(obj)
            except OSError:
                pass
    return src


def _mutating_api_routes() -> list[tuple[str, str, object]]:
    found: list[tuple[str, str, object]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.include_in_schema:
            continue  # web/HTML routes are out of scope for this guard
        for method in route.methods or set():
            if method in _MUTATING_METHODS:
                found.append((method, route.path, route.endpoint))
    return found


def test_every_mutating_route_is_audited() -> None:
    missing: list[str] = []
    for method, path, endpoint in _mutating_api_routes():
        if (method, path) in _EXEMPT:
            continue
        if _AUDIT_MARKER not in _audit_source(endpoint):
            missing.append(f"{method} {path}")
    assert not missing, (
        "These mutating routes are not audit-instrumented (call record_audit or "
        f"add them to _EXEMPT with justification): {sorted(missing)}"
    )


def test_exempt_routes_still_exist() -> None:
    """Stale allowlist entries must fail loudly so the exemptions stay honest."""
    live = {(m, p) for m, p, _ in _mutating_api_routes()}
    stale = _EXEMPT - live
    assert not stale, f"Exempt routes no longer exist: {sorted(stale)}"
