"""Phase 3 Step 40 — the eight-endpoint API (loopback-only, unauthenticated).

Implements the approved ``/api/v1`` route inventory. Every route is a thin
adapter: it validates the request shape, calls one method on the injected
service, and renders either the documented success payload or the single
sanitized error envelope. No route contains business logic, SQL, journal
parsing, or a database connection.

Layering
--------

* ``src.api_service`` — the API-shaped service layer over the approved
  ``EmployeeRepository`` and the attendance journal (see its module docstring
  for the check-out contract and its one documented policy difference).
* this module — HTTP only: request models, response payloads, and the mapping
  from typed service results to approved status codes.

Design rules (unchanged from the foundation step)
-------------------------------------------------

* **Loopback navigation only.** The bind address/port come from the existing
  validated configuration (:mod:`src.config` — ``AppSettings.api_host`` /
  ``api_port``); this module never duplicates or bypasses that validation, and
  ``tools/run_api.py`` feeds the values into uvicorn unchanged.
* **No work at import or app-construction time.** Importing this module loads
  no driver, no attendance runtime, no camera stack and no server runtime;
  ``create_app()`` builds no service and connects to nothing. The production
  services are constructed on the **first request** (see ``_resolve_services``)
  so the import-hygiene contract stays intact.
* **Sanitized error envelope.** Every client-facing error is the stable
  ``{"error": {"code": <machine_code>, "message": <sanitized_human>}}``
  mapping. Raw exception messages, tracebacks, DSNs/credentials, internal
  filesystem paths, and employee/biometric data never appear in responses;
  server-side logging keeps trusted diagnostics.
* **Stable decision codes.** Error ``code`` values are machine-readable and
  deliberately separate from human ``message`` text. A business check-out
  refusal additionally carries a nested ``reason`` code so a client can
  distinguish "inactive" from "no open session" without parsing prose.
* **No CORS, no authentication on business routes yet.** Exactly one
  middleware is installed: the P5.4.3 request-ID middleware
  (:mod:`src.api_request_context`), which only assigns a server-generated UUID
  and echoes it as ``X-Request-ID``. No CORS, authentication or authorization
  middleware exists. P5.4.4 added reusable session-cookie utilities
  (:mod:`src.api_cookies`) and a cookie-only authentication dependency
  (:mod:`src.api_auth`); P5.4.5 adds ``POST /api/v1/auth/login``
  (:mod:`src.api_login`) and P5.4.6 ``POST /api/v1/auth/logout``
  (:mod:`src.api_logout`), both guarded by exact same-origin validation;
  P5.4.7 adds the read-only, cookie-authenticated
  ``GET /api/v1/auth/me`` (:mod:`src.api_me`), which needs no Origin check
  because it changes nothing, and P5.4.9 adds a process-local fixed-window
  limiter on login (:mod:`src.login_throttle`, 429 + ``Retry-After``). The
  dependency still guards nothing else, so the eight business endpoints remain
  unauthenticated; the served inventory is those eight plus login, logout and
  me.
  Loopback-only
  binding is a development safety measure, explicitly not a production
  security boundary. Documentation URLs stay disabled.
* **No automatic retries.** A failed persisted append is reported as
  ``PERSISTENCE_UNCONFIRMED`` and never retried here.
"""

from __future__ import annotations

from typing import Any, Callable, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
# The *base* Starlette HTTPException is registered as the handler key:
# the framework raises its own base class for 404/405 (router-level),
# and the MRO-based handler lookup only walks the raised type's bases.
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api_auth import (
    AUTHENTICATION_REQUIRED_CODE,
    AUTHENTICATION_REQUIRED_MESSAGE,
    AuthenticationRequired,
    build_session_service,
)
from src.api_login import (
    ORIGIN_NOT_ALLOWED_CODE,
    ORIGIN_NOT_ALLOWED_MESSAGE,
    LOGIN_PATH,
    LoginService,
    OriginNotAllowed,
    build_login_service,
    register_login_route,
)
from src.api_logout import LOGOUT_PATH, register_logout_route
from src.api_me import ME_PATH, register_me_route
from src.api_request_context import (
    REQUEST_ID_HEADER,
    RequestIDMiddleware,
    request_id_for,
)
from src.config import AppSettings, ConfigError, load_settings
from src.db import DatabaseUnavailableError, MalformedDsnError
from src.login_throttle import (
    LOGIN_RATE_LIMITED_CODE,
    LOGIN_RATE_LIMITED_MESSAGE,
    LoginRateLimited,
    LoginThrottle,
)
from src.logging_setup import get_logger
from src.session_service import SessionService

__all__ = [
    "API_PREFIX",
    "APPROVED_ROUTES",
    "ApprovedRoute",
    "CheckoutRequest",
    "EmployeeCreateRequest",
    "EmployeeUpdateRequest",
    "ErrorBody",
    "ErrorResponse",
    "HealthResponse",
    "build_server_config",
    "create_app",
    "run_server",
]

_logger = get_logger(__name__)

#: The approved API namespace: every endpoint lives under this prefix.
API_PREFIX: str = "/api/v1"

_INTERNAL_ERROR_CODE: str = "INTERNAL_ERROR"
_INTERNAL_ERROR_MESSAGE: str = "Internal server error."

#: Stable machine codes for the implemented endpoints.
EMPLOYEE_EXISTS_CODE: str = "EMPLOYEE_EXISTS"
EMPLOYEE_NOT_FOUND_CODE: str = "EMPLOYEE_NOT_FOUND"
EMPLOYEE_INVALID_CODE: str = "EMPLOYEE_INVALID"
CHECKOUT_REJECTED_CODE: str = "CHECKOUT_REJECTED"
ATTENDANCE_UNAVAILABLE_CODE: str = "ATTENDANCE_UNAVAILABLE"
PERSISTENCE_UNCONFIRMED_CODE: str = "PERSISTENCE_UNCONFIRMED"
PERSISTENCE_REJECTED_CODE: str = "PERSISTENCE_REJECTED"


# ---------------------------------------------------------------------------
# Approved route inventory (exactly these eleven; never more)
#:
#: P5.4.5 adds the login endpoint, P5.4.6 the logout endpoint and P5.4.7 the
#: current-user endpoint; the eight Phase 3 business routes stay unchanged and
#: unauthenticated.
# ---------------------------------------------------------------------------

ApprovedRoute = tuple[str, str]  # (path without prefix, HTTP method)

APPROVED_ROUTES: tuple[ApprovedRoute, ...] = (
    ("/health", "GET"),
    ("/auth/login", "POST"),
    ("/auth/logout", "POST"),
    ("/auth/me", "GET"),
    ("/employees", "POST"),
    ("/employees", "GET"),
    ("/employees/{employee_id}", "GET"),
    ("/employees/{employee_id}", "PATCH"),
    ("/employees/{employee_id}/deactivate", "POST"),
    ("/attendance", "GET"),
    ("/attendance/{employee_id}/checkout", "POST"),
)


# ---------------------------------------------------------------------------
# Typed request/response models
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Minimal service-health payload: no paths, credentials, employees,
    connection details, or internal exception information."""

    status: Literal["ok"] = "ok"


class EmployeeCreateRequest(BaseModel):
    """The three fields a client may supply; ``created_at`` is never accepted.

    ``created_at`` is application-authoritative (approved D2), so a client
    value is not merely ignored — the field is absent from this model and a
    request carrying it is rejected by the model's ``forbid`` policy below.
    """

    model_config = {"extra": "forbid"}

    employee_id: str
    full_name: str
    is_active: bool = True


class EmployeeUpdateRequest(BaseModel):
    """PATCH accepts ``full_name`` only (``employee_id``/``is_active``/
    ``created_at`` are not patchable in v1 and are refused if supplied)."""

    model_config = {"extra": "forbid"}

    full_name: str


class CheckoutRequest(BaseModel):
    """The check-out body is empty by contract; the employee comes from the
    path. An explicit empty object is accepted, and unknown fields are
    refused so a client cannot smuggle a fabricated timestamp or event ID."""

    model_config = {"extra": "forbid"}


class ErrorBody(BaseModel):
    """Machine-readable ``code`` kept separate from the human ``message``
    (both sanitized; the code is stable API surface)."""

    code: str
    message: str


class ErrorResponse(BaseModel):
    """The one and only client-facing error envelope."""

    error: ErrorBody


def _error_json(
    status_code: int, code: str, message: str, **extra: object
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message)).model_dump()
    body["error"].update(extra)
    return JSONResponse(status_code=status_code, content=body)


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
        "rejected request: invalid service configuration (%s) request_id=%s",
        type(exc).__name__,
        request_id_for(request) or "unassigned",
    )
    return _error_json(503, "CONFIG_INVALID", "Service configuration is invalid.")


def handle_database_unavailable(
    request: Request, exc: DatabaseUnavailableError
) -> JSONResponse:
    """PostgreSQL could not be reached (availability-class failures only,
    with the connection layer's redaction discipline)."""
    _logger.warning(
        "rejected request: postgres unavailable (%s) request_id=%s",
        type(exc).__name__,
        request_id_for(request) or "unassigned",
    )
    return _error_json(503, "DATABASE_UNAVAILABLE", "Database is temporarily unavailable.")


def handle_database_config_error(
    request: Request, exc: MalformedDsnError
) -> JSONResponse:
    """Separate, deliberate code for a malformed database DSN (a
    caller-value config contract failure) — never folded into
    ``DATABASE_UNAVAILABLE``."""
    _logger.warning(
        "rejected request: invalid database configuration (%s) request_id=%s",
        type(exc).__name__,
        request_id_for(request) or "unassigned",
    )
    return _error_json(
        503, "DATABASE_CONFIG_INVALID", "Database configuration is invalid."
    )


def handle_origin_not_allowed(
    request: Request, exc: OriginNotAllowed
) -> JSONResponse:
    """Login was called without exactly one trusted ``Origin``.

    A sanitized 403: the client is told only that the origin is unacceptable,
    never which header it sent or whether an allowlist is configured.
    """
    _logger.warning(
        "rejected request: untrusted origin request_id=%s",
        request_id_for(request) or "unassigned",
    )
    return _error_json(403, ORIGIN_NOT_ALLOWED_CODE, ORIGIN_NOT_ALLOWED_MESSAGE)


def handle_authentication_required(
    request: Request, exc: AuthenticationRequired
) -> JSONResponse:
    """No usable session was presented: a fixed 401 envelope.

    The message never reveals why the credential was refused (absent,
    malformed, unknown, expired, revoked or inactive account), and the request
    ID keeps the refusal correlatable with server logs.
    """
    _logger.info(
        "rejected request: authentication required request_id=%s",
        request_id_for(request) or "unassigned",
    )
    return _error_json(
        401, AUTHENTICATION_REQUIRED_CODE, AUTHENTICATION_REQUIRED_MESSAGE
    )


def handle_login_rate_limited(
    request: Request, exc: LoginRateLimited
) -> JSONResponse:
    """Too many failed attempts for this username: sanitized 429.

    The body never names the username, the account, the attempt count or the
    window start, and the only extra header is the whole-second ``Retry-After``
    the client needs to wait. Logging stays request-ID only.
    """
    _logger.warning(
        "rejected request: login rate limited request_id=%s",
        request_id_for(request) or "unassigned",
    )
    response = _error_json(
        429, LOGIN_RATE_LIMITED_CODE, LOGIN_RATE_LIMITED_MESSAGE
    )
    response.headers["retry-after"] = str(exc.retry_after_seconds)
    return response


def handle_unexpected_error(request: Request, exc: BaseException) -> JSONResponse:
    """Last-resort 500: the client sees only a generic message; trusted
    diagnostics are logged server-side and never returned.

    Starlette renders this handler from its outermost ``ServerErrorMiddleware``,
    above the request-ID middleware, so the response never passes back through
    that middleware. The request ID is therefore attached here (taken from the
    ``request.state`` the middleware already populated); when the handler is
    invoked without that state — as in direct unit tests — no header is added.
    """
    request_id = request_id_for(request)
    _logger.exception(
        "unhandled error while handling a request request_id=%s",
        request_id or "unassigned",
    )
    response = _error_json(
        500, _INTERNAL_ERROR_CODE, _INTERNAL_ERROR_MESSAGE
    )
    if request_id is not None:
        response.headers[REQUEST_ID_HEADER] = request_id
    return response


# ---------------------------------------------------------------------------
# Service resolution (lazy, once per app instance)
# ---------------------------------------------------------------------------


def _resolve_services(
    settings: AppSettings,
    employee_service: object | None,
    attendance_service: object | None,
) -> tuple[Callable[[], Any], Callable[[], Any]]:
    """Return getters that build the production services on first use.

    Both services are optional injection points (offline tests supply fakes).
    When absent, the production composition is imported and built **inside**
    the first call, never during ``create_app()`` — that is what keeps
    ``import src.api`` free of the database driver and the attendance runtime.
    """

    cache: dict[str, Any] = {}

    def employee() -> Any:
        if employee_service is not None:
            return employee_service
        if "employee" not in cache:
            from src.api_service import build_default_services

            built_employee, built_attendance = build_default_services(settings)
            cache.setdefault("attendance", built_attendance)
            cache["employee"] = built_employee
        return cache["employee"]

    def attendance() -> Any:
        if attendance_service is not None:
            return attendance_service
        if "attendance" not in cache:
            from src.api_service import build_default_services

            built_employee, built_attendance = build_default_services(settings)
            cache.setdefault("employee", built_employee)
            cache["attendance"] = built_attendance
        return cache["attendance"]

    return employee, attendance


def _resolve_login_service(
    settings: AppSettings,
    login_service: LoginService | None,
    throttle: LoginThrottle | None,
) -> Callable[[], LoginService]:
    """Return a getter that builds the login service on first use (P5.4.5).

    Same contract as ``_resolve_services``: the production composition (and
    therefore psycopg) is imported inside the first login request, never during
    ``create_app``. Offline tests inject their own service.

    P5.4.9: exactly **one** :class:`LoginThrottle` belongs to the application
    instance and is injected into the login service, so every request shares
    the same counters instead of building a fresh limiter per request.
    """

    resolved_throttle = throttle if throttle is not None else LoginThrottle()
    cache: dict[str, LoginService] = {}

    def login() -> LoginService:
        if login_service is not None:
            return login_service
        if "login" not in cache:
            cache["login"] = build_login_service(
                settings, throttle=resolved_throttle
            )
        return cache["login"]

    return login


def _resolve_session_service(
    settings: AppSettings, session_service: SessionService | None
) -> Callable[[], SessionService]:
    """Return a getter that builds the session service on first use (P5.4.6).

    Logout needs only the P5.3 revocation primitive, so it composes the
    existing :func:`build_session_service` (availability check before the
    repository modules are imported) instead of the login composition. Offline
    tests inject their own service.
    """

    cache: dict[str, SessionService] = {}

    def sessions() -> SessionService:
        if session_service is not None:
            return session_service
        if "sessions" not in cache:
            cache["sessions"] = build_session_service(settings)
        return cache["sessions"]

    return sessions


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(
    settings: AppSettings | None = None,
    *,
    employee_service: object | None = None,
    attendance_service: object | None = None,
    login_service: LoginService | None = None,
    session_service: SessionService | None = None,
    login_throttle: LoginThrottle | None = None,
) -> FastAPI:
    """Create the Phase 3 API application (no server, no connections).

    Parameters
    ----------
    settings:
        Optional acknowledged settings. Used only to derive the attendance
        journal's data directory for the lazily built default services; the
        app itself never reads a DSN or binds a socket.
    employee_service / attendance_service / login_service / session_service /
    login_throttle:
        Offline-test seams. Production passes none of them; the application
        then owns one shared :class:`LoginThrottle` (P5.4.9).
    """
    from src.config import SETTINGS as _default_settings

    resolved_settings = settings if settings is not None else _default_settings
    get_employee, get_attendance = _resolve_services(
        resolved_settings, employee_service, attendance_service
    )
    get_login = _resolve_login_service(
        resolved_settings, login_service, login_throttle
    )
    get_sessions = _resolve_session_service(resolved_settings, session_service)

    app = FastAPI(
        title="Visual Attendance System API (Phase 3, development)",
        # Doc URLs disabled: the served route inventory must be exactly
        # the approved endpoints (eight business routes + login + logout + me).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Exactly one middleware: server-generated request IDs (P5.4.3). It adds no
    # CORS, authentication or authorization behaviour — deliberately.
    app.add_middleware(RequestIDMiddleware)

    # -- health ---------------------------------------------------------

    @app.get(f"{API_PREFIX}/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Service is up. Deliberately no filesystem paths, credentials,
        employee data, or database connection details. ``ok`` is a
        liveness claim about this process only — not a database readiness
        claim (``GET /api/v1/employees`` is the honest database probe)."""
        return HealthResponse()

    # -- authentication (P5.4.5) ----------------------------------------

    register_login_route(
        app,
        path=LOGIN_PATH,
        get_login_service=get_login,
        settings=resolved_settings,
    )

    # -- logout (P5.4.6) -------------------------------------------------

    register_logout_route(
        app,
        path=LOGOUT_PATH,
        get_session_service=get_sessions,
        settings=resolved_settings,
    )

    # -- current user (P5.4.7) -------------------------------------------

    register_me_route(
        app,
        path=ME_PATH,
        get_session_service=get_sessions,
    )

    # -- employees ------------------------------------------------------

    @app.post(f"{API_PREFIX}/employees", status_code=201)
    def create_employee(payload: EmployeeCreateRequest) -> JSONResponse:
        """Create one employee; duplicate ID → 409, invalid input → 422."""
        from src.employee import Employee, EmployeeValidationError
        from src.employee_repository import EmployeeAlreadyExists
        from src.api_service import employee_payload

        try:
            employee = Employee(
                employee_id=payload.employee_id,
                full_name=payload.full_name,
                is_active=payload.is_active,
            )
        except EmployeeValidationError:
            return _error_json(
                422,
                EMPLOYEE_INVALID_CODE,
                "Employee input does not satisfy the documented validation rules.",
            )
        try:
            created = get_employee().create(employee)
        except EmployeeAlreadyExists:
            return _error_json(
                409,
                EMPLOYEE_EXISTS_CODE,
                "An employee already exists for the requested identifier.",
            )
        return JSONResponse(status_code=201, content=employee_payload(created))

    @app.get(f"{API_PREFIX}/employees")
    def list_employees(
        after: str | None = None, limit: int | None = None
    ) -> JSONResponse:
        """One keyset page (default 20, clamped at 100)."""
        from src.employee import EmployeeValidationError
        from src.api_service import employee_payload

        # A non-positive page size is a caller error, never a silent empty
        # page: only sizes above the maximum are clamped (approved contract).
        if limit is not None and limit < 1:
            return _error_json(
                422,
                EMPLOYEE_INVALID_CODE,
                "Page size must be a positive integer (maximum 100).",
            )

        try:
            page = get_employee().list(after=after, limit=limit)
        except EmployeeValidationError:
            return _error_json(
                422,
                EMPLOYEE_INVALID_CODE,
                "The pagination cursor or page size is not valid.",
            )
        return JSONResponse(
            status_code=200,
            content={
                "employees": [employee_payload(row) for row in page.employees],
                "count": len(page.employees),
                "limit": page.limit,
                "next_after": page.next_after,
            },
        )

    @app.get(f"{API_PREFIX}/employees/{{employee_id}}")
    def retrieve_employee(employee_id: str) -> JSONResponse:
        """Retrieve one employee; missing → 404."""
        from src.employee_repository import EmployeeNotFound
        from src.api_service import employee_payload

        try:
            employee = get_employee().get(employee_id)
        except EmployeeNotFound:
            return _error_json(
                404,
                EMPLOYEE_NOT_FOUND_CODE,
                "No employee exists for the requested identifier.",
            )
        return JSONResponse(status_code=200, content=employee_payload(employee))

    @app.patch(f"{API_PREFIX}/employees/{{employee_id}}")
    def update_employee(
        employee_id: str, payload: EmployeeUpdateRequest
    ) -> JSONResponse:
        """Update ``full_name`` only; missing → 404, invalid name → 422."""
        from src.employee import EmployeeValidationError
        from src.employee_repository import EmployeeNotFound
        from src.api_service import employee_payload

        try:
            employee = get_employee().rename(employee_id, payload.full_name)
        except EmployeeNotFound:
            return _error_json(
                404,
                EMPLOYEE_NOT_FOUND_CODE,
                "No employee exists for the requested identifier.",
            )
        except EmployeeValidationError:
            return _error_json(
                422,
                EMPLOYEE_INVALID_CODE,
                "full_name does not satisfy the documented validation rules.",
            )
        return JSONResponse(status_code=200, content=employee_payload(employee))

    @app.post(f"{API_PREFIX}/employees/{{employee_id}}/deactivate")
    def deactivate_employee(employee_id: str) -> JSONResponse:
        """Set ``is_active = FALSE``; missing → 404. Idempotent by nature
        (deactivating an inactive employee returns the same row)."""
        from src.employee_repository import EmployeeNotFound
        from src.api_service import employee_payload

        try:
            employee = get_employee().deactivate(employee_id)
        except EmployeeNotFound:
            return _error_json(
                404,
                EMPLOYEE_NOT_FOUND_CODE,
                "No employee exists for the requested identifier.",
            )
        return JSONResponse(status_code=200, content=employee_payload(employee))

    # -- attendance -----------------------------------------------------

    @app.get(f"{API_PREFIX}/attendance")
    def list_attendance() -> JSONResponse:
        """The validated journal in append order (no attendance pagination
        exists by contract). A corrupt journal → 503, never an empty list."""
        from src.attendance_store import CorruptAttendanceJournalError
        from src.api_service import event_payload

        try:
            events, incomplete = get_attendance().list_events()
        except CorruptAttendanceJournalError:
            return _error_json(
                503,
                ATTENDANCE_UNAVAILABLE_CODE,
                "The attendance journal could not be read; no attendance data "
                "is available.",
            )
        return JSONResponse(
            status_code=200,
            content={
                "events": [event_payload(event) for event in events],
                "count": len(events),
                "incomplete_trailing_record": incomplete,
            },
        )

    @app.post(f"{API_PREFIX}/attendance/{{employee_id}}/checkout", status_code=201)
    def checkout(
        employee_id: str, body: CheckoutRequest | None = None
    ) -> JSONResponse:
        """Operator check-out: 201 only after the durable journal append."""
        from src.attendance_store import (
            AttendanceStoreError,
            AttendanceStoreInputError,
            CorruptAttendanceJournalError,
        )
        from src.employee_repository import EmployeeNotFound
        from src.api_service import CheckoutRejected, event_payload

        def lookup(key: str):
            try:
                return get_employee().get(key)
            except EmployeeNotFound:
                return None

        try:
            event = get_attendance().checkout(employee_id, employee_lookup=lookup)
        except CheckoutRejected as exc:
            status = 404 if exc.code == "EMPLOYEE_NOT_FOUND" else 409
            code = (
                EMPLOYEE_NOT_FOUND_CODE
                if exc.code == "EMPLOYEE_NOT_FOUND"
                else CHECKOUT_REJECTED_CODE
            )
            return _error_json(status, code, exc.message, reason=exc.code)
        except CorruptAttendanceJournalError:
            return _error_json(
                503,
                ATTENDANCE_UNAVAILABLE_CODE,
                "The attendance journal could not be read; the check-out was "
                "not attempted.",
            )
        except AttendanceStoreInputError:
            return _error_json(
                422,
                PERSISTENCE_REJECTED_CODE,
                "The attendance event was rejected before any write; the "
                "journal is unchanged.",
            )
        except AttendanceStoreError:
            return _error_json(
                503,
                PERSISTENCE_UNCONFIRMED_CODE,
                "The check-out could not be confirmed as persisted; it was not "
                "retried and the journal may or may not contain the record.",
            )
        return JSONResponse(status_code=201, content=event_payload(event))

    # -- stable, sanitized error surface -------------------------------

    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_request_validation)
    app.add_exception_handler(ConfigError, handle_config_error)
    app.add_exception_handler(DatabaseUnavailableError, handle_database_unavailable)
    app.add_exception_handler(MalformedDsnError, handle_database_config_error)
    app.add_exception_handler(AuthenticationRequired, handle_authentication_required)
    app.add_exception_handler(OriginNotAllowed, handle_origin_not_allowed)
    app.add_exception_handler(LoginRateLimited, handle_login_rate_limited)
    app.add_exception_handler(Exception, handle_unexpected_error)
    return app


# ---------------------------------------------------------------------------
# Server entry point (unchanged contract)
# ---------------------------------------------------------------------------


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
        app=create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
    )


def _default_startup_verifier(settings: AppSettings) -> None:
    """G2+G8: the real startup schema-compatibility verifier.

    Imported lazily so that importing :mod:`src.api` still pulls in no
    migration or database machinery (the guard must be acquired before the
    verifier runs, and import hygiene is test-enforced). The verifier checks
    the migration history **and** the physical schema, and refuses unless both
    pass; a refusal raises :class:`src.startup_schema.StartupSchemaError`.
    """
    from src.startup_schema import verify_startup_schema

    verify_startup_schema(settings)


def run_server(
    settings: AppSettings | None = None,
    *,
    startup_verifier: Callable[[AppSettings], None] | None = None,
) -> None:
    """Run the settings-driven loopback server to completion (guarded).

    The Windows single-process startup guard
    (:func:`src.api_guard.acquire_startup_guard`) is acquired **before** any
    request can be served and stays held for the entire server lifetime —
    released by the context manager on orderly shutdown, by the OS after
    abnormal process termination. A guard failure raises
    :class:`src.api_guard.StartupGuardError` and Uvicorn is never started, so
    ``--print-bind`` (which does not serve) and any library caller of this
    entry point get the same protection.

    G2+G8: **after** the guard is acquired and **before** Uvicorn is imported
    or constructed, the startup schema-compatibility verifier runs — migration
    history first, then the physical schema, both on one connection inside one
    read-only snapshot. Any refusal raises
    :class:`src.startup_schema.StartupSchemaError`; the ``with`` block then
    releases the guard, so a refused start leaves neither a served request nor
    a held process guard behind. Only a fully ``COMPATIBLE`` verification
    reaches Uvicorn.
    """
    if settings is None:
        settings = load_settings()
    from src.api_guard import acquire_startup_guard  # lazy, side-effect free

    verify = (
        _default_startup_verifier if startup_verifier is None else startup_verifier
    )

    with acquire_startup_guard(settings) as guard:
        # Structured trusted diagnostics only; client/console output
        # never includes paths.
        _logger.info("startup guard acquired (%s)", guard.lock_path.name)

        # G2: refuse before the server runtime is even imported. A refusal
        # propagates out of the ``with`` block, releasing the guard.
        verify(settings)

        import uvicorn  # lazy: only after guard and schema verification

        server = uvicorn.Server(build_server_config(settings))
        server.run()
        # leaving the ``with`` block releases the guard (orderly shutdown)
