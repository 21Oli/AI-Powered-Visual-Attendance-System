"""P5.5.5 audit route-integration tests (offline, temporary directories only).

Every test injects fakes for the business/session services and a real
``AuditJournalWriter`` pointing at ``tmp_path``. The project's real
``data/audit`` journal is never touched; no PostgreSQL, camera or biometric data
is involved.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.audit_writer as writer_module
from src.api import API_PREFIX, create_app
from src.api_audit import AuditSubsystem
from src.api_auth import AuthenticatedPrincipal, AuthenticationRequired
from src.api_login import LoginResult
from src.api_service import CheckoutRejected
from src.attendance import AttendanceEvent, EventType
from src.audit_schema import (
    AuditAction,
    AuditEvent,
    AuditOutcome,
    AuditReasonCode,
)
from src.audit_service import AuditService
from src.audit_writer import (
    AuditJournalContendedError,
    AuditJournalCorruptError,
    AuditJournalIntegrityError,
    AuditJournalUnavailableError,
    AuditJournalWriter,
)
from src.config import AppSettings
from src.employee import Employee
from src.employee_repository import EmployeeAlreadyExists, EmployeeNotFound
from src.login_throttle import LoginRateLimited
from src.session_repository import SessionNotFound
from src.session_service import AuthenticationError
from src.session_token import generate_session_token
from src.user import Role

ORIGIN = "http://localhost:3000"
PRINCIPAL = AuthenticatedPrincipal(
    user_id="user-1", username="alice", role=Role.ADMIN, is_active=True
)


# ---------------------------------------------------------------------------
# ASGI driver
# ---------------------------------------------------------------------------


async def _asgi_call(app, *, method, path, headers, body=b"", swallow=False):
    messages: list[dict] = []
    sent = {"done": False}

    async def receive() -> dict:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
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
    except BaseException:
        # Starlette's ServerErrorMiddleware re-raises after rendering; the
        # rendered 500 response is still in ``messages``.
        if not swallow or not any(
            m["type"] == "http.response.start" for m in messages
        ):
            raise
    start = next(m for m in messages if m["type"] == "http.response.start")
    raw = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return {
        "status": start["status"],
        "headers": [
            (k.decode("latin-1").lower(), v.decode("latin-1"))
            for k, v in start["headers"]
        ],
        "body": raw,
        "payload": json.loads(raw) if raw else None,
    }


def call(app, *, method, path, body=None, headers=None, swallow=False):
    resolved = list(headers or [])
    raw = b""
    if body is not None:
        resolved.append(("content-type", "application/json"))
        raw = json.dumps(body).encode()
    return asyncio.run(
        _asgi_call(
            app,
            method=method,
            path=path,
            headers=resolved,
            body=raw,
            swallow=swallow,
        )
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeEmployees:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.create_result: object = Employee(
            employee_id="E1", full_name="Name", is_active=True
        )
        self.create_error: BaseException | None = None
        self.rename_error: BaseException | None = None
        self.deactivate_error: BaseException | None = None
        self.on_create = None

    def create(self, employee: Employee):
        self.calls.append(("create", employee.employee_id))
        if self.on_create is not None:
            self.on_create()
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    def rename(self, employee_id: str, full_name: str):
        self.calls.append(("rename", employee_id))
        if self.rename_error is not None:
            raise self.rename_error
        return Employee(
            employee_id=employee_id, full_name=full_name, is_active=True
        )

    def deactivate(self, employee_id: str):
        self.calls.append(("deactivate", employee_id))
        if self.deactivate_error is not None:
            raise self.deactivate_error
        return Employee(
            employee_id=employee_id, full_name="Name", is_active=False
        )

    def get(self, employee_id: str):
        self.calls.append(("get", employee_id))
        return None

    def list(self, *, after=None, limit=None):
        self.calls.append(("list", after, limit))
        return SimpleNamespace(employees=[], limit=20, next_after=None)


class FakeAttendance:
    def __init__(self, event: AttendanceEvent | None = None) -> None:
        self.calls: list[tuple] = []
        self.on_checkout = None
        self.event = event or AttendanceEvent(
            event_id="ev-1",
            employee_id="E1",
            event_type=EventType.CHECK_OUT,
            occurred_at=datetime(2026, 11, 16, 9, 30, tzinfo=UTC),
        )
        self.error: BaseException | None = None

    def checkout(self, employee_id: str, *, employee_lookup):
        self.calls.append(("checkout", employee_id))
        if self.on_checkout is not None:
            self.on_checkout()
        if self.error is not None:
            raise self.error
        return self.event

    def list_events(self):
        return [], False


class FakeLogin:
    def __init__(self) -> None:
        self.result: object = LoginResult(
            principal=PRINCIPAL,
            token=generate_session_token(),
        )
        self.error: BaseException | None = None

    def authenticate(self, username: str, password: str):
        if self.error is not None:
            raise self.error
        return self.result


class FakeSessions:
    def __init__(self) -> None:
        self.validate_error: BaseException | None = None
        self.revoke_error: BaseException | None = None

    def validate_session(self, token: str):
        if self.validate_error is not None:
            raise self.validate_error
        return SimpleNamespace(user=PRINCIPAL, session=object())

    def revoke_session(self, token: str):
        if self.revoke_error is not None:
            raise self.revoke_error
        return object()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "audit.jsonl"


def audit_service(tmp_path: Path) -> AuditService:
    return AuditService(AuditJournalWriter(journal_path(tmp_path)))


def build_app(
    tmp_path: Path,
    *,
    employees=None,
    attendance=None,
    login=None,
    sessions=None,
    subsystem: AuditSubsystem | None = None,
    principal: object = PRINCIPAL,
):
    employees = employees or FakeEmployees()
    attendance = attendance or FakeAttendance()
    login = login or FakeLogin()
    sessions = sessions or FakeSessions()
    subsystem = subsystem or AuditSubsystem(
        builder=lambda: audit_service(tmp_path)
    )
    settings = AppSettings(
        app_env="testing",
        login_trusted_origins=(ORIGIN,),
        data_dir=tmp_path / "data",
    )
    app = create_app(
        settings,
        employee_service=employees,
        attendance_service=attendance,
        login_service=login,
        session_service=sessions,
        principal_resolver=lambda request: principal,
        audit_subsystem=subsystem,
    )
    return app, employees, attendance, login, sessions


def events(tmp_path: Path) -> list[AuditEvent]:
    path = journal_path(tmp_path)
    if not path.exists():
        return []
    return [
        AuditEvent.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def restricted(error: BaseException) -> AuditSubsystem:
    def fail():
        raise error

    return AuditSubsystem(builder=fail)


def auth_headers():
    return [("origin", ORIGIN)]


def create_employee(app, body=None, headers=None):
    return call(
        app,
        method="POST",
        path=f"{API_PREFIX}/employees",
        body=body or {"employee_id": "E1", "full_name": "Name"},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Initialization and restricted mode
# ---------------------------------------------------------------------------


def test_health_is_anonymous_and_liveness_only(tmp_path: Path) -> None:
    app, *_ = build_app(tmp_path)
    response = call(app, method="GET", path=f"{API_PREFIX}/health")
    assert response["status"] == 200
    assert response["payload"] == {"status": "ok"}


def test_restricted_subsystem_blocks_every_mutation_before_business(
    tmp_path: Path,
) -> None:
    companies = FakeEmployees()
    attendance = FakeAttendance()
    app, employees, attendance, *_ = build_app(
        tmp_path,
        employees=companies,
        attendance=attendance,
        subsystem=restricted(AuditJournalIntegrityError("unverifiable")),
    )
    responses = [
        create_employee(app),
        call(
            app,
            method="PATCH",
            path=f"{API_PREFIX}/employees/E1",
            body={"full_name": "New"},
        ),
        call(app, method="POST", path=f"{API_PREFIX}/employees/E1/deactivate"),
        call(
            app,
            method="POST",
            path=f"{API_PREFIX}/attendance/E1/checkout",
            body={},
        ),
    ]
    for response in responses:
        assert response["status"] == 503
        assert response["payload"]["error"] == {
            "code": "AUDIT_UNAVAILABLE",
            "message": (
                "The operation was not attempted because it could not be "
                "audited."
            ),
        }
    assert companies.calls == []
    assert attendance.calls == []
    assert not journal_path(tmp_path).exists()


def test_restricted_mode_keeps_reads_available(tmp_path: Path) -> None:
    app, *_ = build_app(
        tmp_path, subsystem=restricted(AuditJournalUnavailableError("x"))
    )
    response = call(app, method="GET", path=f"{API_PREFIX}/employees")
    assert response["status"] == 200


@pytest.mark.parametrize(
    "error",
    [
        AuditJournalUnavailableError("unavailable"),
        AuditJournalCorruptError("corrupt"),
        AuditJournalIntegrityError("integrity"),
        AuditJournalContendedError("contended"),
    ],
)
def test_initialization_failure_enters_restricted_without_replacement(
    tmp_path: Path, error: BaseException
) -> None:
    app, employees, *_ = build_app(tmp_path, subsystem=restricted(error))
    response = create_employee(app)
    assert response["status"] == 503
    assert employees.calls == []
    assert not journal_path(tmp_path).exists()
    subsystem = app.state.audit_subsystem
    assert subsystem.state == "restricted"
    assert subsystem.failure == type(error).__name__


def test_corrupt_journal_is_not_repaired_or_replaced(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    path.parent.mkdir(parents=True)
    original = b'{"not":"a record"}\n'
    path.write_bytes(original)
    app, employees, *_ = build_app(tmp_path)
    response = create_employee(app)
    assert response["status"] == 503
    assert employees.calls == []
    assert path.read_bytes() == original
    assert app.state.audit_subsystem.state == "restricted"


def test_client_request_id_is_ignored(tmp_path: Path) -> None:
    app, *_ = build_app(tmp_path)
    attacker = "11111111-1111-4111-8111-111111111111"
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/employees",
        body={"employee_id": "E1", "full_name": "Name"},
        headers=[("x-request-id", attacker)],
    )
    assert response["status"] == 201
    intent, result = events(tmp_path)[:2]
    server_id = response["headers"]
    echoed = next(value for key, value in server_id if key == "x-request-id")
    assert intent.request_id == echoed
    assert intent.request_id != attacker
    assert result.request_id == echoed
    Intent = intent.audit_id
    assert result.correlation_id == Intent


# ---------------------------------------------------------------------------
# Employee create
# ---------------------------------------------------------------------------


def test_create_writes_intent_before_business_and_correlated_success(
    tmp_path: Path,
) -> None:
    employees = FakeEmployees()
    seen: list[int] = []
    employees.on_create = lambda: seen.append(len(events(tmp_path)))
    app, *_ = build_app(tmp_path, employees=employees)
    response = create_employee(app)
    assert response["status"] == 201
    assert seen == [1]
    intent, result = events(tmp_path)
    assert intent.action is AuditAction.EMPLOYEE_CREATE
    assert intent.outcome is AuditOutcome.UNCERTAIN
    assert intent.reason_code is AuditReasonCode.AWAITING_RESULT
    assert intent.correlation_id is None
    assert result.correlation_id == intent.audit_id
    assert result.outcome is AuditOutcome.SUCCESS
    assert result.reason_code is AuditReasonCode.OK
    assert result.target_type.value == "employee"
    assert result.target_id == "E1"
    assert result.actor_user_id == "user-1"
    assert result.source.value == "api"
    assert result.metadata["http_status"] == 201


def test_unrepresentable_target_is_refused_before_business(
    tmp_path: Path,
) -> None:
    employees = FakeEmployees()
    app, *_ = build_app(tmp_path, employees=employees)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/employees",
        body={"employee_id": "E" * 257, "full_name": "Name"},
    )
    assert response["status"] == 503
    assert response["payload"]["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert employees.calls == []
    assert events(tmp_path) == []


def test_empty_employee_id_reaches_validation_without_audit_event(
    tmp_path: Path,
) -> None:
    employees = FakeEmployees()
    app, *_ = build_app(tmp_path, employees=employees)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/employees",
        body={"employee_id": "", "full_name": "Name"},
    )
    assert response["status"] == 422
    assert response["payload"]["error"]["code"] == "EMPLOYEE_INVALID"
    assert employees.calls == []
    assert events(tmp_path) == []


def test_create_duplicate_records_matching_error_result(tmp_path: Path) -> None:
    employees = FakeEmployees()
    employees.create_error = EmployeeAlreadyExists("duplicate")
    app, *_ = build_app(tmp_path, employees=employees)
    response = create_employee(app)
    assert response["status"] == 409
    result = events(tmp_path)[1]
    assert result.outcome is AuditOutcome.ERROR
    assert result.reason_code is AuditReasonCode.EMPLOYEE_EXISTS


def test_create_validation_failure_records_error_result(tmp_path: Path) -> None:
    app, *_ = build_app(tmp_path)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/employees",
        body={"employee_id": "E1", "full_name": ""},
    )
    assert response["status"] == 422
    result = events(tmp_path)[1]
    assert result.reason_code is AuditReasonCode.EMPLOYEE_INVALID


def test_create_database_unavailable_is_confirmed_not_applied(
    tmp_path: Path,
) -> None:
    from src.db import DatabaseUnavailableError

    employees = FakeEmployees()
    employees.create_error = DatabaseUnavailableError("down secret detail")
    app, *_ = build_app(tmp_path, employees=employees)
    response = create_employee(app)
    assert response["status"] == 503
    assert response["payload"]["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert "secret detail" not in response["body"].decode()
    result = events(tmp_path)[1]
    assert result.outcome is AuditOutcome.ERROR
    assert result.reason_code is AuditReasonCode.DATABASE_UNAVAILABLE


def test_create_unknown_database_outcome_records_uncertain(tmp_path: Path) -> None:
    class DriverError(Exception):
        sqlstate = "08006"

    employees = FakeEmployees()
    employees.create_error = DriverError("connection lost after insert")
    app, *_ = build_app(tmp_path, employees=employees)
    response = create_employee(app)
    assert response["status"] == 503
    assert response["payload"]["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert "could not be confirmed" in response["payload"]["error"]["message"]
    result = events(tmp_path)[1]
    assert result.outcome is AuditOutcome.UNCERTAIN
    assert result.reason_code is AuditReasonCode.DATABASE_OUTCOME_UNKNOWN


def test_unknown_non_database_error_stays_internal_error(tmp_path: Path) -> None:
    employees = FakeEmployees()
    employees.create_error = RuntimeError("programming bug")
    app, *_ = build_app(tmp_path, employees=employees)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/employees",
        body={"employee_id": "E1", "full_name": "Name"},
        swallow=True,
    )
    assert response["status"] == 500
    assert response["payload"]["error"]["code"] == "INTERNAL_ERROR"
    result = events(tmp_path)[1]
    assert result.outcome is AuditOutcome.UNCERTAIN
    assert result.metadata["http_status"] == 500


def test_applied_mutation_result_failure_returns_audit_result_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = writer_module._fsync_file
    calls = {"n": 0}

    def flaky(handle) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("injected result failure")
        real_fsync(handle)

    monkeypatch.setattr(writer_module, "_fsync_file", flaky)
    app, employees, *_ = build_app(tmp_path)
    response = create_employee(app)
    monkeypatch.undo()

    assert len(employees.calls) == 1
    assert response["status"] == 503
    assert response["payload"]["error"] == {
        "code": "AUDIT_RESULT_UNCERTAIN",
        "message": (
            "The operation may have been applied, but its audit result "
            "could not be recorded. Do not retry blindly."
        ),
    }
    assert app.state.audit_subsystem.state == "ready"
    # Exactly one business call and no automatic retry.
    assert [call_[0] for call_ in employees.calls] == ["create"]


def test_confirmed_not_applied_result_failure_keeps_business_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = writer_module._fsync_file
    calls = {"n": 0}

    def flaky(handle) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("injected result failure")
        real_fsync(handle)

    employees = FakeEmployees()
    employees.create_error = EmployeeAlreadyExists("duplicate")
    monkeypatch.setattr(writer_module, "_fsync_file", flaky)
    app, *_ = build_app(tmp_path, employees=employees)
    response = create_employee(app)
    monkeypatch.undo()

    assert response["status"] == 409
    assert response["payload"]["error"]["code"] == "EMPLOYEE_EXISTS"


# ---------------------------------------------------------------------------
# Other mandatory mutations
# ---------------------------------------------------------------------------


def test_update_success_and_not_found_results(tmp_path: Path) -> None:
    app, employees, *_ = build_app(tmp_path)
    response = call(
        app,
        method="PATCH",
        path=f"{API_PREFIX}/employees/E1",
        body={"full_name": "New"},
    )
    assert response["status"] == 200
    intent, result = events(tmp_path)
    assert intent.action is AuditAction.EMPLOYEE_UPDATE
    assert result.correlation_id == intent.audit_id
    assert result.outcome is AuditOutcome.SUCCESS

    employees.rename_error = EmployeeNotFound("missing")
    response = call(
        app,
        method="PATCH",
        path=f"{API_PREFIX}/employees/E2",
        body={"full_name": "New"},
    )
    assert response["status"] == 404
    last = events(tmp_path)[-1]
    assert last.reason_code is AuditReasonCode.EMPLOYEE_NOT_FOUND
    assert last.outcome is AuditOutcome.ERROR


def test_deactivate_success_and_not_found_results(tmp_path: Path) -> None:
    app, employees, *_ = build_app(tmp_path)
    response = call(
        app, method="POST", path=f"{API_PREFIX}/employees/E1/deactivate"
    )
    assert response["status"] == 200
    intent, result = events(tmp_path)
    assert intent.action is AuditAction.EMPLOYEE_DEACTIVATE
    assert result.outcome is AuditOutcome.SUCCESS

    employees.deactivate_error = EmployeeNotFound("missing")
    response = call(
        app, method="POST", path=f"{API_PREFIX}/employees/E2/deactivate"
    )
    assert response["status"] == 404
    assert (
        events(tmp_path)[-1].reason_code
        is AuditReasonCode.EMPLOYEE_NOT_FOUND
    )


def test_checkout_writes_intent_before_the_business_operation(
    tmp_path: Path,
) -> None:
    attendance = FakeAttendance()
    seen: list[int] = []
    attendance.on_checkout = lambda: seen.append(len(events(tmp_path)))
    app, *_ = build_app(tmp_path, attendance=attendance)
    call(
        app,
        method="POST",
        path=f"{API_PREFIX}/attendance/E1/checkout",
        body={},
    )
    assert seen == [1]


def test_checkout_success_records_journal_event_id(tmp_path: Path) -> None:
    attendance = FakeAttendance()
    app, *_ = build_app(tmp_path, attendance=attendance)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/attendance/E1/checkout",
        body={},
    )
    assert response["status"] == 201
    intent, result = events(tmp_path)
    assert intent.action is AuditAction.ATTENDANCE_CHECKOUT
    assert result.outcome is AuditOutcome.SUCCESS
    assert result.metadata["journal_event_id"] == "ev-1"


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("EMPLOYEE_NOT_FOUND", AuditReasonCode.EMPLOYEE_NOT_FOUND),
        ("EMPLOYEE_INACTIVE", AuditReasonCode.EMPLOYEE_INACTIVE),
        ("NO_OPEN_SESSION", AuditReasonCode.NO_OPEN_SESSION),
        ("CHECKOUT_COOLDOWN", AuditReasonCode.CHECKOUT_COOLDOWN),
    ],
)
def test_checkout_business_refusals_map_to_reason_codes(
    tmp_path: Path, code: str, reason: AuditReasonCode
) -> None:
    attendance = FakeAttendance()
    attendance.error = CheckoutRejected(code)
    app, *_ = build_app(tmp_path, attendance=attendance)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/attendance/E1/checkout",
        body={},
    )
    assert response["status"] in {404, 409}
    result = events(tmp_path)[1]
    assert result.outcome is AuditOutcome.ERROR
    assert result.reason_code is reason


def test_checkout_persistence_uncertain_is_unknown(tmp_path: Path) -> None:
    from src.attendance_store import AttendanceStorePersistenceError

    attendance = FakeAttendance()
    attendance.error = AttendanceStorePersistenceError("possibly written")
    app, *_ = build_app(tmp_path, attendance=attendance)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/attendance/E1/checkout",
        body={},
    )
    assert response["status"] == 503
    assert response["payload"]["error"]["code"] == "PERSISTENCE_UNCONFIRMED"
    result = events(tmp_path)[1]
    assert result.outcome is AuditOutcome.UNCERTAIN
    assert result.reason_code is AuditReasonCode.PERSISTENCE_UNCONFIRMED
    assert len(attendance.calls) == 1


def test_checkout_corrupt_journal_is_not_applied(tmp_path: Path) -> None:
    from src.attendance_store import CorruptAttendanceJournalError

    attendance = FakeAttendance()
    attendance.error = CorruptAttendanceJournalError("corrupt")
    app, *_ = build_app(tmp_path, attendance=attendance)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/attendance/E1/checkout",
        body={},
    )
    assert response["status"] == 503
    result = events(tmp_path)[1]
    assert result.outcome is AuditOutcome.ERROR
    assert result.reason_code is AuditReasonCode.DATABASE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Login / logout / denial best-effort
# ---------------------------------------------------------------------------


def test_login_success_records_actor_and_account(tmp_path: Path) -> None:
    app, *_ = build_app(tmp_path)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/auth/login",
        body={"username": "alice", "password": "correct horse"},
        headers=auth_headers(),
    )
    assert response["status"] == 200
    event = events(tmp_path)[-1]
    assert event.action is AuditAction.AUTH_LOGIN
    assert event.outcome is AuditOutcome.SUCCESS
    assert event.actor_user_id == "user-1"
    assert event.target_type.value == "account"
    assert event.target_id == "user-1"


def test_login_failure_records_no_username_and_null_target(
    tmp_path: Path,
) -> None:
    login = FakeLogin()
    login.error = AuthenticationRequired()
    app, *_ = build_app(tmp_path, login=login)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/auth/login",
        body={"username": "alice", "password": "wrong"},
        headers=auth_headers(),
    )
    assert response["status"] == 401
    event = events(tmp_path)[-1]
    assert event.outcome is AuditOutcome.DENIED
    assert event.reason_code is AuditReasonCode.INVALID_CREDENTIALS
    assert event.actor_user_id is None
    assert event.target_type.value == "account"
    assert event.target_id is None
    raw = journal_path(tmp_path).read_text(encoding="utf-8")
    assert "alice" not in raw
    assert "wrong" not in raw


def test_login_rate_limited_is_audited_best_effort(tmp_path: Path) -> None:
    login = FakeLogin()
    login.error = LoginRateLimited(30)
    app, *_ = build_app(tmp_path, login=login)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/auth/login",
        body={"username": "alice", "password": "wrong"},
        headers=auth_headers(),
    )
    assert response["status"] == 429
    assert events(tmp_path)[-1].reason_code is AuditReasonCode.RATE_LIMITED


def test_login_response_unaffected_by_audit_failure(tmp_path: Path) -> None:
    app, *_ = build_app(
        tmp_path, subsystem=restricted(AuditJournalUnavailableError("x"))
    )
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/auth/login",
        body={"username": "alice", "password": "correct horse"},
        headers=auth_headers(),
    )
    assert response["status"] == 200
    assert response["payload"]["user"]["user_id"] == "user-1"


def test_logout_success_records_actor(tmp_path: Path) -> None:
    app, *_ = build_app(tmp_path)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/auth/logout",
        headers=[("origin", ORIGIN), ("cookie", "va_session=live-token")],
    )
    assert response["status"] == 204
    event = events(tmp_path)[-1]
    assert event.outcome is AuditOutcome.SUCCESS
    assert event.actor_user_id == "user-1"
    assert event.target_id == "user-1"


def test_logout_invalid_session_records_null_actor(tmp_path: Path) -> None:
    sessions = FakeSessions()
    sessions.validate_error = AuthenticationError("invalid")
    app, *_ = build_app(tmp_path, sessions=sessions)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/auth/logout",
        headers=[("origin", ORIGIN), ("cookie", "va_session=bad")],
    )
    assert response["status"] == 204
    event = events(tmp_path)[-1]
    assert event.outcome is AuditOutcome.ERROR
    assert event.reason_code is AuditReasonCode.SESSION_INVALID
    assert event.actor_user_id is None
    assert event.target_type is None


def test_logout_without_cookie_records_nothing(tmp_path: Path) -> None:
    app, *_ = build_app(tmp_path)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/auth/logout",
        headers=[("origin", ORIGIN)],
    )
    assert response["status"] == 204
    assert events(tmp_path) == []


def test_denied_authorization_records_route_template(tmp_path: Path) -> None:
    principal = AuthenticatedPrincipal(
        user_id="user-1", username="alice", role=Role.AUDITOR, is_active=True
    )
    app, employees, *_ = build_app(tmp_path, principal=principal)
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/employees",
        body={"employee_id": "E1", "full_name": "Name"},
    )
    assert response["status"] == 403
    assert employees.calls == []
    event = events(tmp_path)[-1]
    assert event.action is AuditAction.AUTHZ_DENIED
    assert event.outcome is AuditOutcome.DENIED
    assert event.target_type is None
    assert event.target_id is None
    assert dict(event.metadata) == {
        "endpoint": "/api/v1/employees",
        "method": "POST",
        "http_status": 403,
    }


def test_successful_authorization_records_no_denial(tmp_path: Path) -> None:
    app, *_ = build_app(tmp_path)
    response = call(app, method="GET", path=f"{API_PREFIX}/employees")
    assert response["status"] == 200
    assert events(tmp_path) == []


def test_denial_audit_failure_does_not_change_403(tmp_path: Path) -> None:
    principal = AuthenticatedPrincipal(
        user_id="user-1", username="alice", role=Role.AUDITOR, is_active=True
    )
    app, *_ = build_app(
        tmp_path,
        principal=principal,
        subsystem=restricted(AuditJournalUnavailableError("x")),
    )
    response = call(
        app,
        method="POST",
        path=f"{API_PREFIX}/employees",
        body={"employee_id": "E1", "full_name": "Name"},
    )
    assert response["status"] == 403
    assert response["payload"]["error"]["code"] == "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


def test_no_secrets_or_biometric_material_in_records(tmp_path: Path) -> None:
    app, *_ = build_app(tmp_path)
    create_employee(app)
    call(
        app,
        method="POST",
        path=f"{API_PREFIX}/auth/login",
        body={"username": "alice", "password": "correct horse"},
        headers=auth_headers(),
    )
    raw = journal_path(tmp_path).read_text(encoding="utf-8")
    for forbidden in (
        "alice",
        "correct horse",
        "session-token-value",
        "password",
        "va_session",
        "embedding",
        "face",
        "postgresql://",
    ):
        assert forbidden not in raw
