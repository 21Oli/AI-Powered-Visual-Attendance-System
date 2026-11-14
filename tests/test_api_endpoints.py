"""Offline tests for the implemented Phase 3 API endpoints (Step 40).

Everything here is hardware-free and database-free: the employee repository is
an in-memory double, and the attendance side runs the **real**
:class:`src.attendance_store.AttendanceEventStore` over a ``tmp_path`` journal so
the store's validation, torn-tail and corruption behaviour is exercised for
real.

No test connects to PostgreSQL, reads a DSN, touches a camera, or uses real
employee/biometric data.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.api import API_PREFIX, APPROVED_ROUTES, create_app
from src.api_auth import AuthenticatedPrincipal
from src.api_service import AttendanceQueryService, EmployeeQueryService
from src.attendance import AttendanceEvent, EventType
from src.attendance_store import AttendanceEventStore
from src.config import AppSettings
from src.employee import Employee
from src.employee_repository import EmployeeAlreadyExists, EmployeeNotFound
from src.user import Role

#: Offline business-logic tests have no session cookie; the app's documented
#: test seam resolves this Admin principal instead. The RBAC guard still runs,
#: so these tests exercise the *authorized* path only (P5.4.10 semantics are
#: covered by tests/test_api_rbac.py).
_TEST_PRINCIPAL = AuthenticatedPrincipal(
    user_id="test-admin",
    username="test-admin",
    role=Role.ADMIN,
    is_active=True,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeEmployeeRepository:
    """In-memory stand-in for the approved repository contract.

    Raises the *real* typed repository errors so the API's mapping is tested
    against the production taxonomy rather than a test-only one.
    """

    def __init__(self) -> None:
        self.rows: dict[str, Employee] = {}

    def create(self, employee: Employee) -> Employee:
        if employee.employee_id in self.rows:
            raise EmployeeAlreadyExists("duplicate")
        self.rows[employee.employee_id] = employee
        return employee

    def get(self, employee_id: str) -> Employee | None:
        return self.rows.get(employee_id.strip())

    def list(self, *, after: str | None = None, limit: int | None = None):
        keys = sorted(self.rows)
        if after is not None:
            keys = [key for key in keys if key > after]
        page = keys[: (limit or 20)]
        return [self.rows[key] for key in page]

    def update_full_name(self, employee_id: str, full_name: str) -> Employee:
        current = self.rows.get(employee_id.strip())
        if current is None:
            raise EmployeeNotFound("missing")
        updated = Employee(
            employee_id=current.employee_id,
            full_name=full_name,
            is_active=current.is_active,
            created_at=current.created_at,
        )
        self.rows[updated.employee_id] = updated
        return updated

    def deactivate(self, employee_id: str) -> Employee:
        current = self.rows.get(employee_id.strip())
        if current is None:
            raise EmployeeNotFound("missing")
        updated = Employee(
            employee_id=current.employee_id,
            full_name=current.full_name,
            is_active=False,
            created_at=current.created_at,
        )
        self.rows[updated.employee_id] = updated
        return updated


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


@contextmanager
def _serve(app):
    """Real loopback uvicorn server on an ephemeral port."""
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start in time")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _call(base: str, method: str, path: str, body: object | None = None):
    data = None if body is None else json.dumps(body).encode()
    headers = {} if data is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(
        base + path, method=method, data=data, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode() or "{}")


# ---------------------------------------------------------------------------
# Fixture: real store over a temporary journal + in-memory repository
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_path: Path):
    settings = AppSettings(data_dir=tmp_path)
    store = AttendanceEventStore(settings)
    repository = FakeEmployeeRepository()
    app = create_app(
        settings,
        employee_service=EmployeeQueryService(repository),
        attendance_service=AttendanceQueryService(store),
        principal_resolver=lambda request: _TEST_PRINCIPAL,
    )
    with _serve(app) as base:
        yield base, repository, store


def _event(employee_id: str, event_type: EventType, offset_minutes: int = 0):
    return AttendanceEvent(
        event_id=f"ev-{employee_id}-{event_type.value}-{offset_minutes}",
        employee_id=employee_id,
        event_type=event_type,
        occurred_at=datetime.now(UTC) + timedelta(minutes=offset_minutes),
    )


# ---------------------------------------------------------------------------
# Health / route inventory
# ---------------------------------------------------------------------------


def test_health_is_ok(api) -> None:
    base, _, _ = api
    status, payload = _call(base, "GET", f"{API_PREFIX}/health")
    assert status == 200
    assert payload == {"status": "ok"}


def test_route_inventory_is_still_exactly_the_approved_eight() -> None:
    app = create_app()
    pairs = set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not path:
            continue
        for method in methods:
            if method != "HEAD":
                pairs.add((path, method))
    expected = {(f"{API_PREFIX}{path}", method) for path, method in APPROVED_ROUTES}
    assert pairs == expected


def test_no_endpoint_answers_501_any_more(api) -> None:
    base, _, _ = api
    for path, method in APPROVED_ROUTES:
        if path == "/health":
            continue
        status, payload = _call(base, method, f"{API_PREFIX}{path}")
        assert status != 501, f"{method} {path} still reports NOT_IMPLEMENTED"
        assert payload.get("error", {}).get("code") != "NOT_IMPLEMENTED"


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------


def test_create_employee_returns_201_and_the_four_metadata_fields(api) -> None:
    base, _, _ = api
    status, payload = _call(
        base,
        "POST",
        f"{API_PREFIX}/employees",
        {"employee_id": "007", "full_name": "  Ada   Lovelace "},
    )
    assert status == 201
    assert set(payload) == {"employee_id", "full_name", "is_active", "created_at"}
    assert payload["employee_id"] == "007"
    assert payload["full_name"] == "Ada Lovelace"
    assert payload["is_active"] is True
    datetime.fromisoformat(payload["created_at"])


def test_duplicate_employee_returns_409(api) -> None:
    base, _, _ = api
    body = {"employee_id": "EMP-1", "full_name": "Grace Hopper"}
    assert _call(base, "POST", f"{API_PREFIX}/employees", body)[0] == 201
    status, payload = _call(base, "POST", f"{API_PREFIX}/employees", body)
    assert status == 409
    assert payload["error"]["code"] == "EMPLOYEE_EXISTS"


def test_invalid_employee_input_returns_422(api) -> None:
    base, _, _ = api
    status, payload = _call(
        base, "POST", f"{API_PREFIX}/employees", {"employee_id": "  ", "full_name": "X"}
    )
    assert status == 422
    assert payload["error"]["code"] == "EMPLOYEE_INVALID"


def test_client_cannot_supply_created_at(api) -> None:
    base, _, _ = api
    status, _ = _call(
        base,
        "POST",
        f"{API_PREFIX}/employees",
        {"employee_id": "E", "full_name": "N", "created_at": "2020-01-01T00:00:00Z"},
    )
    assert status == 422


def test_list_employees_uses_keyset_pagination(api) -> None:
    base, _, _ = api
    for index in range(3):
        _call(
            base,
            "POST",
            f"{API_PREFIX}/employees",
            {"employee_id": f"E{index}", "full_name": f"Name {index}"},
        )
    status, page = _call(base, "GET", f"{API_PREFIX}/employees?limit=2")
    assert status == 200
    assert [row["employee_id"] for row in page["employees"]] == ["E0", "E1"]
    assert page["next_after"] == "E1"
    assert page["limit"] == 2

    status, rest = _call(
        base, "GET", f"{API_PREFIX}/employees?limit=2&after=E1"
    )
    assert status == 200
    assert [row["employee_id"] for row in rest["employees"]] == ["E2"]
    # A short page means the keyset walk is exhausted.
    assert rest["next_after"] is None


def test_list_employees_clamps_limit_at_one_hundred(api) -> None:
    base, _, _ = api
    status, page = _call(base, "GET", f"{API_PREFIX}/employees?limit=500")
    assert status == 200
    assert page["limit"] == 100


def test_list_employees_rejects_invalid_page_size(api) -> None:
    base, _, _ = api
    status, payload = _call(base, "GET", f"{API_PREFIX}/employees?limit=0")
    assert status == 422
    assert payload["error"]["code"] == "EMPLOYEE_INVALID"


def test_get_missing_employee_returns_404(api) -> None:
    base, _, _ = api
    status, payload = _call(base, "GET", f"{API_PREFIX}/employees/nope")
    assert status == 404
    assert payload["error"]["code"] == "EMPLOYEE_NOT_FOUND"


def test_patch_updates_full_name_only(api) -> None:
    base, repository, _ = api
    _call(
        base,
        "POST",
        f"{API_PREFIX}/employees",
        {"employee_id": "E1", "full_name": "Old Name"},
    )
    original = repository.rows["E1"]
    status, payload = _call(
        base, "PATCH", f"{API_PREFIX}/employees/E1", {"full_name": "New Name"}
    )
    assert status == 200
    assert payload["full_name"] == "New Name"
    # is_active and created_at are untouched by the name update.
    assert payload["is_active"] is True
    assert payload["created_at"] == original.created_at.isoformat()

    status, _ = _call(
        base, "PATCH", f"{API_PREFIX}/employees/E1", {"is_active": False}
    )
    assert status == 422


def test_patch_missing_employee_returns_404(api) -> None:
    base, _, _ = api
    status, payload = _call(
        base, "PATCH", f"{API_PREFIX}/employees/ghost", {"full_name": "X"}
    )
    assert status == 404
    assert payload["error"]["code"] == "EMPLOYEE_NOT_FOUND"


def test_deactivate_sets_is_active_false(api) -> None:
    base, _, _ = api
    _call(
        base,
        "POST",
        f"{API_PREFIX}/employees",
        {"employee_id": "E1", "full_name": "Name"},
    )
    status, payload = _call(base, "POST", f"{API_PREFIX}/employees/E1/deactivate")
    assert status == 200
    assert payload["is_active"] is False

    status, payload = _call(base, "POST", f"{API_PREFIX}/employees/ghost/deactivate")
    assert status == 404


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


def test_attendance_empty_journal_is_an_empty_list_not_an_error(api) -> None:
    base, _, _ = api
    status, payload = _call(base, "GET", f"{API_PREFIX}/attendance")
    assert status == 200
    assert payload == {
        "events": [],
        "count": 0,
        "incomplete_trailing_record": False,
    }


def test_attendance_returns_validated_journal_records(api) -> None:
    base, _, store = api
    store.append(_event("E1", EventType.CHECK_IN))
    store.append(_event("E1", EventType.CHECK_OUT, 5))
    status, payload = _call(base, "GET", f"{API_PREFIX}/attendance")
    assert status == 200
    assert payload["count"] == 2
    assert [row["event_type"] for row in payload["events"]] == [
        "check_in",
        "check_out",
    ]
    assert set(payload["events"][0]) == {
        "event_id",
        "employee_id",
        "event_type",
        "occurred_at",
    }


def test_corrupt_journal_returns_503_and_is_not_repaired(api) -> None:
    base, _, store = api
    journal = store.journal_path
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text('{"not":"a record"}\n', encoding="utf-8")
    before = journal.read_text(encoding="utf-8")

    status, payload = _call(base, "GET", f"{API_PREFIX}/attendance")
    assert status == 503
    assert payload["error"]["code"] == "ATTENDANCE_UNAVAILABLE"
    # Reading never rewrites or repairs the journal.
    assert journal.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------


def test_checkout_missing_employee_returns_404(api) -> None:
    base, _, _ = api
    status, payload = _call(base, "POST", f"{API_PREFIX}/attendance/ghost/checkout")
    assert status == 404
    assert payload["error"]["code"] == "EMPLOYEE_NOT_FOUND"


def test_checkout_inactive_employee_returns_409(api) -> None:
    base, repository, store = api
    _call(
        base,
        "POST",
        f"{API_PREFIX}/employees",
        {"employee_id": "E1", "full_name": "Name", "is_active": False},
    )
    store.append(_event("E1", EventType.CHECK_IN))
    status, payload = _call(base, "POST", f"{API_PREFIX}/attendance/E1/checkout")
    assert status == 409
    assert payload["error"]["code"] == "CHECKOUT_REJECTED"
    assert payload["error"]["reason"] == "EMPLOYEE_INACTIVE"


def test_checkout_without_open_session_returns_409(api) -> None:
    base, _, _ = api
    _call(
        base,
        "POST",
        f"{API_PREFIX}/employees",
        {"employee_id": "E1", "full_name": "Name"},
    )
    status, payload = _call(base, "POST", f"{API_PREFIX}/attendance/E1/checkout")
    assert status == 409
    assert payload["error"]["reason"] == "NO_OPEN_SESSION"


def test_checkout_after_check_in_returns_201_and_appends_durably(api) -> None:
    base, _, store = api
    _call(
        base,
        "POST",
        f"{API_PREFIX}/employees",
        {"employee_id": "E1", "full_name": "Name"},
    )
    store.append(_event("E1", EventType.CHECK_IN))

    status, payload = _call(base, "POST", f"{API_PREFIX}/attendance/E1/checkout")
    assert status == 201
    assert payload["event_type"] == "check_out"
    assert payload["employee_id"] == "E1"

    # The durable append is observable in the journal (validated reload).
    events = store.load_events().events
    assert [event.event_type for event in events] == [
        EventType.CHECK_IN,
        EventType.CHECK_OUT,
    ]

    # A second check-out now has no open session.
    status, payload = _call(base, "POST", f"{API_PREFIX}/attendance/E1/checkout")
    assert status == 409
    assert payload["error"]["reason"] == "NO_OPEN_SESSION"


def test_checkout_within_cooldown_returns_409(api) -> None:
    """The engine's check-out cooldown is enforced through the HTTP path."""
    base, _, store = api
    _call(
        base,
        "POST",
        f"{API_PREFIX}/employees",
        {"employee_id": "E1", "full_name": "Name"},
    )
    store.append(_event("E1", EventType.CHECK_IN, -10))
    assert _call(base, "POST", f"{API_PREFIX}/attendance/E1/checkout")[0] == 201

    # A fresh open session inside the 300 s window: the cooldown declines it.
    store.append(_event("E1", EventType.CHECK_IN, 1))
    status, payload = _call(
        base, "POST", f"{API_PREFIX}/attendance/E1/checkout"
    )
    assert status == 409
    assert payload["error"]["code"] == "CHECKOUT_REJECTED"
    assert payload["error"]["reason"] == "CHECKOUT_COOLDOWN"


def test_checkout_rejects_a_fabricated_body(api) -> None:
    """The body is empty by contract: a client cannot supply an event ID."""
    base, _, _ = api
    status, _ = _call(
        base,
        "POST",
        f"{API_PREFIX}/attendance/E1/checkout",
        {"event_id": "forged", "occurred_at": "2020-01-01T00:00:00Z"},
    )
    assert status == 422


def test_checkout_does_not_fabricate_verification_evidence(api) -> None:
    """The route must not import or construct a TemporalVerificationResult.

    The engine's check-out path is authorized only by camera evidence; the API
    deliberately appends an operator-authorized event instead.
    """
    source = (Path("src") / "api_service.py").read_text(encoding="utf-8")
    assert "TemporalVerificationResult" in source  # mentioned only in prose
    for forbidden in (
        "TemporalVerificationResult(",
        "from src.temporal_verification import",
    ):
        assert forbidden not in source
