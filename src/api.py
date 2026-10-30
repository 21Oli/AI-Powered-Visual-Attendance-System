"""Phase 3 Step 1.3 — API foundation (loopback-only, no authentication).

Implements only the Step 1.3 deliverables: the FastAPI application
structure, the settings-driven loopback binding contract, the approved
eight-endpoint route inventory, and sanitized error handling. Database-
backed and attendance-mutating behavior is deliberately **not yet
implemented** — the seven non-health approved endpoints are declared
with their approved methods and reply with a typed, documented
``501 NOT_IMPLEMENTED`` envelope until their authorized later step.

Design rules
------------
* **Loopback navigation only.** The bind address/port come from the
  existing validated configuration (:mod:`src.config` —
  ``AppSettings.api_host``/``api_port``); this module never duplicates
  or bypasses that validation, and the CLI runner (:mod:`tools.run_api`)
  feeds the values into uvicorn unchanged.
* **No database activity.** Importing this module or creating the app
  connects to no PostgreSQL instance. ``src.db`` is imported solely for
  its (already side-effect-free) typed exceptions so that error
  handlers exist ahead of the future database-backed steps; the
  connection factory itself is never called by any route in this step.
* **No attendance/camera/biometric runtime.** None of the recognition,
  camera, enrollment, embedding, journal, or demo modules are imported
  here (subprocess-proven in tests).
* **Sanitized error envelope.** Every client-facing error is the stable
  ``{"error": {"code": <machine_code>, "message": <sanitized_human>}}``
  mapping. Raw exception messages, tracebacks, DSNs/credentials,
  internal filesystem paths, and employee/biometric data never appear
  in responses; server-side logging keeps trusted diagnostics.
* **Stable decision/persistence codes.** Error ``code`` values are
  machine-readable and deliberately separate from human ``message``
  text (Phase 0 sign-off contract); persistence outcomes remain a
  later-step concern.
* **No CORS, no authentication.** No middleware (CORS or otherwise) is
  installed: the development API must not be network-exposed, and
  loopback-only binding is a development safety measure — explicitly
  not a production security boundary. The FastAPI documentation URLs
  (``/openapi.json``, ``/docs``, ``/redoc``) are disabled so the served
  route inventory is exactly the approved eight endpoints. The
  request/response models are still declared in code; a later,
  explicitly authorized step may re-enable a schema endpoint.
* **No automatic retries.** Nothing here retries any future persisted
  write; the four-outcome persistence contract belongs to the checkout
  step.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
# The *base* Starlette HTTPException is registered as the handler key:
# the framework raises its own base class for 404/405 (router-level),
# and the MRO-based handler lookup only walks the raised type's bases.
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.config import AppSettings, ConfigError, load_settings
from src.db import DatabaseUnavailableError, MalformedDsnError
from src.logging_setup import get_logger

__all__ = [
    "API_PREFIX",
    "APPROVED_ROUTES",
    "ApprovedRoute",
    "ErrorBody",
    "ErrorResponse",
    "HealthResponse",
    "NOT_IMPLEMENTED_CODE",
    "create_app",
    "build_server_config",
    "run_server",
]

_logger = get_logger(__name__)

#: The approved API namespace: every endpoint lives under this prefix.
API_PREFIX: str = "/api/v1"

#: Stable machine code returned by the declared-but-deferred endpoints.
NOT_IMPLEMENTED_CODE: str = "NOT_IMPLEMENTED"

_NOT_IMPLEMENTED_MESSAGE: str = (
    "This endpoint is intentionally not implemented in the API-foundation "
    "step; its behavior arrives with an authorized later step."
)

_INTERNAL_ERROR_CODE: str = "INTERNAL_ERROR"
_INTERNAL_ERROR_MESSAGE: str = "Internal server error."


# ---------------------------------------------------------------------------
# Approved route inventory (exactly these eight; never more)
# ---------------------------------------------------------------------------

ApprovedRoute = tuple[str, str]  # (path without prefix, HTTP method)

APPROVED_ROUTES: tuple[ApprovedRoute, ...] = (
    ("/health", "GET"),
    ("/employees", "POST"),
    ("/employees", "GET"),
    ("/employees/{employee_id}", "GET"),
    ("/employees/{employee_id}", "PATCH"),
    ("/employees/{employee_id}/deactivate", "POST"),
    ("/attendance", "GET"),
    ("/attendance/{employee_id}/checkout", "POST"),
)


# ---------------------------------------------------------------------------
# Typed response models (explicit contracts; no raw serialization)
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Minimal service-health payload: no paths, credentials, employees,
    connection details, or internal exception information."""

    status: Literal["ok"] = "ok"


class ErrorBody(BaseModel):
    """Machine-readable ``code`` kept separate from the human ``message``
    (both sanitized; the code is stable API surface)."""

    code: str
    message: str


class ErrorResponse(BaseModel):
    """The one and only client-facing error envelope."""

    error: ErrorBody


def _error_json(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error=ErrorBody(code=code, message=message)).model_dump(),
    )


def _not_implemented_response() -> JSONResponse:
    return _error_json(501, NOT_IMPLEMENTED_CODE, _NOT_IMPLEMENTED_MESSAGE)


# ---------------------------------------------------------------------------
# Sanitized exception handlers (client-facing messages are fixed constants)
# ---------------------------------------------------------------------------


def handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Stable envelope for :class:`HTTPException`.

    The message text is always something this codebase controls: when a
    handler supplies a detail string, it is already a safe constant;
    otherwise a generic fallback is used. Never a driver, path, DSN, or
    credential value.
    """
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail.strip() else ""
    message = detail.strip() if detail else "Resource could not be retrieved."
    if not message.endswith("."):
        message = f"{message}."
    return _error_json(int(exc.status_code), "HTTP_ERROR", message)


def handle_request_validation(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Unprocessable input: sanitized field surfaces only — the location
    paths and the validation reason, never the caller's raw input
    values (pydantic's error objects can echo them; they are dropped)."""
    fields = [
        {
            "field": ".".join(str(loc) for loc in err.get("loc", ())),
            "message": err.get("msg", "validation failed"),
        }
        for err in exc.errors()
    ]
    body = ErrorResponse(
        error=ErrorBody(code="REQUEST_INVALID", message="Request validation failed.")
    ).model_dump()
    body["fields"] = fields
    return JSONResponse(status_code=422, content=body)


def handle_config_error(request: Request, exc: ConfigError) -> JSONResponse:
    """Invalid service configuration — never confused with an offline
    database, and never exposing the configuration value itself."""
    _logger.warning(
        "rejected request: invalid service configuration (%s)", type(exc).__name__
    )
    return _error_json(503, "CONFIG_INVALID", "Service configuration is invalid.")


def handle_database_unavailable(
    request: Request, exc: DatabaseUnavailableError
) -> JSONResponse:
    """PostgreSQL could not be reached (availability-class failures only,
    with the connection layer's redaction discipline)."""
    _logger.warning("rejected request: postgres unavailable (%s)", type(exc).__name__)
    return _error_json(503, "DATABASE_UNAVAILABLE", "Database is temporarily unavailable.")


def handle_database_config_error(
    request: Request, exc: MalformedDsnError
) -> JSONResponse:
    """Separate, deliberate code for a malformed database DSN (a
    caller-value config contract failure) — never folded into
    ``DATABASE_UNAVAILABLE``."""
    _logger.warning(
        "rejected request: invalid database configuration (%s)", type(exc).__name__
    )
    return _error_json(
        503, "DATABASE_CONFIG_INVALID", "Database configuration is invalid."
    )


def handle_unexpected_error(request: Request, exc: BaseException) -> JSONResponse:
    """Last-resort 500: the client sees only a generic message; trusted
    diagnostics are logged server-side and never returned."""
    _logger.exception("unhandled error while handling a request")
    return _error_json(500, _INTERNAL_ERROR_CODE, _INTERNAL_ERROR_MESSAGE)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def _register_declared_endpoints(app: FastAPI) -> None:
    """Register the seven approved-but-deferred endpoints (full paths).

    Routes are registered directly on the application router with their
    complete approved paths (no sub-router wrapping): the served route
    inventory is flat and introspectable across FastAPI versions. Each
    carries its approved path and method so the route inventory already
    matches the approved surface, and each rejects *every* request with
    the typed 501 envelope until its authorized implementing step.
    Handlers take no request parameters: nothing about a request is
    trusted or interpreted yet, so no validation or business contract is
    claimed prematurely.
    """
    register = app.router

    @register.post(f"{API_PREFIX}/employees")
    def create_employee_endpoint() -> JSONResponse:
        return _not_implemented_response()

    @register.get(f"{API_PREFIX}/employees")
    def list_employees_endpoint() -> JSONResponse:
        return _not_implemented_response()

    @register.get(f"{API_PREFIX}/employees/{{employee_id}}")
    def get_employee_endpoint(employee_id: str) -> JSONResponse:
        return _not_implemented_response()

    @register.patch(f"{API_PREFIX}/employees/{{employee_id}}")
    def update_employee_endpoint(employee_id: str) -> JSONResponse:
        return _not_implemented_response()

    @register.post(f"{API_PREFIX}/employees/{{employee_id}}/deactivate")
    def deactivate_employee_endpoint(employee_id: str) -> JSONResponse:
        return _not_implemented_response()

    @register.get(f"{API_PREFIX}/attendance")
    def list_attendance_endpoint() -> JSONResponse:
        return _not_implemented_response()

    @register.post(f"{API_PREFIX}/attendance/{{employee_id}}/checkout")
    def checkout_endpoint(employee_id: str) -> JSONResponse:
        return _not_implemented_response()


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Create the Phase 3 API application (no server, no connections).

    Parameters
    ----------
    settings:
        Optional acknowledged settings; the app itself never reads a
        DSN or binds a socket — binding happens outside this module
        (:func:`build_server_config` / :mod:`tools.run_api`).
    """
    _ = settings  # unused by the foundation app; part of the runner contract

    app = FastAPI(
        title="Visual Attendance System API (Phase 3, development)",
        # Doc URLs disabled: the served route inventory must be exactly
        # the approved eight endpoints (see module docstring).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # No CORS or other middleware is installed, deliberately.

    @app.get(f"{API_PREFIX}/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Service is up. Deliberately no filesystem paths, credentials,
        employee data, or database connection details — and no readiness
        claim about PostgreSQL (deferred to the database-backed step)."""
        return HealthResponse()

    _register_declared_endpoints(app)

    # Stable, sanitized error surface (FastAPI picks the most specific
    # handler via the raised exception's MRO).
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_request_validation)
    app.add_exception_handler(ConfigError, handle_config_error)
    app.add_exception_handler(DatabaseUnavailableError, handle_database_unavailable)
    app.add_exception_handler(MalformedDsnError, handle_database_config_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
    return app


def build_server_config(settings: AppSettings):
    """Build the uvicorn configuration from the validated settings.

    The host/port are the already-validated ``settings.api_host`` /
    ``settings.api_port`` values — passed through unchanged so that
    ``src.config`` remains the single loopback validation source.
    Returns a ``uvicorn.Config``; uvicorn is imported lazily so that
    importing :mod:`src.api` stays free of the server runtime.
    """
    import uvicorn  # lazy: src.api stays importable without the server runtime

    return uvicorn.Config(
        app=create_app(),
        host=settings.api_host,
        port=settings.api_port,
    )


def run_server(settings: AppSettings | None = None) -> None:
    """Run the settings-driven loopback server to completion (guarded).

    Step 1.4: acquiring the Windows single-process startup guard
    (:func:`src.api_guard.acquire_startup_guard`) happens **before** any
    request can be served, and the guard stays held for the entire
    server lifetime — released by the context manager on orderly
    shutdown, by the OS after abnormal process termination. A guard
    failure raises :class:`src.api_guard.StartupGuardError` and Uvicorn
    is never started, so ``--print-bind`` (which does not serve) and any
    library caller of this entry point get the same protection.

    Uses a fresh :func:`load_settings()` by default (current environment,
    consistent with :mod:`src.db`), then serves on the validated loopback
    host/port. A separate runner (``tools/run_api.py``) owns nothing
    beyond settings resolution and translating the typed guard failure
    into its documented startup exit code — binding cannot bypass the
    loopback contract.
    """
    if settings is None:
        settings = load_settings()
    from src.api_guard import acquire_startup_guard  # lazy, side-effect free

    with acquire_startup_guard(settings) as guard:
        # Structured trusted diagnostics only; client/console output
        # never includes paths.
        _logger.info("startup guard acquired (%s)", guard.lock_path.name)
        import uvicorn  # lazy: only after successful guard acquisition

        server = uvicorn.Server(build_server_config(settings))
        server.run()
        # leaving the ``with`` block releases the guard (orderly shutdown)
