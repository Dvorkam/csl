# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end test of the real Docker deployment stack (Phase 10).

Unlike the other e2e suites (which wire the app and agent together in-process
via ``TestClient``), this one exercises the *actual* shipped artifacts:

* builds the production image from ``deploy/Dockerfile`` (``prod`` target),
* brings up the two-service stack (app behind nginx) via ``docker compose``,
* drives it over the network exactly as a browser / API client would.

It verifies the things the deploy artifacts are responsible for: migrations run
on start, the app is reachable only through nginx, TLS termination + the
HTTP→HTTPS redirect, the auth rate limit, request-id propagation, stable error
codes, and a full create-admin → login → protected-call round trip.

Skipped automatically when Docker is unavailable. Runs in the ``e2e`` CI job
(Linux only); ``ci.yml`` does not collect ``tests/e2e``. Set
``CSL_E2E_DOCKER="sudo docker"`` to drive a daemon that needs elevation.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml

_DOCKER = os.environ.get("CSL_E2E_DOCKER", "docker").split()
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROJECT = "csl_e2e"
_HTTP_PORT = 18080
_HTTPS_PORT = 18443
_BASE_URL = f"https://localhost:{_HTTPS_PORT}"
_ADMIN_USER = "admin"
_ADMIN_PASS = "e2e-admin-pass"  # noqa: S105 — throwaway credential for a disposable stack


def _docker_available() -> bool:
    if shutil.which(_DOCKER[0]) is None:
        return False
    try:
        return subprocess.run([*_DOCKER, "info"], capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon not available for the deployment-stack e2e"
)


def _write_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    """Write a throwaway self-signed cert/key pair (via ``cryptography``)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _compose_config(work: Path) -> dict:
    """Standalone compose config: real Dockerfile + real nginx.conf, temp state.

    Mirrors ``deploy/docker-compose.yml`` (app expose-only, nginx as the edge)
    but points volumes at *work* and publishes nginx on test ports.
    """
    return {
        "services": {
            "app": {
                "build": {
                    "context": str(_REPO_ROOT),
                    "dockerfile": "deploy/Dockerfile",
                    "target": "prod",
                },
                "image": "csl-e2e:latest",
                "environment": {
                    "CSL_HOST": "0.0.0.0",
                    "CSL_PORT": "8000",
                    "CSL_DATABASE_URL": "sqlite+aiosqlite:////var/lib/csl/db/control-station.sqlite",
                    "CSL_MASTER_KEY_PATH": "/var/lib/csl/secrets/master.key",
                    "CSL_JWT_KEY_PATH": "/var/lib/csl/secrets/jwt.key",
                    "CSL_LOG_LEVEL": "DEBUG",
                    "CSL_COOKIE_SECURE": "false",
                },
                "volumes": [
                    f"{work}/db:/var/lib/csl/db",
                    f"{work}/logs:/var/lib/csl/logs",
                    f"{work}/secrets:/var/lib/csl/secrets:ro",
                ],
                "expose": ["8000"],
            },
            "nginx": {
                "image": "nginx:1.27-alpine",
                "depends_on": ["app"],
                "ports": [f"{_HTTP_PORT}:80", f"{_HTTPS_PORT}:443"],
                "volumes": [
                    f"{_REPO_ROOT}/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro",
                    f"{work}/certs:/etc/nginx/certs:ro",
                ],
            },
        },
    }


def _provision(work: Path) -> Path:
    """Create data dirs, throwaway secrets + cert, and the compose file."""
    for sub in ("db", "logs", "secrets", "certs"):
        (work / sub).mkdir()
    # The prod image runs as the non-root `csl` user (a different uid than the
    # host owner of these bind mounts), so make the writable dirs world-writable
    # and the read-only secrets world-readable. Fine for disposable test state.
    os.chmod(work / "db", 0o777)
    os.chmod(work / "logs", 0o777)

    master = work / "secrets" / "master.key"
    master.write_text(base64.b64encode(os.urandom(32)).decode())
    jwt = work / "secrets" / "jwt.key"
    jwt.write_bytes(os.urandom(64))
    os.chmod(master, 0o644)
    os.chmod(jwt, 0o644)

    _write_self_signed_cert(work / "certs" / "fullchain.pem", work / "certs" / "privkey.pem")

    compose = work / "docker-compose.yml"
    compose.write_text(yaml.safe_dump(_compose_config(work)))
    return compose


def _wait_healthy(timeout: float = 150.0) -> None:
    deadline = time.time() + timeout
    last = "no response"
    with httpx.Client(verify=False, timeout=5.0) as client:
        while time.time() < deadline:
            try:
                resp = client.get(f"{_BASE_URL}/healthz")
                if resp.status_code == 200 and resp.json().get("db") == "ok":
                    return
                last = f"status={resp.status_code} body={resp.text!r}"
            except Exception as exc:  # noqa: BLE001 — startup races surface as connection errors
                last = repr(exc)
            time.sleep(2.0)
    raise RuntimeError(f"stack did not become healthy within {timeout}s; last={last}")


@dataclass
class _Stack:
    base_url: str
    compose_cmd: list[str]

    def exec(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*self.compose_cmd, "exec", "-T", *args],
            capture_output=True,
            text=True,
            timeout=60,
        )


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    work = tmp_path_factory.mktemp("csl-e2e")
    compose_file = _provision(work)
    compose_cmd = [*_DOCKER, "compose", "-p", _PROJECT, "-f", str(compose_file)]

    up = subprocess.run(
        [*compose_cmd, "up", "-d", "--build"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if up.returncode != 0:
        pytest.fail(f"docker compose up failed:\nSTDOUT:\n{up.stdout}\nSTDERR:\n{up.stderr}")

    try:
        _wait_healthy()
        yield _Stack(base_url=_BASE_URL, compose_cmd=compose_cmd)
    finally:
        logs = subprocess.run([*compose_cmd, "logs", "--no-color"], capture_output=True, text=True)
        # Surface container logs on the CI runner for post-mortem debugging.
        print("\n===== app/nginx logs =====\n" + (logs.stdout or "") + (logs.stderr or ""))
        subprocess.run([*compose_cmd, "down", "-v"], capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(verify=False, timeout=10.0, follow_redirects=False) as client:
        yield client


class TestDeploymentStack:
    def test_healthz_through_nginx(self, stack: _Stack, http: httpx.Client) -> None:
        resp = http.get(f"{stack.base_url}/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["db"] == "ok"
        assert body["version"]  # version string is populated from package metadata

    def test_http_redirects_to_https(self, stack: _Stack, http: httpx.Client) -> None:
        resp = http.get(f"http://localhost:{_HTTP_PORT}/healthz")
        assert resp.status_code == 301
        assert resp.headers["location"].startswith("https://")

    def test_request_id_is_propagated(self, stack: _Stack, http: httpx.Client) -> None:
        resp = http.get(f"{stack.base_url}/healthz", headers={"X-Request-ID": "e2e-trace-1"})
        assert resp.headers.get("x-request-id") == "e2e-trace-1"

    def test_bad_login_returns_stable_error_code(self, stack: _Stack, http: httpx.Client) -> None:
        resp = http.post(
            f"{stack.base_url}/api/auth/login",
            json={"username": "nobody", "password": "wrong-password"},
        )
        assert resp.status_code == 401
        assert resp.json()["code"] == "auth.invalid_credentials"

    def test_admin_can_login_and_reach_protected_api(
        self, stack: _Stack, http: httpx.Client
    ) -> None:
        created = stack.exec(
            "app",
            "sh",
            "-c",
            f"printf '{_ADMIN_USER}\\n{_ADMIN_PASS}\\n{_ADMIN_PASS}\\n' | csl-admin create-admin",
        )
        assert "created" in (created.stdout + created.stderr), created.stderr

        login = http.post(
            f"{stack.base_url}/api/auth/login",
            json={"username": _ADMIN_USER, "password": _ADMIN_PASS},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]

        machines = http.get(
            f"{stack.base_url}/api/machines",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert machines.status_code == 200
        assert machines.json() == []

    def test_auth_endpoint_is_rate_limited(self, stack: _Stack, http: httpx.Client) -> None:
        # nginx limits /api/auth/* to 5r/s with burst=10; a rapid flood past the
        # burst is rejected at the edge (default limit_req status is 503).
        statuses = [
            http.post(
                f"{stack.base_url}/api/auth/login",
                json={"username": "x", "password": "y"},
            ).status_code
            for _ in range(20)
        ]
        assert any(code in (429, 503) for code in statuses), statuses
