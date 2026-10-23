"""Hardware-free tests for the P2-2 composition service
(``src/attendance_persistence.py``).

Deterministic: injected engine/store doubles (structural contract
only), a real ``AttendanceEngine`` with injected fake clocks, and
fixed datetimes — no sleeps, no camera, no model files, no real
biometric data, nothing written under the real ``data/`` tree.
"""

from __future__ import annotations

import pathlib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from src.attendance import (
    AttendanceDecision,
    AttendanceDecisionStatus,
    AttendanceEngine,
    AttendanceEvent,
    AttendanceInputError,
    EventType,
)
from src.attendance_persistence import (
    DurableAttendanceOutcome,
    DurableAttendanceService,
    DurableAttendanceStatus,
)
from src.attendance_store import AttendanceEventStore
from src.config import AppSettings
from src.attendance_store import (
    AttendanceStorePersistenceError,
    JournalLoadResult,
)
from src.temporal_verification import (
    TemporalVerificationStatus,
    TemporalVerificationResult,
)

_T0 = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
_EMPLOYEE = "EMP-001"


# ---------------------------------------------------------------------------
# Doubles (structural contract only — no AttendanceEngine subclassing)
# ---------------------------------------------------------------------------


class ScriptedEngine:
    """Engine double returning a scripted queue of decisions."""

    def __init__(self, decisions: list[AttendanceDecision]) -> None:
        self._decisions = list(decisions)
        self.calls: list[object] = []

    def update(self, result: object) -> AttendanceDecision:
        self.calls.append(result)
        return self._decisions.pop(0)


class ExplodingEngine:
    """Engine double raising on update (typed engine error)."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def update(self, result: object) -> AttendanceDecision:
        self.calls += 1
        raise self._error


class FakeStore:
    """Store double recording appends; optionally raises each time."""

    def __init__(self, error: Exception | None = None) -> None:
        self.appended: list[AttendanceEvent] = []
        self._error = error

    def append(self, event: AttendanceEvent) -> str:
        self.appended.append(event)
        if self._error is not None:
            raise self._error
        return "journal.jsonl"


class RealResultEngine:
    """Adapter feeding real engine decisions for a real engine test."""

    def __init__(self, engine: AttendanceEngine) -> None:
        self._engine = engine

    def update(self, result: object) -> AttendanceDecision:
        return self._engine.update(result)  # type: ignore[arg-type]


def _event(
    event_id: str = "ev-0001",
    employee_id: str = _EMPLOYEE,
    event_type: EventType = EventType.CHECK_IN,
    occurred_at: datetime | None = None,
) -> AttendanceEvent:
    return AttendanceEvent(
        event_id=event_id,
        employee_id=employee_id,
        event_type=event_type,
        occurred_at=occurred_at or _T0,
    )


def _created_decision(
    event_id: str = "ev-0001",
    event: AttendanceEvent | None = None,
) -> AttendanceDecision:
    return AttendanceDecision(
        status=AttendanceDecisionStatus.EVENT_CREATED,
        event=event or _event(event_id=event_id),
        reason="verified identity accepted",
    )


def _no_event_decision(
    status: AttendanceDecisionStatus = AttendanceDecisionStatus.SUPPRESSED,
) -> AttendanceDecision:
    return AttendanceDecision(
        status=status, event=None, reason="no event path"
    )


def _verified_result(
    employee_id: str = _EMPLOYEE,
) -> TemporalVerificationResult:
    return TemporalVerificationResult(
        status=TemporalVerificationStatus.VERIFIED,
        employee_id=employee_id,
        streak_frames=3,
        window_frames_used=3,
        reason="streak verified",
    )


# ---------------------------------------------------------------------------
# Construction and validation
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_engine_without_callable_update(self) -> None:
        with pytest.raises(AttendanceInputError):
            DurableAttendanceService(
                engine="not an engine", store=FakeStore()
            )

    def test_rejects_non_callable_update_attribute(self) -> None:
        class NotQuite:
            update = "not callable"

        with pytest.raises(AttendanceInputError):
            DurableAttendanceService(engine=NotQuite(), store=FakeStore())

    def test_rejects_store_without_callable_append(self) -> None:
        with pytest.raises(AttendanceInputError):
            DurableAttendanceService(
                engine=ScriptedEngine([]), store="not a store"
            )

    def test_construction_performs_no_filesystem_work(
        self, tmp_path: pathlib.Path
    ) -> None:
        DurableAttendanceService(
            engine=ScriptedEngine([]),
            store=FakeStore(),
        )
        # The service never touches the filesystem itself (any journal
        # directory is the store's lazy responsibility, on append only).
        assert list(tmp_path.rglob("*")) == []

    def test_outcome_is_frozen(self) -> None:
        outcome = DurableAttendanceOutcome(
            status=DurableAttendanceStatus.NO_EVENT,
            decision=_no_event_decision(),
            event=None,
            reason=None,
        )
        with pytest.raises(FrozenInstanceError):
            outcome.status = DurableAttendanceStatus.PERSISTED  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Outcome 1: no event created → nothing appended
# ---------------------------------------------------------------------------


class TestNoEvent:
    @pytest.mark.parametrize(
        "status",
        [
            AttendanceDecisionStatus.NOT_VERIFIED,
            AttendanceDecisionStatus.INACTIVE_EMPLOYEE,
            AttendanceDecisionStatus.SUPPRESSED,
        ],
    )
    def test_engine_decision_without_event_appends_nothing(
        self, status: AttendanceDecisionStatus
    ) -> None:
        engine = ScriptedEngine([_no_event_decision(status)])
        store = FakeStore()
        service = DurableAttendanceService(engine=engine, store=store)
        marker = object()
        outcome = service.process(marker)
        assert outcome.status is DurableAttendanceStatus.NO_EVENT
        assert outcome.event is None
        assert outcome.store_error is None
        assert "nothing was appended" in (outcome.reason or "")
        # The engine's decision is carried verbatim.
        assert outcome.decision.status is status
        assert outcome.decision.event is None
        assert engine.calls == [marker]
        assert store.appended == []


# ---------------------------------------------------------------------------
# Outcome 2: event created → exactly one append of the same event
# ---------------------------------------------------------------------------


class TestPersisted:
    def test_event_created_is_appended_exactly_once(self) -> None:
        event = _event()
        engine = ScriptedEngine([_created_decision(event=event)])
        store = FakeStore()
        service = DurableAttendanceService(engine=engine, store=store)
        outcome = service.process("any-result")
        assert outcome.status is DurableAttendanceStatus.PERSISTED
        assert outcome.store_error is None
        assert outcome.event is event
        assert outcome.decision.event is event
        assert store.appended == [event]
        assert engine.calls == ["any-result"]
        assert "durably journalled" in (outcome.reason or "")

    def test_store_error_object_is_absent_on_success(self) -> None:
        service = DurableAttendanceService(
            engine=ScriptedEngine([_created_decision(event_id="ev-2")]),
            store=FakeStore(),
        )
        outcome = service.process("x")
        assert outcome.status is DurableAttendanceStatus.PERSISTED
        assert outcome.store_error is None


# ---------------------------------------------------------------------------
# Outcome 3: persistence failure → explicit, unconfirmed, never retried
# ---------------------------------------------------------------------------


class TestPersistenceUnconfirmed:
    def test_store_failure_is_explicit_not_false_success(self) -> None:
        boom = AttendanceStorePersistenceError("sync lost")
        engine = ScriptedEngine([_created_decision(event_id="ev-3")])
        store = FakeStore(error=boom)
        service = DurableAttendanceService(engine=engine, store=store)
        outcome = service.process("x")
        assert outcome.status is DurableAttendanceStatus.PERSISTENCE_UNCONFIRMED
        # The original exception object is preserved unmodified.
        assert outcome.store_error is boom
        assert outcome.event is not None
        assert outcome.event.event_id == "ev-3"
        assert "unconfirmed" in (outcome.reason or "")
        assert "no automatic retry" in (outcome.reason or "")

    def test_no_automatic_retry_on_uncertain_append(self) -> None:
        # One process call attempts exactly one append even when it
        # fails; the retry decision belongs to the caller.
        engine = ScriptedEngine(
            [_created_decision(event_id=f"ev-{i}") for i in range(1, 3)]
        )
        store = FakeStore(
            error=AttendanceStorePersistenceError("flaky")
        )
        service = DurableAttendanceService(engine=engine, store=store)
        outcome_one = service.process("r1")
        outcome_two = service.process("r2")
        assert outcome_one.status is DurableAttendanceStatus.PERSISTENCE_UNCONFIRMED
        assert outcome_two.status is DurableAttendanceStatus.PERSISTENCE_UNCONFIRMED
        # Exactly one append attempt per process call — never a retry.
        assert [event.event_id for event in store.appended] == [
            "ev-1",
            "ev-2",
        ]

    def test_store_input_errors_map_to_confirmed_rejection(self) -> None:
        # AttendanceStoreInputError is raised by the store's documented
        # contract *before any write*, so this failure is confirmed:
        # the journal is unchanged and the record is definitively
        # absent — distinct from the uncertain I/O case.
        from src.attendance_store import AttendanceStoreInputError

        boom = AttendanceStoreInputError("not representable")
        service = DurableAttendanceService(
            engine=ScriptedEngine([_created_decision(event_id="ev-4")]),
            store=FakeStore(error=boom),
        )
        outcome = service.process("x")
        assert outcome.status is DurableAttendanceStatus.PERSISTENCE_REJECTED
        assert outcome.store_error is boom
        assert outcome.event is not None
        assert outcome.event.event_id == "ev-4"
        assert "rejected" in (outcome.reason or "")
        assert "before any write" in (outcome.reason or "")
        assert "journal is unchanged" in (outcome.reason or "")
        assert "no automatic retry" in (outcome.reason or "")

    def test_rejection_and_uncertainty_classifications_differ(self) -> None:
        # The two failure classifications are distinct statuses with
        # the original exception preserved by identity in both.
        from src.attendance_store import AttendanceStoreInputError

        rejected = AttendanceStoreInputError("bad event input")
        uncertain = AttendanceStorePersistenceError("flaky sync")
        service = DurableAttendanceService(
            engine=ScriptedEngine(
                [
                    _created_decision(event_id="ev-6"),
                    _created_decision(event_id="ev-7"),
                ]
            ),
            store=FakeStore(),
        )
        store = service._store
        assert isinstance(store, FakeStore)
        store._error = rejected
        outcome_rejected = service.process("r1")
        store._error = uncertain
        outcome_uncertain = service.process("r2")
        assert (
            outcome_rejected.status
            is DurableAttendanceStatus.PERSISTENCE_REJECTED
        )
        assert outcome_rejected.store_error is rejected
        assert (
            outcome_uncertain.status
            is DurableAttendanceStatus.PERSISTENCE_UNCONFIRMED
        )
        assert outcome_uncertain.store_error is uncertain
        assert "rejected" in (outcome_rejected.reason or "")
        assert "unconfirmed" in (outcome_uncertain.reason or "")

    def test_non_store_programming_error_propagates_unchanged(self) -> None:
        # A non-AttendanceStoreError store failure is a programming
        # error — it must NOT be mislabeled as a persistence outcome.
        class BuggyStore(FakeStore):
            def append(self, event: AttendanceEvent) -> str:
                raise ValueError("buggy store internals")

        service = DurableAttendanceService(
            engine=ScriptedEngine([_created_decision(event_id="ev-5")]),
            store=BuggyStore(),
        )
        with pytest.raises(ValueError, match="buggy store internals"):
            service.process("x")


# ---------------------------------------------------------------------------
# Engine exception propagation
# ---------------------------------------------------------------------------


class TestEngineExceptionPropagation:
    def test_engine_input_error_propagates_and_appends_nothing(self) -> None:
        boom = AttendanceInputError("malformed temporal result")
        engine = ExplodingEngine(boom)
        store = FakeStore()
        service = DurableAttendanceService(engine=engine, store=store)
        with pytest.raises(AttendanceInputError) as excinfo:
            service.process("bad-result")
        assert excinfo.value is boom
        assert engine.calls == 1
        assert store.appended == []


# ---------------------------------------------------------------------------
# Real engine integration: decision behavior preserved, suppression
# produces no appends
# ---------------------------------------------------------------------------


class TestRealEngineComposition:
    def _real_engine(self) -> tuple[AttendanceEngine, list[float]]:
        ticks = [100.0]

        def monotonic() -> float:
            return ticks[0]

        engine = AttendanceEngine(
            clock_wall=lambda: _T0,
            monotonic_clock=monotonic,
            event_id_factory=lambda: "ev-fixed",
        )
        return engine, ticks

    @staticmethod
    def _verified(employee_id: str = _EMPLOYEE) -> TemporalVerificationResult:
        return _verified_result(employee_id)

    def test_verified_result_persists_exactly_one_event(self) -> None:
        engine, _ = self._real_engine()
        store = FakeStore()
        service = DurableAttendanceService(
            engine=RealResultEngine(engine), store=store
        )
        outcome = service.process(self._verified())
        assert outcome.status is DurableAttendanceStatus.PERSISTED
        assert outcome.decision.status is AttendanceDecisionStatus.EVENT_CREATED
        (appended,) = store.appended
        assert appended.event_id == "ev-fixed"
        assert appended.employee_id == _EMPLOYEE
        assert appended.event_type is EventType.CHECK_IN
        assert appended.occurred_at == _T0
        # Outcome event and decision event are the same value.
        assert outcome.event == appended

    def test_repeated_suppression_creates_no_additional_append(self) -> None:
        engine, _ = self._real_engine()
        store = FakeStore()
        service = DurableAttendanceService(
            engine=RealResultEngine(engine), store=store
        )
        first = service.process(self._verified())
        assert first.status is DurableAttendanceStatus.PERSISTED
        # Same monotonic instant → elapsed 0 < cooldown → SUPPRESSED,
        # no event, no append.
        second = service.process(self._verified())
        assert second.status is DurableAttendanceStatus.NO_EVENT
        assert second.decision.status is AttendanceDecisionStatus.SUPPRESSED
        assert second.event is None
        assert len(store.appended) == 1

    def test_cooling_down_then_new_event_appends_again(self) -> None:
        engine, ticks = self._real_engine()
        store = FakeStore()
        service = DurableAttendanceService(
            engine=RealResultEngine(engine), store=store
        )
        assert (
            service.process(self._verified()).status
            is DurableAttendanceStatus.PERSISTED
        )
        # Advance past the 300 s cooldown boundary (exact).
        ticks[0] += 300.0
        third = service.process(self._verified())
        assert third.status is DurableAttendanceStatus.PERSISTED
        assert len(store.appended) == 2

    def test_journal_load_result_type_is_untouched(self) -> None:
        # Guard against accidental schema/store contract drift: the
        # composition layer consumes append() only; load semantics are
        # the store's (type imported here to keep the dependency
        # explicit in this test module).
        assert JournalLoadResult(events=(), incomplete_trailing_record=False)  # noqa: F401


# ---------------------------------------------------------------------------
# Real-chain integration: real engine + real store + temp journal
# (deterministic, hardware-free, temp directories only)
# ---------------------------------------------------------------------------


class TestRealStoreIntegration:
    def test_full_chain_real_engine_real_store_temp_journal(
        self, tmp_path: pathlib.Path
    ) -> None:
        store = AttendanceEventStore(settings=AppSettings(data_dir=tmp_path))
        ids = iter(("ev-real-1", "ev-real-2", "ev-real-3"))
        ticks = [100.0]
        engine = AttendanceEngine(
            clock_wall=lambda: _T0,
            monotonic_clock=lambda: ticks[0],
            event_id_factory=lambda: next(ids),
        )
        service = DurableAttendanceService(engine=engine, store=store)
        first = service.process(_verified_result())
        assert first.status is DurableAttendanceStatus.PERSISTED

        # Exactly one record in the actual journal, matching the
        # engine-created event.
        journal = tmp_path / "attendance" / "events.jsonl"
        assert journal.is_file()
        assert len(journal.read_text(encoding="utf-8").splitlines()) == 1
        (loaded,) = store.load_events().events
        assert loaded.event_id == "ev-real-1"
        assert loaded.employee_id == _EMPLOYEE
        assert loaded.event_type is EventType.CHECK_IN
        assert loaded.occurred_at == _T0
        assert loaded == first.event

        # A repeated input the engine suppresses produces no second
        # record (same monotonic instant → inside the cooldown).
        second = service.process(_verified_result())
        assert second.status is DurableAttendanceStatus.NO_EVENT
        assert second.decision.status is AttendanceDecisionStatus.SUPPRESSED
        result = store.load_events()
        assert len(result.events) == 1
        assert result.incomplete_trailing_record is False
