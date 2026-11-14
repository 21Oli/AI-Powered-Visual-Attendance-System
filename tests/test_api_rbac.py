"""P5.4.10 RBAC route-integration tests (offline, pure ASGI, no database).

Every protected business endpoint is driven through the real ``create_app``
composition (request-ID middleware, exception handlers, the P5.4.4 session
dependency, the P5.4.10 authorization guard) with a real ``SessionService`` over
in-memory repositories. Only the repositories, the employee service and the
attendance service are doubled, so session validation and the permission policy
are the production implementations.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.api import APPROVED_ROUTES, API_PREFIX, create_app
from src.api_auth import (
    AUTHENTICATION_REQUIRED_CODE,
    AUTHENTICATION_REQUIRED_MESSAGE,
    AuthenticatedPrincipal,
)
from src.api_login import ORIGIN_NOT_ALLOWED_CODE
from src.api_request_context import REQUEST_ID_HEADER
from src.api_service import CheckoutRejected
from src.authz import (
    PERMISSION_DENIED_CODE,
    PERMISSION_DENIED_MESSAGE,
    Permission,
    ROLE_PERMISSIONS,
)
from src.config import SESSION_COOKIE, AppSettings
from src.db import DatabaseUnavailableError
from src.employee_repository import EmployeeNotFound
from src.session_service import SessionService
from src.user import Role, User

NOW = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
TEST_ORIGIN = "https://attendance.test.invalid"
_PASSWORD_HASH = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"

EMP = f"{API_PREFIX}/employees"
ATT = f"{API_PREFIX}/attendance"


# ---------------------------------------------------------------------------
# Doubles (repositories) + real SessionService
# ---------------------------------------------------------------------------


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class FakeUserRepository:
    def __init__(self, users: list[User]) -> None:
        self.users = {user.user_id: user for user in users}
        self.failure: BaseException | None = None

    def get(self, user_id: str) -> User | None:
        if self.failure is not None:
            raise self.failure
        return self.users.get(user_id)


class FakeSessionRepository:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}
        self.find_failure: BaseException | None = None

    def insert(self, session):
        self.rows[session.session_token_hash] = session
        return session

    def find_by_token_hash(self, digest: str):
        if self.find_failure is not None:
            raise self.find_failure
        return self.rows.get(digest)

    def touch(self, digest: str, *, last_seen_at: datetime, expires_at: datetime):
        updated = replace(
            self.rows[digest], last_seen_at=last_seen_at, expires_at=expires_at
        )
        self.rows[digest] = updated
        return updated

    def revoke(self, digest: str, *, revoked_at: datetime):
        updated = replace(self.rows[digest], revoked_at=self.rows[digest].revoked_at or revoked_at)
        self.rows[digest] = updated
        return updated

    def revoke_all_for_user(self, user_id: str, *, revoked_at: datetime) -> int:
        return 0


def make_user(
    *,
    user_id: str = "user-1",
    username: str = "ada",
    role: Role = Role.ADMIN,
    is_active: bool = True,
) -> User:
    return User(
        user_id=user_id,
        username=username,
        password_hash=_PASSWORD_HASH,
        role=role,
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


def build(*, role: Role = Role.ADMIN, is_active: bool = True):
    user_repository = FakeUserRepository([make_user(role=role, is_active=is_active)])
    session_repository = FakeSessionRepository()
    clock = MutableClock()
    service = SessionService(
        users=user_repository,  # type: ignore[arg-type]
        sessions=session_repository,  # type: ignore[arg-type]
        clock=clock,
    )
    return service, user_repository, session_repository, clock


# ---------------------------------------------------------------------------
# Business-service recorders: proving denial happens *before* business logic
# ---------------------------------------------------------------------------


class RecordingEmployeeService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def create(self, employee):
        self.calls.append(("create", employee.employee_id))
        return employee

    def list(self, *, after=None, limit=None):
        self.calls.append(("list", after, limit))
        return SimpleNamespace(employees=[], limit=20, next_after=None)

    def get(self, employee_id: str):
        self.calls.append(("get", employee_id))
        raise EmployeeNotFound("missing")

    def rename(self, employee_id: str, full_name: str):
        self.calls.append(("rename", employee_id, full_name))
        raise EmployeeNotFound("missing")

    def deactivate(self, employee_id: str):
        self.calls.append(("deactivate", employee_id))
        raise EmployeeNotFound("missing")


class RecordingAttendanceService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def list_events(self):
        self.calls.append(("list_events",))
        return [], False

    def checkout(self, employee_id: str, *, employee_lookup):
        self.calls.append(("checkout", employee_id))
        raise CheckoutRejected("EMPLOYEE_NOT_FOUND")


# ---------------------------------------------------------------------------
# ASGI driver
# ---------------------------------------------------------------------------


async def _asgi_call(
    app,
    *,
    headers: list[tuple[str, str]],
    method: str,
    path: str,
    query_string: bytes = b"",
    body: bytes = b"",
    swallow: bool = False,
) -> dict:
    messages: list[dict] = []
    sent = {"done": False}

    async def receive() -> dict:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    base_path, _, inline_query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": base_path,
        "raw_path": base_path.encode("ascii"),
        "query_string": query_string or inline_query.encode("ascii"),
        "root_path": "",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in headers
        ],
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8000),
    }
    try:
        await app(scope, receive, send)
    except BaseException:  # noqa: BLE001 - Starlette re-raises after rendering
        if not swallow:
            raise
    start = next(m for m in messages if m["type"] == "http.response.start")
    raw_body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return {
        "status": start["status"],
        "headers": [
            (key.decode("latin-1").lower(), value.decode("latin-1"))
            for key, value in start["headers"]
        ],
        "body": raw_body,
        "payload": json.loads(raw_body) if raw_body else None,
    }


def call(
    app,
    *,
    method: str,
    path: str,
    token: str | None = None,
    body: object | None = None,
    headers: list[tuple[str, str]] | None = None,
    query_string: bytes = b"",
    swallow: bool = False,
) -> dict:
    resolved: list[tuple[str, str]] = []
    if token is not None:
        resolved.append(("cookie", f"{SESSION_COOKIE.name}={token}"))
    if body is not None:
        resolved.append(("content-type", "application/json"))
    resolved.extend(headers or [])
    raw = b"" if body is None else json.dumps(body).encode()
    return asyncio.run(
        _asgi_call(
            app,
            headers=resolved,
            method=method,
            path=path,
            query_string=query_string,
            body=raw,
            swallow=swallow,
        )
    )


def _header_values(response: dict, name: str) -> list[str]:
    return [value for key, value in response["headers"] if key == name]


def _request_id(response: dict) -> str | None:
    values = _header_values(response, REQUEST_ID_HEADER.lower())
    return values[0] if values else None


def _app(
    service: SessionService,
    *,
    employees: RecordingEmployeeService | None = None,
    attendance: RecordingAttendanceService | None = None,
    resolver=None,
):
    return create_app(
        settings=AppSettings(
            app_env="testing", login_trusted_origins=(TEST_ORIGIN,)
        ),
        session_service=service,
        employee_service=employees or RecordingEmployeeService(),
        attendance_service=attendance or RecordingAttendanceService(),
        principal_resolver=resolver,
    )


# ---------------------------------------------------------------------------
# The protected-endpoint mapping (mirrors src/api.py exactly)
# ---------------------------------------------------------------------------

PROTECTED = [
    pytest.param(
        "POST", EMP, {"employee_id": "E1", "full_name": "Name"},
        Permission.EMPLOYEES_CREATE, Role.HR_MANAGER, Role.OPERATOR,
        id="POST /employees",
    ),
    pytest.param(
        "GET", EMP, None,
        Permission.EMPLOYEES_READ, Role.AUDITOR, None,
        id="GET /employees",
    ),
    pytest.param(
        "GET", f"{EMP}/E1", None,
        Permission.EMPLOYEES_READ, Role.OPERATOR, None,
        id="GET /employees/{id}",
    ),
    pytest.param(
        "PATCH", f"{EMP}/E1", {"full_name": "New"},
        Permission.EMPLOYEES_UPDATE, Role.HR_MANAGER, Role.OPERATOR,
        id="PATCH /employees/{id}",
    ),
    pytest.param(
        "POST", f"{EMP}/E1/deactivate", None,
        Permission.EMPLOYEES_DEACTIVATE, Role.ADMIN, Role.HR_MANAGER,
        id="POST /employees/{id}/deactivate",
    ),
    pytest.param(
        "GET", ATT, None,
        Permission.ATTENDANCE_READ, Role.AUDITOR, None,
        id="GET /attendance",
    ),
    pytest.param(
        "POST", f"{ATT}/E1/checkout", None,
        Permission.ATTENDANCE_CHECKOUT, Role.OPERATOR, Role.AUDITOR,
        id="POST /attendance/{id}/checkout",
    ),
]


def _recorders():
    return RecordingEmployeeService(), RecordingAttendanceService()


# ---------------------------------------------------------------------------
# A. Every business endpoint is protected (no accidental bypass)
# ---------------------------------------------------------------------------


def test_the_only_unprotected_routes_are_health_and_authentication() -> None:
    business = {
        (path, method)
        for path, method in APPROVED_ROUTES
        if path not in {"/health", "/auth/login", "/auth/logout", "/auth/me"}
    }
    # The seven business templates behind the RBAC guard. Anything else
    # unprotected would be an accidental authorization bypass.
    protected_templates = {
        ("/employees", "POST"),
        ("/employees", "GET"),
        ("/employees/{employee_id}", "GET"),
        ("/employees/{employee_id}", "PATCH"),
        ("/employees/{employee_id}/deactivate", "POST"),
        ("/attendance", "GET"),
        ("/attendance/{employee_id}/checkout", "POST"),
    }
    assert business == protected_templates
    assert len(PROTECTED) == len(protected_templates) == 7


@pytest.mark.parametrize(("method", "path", "body", "permission", "allowed", "denied"), PROTECTED)
def test_anonymous_request_is_rejected_with_the_generic_401(
    method, path, body, permission, allowed, denied
) -> None:
    service, _, _, _ = build()
    employees, attendance = _recorders()
    app = _app(service, employees=employees, attendance=attendance)

    response = call(app, method=method, path=path, body=body)

    assert response["status"] == 401
    assert response["payload"] == {
        "error": {
            "code": AUTHENTICATION_REQUIRED_CODE,
            "message": AUTHENTICATION_REQUIRED_MESSAGE,
        }
    }
    assert _header_values(response, "set-cookie") == []
    assert _request_id(response)
    assert employees.calls == [] and attendance.calls == []


@pytest.mark.parametrize(("method", "path", "body", "permission", "allowed", "denied"), PROTECTED)
def test_a_role_with_the_permission_reaches_the_business_logic(
    method, path, body, permission, allowed, denied
) -> None:
    service, _, _, _ = build(role=allowed)
    employees, attendance = _recorders()
    app = _app(service, employees=employees, attendance=attendance)
    token = service.issue_session("user-1").token

    response = call(app, method=method, path=path, token=token, body=body)

    assert response["status"] not in (401, 403), response["payload"]
    assert permission in ROLE_PERMISSIONS[allowed]
    assert employees.calls or attendance.calls, "the endpoint's service was invoked"


@pytest.mark.parametrize(("method", "path", "body", "permission", "allowed", "denied"), PROTECTED)
def test_a_role_without_the_permission_gets_a_sanitized_403(
    method, path, body, permission, allowed, denied
) -> None:
    employees, attendance = _recorders()
    if denied is None:
        # Every approved role holds this permission, so the negative case uses
        # a principal whose role value is not a Role member at all: an unknown
        # role must never be granted "read".
        app = _app(
            build()[0],
            employees=employees,
            attendance=attendance,
            resolver=_raw_principal("Admin"),
        )
        response = call(app, method=method, path=path, body=body)
    else:
        service, _, _, _ = build(role=denied)
        app = _app(service, employees=employees, attendance=attendance)
        token = service.issue_session("user-1").token
        response = call(app, method=method, path=path, token=token, body=body)

    assert response["status"] == 403
    assert response["payload"] == {
        "error": {
            "code": PERMISSION_DENIED_CODE,
            "message": PERMISSION_DENIED_MESSAGE,
        }
    }
    assert _header_values(response, "set-cookie") == []
    assert _request_id(response)
    # No business operation ran after the denial.
    assert employees.calls == [] and attendance.calls == []


# ---------------------------------------------------------------------------
# B. Fail-closed principals (unknown role / inactive) reach the guard too
# ---------------------------------------------------------------------------


def _raw_principal(role, *, is_active: bool = True):
    return lambda request: AuthenticatedPrincipal(
        user_id="user-1",
        username="ada",
        role=role,
        is_active=is_active,
    )


@pytest.mark.parametrize(("method", "path", "body", "permission", "allowed", "denied"), PROTECTED)
def test_an_inactive_principal_is_denied_even_with_an_admin_role(
    method, path, body, permission, allowed, denied
) -> None:
    employees, attendance = _recorders()
    app = _app(
        build()[0],
        employees=employees,
        attendance=attendance,
        resolver=_raw_principal(Role.ADMIN, is_active=False),
    )

    response = call(app, method=method, path=path, body=body)

    assert response["status"] == 403
    assert response["payload"]["error"]["code"] == PERMISSION_DENIED_CODE
    assert employees.calls == [] and attendance.calls == []


@pytest.mark.parametrize(
    "raw_role", ["Admin", "admin", "", None, object()], ids=repr
)
def test_a_non_role_value_cannot_authorize_anything(raw_role) -> None:
    employees, attendance = _recorders()
    app = _app(
        build()[0],
        employees=employees,
        attendance=attendance,
        resolver=_raw_principal(raw_role),
    )

    response = call(app, method="GET", path=EMP)

    assert response["status"] == 403
    assert response["payload"]["error"]["code"] == PERMISSION_DENIED_CODE
    assert employees.calls == []


# ---------------------------------------------------------------------------
# C. Session outcomes keep their existing HTTP semantics
# ---------------------------------------------------------------------------


def test_invalid_session_is_a_401() -> None:
    service, _, _, _ = build()
    response = call(_app(service), method="GET", path=EMP, token="not-a-valid-token")
    assert response["status"] == 401
    assert response["payload"]["error"]["code"] == AUTHENTICATION_REQUIRED_CODE


def test_revoked_session_is_a_401() -> None:
    service, _, _, _ = build()
    token = service.issue_session("user-1").token
    service.revoke_session(token)

    response = call(_app(service), method="GET", path=EMP, token=token)
    assert response["status"] == 401


def test_expired_session_is_a_401() -> None:
    service, _, _, clock = build()
    token = service.issue_session("user-1").token
    clock.advance(timedelta(hours=9))

    response = call(_app(service), method="GET", path=EMP, token=token)
    assert response["status"] == 401


def test_inactive_account_is_denied_at_session_validation() -> None:
    service, users, _, _ = build()
    token = service.issue_session("user-1").token
    users.users["user-1"] = replace(users.users["user-1"], is_active=False)

    response = call(_app(service), method="GET", path=EMP, token=token)
    assert response["status"] == 401
    assert response["payload"]["error"]["code"] == AUTHENTICATION_REQUIRED_CODE


def test_database_failure_during_session_validation_stays_a_503() -> None:
    service, _, sessions, _ = build()
    token = service.issue_session("user-1").token
    sessions.find_failure = DatabaseUnavailableError("offline")

    response = call(_app(service), method="GET", path=EMP, token=token)

    assert response["status"] == 503
    assert response["payload"]["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert "offline" not in json.dumps(response["payload"])


def test_unexpected_error_is_a_sanitized_500_not_a_401_or_403() -> None:
    service, _, sessions, _ = build()
    token = service.issue_session("user-1").token
    sessions.find_failure = RuntimeError("boom with a secret token")

    response = call(_app(service), method="GET", path=EMP, token=token, swallow=True)

    assert response["status"] == 500
    assert response["payload"]["error"]["code"] == "INTERNAL_ERROR"
    assert "boom" not in json.dumps(response["payload"])


# ---------------------------------------------------------------------------
# D. Client-supplied identity can never elevate privileges
# ---------------------------------------------------------------------------


def test_body_cannot_grant_a_permission() -> None:
    service, _, _, _ = build(role=Role.OPERATOR)
    employees, attendance = _recorders()
    app = _app(service, employees=employees, attendance=attendance)
    token = service.issue_session("user-1").token

    response = call(
        app,
        method="POST",
        path=EMP,
        token=token,
        body={
            "employee_id": "E1",
            "full_name": "Name",
            "role": "Admin",
            "permissions": ["employees:create"],
            "user_id": "admin-1",
        },
    )

    assert response["status"] in (403, 422)
    assert response["payload"]["error"]["code"] in {
        PERMISSION_DENIED_CODE,
        "REQUEST_INVALID",
    }
    assert employees.calls == []


def test_query_parameters_cannot_grant_a_permission() -> None:
    service, _, _, _ = build(role=Role.OPERATOR)
    employees, _ = _recorders()
    app = _app(service, employees=employees)
    token = service.issue_session("user-1").token

    response = call(
        app,
        method="POST",
        path=f"{EMP}?role=Admin&permission=employees:create&user_id=admin-1",
        token=token,
        body={"employee_id": "E1", "full_name": "Name"},
    )

    assert response["status"] == 403
    assert employees.calls == []


def test_role_and_permission_headers_cannot_grant_a_permission() -> None:
    service, _, _, _ = build(role=Role.OPERATOR)
    employees, _ = _recorders()
    app = _app(service, employees=employees)
    token = service.issue_session("user-1").token

    response = call(
        app,
        method="POST",
        path=EMP,
        token=token,
        body={"employee_id": "E1", "full_name": "Name"},
        headers=[
            ("x-role", "Admin"),
            ("x-user-role", "Admin"),
            ("x-permissions", "employees:create"),
            ("x-user-id", "admin-1"),
        ],
    )

    assert response["status"] == 403
    assert employees.calls == []


def test_an_authorized_body_field_cannot_change_the_stored_role() -> None:
    """An Admin/HR request carrying ``role`` is refused as malformed input."""
    service, _, _, _ = build(role=Role.HR_MANAGER)
    employees, _ = _recorders()
    app = _app(service, employees=employees)
    token = service.issue_session("user-1").token

    response = call(
        app,
        method="POST",
        path=EMP,
        token=token,
        body={"employee_id": "E1", "full_name": "Name", "role": "Admin"},
    )

    assert response["status"] == 422
    assert employees.calls == []


def test_403_response_does_not_disclose_account_or_policy_details() -> None:
    service, _, _, _ = build(role=Role.OPERATOR)
    app = _app(service)
    token = service.issue_session("user-1").token

    response = call(app, method="POST", path=EMP, token=token,
                    body={"employee_id": "E1", "full_name": "Name"})
    raw = response["body"].decode()

    assert response["status"] == 403
    for fragment in ("ada", "user-1", "Operator", "employees:create", "argon2", "role"):
        assert fragment not in raw


# ---------------------------------------------------------------------------
# E. Routes outside business RBAC keep their behaviour
# ---------------------------------------------------------------------------


def test_health_stays_anonymous() -> None:
    service, _, _, _ = build()
    response = call(_app(service), method="GET", path=f"{API_PREFIX}/health")
    assert response["status"] == 200
    assert response["payload"] == {"status": "ok"}


def test_me_still_works_for_every_role_without_a_business_permission() -> None:
    for role in Role:
        service, _, _, _ = build(role=role)
        token = service.issue_session("user-1").token
        response = call(_app(service), method="GET", path=f"{API_PREFIX}/auth/me", token=token)
        assert response["status"] == 200
        assert response["payload"]["role"] == role.value


def test_login_keeps_its_origin_guard() -> None:
    service, _, _, _ = build()
    response = call(
        _app(service),
        method="POST",
        path=f"{API_PREFIX}/auth/login",
        body={"username": "ada", "password": "whatever"},
    )
    assert response["status"] == 403
    assert response["payload"]["error"]["code"] == ORIGIN_NOT_ALLOWED_CODE


def test_logout_still_does_not_require_a_business_permission() -> None:
    service, _, _, _ = build(role=Role.AUDITOR)  # no checkout/users/manage etc.
    token = service.issue_session("user-1").token
    response = call(
        _app(service),
        method="POST",
        path=f"{API_PREFIX}/auth/logout",
        token=token,
        headers=[("origin", TEST_ORIGIN)],
    )
    assert response["status"] == 204
    assert _header_values(response, "set-cookie")
