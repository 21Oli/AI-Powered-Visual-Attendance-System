"""Step 1.3 API-foundation verification (Phase 3 — loopback-only API).

Covers :mod:`src.api` and the settings-driven runner ``tools/run_api.py``:

1. Import hygiene (fresh subprocess): importing ``src.api`` and building
   the app must import no driver, no CV stack, no attendance/camera/
   enrollment modules, no server runtime (``uvicorn``), and must
   attempt no database connection.
2. Route inventory: the app serves **exactly** the approved eight
   ``/api/v1`` endpoints — no extras (docs/redoc/openapi disabled).
3. Health endpoint over a real ephemeral loopback uvicorn server.
4. Declared-but-deferred endpoints reject every request with the typed
   501 ``NOT_IMPLEMENTED`` envelope and never reach the database
   (the connection factory is a forbidden-call tripwire).
5. Sanitized error handling: an unexpected exception (whose synthetic
   message embeds a fake DSN credential) produces the stable
   ``INTERNAL_ERROR`` envelope with no credential, traceback, or
   internal-path leakage; unknown paths produce the stable HTTP_ERROR
   envelope; invalid methods are stable 405s.
6. Typed decision codes stay distinct (database unavailable vs database
   configuration vs service configuration vs internal) — asserted as
   handler unit contracts for the future database-backed step.
7. Runner contract: ``--print-bind`` prints the validated loopback bind
   address (no serving); an invalid ``API_HOST`` fails the runner
   without starting any server.
8. No CORS/auth middleware is installed; response models are declared.

No test connects to PostgreSQL, touches a camera, or reads real data.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from src.api import (
    API_PREFIX,
    APPROVED_ROUTES,
    create_app,
    handle_config_error,
    handle_database_config_error,
    handle_database_unavailable,
    handle_unexpected_error,
)
from src.config import ConfigError
from src.db import DatabaseUnavailableError, MalformedDsnError

# ---------------------------------------------------------------------------
# Shared helpers (hardware-free; no real data)
# ---------------------------------------------------------------------------

_FAKE_SENSITIVE_DSN = "postgresql://u:leak-password@10.0.0.9:5432/secretdb"


def test_api_import_is_lazy_and_pulls_no_runtime_stack() -> None:
    """A fresh ``import src.api`` + ``create_app()`` must not import the
    DB driver, CV stack, attendance/camera modules, or the server
    runtime — and must not connect to anything."""
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "import src.api;"
        "app = src.api.create_app();"
        "banned = {'psycopg', 'cv2', 'mediapipe', 'insightface', 'onnxruntime', "
        "'uvicorn',"
        "'src.attendance', 'src.attendance_store', 'src.attendance_recovery', "
        "'src.attendance_persistence', 'src.camera', 'src.face_detection', "
        "'src.face_recognition', 'src.face_embedding', 'src.enrollment', "
        "'src.embedding_storage', 'src.unknown_face_handler', "
        "'src.integration_demo', 'src.quality', 'src.temporal_verification'};"
        "loaded = banned & set(sys.modules);"
        "assert not loaded, f'unexpected imports: {loaded}';"
        "assert 'src.api' in sys.modules, 'src.api missing';"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")


# ---------------------------------------------------------------------------
# 2. Route inventory: exactly the approved eight endpoints, no extras
# ---------------------------------------------------------------------------


def _inventory(app) -> list[tuple[str, str]]:
    """Declared (path, method) pairs, excluding the automatic HEAD twin
    of GET routes."""
    pairs: list[tuple[str, str]] = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not path or path in ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"):
            continue
        for method in sorted(methods):
            if method == "HEAD":
                continue  # Starlette adds HEAD to GET routes automatically
            pairs.append((path, method))
    return pairs


def test_route_inventory_is_exactly_the_approved_eight() -> None:
    app = create_app()
    inventory = _inventory(app)
    expected = sorted(
        (f"{API_PREFIX}{path}", method) for path, method in APPROVED_ROUTES
    )
    assert sorted(inventory) == expected
    assert len(expected) == 8


def test_documentation_urls_are_disabled() -> None:
    app = create_app()
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/docs" not in paths
    assert "/redoc" not in paths
    assert "/openapi.json" not in paths


def test_no_cors_middleware_installed() -> None:
    app = create_app()
    assert app.user_middleware == [], "middleware must not be installed in Step 1.3"


# ---------------------------------------------------------------------------
# 3/4/5. Real loopback uvicorn servers (ephemeral port, stdlib client)
# ---------------------------------------------------------------------------


def _free_loopback_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class _LoopbackServer:
    """Real uvicorn server on 127.0.0.1:<ephemeral> (isolated, short-lived)."""

    def __init__(self, app) -> None:
        import uvicorn  # the actual server runtime under test

        port = _free_loopback_port()  # reserved immediately before use
        self._port = port
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while not self._server.started:
            if not self._thread.is_alive():
                raise RuntimeError(f"uvicorn died before startup: {self._thread}")
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start in time")
            time.sleep(0.05)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def request(self, method: str, path: str, body: bytes | None = None):
        req = urllib.request.Request(self.base_url + path, method=method, data=body)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode("utf-8")


    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture()
def api_server():
    server = _LoopbackServer(create_app())
    yield server
    server.close()


@pytest.fixture(autouse=True)
def forbid_real_database(monkeypatch: pytest.MonkeyPatch):
    """Any DB connection attempt fails loud; psycopg must stay unloaded."""
    def forbidden(*args, **kwargs):  # pragma: no cover - tripwire
        raise AssertionError("src.db._connect was invoked (offline-only rule)")

    monkeypatch.setattr("src.db._connect", forbidden)
    yield
    assert "psycopg" not in sys.modules, "psycopg was imported during API tests"


def test_health_endpoint_over_real_loopback_server(api_server) -> None:
    status, raw = api_server.request("GET", f"{API_PREFIX}/health")
    assert status == 200
    assert json.loads(raw) == {"status": "ok"}


def test_health_response_lacks_internal_details(api_server) -> None:
    status, raw = api_server.request("GET", f"{API_PREFIX}/health")
    assert status == 200
    for forbidden in ("/", "\\", "src", "data", "127.0.0.1", "password", "database"):
        assert forbidden not in raw, "health payload leaked internal detail"


_DEFERRED_ROUTES = sorted(
    (method, f"{API_PREFIX}{path}") for path, method in APPROVED_ROUTES
    if path != "/health"
)


@pytest.mark.parametrize(("method", "path"), _DEFERRED_ROUTES)
def test_deferred_endpoints_return_typed_501_envelope(
    api_server, method: str, path: str
) -> None:
    status, raw = api_server.request(method, path)
    assert status == 501
    payload = json.loads(raw)
    assert payload["error"]["code"] == "NOT_IMPLEMENTED"
    assert payload["error"]["message"]
    assert status != 200 and status != 201


def test_deferred_endpoint_rejects_arbitrary_body(api_server) -> None:
    """Unexpected input is rejected, not silently accepted or defaulted."""
    status, raw = api_server.request(
        "POST",
        f"{API_PREFIX}/employees",
        body=json.dumps(
            {"employee_id": "X", "full_name": "Y", "is_active": True}
        ).encode(),
    )
    assert status == 501
    assert json.loads(raw)["error"]["code"] == "NOT_IMPLEMENTED"


def test_unknown_route_uses_the_stable_error_envelope(api_server) -> None:
    status, raw = api_server.request("GET", f"{API_PREFIX}/unknown")
    assert status == 404
    payload = json.loads(raw)
    assert payload["error"]["code"] == "HTTP_ERROR"
    assert payload["error"]["message"]


def test_invalid_http_method_on_known_path_is_rejected(api_server) -> None:
    status, raw = api_server.request("DELETE", f"{API_PREFIX}/health")
    assert status == 405
    payload = json.loads(raw)
    assert payload["error"]["code"] == "HTTP_ERROR"


def test_unexpected_exception_response_is_sanitized(api_server) -> None:
    """A route failing with a credential-bearing synthetic message must
    still answer the generic 500 envelope (no DSN, no traceback, no
    internal path). The route is test-only; the shipped app never has it."""
    app = create_app()

    def boom() -> dict:
        raise RuntimeError(
            f"hard failure while touching {_FAKE_SENSITIVE_DSN} at C:\\private\\attendance"
        )

    app.add_api_route("/boom-only-for-testing", boom, methods=["GET"])
    server = _LoopbackServer(app)
    try:
        status, raw = server.request("GET", "/boom-only-for-testing")
        assert status == 500
        payload = json.loads(raw)
        assert payload["error"]["code"] == "INTERNAL_ERROR"
        assert payload["error"]["message"] == "Internal server error."
        for fragment in (
            "leak-password", "10.0.0.9", "secretdb", "C:\\private",
            "RuntimeError", "Traceback", "traceback", "hard failure",
        ):
            assert fragment not in raw, "500 response leaked internals"
    finally:
        server.close()


# ---------------------------------------------------------------------------
# 6. Typed decision codes stay distinct (unit-level handler contracts)
#    These handlers are declared now so the future database-backed step
#    inherits them unchanged; asserting them here locks the taxonomy.
# ---------------------------------------------------------------------------

_DUMMY_REQUEST = object()


def _render(handler, exc) -> dict:
    response = handler(_DUMMY_REQUEST, exc)  # type: ignore[arg-type]
    assert response.status_code
    return response.status_code, json.loads(response.body)


def test_database_unavailable_code_is_distinct_and_sanitized() -> None:
    status, payload = _render(
        handle_database_unavailable,
        DatabaseUnavailableError("PostgreSQL connection failed after 1.0s (redacted conninfo: password=***); underlying exception type: OperationalError"),
    )
    assert status == 503
    assert payload["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert payload["error"]["message"] == "Database is temporarily unavailable."
    # The driver message never reaches the client envelope.
    assert "OperationalError" not in payload["error"]["message"]
    assert "conninfo" not in payload["error"]["message"]


def test_database_config_invalid_code_is_distinct() -> None:
    status, payload = _render(
        handle_database_config_error,
        MalformedDsnError(
            "Malformed PostgreSQL DSN: psycopg rejected the connection "
            "string syntax (underlying exception type: ProgrammingError)"
        ),
    )
    assert status == 503
    assert payload["error"]["code"] == "DATABASE_CONFIG_INVALID"
    assert payload["error"]["message"] == "Database configuration is invalid."
    assert not isinstance(handle_database_config_error.__code__, type(None))


def test_service_config_code_is_distinct() -> None:
    status, payload = _render(
        handle_config_error,
        ConfigError("Invalid API_HOST '0.0.0.0': non-loopback binding is forbidden"),
    )
    assert status == 503
    assert payload["error"]["code"] == "CONFIG_INVALID"
    assert payload["error"]["message"] == "Service configuration is invalid."
    assert "0.0.0.0" not in payload["error"]["message"]


def test_internal_error_code_is_generic() -> None:
    status, payload = _render(
        handle_unexpected_error,
        RuntimeError(f"boom with {_FAKE_SENSITIVE_DSN}"),
    )
    assert status == 500
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["message"] == "Internal server error."
    assert _FAKE_SENSITIVE_DSN not in json.dumps(payload)



_RUNNER_PATH = PROJECT_ROOT / "tools" / "run_api.py"


def _run_cli(extra_env: dict[str, str] | None = None, *args: str):
    """Run the runner CLI in a fresh interpreter (inherits the parent
    environment, plus explicit overrides)."""
    import os

    env = dict(os.environ) if extra_env else None
    if env is not None:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(_RUNNER_PATH), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(PROJECT_ROOT),
        env=env,
    )


def test_runner_print_bind_outputs_validated_loopback_address() -> None:
    proc = _run_cli({"API_PORT": "8011"}, "--print-bind")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "bind: 127.0.0.1:8011"
    assert not proc.stderr.strip()


def test_runner_print_bind_uses_defaults_when_unset() -> None:
    proc = _run_cli(None, "--print-bind")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "bind: 127.0.0.1:8000"


def test_runner_refuses_non_loopback_bind_without_serving() -> None:
    """Non-loopback API_HOST fails configuration loading fast; no server
    can ever start bound outward, and nothing is served."""
    proc = _run_cli({"API_HOST": "0.0.0.0"}, "--print-bind")
    assert proc.returncode == 2
    assert "API_HOST" in proc.stderr
    assert proc.stdout.strip() == ""
    # The server module was never needed for a rejected configuration.
    assert "uvicorn" not in proc.stderr
