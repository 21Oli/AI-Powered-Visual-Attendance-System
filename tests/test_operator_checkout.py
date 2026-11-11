"""Operator-authorized check-out through the additive engine path.

Covers the corrected architecture: ``AttendanceEngine.operator_checkout`` owns
the check-out policy (active gate, open session, check-out cooldown) and
requires an explicit :class:`OperatorAuthorization`; the API service supplies
that authorization, rebuilds the engine's bounded state from the folded journal,
and appends only after the engine accepted.

All offline: a temporary journal, injected clocks and in-memory employees. No
database, no camera, no biometric data, no live operation.
"""

from __future__ import annotations

import inspect
import textwrap
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.api_service import (
    CHECKOUT_COOLDOWN,
    CHECKOUT_EMPLOYEE_INACTIVE,
    CHECKOUT_NO_EMPLOYEE,
    CHECKOUT_NO_OPEN_SESSION,
    OPERATOR_CHECKOUT_AUTHORIZATION,
    AttendanceQueryService,
    CheckoutRejected,
)
from src.attendance import (
    REASON_AUTHORIZATION_REQUIRED,
    REASON_CHECKOUT_COOLDOWN,
    REASON_INACTIVE_EMPLOYEE,
    REASON_NO_OPEN_SESSION,
    AttendanceDecisionStatus,
    AttendanceEngine,
    AttendanceEvent,
    AttendanceInputError,
    AuthorizationSource,
    EventType,
    OperatorAuthorization,
)
from src.attendance_store import (
    AttendanceEventStore,
    AttendanceStorePersistenceError,
)
from src.config import AppSettings, AttendanceConfig
from src.employee import Employee

_NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeClocks:
    def __init__(self, start: float = 1000.0) -> None:
        self.mono = start

    def mono_now(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.mono += seconds

    def wall(self) -> datetime:
        return _NOW


def _engine(
    clocks: FakeClocks,
    *,
    active: bool | None = None,
    open_session: bool | None = None,
) -> AttendanceEngine:
    return AttendanceEngine(
        config=AttendanceConfig(
            check_in_cooldown_seconds=300.0,
            check_out_cooldown_seconds=300.0,
        ),
        clock_wall=clocks.wall,
        monotonic_clock=clocks.mono_now,
        active_lookup=None if active is None else (lambda _id: active),
        open_session_lookup=(
            None if open_session is None else (lambda _id: open_session)
        ),
        event_id_factory=lambda: f"ev-{clocks.mono}",
    )


def _employee(employee_id: str = "E1", *, active: bool = True) -> Employee:
    return Employee(
        employee_id=employee_id,
        full_name="Ada Lovelace",
        is_active=active,
        created_at=_NOW,
    )


def _event(
    employee_id: str, event_type: EventType, at: datetime, tag: str
) -> AttendanceEvent:
    return AttendanceEvent(
        event_id=tag,
        employee_id=employee_id,
        event_type=event_type,
        occurred_at=at,
    )


@dataclass
class Harness:
    """One temporary journal plus the service and lookup under test."""

    service: AttendanceQueryService
    store: AttendanceEventStore
    lookup: Callable[[str], Employee | None]


class _FailingStore:
    """Delegating store whose append fails (persistence failure/uncertainty)."""

    def __init__(self, inner: AttendanceEventStore, error: Exception) -> None:
        self._inner = inner
        self._error = error
        self.append_calls = 0

    def load_events(self):
        return self._inner.load_events()

    def append(self, event: AttendanceEvent):
        self.append_calls += 1
        raise self._error


def _harness(
    tmp_path: Path,
    *,
    store: object | None = None,
    employee: Employee | None = None,
) -> Harness:
    real = AttendanceEventStore(AppSettings(data_dir=tmp_path))
    employees = {(employee or _employee()).employee_id: (employee or _employee())}
    return Harness(
        service=AttendanceQueryService(store or real),
        store=real,
        lookup=lambda key: employees.get(key),
    )


# ---------------------------------------------------------------------------
# Engine: authorization
# ---------------------------------------------------------------------------


def test_authorized_operator_checkout_creates_a_check_out_event() -> None:
    clocks = FakeClocks()
    engine = _engine(clocks, active=True, open_session=True)

    decision = engine.operator_checkout(
        "E1", authorization=OperatorAuthorization(operator="attendance_api")
    )

    assert decision.status is AttendanceDecisionStatus.EVENT_CREATED
    assert decision.event is not None
    assert decision.event.event_type is EventType.CHECK_OUT
    assert decision.event.employee_id == "E1"
    assert decision.reason_code is None
    assert decision.authorization is AuthorizationSource.OPERATOR_AUTHORIZATION
    assert "operator authorization" in (decision.reason or "")


def test_missing_authorization_is_refused_and_changes_nothing() -> None:
    clocks = FakeClocks()
    engine = _engine(clocks, active=True, open_session=True)

    refused = engine.operator_checkout("E1", authorization=None)

    assert refused.status is AttendanceDecisionStatus.AUTHORIZATION_REQUIRED
    assert refused.event is None
    assert refused.reason_code == REASON_AUTHORIZATION_REQUIRED
    assert refused.authorization is None
    assert engine.tracked_employee_count() == 0

    # Nothing was consumed by the refusal: a properly authorized call works.
    accepted = engine.operator_checkout(
        "E1", authorization=OperatorAuthorization(operator="attendance_api")
    )
    assert accepted.status is AttendanceDecisionStatus.EVENT_CREATED


def test_authorization_object_is_required_not_duck_typed() -> None:
    engine = _engine(FakeClocks(), active=True, open_session=True)
    refused = engine.operator_checkout("E1", authorization="operator")  # type: ignore[arg-type]
    assert refused.status is AttendanceDecisionStatus.AUTHORIZATION_REQUIRED


def test_authorization_cannot_claim_biometric_verification() -> None:
    with pytest.raises(AttendanceInputError):
        OperatorAuthorization(
            operator="attendance_api",
            source=AuthorizationSource.BIOMETRIC_VERIFICATION,
        )


@pytest.mark.parametrize("operator", ["", "   ", None, 7])
def test_authorization_requires_a_named_operator(operator: object) -> None:
    with pytest.raises(AttendanceInputError):
        OperatorAuthorization(operator=operator)  # type: ignore[arg-type]


def test_authorization_source_is_explicitly_operator() -> None:
    authorization = OperatorAuthorization(operator="attendance_api")
    assert authorization.source is AuthorizationSource.OPERATOR_AUTHORIZATION
    assert authorization.source_name == "operator_authorization"


def test_blank_employee_id_is_a_contract_error() -> None:
    engine = _engine(FakeClocks(), active=True, open_session=True)
    with pytest.raises(AttendanceInputError):
        engine.operator_checkout(
            "  ", authorization=OperatorAuthorization(operator="attendance_api")
        )


# ---------------------------------------------------------------------------
# Engine: business rules (the same code the biometric path uses)
# ---------------------------------------------------------------------------


def test_inactive_employee_is_refused_by_the_engine() -> None:
    engine = _engine(FakeClocks(), active=False, open_session=True)
    decision = engine.operator_checkout(
        "E1", authorization=OperatorAuthorization(operator="attendance_api")
    )
    assert decision.status is AttendanceDecisionStatus.INACTIVE_EMPLOYEE
    assert decision.event is None
    assert decision.reason_code == REASON_INACTIVE_EMPLOYEE


def test_no_open_session_is_refused_with_a_stable_code() -> None:
    engine = _engine(FakeClocks(), active=True, open_session=False)
    decision = engine.operator_checkout(
        "E1", authorization=OperatorAuthorization(operator="attendance_api")
    )
    assert decision.status is AttendanceDecisionStatus.SUPPRESSED
    assert decision.reason_code == REASON_NO_OPEN_SESSION
    assert decision.event is None


def test_missing_marker_without_hook_is_refused_honestly() -> None:
    engine = _engine(FakeClocks(), active=True, open_session=None)
    decision = engine.operator_checkout(
        "E1", authorization=OperatorAuthorization(operator="attendance_api")
    )
    assert decision.reason_code == REASON_NO_OPEN_SESSION
    assert "bounded" in (decision.reason or "")


def test_check_out_cooldown_still_applies() -> None:
    clocks = FakeClocks()
    engine = _engine(clocks, active=True, open_session=True)
    authorization = OperatorAuthorization(operator="attendance_api")

    first = engine.operator_checkout("E1", authorization=authorization)
    assert first.status is AttendanceDecisionStatus.EVENT_CREATED

    clocks.advance(299.0)  # inside the 300 s window
    second = engine.operator_checkout("E1", authorization=authorization)
    assert second.status is AttendanceDecisionStatus.SUPPRESSED
    assert second.reason_code == REASON_CHECKOUT_COOLDOWN
    assert second.event is None

    clocks.advance(1.0)  # exactly at the boundary: expired
    third = engine.operator_checkout("E1", authorization=authorization)
    assert third.status is AttendanceDecisionStatus.EVENT_CREATED


def test_refusals_never_touch_engine_state() -> None:
    engine = _engine(FakeClocks(), active=False, open_session=True)
    engine.operator_checkout(
        "E1", authorization=OperatorAuthorization(operator="attendance_api")
    )
    assert engine.tracked_employee_count() == 0


# ---------------------------------------------------------------------------
# Service: delegation, journal authority, persistence boundary
# ---------------------------------------------------------------------------


def test_service_checkout_succeeds_after_a_check_in(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.store.append(
        _event("E1", EventType.CHECK_IN, _NOW - timedelta(minutes=5), "ev-1")
    )

    event = h.service.checkout("E1", employee_lookup=h.lookup)

    assert event.event_type is EventType.CHECK_OUT
    assert [e.event_type for e in h.store.load_events().events] == [
        EventType.CHECK_IN,
        EventType.CHECK_OUT,
    ]


def test_service_reports_unknown_employee(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    with pytest.raises(CheckoutRejected) as excinfo:
        h.service.checkout("ghost", employee_lookup=h.lookup)
    assert excinfo.value.code == CHECKOUT_NO_EMPLOYEE

    with pytest.raises(CheckoutRejected) as blank:
        h.service.checkout("  ", employee_lookup=h.lookup)
    assert blank.value.code == CHECKOUT_NO_EMPLOYEE


def test_service_reports_inactive_employee(tmp_path: Path) -> None:
    inactive = _employee(active=False)
    h = _harness(tmp_path, employee=inactive)
    h.store.append(
        _event("E1", EventType.CHECK_IN, _NOW - timedelta(minutes=5), "ev-1")
    )

    with pytest.raises(CheckoutRejected) as excinfo:
        h.service.checkout("E1", employee_lookup=h.lookup)
    assert excinfo.value.code == CHECKOUT_EMPLOYEE_INACTIVE
    assert len(h.store.load_events().events) == 1


def test_service_reports_no_open_session(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.store.append(
        _event("E1", EventType.CHECK_OUT, _NOW - timedelta(minutes=5), "ev-1")
    )

    with pytest.raises(CheckoutRejected) as excinfo:
        h.service.checkout("E1", employee_lookup=h.lookup)
    assert excinfo.value.code == CHECKOUT_NO_OPEN_SESSION


def test_service_reports_the_check_out_cooldown(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    # The service uses the real wall clock for the age it hands the engine, so
    # the journal timestamps are relative to *now*: check-in, check-out, then a
    # fresh open session — all inside the 300 s window.
    now = datetime.now(UTC)
    h.store.append(
        _event("E1", EventType.CHECK_IN, now - timedelta(minutes=10), "ev-1")
    )
    h.store.append(
        _event("E1", EventType.CHECK_OUT, now - timedelta(seconds=30), "ev-2")
    )
    h.store.append(
        _event("E1", EventType.CHECK_IN, now - timedelta(seconds=5), "ev-3")
    )

    with pytest.raises(CheckoutRejected) as excinfo:
        h.service.checkout("E1", employee_lookup=h.lookup)
    assert excinfo.value.code == CHECKOUT_COOLDOWN
    assert len(h.store.load_events().events) == 3


def test_service_duplicate_check_out_is_refused(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.store.append(
        _event("E1", EventType.CHECK_IN, _NOW - timedelta(minutes=5), "ev-1")
    )
    h.service.checkout("E1", employee_lookup=h.lookup)

    with pytest.raises(CheckoutRejected) as excinfo:
        h.service.checkout("E1", employee_lookup=h.lookup)
    assert excinfo.value.code == CHECKOUT_NO_OPEN_SESSION


def test_persistence_failure_is_never_reported_as_success(tmp_path: Path) -> None:
    real = _harness(tmp_path)
    real.store.append(
        _event("E1", EventType.CHECK_IN, _NOW - timedelta(minutes=5), "ev-1")
    )
    failing = _FailingStore(
        real.store, AttendanceStorePersistenceError("simulated fsync failure")
    )
    h = _harness(tmp_path, store=failing)

    with pytest.raises(AttendanceStorePersistenceError):
        h.service.checkout("E1", employee_lookup=h.lookup)

    # Exactly one attempt, no retry, and no check-out reached the journal.
    assert failing.append_calls == 1
    assert [e.event_type for e in real.store.load_events().events] == [
        EventType.CHECK_IN
    ]


def test_service_passes_the_explicit_operator_authorization(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.store.append(
        _event("E1", EventType.CHECK_IN, _NOW - timedelta(minutes=5), "ev-1")
    )

    recorded: list[object] = []
    real_engine = AttendanceEngine.operator_checkout

    def spy(self, employee_id, *, authorization):
        recorded.append(authorization)
        return real_engine(self, employee_id, authorization=authorization)

    AttendanceEngine.operator_checkout = spy  # type: ignore[method-assign]
    try:
        h.service.checkout("E1", employee_lookup=h.lookup)
    finally:
        AttendanceEngine.operator_checkout = real_engine  # type: ignore[method-assign]

    assert len(recorded) == 1
    assert isinstance(recorded[0], OperatorAuthorization)
    assert recorded[0].source is AuthorizationSource.OPERATOR_AUTHORIZATION
    assert recorded[0] == OPERATOR_CHECKOUT_AUTHORIZATION


# ---------------------------------------------------------------------------
# Regression / architecture guards
# ---------------------------------------------------------------------------


def test_biometric_check_in_is_unchanged() -> None:
    from src.temporal_verification import (
        TemporalVerificationResult,
        TemporalVerificationStatus,
    )

    clocks = FakeClocks()
    engine = _engine(clocks, active=True, open_session=True)
    verified = TemporalVerificationResult(
        status=TemporalVerificationStatus.VERIFIED,
        employee_id="E1",
        streak_frames=5,
        window_frames_used=6,
        reason="ok",
    )

    decision = engine.update(verified)
    assert decision.status is AttendanceDecisionStatus.EVENT_CREATED
    assert decision.event is not None
    assert decision.event.event_type is EventType.CHECK_IN
    # The biometric path's decisions keep their pre-existing shape.
    assert decision.reason_code is None
    assert decision.authorization is None


def test_operator_checkout_takes_no_temporal_verification_result() -> None:
    signature = inspect.signature(AttendanceEngine.operator_checkout)
    annotations = {
        str(parameter.annotation) for parameter in signature.parameters.values()
    }
    assert not any("TemporalVerification" in annotation for annotation in annotations)
    assert "authorization" in signature.parameters
    assert signature.parameters["authorization"].default is inspect.Parameter.empty


def test_api_service_never_constructs_temporal_verification_evidence() -> None:
    source = Path("src", "api_service.py").read_text(encoding="utf-8")
    assert "TemporalVerificationResult(" not in source
    assert "from src.temporal_verification import" not in source


def test_routes_contain_no_check_out_business_rules() -> None:
    source = Path("src", "api.py").read_text(encoding="utf-8")
    checkout_source = textwrap.dedent(
        source.split("def checkout(", 1)[1].split("@app.get", 1)[0]
    )
    for forbidden in (
        "fold_latest_events",
        "AttendanceEngine",
        "check_out_cooldown",
        "is_active",
        "OperatorAuthorization",
    ):
        assert forbidden not in checkout_source, forbidden
    # The route still delegates, and the documented 201 is still declared.
    assert "get_attendance().checkout(" in checkout_source
    assert "status_code=201" in source


class _GatedStore:
    """Delegating store that can hold the first journal load open.

    It also records how many journal loads ever overlapped, which is the direct
    evidence for (or against) serialization of the check-out critical section.
    """

    def __init__(self, inner: AttendanceEventStore, gate: threading.Event) -> None:
        self._inner = inner
        self._gate = gate
        self.entered = threading.Event()
        self._state = threading.Lock()
        self.loads = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.appended: list[AttendanceEvent] = []

    def load_events(self):
        with self._state:
            self.loads += 1
            first = self.loads == 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if first:
                self.entered.set()
                self._gate.wait(5.0)
            return self._inner.load_events()
        finally:
            with self._state:
                self.in_flight -= 1

    def append(self, event: AttendanceEvent):
        with self._state:
            self.appended.append(event)
        return self._inner.append(event)


def test_concurrent_checkouts_are_serialized_and_append_once(
    tmp_path: Path,
) -> None:
    """Two overlapping check-outs cannot both observe the same open session.

    Deterministic overlap: the first request is held inside its journal load
    while the second is already running. Without the critical section the second
    request would load immediately (observing the still-open session) and append
    a second ``CHECK_OUT``.
    """
    real = _harness(tmp_path)
    real.store.append(
        _event("E1", EventType.CHECK_IN, datetime.now(UTC) - timedelta(minutes=5), "ev-1")
    )
    gate = threading.Event()
    gated = _GatedStore(real.store, gate)
    h = _harness(tmp_path, store=gated)

    outcomes: list[object] = []
    second_started = threading.Event()

    def attempt(signal: threading.Event | None) -> None:
        if signal is not None:
            signal.set()
        try:
            outcomes.append(h.service.checkout("E1", employee_lookup=h.lookup))
        except CheckoutRejected as exc:
            outcomes.append(exc.code)

    first = threading.Thread(target=attempt, args=(None,), daemon=True)
    second = threading.Thread(target=attempt, args=(second_started,), daemon=True)
    first.start()
    assert gated.entered.wait(5.0), "first check-out never reached the journal load"
    second.start()
    assert second_started.wait(5.0), "second check-out never started"

    # The second request must be blocked on the critical section, not merely
    # slow: it must not have issued its journal load while the first holds it.
    deadline = time.monotonic() + 1.0
    while gated.loads == 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert gated.loads == 1, "a concurrent check-out entered the critical section"

    gate.set()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive()

    assert gated.max_in_flight == 1
    assert gated.loads == 2
    assert len(gated.appended) == 1
    assert sum(1 for outcome in outcomes if isinstance(outcome, AttendanceEvent)) == 1
    assert [
        outcome
        for outcome in outcomes
        if isinstance(outcome, str)
    ] == [CHECKOUT_NO_OPEN_SESSION]

    # Exactly one durable check-out in the authoritative journal.
    journal = real.store.load_events().events
    assert [event.event_type for event in journal] == [
        EventType.CHECK_IN,
        EventType.CHECK_OUT,
    ]


def test_engine_refuses_a_wrong_type_hook_return() -> None:
    clocks = FakeClocks()
    engine = AttendanceEngine(
        config=AttendanceConfig(
            check_in_cooldown_seconds=300.0,
            check_out_cooldown_seconds=300.0,
        ),
        clock_wall=clocks.wall,
        monotonic_clock=clocks.mono_now,
        active_lookup=lambda _id: "yes",  # type: ignore[return-value]
    )
    with pytest.raises(AttendanceInputError):
        engine.operator_checkout(
            "E1", authorization=OperatorAuthorization(operator="attendance_api")
        )
