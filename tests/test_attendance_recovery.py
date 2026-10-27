"""Hardware-free tests for P2-3 restart recovery
(``src/attendance_recovery.py``).

Deterministic: journals live in ``pytest`` temporary directories and
contain synthetic event records only; wall and monotonic clocks are
injected constants — no sleeps, no camera, no model files, no real
biometric or attendance data, nothing under the real ``data/`` tree.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.attendance import (
    AttendanceDecisionStatus,
    AttendanceEngine,
    AttendanceInputError,
    AttendanceEvent,
    EventType,
)
from src.attendance_persistence import (
    DurableAttendanceService,
    DurableAttendanceStatus,
)
from src.attendance_recovery import (
    AttendanceStartupRecoveryReport,
    CooldownRecoveryReport,
    SessionRecoveryReport,
    fold_latest_events,
    make_journal_open_session_lookup,
    recover_attendance_startup_state,
    recover_cooldown_anchors,
    recover_session_state,
)
from src.attendance_store import (
    ATTENDANCE_JOURNAL_SCHEMA_VERSION,
    JOURNAL_FILENAME,
    AttendanceEventStore,
    CorruptAttendanceJournalError,
    UnsupportedJournalSchemaVersionError,
)
from src.config import AppSettings, AttendanceConfig
from src.temporal_verification import (
    TemporalVerificationStatus,
    TemporalVerificationResult,
)

_T0 = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
_EMPLOYEE = "EMP-001"
_COOLDOWN = 300.0


class FakeClocks:
    """Injectable wall + monotonic clock pair (advanced by hand)."""

    def __init__(self) -> None:
        self.mono = 0.0
        self.wall = _T0

    def utc_now(self) -> datetime:
        return self.wall

    def mono_now(self) -> float:
        return self.mono

    def advance(self, mono_seconds: float = 0.0, wall_seconds: float = 0.0):
        self.mono += mono_seconds
        self.wall = self.wall + timedelta(seconds=wall_seconds)


def _engine(
    clocks: FakeClocks,
    *,
    cooldown: float = _COOLDOWN,
    max_tracked: int = 64,
) -> AttendanceEngine:
    return AttendanceEngine(
        config=AttendanceConfig(
            check_in_cooldown_seconds=cooldown,
            max_tracked_employees=max_tracked,
        ),
        clock_wall=clocks.utc_now,
        monotonic_clock=clocks.mono_now,
        event_id_factory=lambda: "ev-fixed",
    )


def _store(tmp_path: pathlib.Path) -> AttendanceEventStore:
    return AttendanceEventStore(settings=AppSettings(data_dir=tmp_path))


def _temporary_event(
    event_id: str,
    employee_id: str,
    occurred_at: datetime,
    event_type: EventType = EventType.CHECK_IN,
) -> AttendanceEvent:
    return AttendanceEvent(
        event_id=event_id,
        employee_id=employee_id,
        event_type=event_type,
        occurred_at=occurred_at,
    )


def _verified_result(employee_id: str = _EMPLOYEE):
    return TemporalVerificationResult(
        status=TemporalVerificationStatus.VERIFIED,
        employee_id=employee_id,
        streak_frames=3,
        window_frames_used=3,
        reason="streak verified",
    )


def _write_journal(tmp_path: pathlib.Path, lines: list[str]) -> None:
    journal_dir = tmp_path / "attendance"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / JOURNAL_FILENAME).write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )


def _record_line(event: AttendanceEvent) -> str:
    return json.dumps(
        {
            "schema_version": ATTENDANCE_JOURNAL_SCHEMA_VERSION,
            "event_id": event.event_id,
            "employee_id": event.employee_id,
            "event_type": event.event_type.value,
            "occurred_at": event.occurred_at.isoformat(),
        }
    )


def _read_journal_bytes(tmp_path: pathlib.Path) -> bytes:
    return (tmp_path / "attendance" / JOURNAL_FILENAME).read_bytes()


class RecordingRestore:
    """Restore double recording calls and honoring an age map."""

    def __init__(self, expired_ids: set[str] | None = None) -> None:
        self.calls: list[tuple[str, float]] = []
        self._expired = expired_ids or set()

    def restore_suppression_anchor(
        self, employee_id: str, *, elapsed_seconds: float
    ) -> bool:
        self.calls.append((employee_id, elapsed_seconds))
        return employee_id not in self._expired


class ExplodingLoader:
    """Store double whose load fails the requested way."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def load_events(self):
        if self._error is not None:
            raise self._error
        raise AssertionError("loader must be scripted")


class RecordingP26Restore:
    """P2-6 restore double recording both session/checkout calls."""

    def __init__(self) -> None:
        self.session_calls: list[tuple[str, float]] = []
        self.anchor_calls: list[tuple[str, float]] = []

    def restore_open_session(
        self, employee_id: str, *, elapsed_seconds: float
    ) -> bool:
        self.session_calls.append((employee_id, elapsed_seconds))
        return True

    def restore_check_out_anchor(
        self, employee_id: str, *, elapsed_seconds: float
    ) -> bool:
        self.anchor_calls.append((employee_id, elapsed_seconds))
        return elapsed_seconds < 300.0


def _append_record(tmp_path: pathlib.Path, event: AttendanceEvent) -> None:
    """Append one validated record to the existing journal file."""
    path = tmp_path / "attendance" / JOURNAL_FILENAME
    path.write_bytes(
        path.read_bytes() + _record_line(event).encode("utf-8") + b"\n"
    )


# ---------------------------------------------------------------------------
# 1. Empty journal recovery
# ---------------------------------------------------------------------------


class TestEmptyJournal:
    def test_missing_journal_recovers_nothing_without_side_effects(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        h = _engine(clocks)
        report = recover_cooldown_anchors(
            h, _store(tmp_path), wall_now=clocks.wall
        )
        assert isinstance(report, CooldownRecoveryReport)
        assert report.anchors_restored == ()
        assert report.events_scanned == 0
        assert report.expired_anchors == 0
        assert report.future_timestamps_clamped == 0
        assert report.incomplete_trailing_record is False
        assert h.tracked_employee_count() == 0
        # Loading a missing journal never creates the directory.
        assert not (tmp_path / "attendance").exists()

    def test_zero_byte_journal_is_empty_history(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        _write_journal(tmp_path, [])
        report = recover_cooldown_anchors(
            _engine(clocks), _store(tmp_path), wall_now=clocks.wall
        )
        assert report.anchors_restored == ()
        assert report.events_scanned == 0


# ---------------------------------------------------------------------------
# 2–4. Per-employee restoration, independence, latest-event selection
# ---------------------------------------------------------------------------


class TestRestoreSemantics:
    def test_one_valid_event_restores_employee_cooldown(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        event = _temporary_event(
            "ev-a", _EMPLOYEE, clocks.wall - timedelta(seconds=60)
        )
        _write_journal(tmp_path, [_record_line(event)])
        report = recover_cooldown_anchors(
            _engine(clocks), _store(tmp_path), wall_now=clocks.wall
        )
        assert report.anchors_restored == (_EMPLOYEE,)
        assert report.events_scanned == 1
        assert report.anchors_restored == (_EMPLOYEE,)
        assert report.expired_anchors == 0

    def test_restored_cooldown_exactly_matches_remaining_time(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        event = _temporary_event(
            "ev-a", _EMPLOYEE, clocks.wall - timedelta(seconds=60)
        )
        _write_journal(tmp_path, [_record_line(event)])
        h = _engine(clocks)
        recover_cooldown_anchors(h, _store(tmp_path), wall_now=clocks.wall)
        # 60 s of 300 s elapsed → 240 s remaining (the restored anchor
        # already absorbed the 60 s at the recovery instant).
        clocks.advance(mono_seconds=239.0)
        assert (
            h.update(_verified_result()).status
            is AttendanceDecisionStatus.SUPPRESSED
        )
        clocks.advance(mono_seconds=2.0)
        assert (
            h.update(_verified_result()).status
            is AttendanceDecisionStatus.EVENT_CREATED
        )

    def test_multiple_employees_restore_independently(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=30)
        _write_journal(
            tmp_path,
            [
                _record_line(_temporary_event("ev-1", "EMP-A", at)),
                _record_line(_temporary_event("ev-2", "EMP-B", at)),
            ],
        )
        h = _engine(clocks)
        report = recover_cooldown_anchors(
            h, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.anchors_restored == ("EMP-A", "EMP-B")
        assert h.tracked_employee_count() == 2
        # Both suppress; both expire independently (same window here).
        assert (
            h.update(_verified_result("EMP-A")).status
            is AttendanceDecisionStatus.SUPPRESSED
        )
        assert (
            h.update(_verified_result("EMP-B")).status
            is AttendanceDecisionStatus.SUPPRESSED
        )

    def test_latest_event_per_employee_selects_most_recent(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        turns = FakeClocks()
        old_event = _temporary_event(
            "ev-old", _EMPLOYEE, clocks.wall - timedelta(seconds=250)
        )
        new_event = _temporary_event(
            "ev-new", _EMPLOYEE, clocks.wall - timedelta(seconds=50)
        )
        _write_journal(
            tmp_path, [_record_line(old_event), _record_line(new_event)]
        )
        restore = RecordingRestore()
        report = recover_cooldown_anchors(
            restore, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.anchors_restored == (_EMPLOYEE,)
        assert restore.calls == [(_EMPLOYEE, 50.0)]

    def test_restored_anchor_is_replayable_suppression_not_fabricated_event(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        event = _temporary_event(
            "ev-a", _EMPLOYEE, clocks.wall - timedelta(seconds=10)
        )
        _write_journal(tmp_path, [_record_line(event)])
        h = _engine(clocks)
        recover_cooldown_anchors(h, _store(tmp_path), wall_now=clocks.wall)
        # The engine can now serve a suppressed decision without
        # creating (or needing) any new event.
        decision = h.update(_verified_result())
        assert decision.status is AttendanceDecisionStatus.SUPPRESSED
        assert decision.event is None


# ---------------------------------------------------------------------------
# 5. Suppressed / non-attendance outcomes never become replayable events
# ---------------------------------------------------------------------------


class TestReplayAllowlist:
    def test_suppressed_frame_produced_no_event_so_nothing_replays(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        # Full real chain: one event-producing pass, then suppression.
        store = _store(tmp_path)
        h = _engine(clocks)
        service = DurableAttendanceService(engine=h, store=store)
        first = service.process(_verified_result())
        assert first.status is DurableAttendanceStatus.PERSISTED
        suppressed = service.process(_verified_result())
        assert suppressed.status is DurableAttendanceStatus.NO_EVENT
        # Replay after restart: exactly the one accepted event counts.
        clocks.advance(wall_seconds=10.0)
        restore = RecordingRestore()
        report = recover_cooldown_anchors(
            restore, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.events_scanned == 1
        assert report.anchors_restored == (_EMPLOYEE,)

    def test_only_check_in_records_count_for_cooldown_recovery(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        store = _store(tmp_path)
        h = _engine(clocks)
        service = DurableAttendanceService(engine=h, store=store)
        assert (
            service.process(_verified_result()).status
            is DurableAttendanceStatus.PERSISTED
        )
        lines = _read_journal_bytes(tmp_path).decode("utf-8").splitlines()
        assert len(lines) == 1
        # Journal lines are loadable as CHECK_IN only via the real
        # loader — a non-CHECK_IN "future" record must never count.
        clocks.advance(wall_seconds=5.0)
        restore = RecordingRestore()
        recover_cooldown_anchors(
            restore, _store(tmp_path), wall_now=clocks.wall
        )
        assert restore.calls == [(_EMPLOYEE, 5.0)]


# ---------------------------------------------------------------------------
# 6–7. Exact boundary and expired cooldown after recovery
# ---------------------------------------------------------------------------


class TestBoundaryAndExpiry:
    def test_exact_cooldown_boundary_after_recovery(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=_COOLDOWN)
        event = _temporary_event("ev-a", _EMPLOYEE, at)
        _write_journal(tmp_path, [_record_line(event)])
        h = _engine(clocks)
        report = recover_cooldown_anchors(
            h, _store(tmp_path), wall_now=clocks.wall
        )
        # Age == cooldown → expired at recovery, not restored.
        assert report.anchors_restored == ()
        assert report.expired_anchors == 1
        assert h.tracked_employee_count() == 0
        assert (
            h.update(_verified_result()).status
            is AttendanceDecisionStatus.EVENT_CREATED
        )

    def test_one_second_before_boundary_is_restored(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=_COOLDOWN - 1.0)
        _write_journal(
            tmp_path,
            [_record_line(_temporary_event("ev-a", _EMPLOYEE, at))],
        )
        h = _engine(clocks)
        report = recover_cooldown_anchors(
            h, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.anchors_restored == (_EMPLOYEE,)
        assert report.expired_anchors == 0
        # Exactly one second must remain (elapsed = age + mono delta).
        clocks.advance(mono_seconds=0.5)
        assert (
            h.update(_verified_result()).status
            is AttendanceDecisionStatus.SUPPRESSED
        )
        clocks.advance(mono_seconds=0.5)
        assert (
            h.update(_verified_result()).status
            is AttendanceDecisionStatus.EVENT_CREATED
        )

    def test_long_expired_journal_restores_nothing(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=_COOLDOWN * 5)
        _write_journal(
            tmp_path,
            [_record_line(_temporary_event("ev-a", _EMPLOYEE, at))],
        )
        h = _engine(clocks)
        report = recover_cooldown_anchors(
            h, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.anchors_restored == ()
        assert report.expired_anchors == 1
        assert h.tracked_employee_count() == 0


# ---------------------------------------------------------------------------
# 8. Out-of-order records and duplicate event IDs
# ---------------------------------------------------------------------------


class TestOrderingAndDuplicates:
    def test_out_of_order_journal_selects_latest_by_timestamp(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        old = _temporary_event(
            "ev-old", _EMPLOYEE, clocks.wall - timedelta(seconds=250)
        )
        new = _temporary_event(
            "ev-new", _EMPLOYEE, clocks.wall - timedelta(seconds=50)
        )
        _write_journal(tmp_path, [_record_line(new), _record_line(old)])
        restore = RecordingRestore()
        report = recover_cooldown_anchors(
            restore, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.anchors_restored == (_EMPLOYEE,)
        # The newest record wins regardless of its journal position.
        assert restore.calls == [(_EMPLOYEE, 50.0)]
        assert report.events_scanned == 2

    def test_duplicate_event_ids_collapse_deterministically(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=70)
        line = _record_line(_temporary_event("ev-dup", _EMPLOYEE, at))
        _write_journal(tmp_path, [line, line, line])
        restore = RecordingRestore()
        report = recover_cooldown_anchors(
            restore, _store(tmp_path), wall_now=clocks.wall
        )
        # Per-employee selection: identical duplicates become one
        # restore call — never more than one anchor per employee.
        assert report.events_scanned == 3
        assert restore.calls == [(_EMPLOYEE, 70.0)]

    def test_tie_break_by_journal_position(self, tmp_path: pathlib.Path) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=100)
        first = _temporary_event("ev-1", _EMPLOYEE, at)
        second = _temporary_event("ev-2", _EMPLOYEE, at)
        _write_journal(
            tmp_path, [_record_line(first), _record_line(second)]
        )
        restore = RecordingRestore()
        report = recover_cooldown_anchors(
            restore, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.anchors_restored == (_EMPLOYEE,)
        assert report.events_scanned == 2
        assert restore.calls == [(_EMPLOYEE, 100.0)]


# ---------------------------------------------------------------------------
# 9. Incomplete trailing record handling
# ---------------------------------------------------------------------------


class TestIncompleteTail:
    def test_torn_tail_is_excluded_and_reported(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        good = _temporary_event(
            "ev-good", _EMPLOYEE, clocks.wall - timedelta(seconds=20)
        )
        torn = _record_line(_temporary_event("ev-torn", _EMPLOYEE, clocks.wall))[
            :-15
        ]
        # Binary write so the torn tail stays a no-trailing-newline
        # fragment regardless of platform newline translation.
        journal = tmp_path / "attendance" / JOURNAL_FILENAME
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_bytes(
            _record_line(good).encode("utf-8") + b"\n" + torn.encode("utf-8")
        )
        restore = RecordingRestore()
        report = recover_cooldown_anchors(
            restore, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.incomplete_trailing_record is True
        assert report.events_scanned == 1
        assert report.anchors_restored == (_EMPLOYEE,)
        assert restore.calls == [(_EMPLOYEE, 20.0)]


# ---------------------------------------------------------------------------
# 10. Corruption / unsupported schema → no false success, no partial state
# ---------------------------------------------------------------------------


class TestLoadFailures:
    def test_corrupt_journal_propagates_and_touches_no_engine_state(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        _write_journal(tmp_path, ['{"broken json'])
        h = _engine(clocks)
        with pytest.raises(CorruptAttendanceJournalError):
            recover_cooldown_anchors(h, _store(tmp_path), wall_now=clocks.wall)
        # All-or-nothing: no anchor may exist after the failed load.
        assert h.tracked_employee_count() == 0

    def test_unsupported_schema_version_propagates_unchanged(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        payload = json.loads(
            _record_line(_temporary_event("ev-a", _EMPLOYEE, clocks.wall))
        )
        payload["schema_version"] = 99
        _write_journal(tmp_path, [json.dumps(payload)])
        h = _engine(clocks)
        with pytest.raises(UnsupportedJournalSchemaVersionError):
            recover_cooldown_anchors(h, _store(tmp_path), wall_now=clocks.wall)
        assert h.tracked_employee_count() == 0

    def test_corruption_after_valid_line_still_fails_all_or_nothing(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        good = _temporary_event(
            "ev-a", _EMPLOYEE, clocks.wall - timedelta(seconds=15)
        )
        _write_journal(
            tmp_path,
            [_record_line(good), '{"schema_version": 1, "event_id": ""}'],
        )
        h = _engine(clocks)
        with pytest.raises(CorruptAttendanceJournalError):
            recover_cooldown_anchors(h, _store(tmp_path), wall_now=clocks.wall)
        assert h.tracked_employee_count() == 0

    def test_loader_error_propagates_without_any_restore_call(self) -> None:
        restore = RecordingRestore()
        boom = CorruptAttendanceJournalError("unreadable journal")
        with pytest.raises(CorruptAttendanceJournalError) as excinfo:
            recover_cooldown_anchors(
                restore, ExplodingLoader(error=boom), wall_now=_T0
            )
        assert excinfo.value is boom
        assert restore.calls == []


# ---------------------------------------------------------------------------
# 11. Replay is read-only: journal bytes are never modified or appended
# ---------------------------------------------------------------------------


class TestReplayIsReadOnly:
    def test_journal_bytes_unchanged_by_recovery(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        event = _temporary_event(
            "ev-a", _EMPLOYEE, clocks.wall - timedelta(seconds=30)
        )
        twofold = [_record_line(event), _record_line(event)]
        _write_journal(tmp_path, twofold)
        before = _read_journal_bytes(tmp_path)
        h = _engine(clocks)
        recover_cooldown_anchors(h, _store(tmp_path), wall_now=clocks.wall)
        assert _read_journal_bytes(tmp_path) == before
        # One restore for one employee despite two identical lines.
        assert h.tracked_employee_count() == 1

    def test_recovery_creates_no_new_events_or_journal(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        h = _engine(clocks)
        recover_cooldown_anchors(h, _store(tmp_path), wall_now=clocks.wall)
        # Empty journal case proves no append/download/create happened.
        assert not (tmp_path / "attendance").exists()


# ---------------------------------------------------------------------------
# 12. Wall↔monotonic mapping and explicit clock-skew policy
# ---------------------------------------------------------------------------


class TestClockSkewPolicy:
    def test_wall_clock_moved_backward_clamps_age_to_zero(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        future_event = _temporary_event(
            "ev-a", _EMPLOYEE, clocks.wall + timedelta(seconds=45)
        )
        _write_journal(tmp_path, [_record_line(future_event)])
        restore = RecordingRestore()
        report = recover_cooldown_anchors(
            restore, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.future_timestamps_clamped == 1
        assert restore.calls == [(_EMPLOYEE, 0.0)]
        assert report.anchors_restored == (_EMPLOYEE,)

    def test_clamped_restore_suppresses_for_full_cooldown(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        future_event = _temporary_event(
            "ev-a", _EMPLOYEE, clocks.wall + timedelta(seconds=45)
        )
        _write_journal(tmp_path, [_record_line(future_event)])
        h = _engine(clocks)
        report = recover_cooldown_anchors(
            h, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.future_timestamps_clamped == 1
        # Suppressed for the full cooldown from the wall-now instant.
        clocks.advance(mono_seconds=299.0)
        assert (
            h.update(_verified_result()).status
            is AttendanceDecisionStatus.SUPPRESSED
        )
        clocks.advance(mono_seconds=2.0)
        assert (
            h.update(_verified_result()).status
            is AttendanceDecisionStatus.EVENT_CREATED
        )

    def test_wall_clock_moved_forward_lets_cooldown_expire(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=60)
        _write_journal(
            tmp_path, [_record_line(_temporary_event("ev-a", _EMPLOYEE, at))]
        )
        h = _engine(clocks)
        # Wall clock jumped forward two days between runs.
        report = recover_cooldown_anchors(
            h,
            _store(tmp_path),
            wall_now=clocks.wall + timedelta(days=2),
        )
        assert report.anchors_restored == ()
        assert report.expired_anchors == 1
        assert report.future_timestamps_clamped == 0
        assert h.tracked_employee_count() == 0
        # A fresh event is allowed immediately.
        assert (
            h.update(_verified_result()).status
            is AttendanceDecisionStatus.EVENT_CREATED
        )

    def test_wall_now_may_be_in_another_timezone_than_the_record(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=30)
        _write_journal(
            tmp_path, [_record_line(_temporary_event("ev-a", _EMPLOYEE, at))]
        )
        # wall_now given in a +05:00 zone representing the same instant.
        wall_now_shifted = clocks.wall.astimezone(
            timezone(timedelta(hours=5))
        )
        restore = RecordingRestore()
        recover_cooldown_anchors(
            restore, _store(tmp_path), wall_now=wall_now_shifted
        )
        assert restore.calls == [(_EMPLOYEE, 30.0)]

    def test_engine_whose_clock_output_is_invalid_propagates(self) -> None:
        # The restore double never advances a broken monotonic clock;
        # a real engine's _monotonic_now validation applies during
        # restore exactly as during update.
        class BrokenMonoEngine(FakeClocks):
            def mono_now(self) -> float:
                return float("nan")

        h = AttendanceEngine(
            config=AttendanceConfig(check_in_cooldown_seconds=_COOLDOWN),
            clock_wall=FakeClocks().utc_now,
            monotonic_clock=BrokenMonoEngine().mono_now,
        )
        with pytest.raises(AttendanceInputError):
            h.restore_suppression_anchor(
                _EMPLOYEE, elapsed_seconds=1.0
            )


# ---------------------------------------------------------------------------
# Contemporaneous composition: replay then process (integration)
# ---------------------------------------------------------------------------


class TestReplayThenCompose:
    def test_replay_then_process_matches_pre_restart_behavior(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        h = _engine(clocks)
        store = _store(tmp_path)
        service = DurableAttendanceService(engine=h, store=store)
        assert (
            service.process(_verified_result()).status
            is DurableAttendanceStatus.PERSISTED
        )
        restart_wall = clocks.wall + timedelta(seconds=45)
        h2 = _engine(clocks)
        report = recover_cooldown_anchors(h2, store, wall_now=restart_wall)
        assert report.anchors_restored == (_EMPLOYEE,)
        clocks.advance(mono_seconds=100.0)
        service2 = DurableAttendanceService(engine=h2, store=store)
        second = service2.process(_verified_result())
        assert second.status is DurableAttendanceStatus.NO_EVENT
        assert second.decision.status is AttendanceDecisionStatus.SUPPRESSED
        assert len(store.load_events().events) == 1


# ---------------------------------------------------------------------------
# P2-6 S3: shared pure fold (latest event per employee)
# ---------------------------------------------------------------------------


class TestFoldLatestEvents:
    def test_latest_timestamp_wins_regardless_of_position(self) -> None:
        old = _temporary_event("ev-old", "E1", _T0 - timedelta(seconds=250))
        new = _temporary_event("ev-new", "E1", _T0 - timedelta(seconds=50))
        # The newest timestamp wins in both journal orders.
        folded = fold_latest_events([new, old])
        assert folded["E1"] is new
        folded = fold_latest_events([old, new])
        assert folded["E1"] is new

    def test_equal_timestamp_resolves_by_later_journal_position(self) -> None:
        first = _temporary_event("ev-1", "E1", _T0)
        second = _temporary_event("ev-2", "E1", _T0)
        out = _temporary_event(
            "ev-3", "E1", _T0, event_type=EventType.CHECK_OUT
        )
        assert fold_latest_events([first, second])["E1"] is second
        assert fold_latest_events([second, out])["E1"] is out

    def test_interleaved_employees_are_selected_independently(self) -> None:
        a_in = _temporary_event("ev-1", "E-A", _T0 - timedelta(seconds=100))
        b_in = _temporary_event("ev-2", "E-B", _T0 - timedelta(seconds=90))
        a_out = _temporary_event(
            "ev-3", "E-A", _T0 - timedelta(seconds=80),
            event_type=EventType.CHECK_OUT,
        )
        b_in2 = _temporary_event("ev-4", "E-B", _T0 - timedelta(seconds=70))
        folded = fold_latest_events([a_in, b_in, a_out, b_in2])
        assert set(folded) == {"E-A", "E-B"}
        assert folded["E-A"] is a_out
        assert folded["E-B"] is b_in2

    def test_deterministic_and_first_seen_order(self) -> None:
        a_in = _temporary_event("ev-1", "E-A", _T0 - timedelta(seconds=100))
        b_in = _temporary_event("ev-2", "E-B", _T0 - timedelta(seconds=90))
        b_in2 = _temporary_event("ev-3", "E-B", _T0 - timedelta(seconds=70))
        events = [a_in, b_in, b_in2]
        first = fold_latest_events(events)
        again = fold_latest_events(list(events))
        assert first == again
        assert list(first) == ["E-A", "E-B"]  # first-seen journal order


# ---------------------------------------------------------------------------
# P2-6 S3: open-session + check-out cooldown recovery
# ---------------------------------------------------------------------------


class TestRecoverSessionState:
    def test_latest_check_in_restores_open_session(self, tmp_path) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=60)
        _write_journal(
            tmp_path,
            [_record_line(_temporary_event("ev-in", _EMPLOYEE, at))],
        )
        h = _engine(clocks)
        report = recover_session_state(
            h, _store(tmp_path), wall_now=clocks.wall
        )
        assert isinstance(report, SessionRecoveryReport)
        assert report.open_sessions_restored == (_EMPLOYEE,)
        assert report.check_out_anchors_restored == ()
        assert report.expired_check_out_anchors == 0
        assert report.events_scanned == 1
        assert report.incomplete_trailing_record is False
        # The restored session authorizes a check-out immediately.
        assert (
            h.update(_verified_result(), event_type=EventType.CHECK_OUT)
            .status
            is AttendanceDecisionStatus.EVENT_CREATED
        )

    def test_latest_check_out_closes_session_and_restores_out_anchor(
        self, tmp_path
    ) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=60)
        _write_journal(
            tmp_path,
            [
                _record_line(
                    _temporary_event(
                        "ev-out", _EMPLOYEE, at, event_type=EventType.CHECK_OUT
                    )
                )
            ],
        )
        h = _engine(clocks)
        report = recover_session_state(
            h, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.open_sessions_restored == ()
        assert report.check_out_anchors_restored == (_EMPLOYEE,)
        # No open session: an honest (hookless) checkout is rejected.
        h = _engine(clocks)
        assert (
            h.update(_verified_result(), event_type=EventType.CHECK_OUT)
            .status
            is AttendanceDecisionStatus.SUPPRESSED
        )
        # The restored anchor suppresses a hook-admitted checkout.
        h2 = AttendanceEngine(
            config=AttendanceConfig(
                check_in_cooldown_seconds=_COOLDOWN,
                check_out_cooldown_seconds=_COOLDOWN,
            ),
            clock_wall=clocks.utc_now,
            monotonic_clock=clocks.mono_now,
            open_session_lookup=lambda _id: True,
            event_id_factory=lambda: "ev-fixed",
        )
        report2 = recover_session_state(
            h2, _store(tmp_path), wall_now=clocks.wall
        )
        assert report2.open_sessions_restored == ()
        assert report2.check_out_anchors_restored == (_EMPLOYEE,)
        assert (
            h2.update(_verified_result(), event_type=EventType.CHECK_OUT)
            .status
            is AttendanceDecisionStatus.SUPPRESSED
        )

    def test_missing_journal_restores_nothing(self, tmp_path) -> None:
        clocks = FakeClocks()
        h = _engine(clocks)
        report = recover_session_state(
            h, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.open_sessions_restored == ()
        assert report.check_out_anchors_restored == ()
        assert report.events_scanned == 0
        assert not (tmp_path / "attendance").exists()

    def test_corrupt_journal_fails_closed_without_partial_state(
        self, tmp_path
    ) -> None:
        clocks = FakeClocks()
        good = _temporary_event(
            "ev-good", _EMPLOYEE, clocks.wall - timedelta(seconds=15)
        )
        _write_journal(
            tmp_path,
            [_record_line(good), '{"schema_version": 1, "event_id": ""}'],
        )
        h = _engine(clocks)
        with pytest.raises(CorruptAttendanceJournalError):
            recover_session_state(
                h, _store(tmp_path), wall_now=clocks.wall
            )
        # All-or-nothing: nothing restored before the failed load.
        assert (
            h.update(_verified_result(), event_type=EventType.CHECK_OUT)
            .status
            is AttendanceDecisionStatus.SUPPRESSED
        )
        assert h.tracked_employee_count() == 0

    def test_later_check_in_means_no_check_out_anchor(self, tmp_path) -> None:
        clocks = FakeClocks()
        at_out = clocks.wall - timedelta(seconds=300)
        at_in = clocks.wall - timedelta(seconds=10)
        _write_journal(
            tmp_path,
            [
                _record_line(
                    _temporary_event(
                        "ev-out", _EMPLOYEE, at_out,
                        event_type=EventType.CHECK_OUT,
                    )
                ),
                _record_line(_temporary_event("ev-in", _EMPLOYEE, at_in)),
            ],
        )
        double = RecordingP26Restore()
        report = recover_session_state(
            double, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.open_sessions_restored == (_EMPLOYEE,)
        assert report.check_out_anchors_restored == ()
        assert double.session_calls == [(_EMPLOYEE, 10.0)]
        assert double.anchor_calls == []

    def test_recurring_sessions_restore_from_the_final_checkout(
        self, tmp_path
    ) -> None:
        clocks = FakeClocks()
        at_in1 = clocks.wall - timedelta(seconds=1000)
        at_out1 = clocks.wall - timedelta(seconds=900)
        at_in2 = clocks.wall - timedelta(seconds=100)
        at_out2 = clocks.wall - timedelta(seconds=60)
        _write_journal(
            tmp_path,
            [
                _record_line(_temporary_event("ev-1", _EMPLOYEE, at_in1)),
                _record_line(
                    _temporary_event(
                        "ev-2", _EMPLOYEE, at_out1,
                        event_type=EventType.CHECK_OUT,
                    )
                ),
                _record_line(_temporary_event("ev-3", _EMPLOYEE, at_in2)),
                _record_line(
                    _temporary_event(
                        "ev-4", _EMPLOYEE, at_out2,
                        event_type=EventType.CHECK_OUT,
                    )
                ),
            ],
        )
        double = RecordingP26Restore()
        report = recover_session_state(
            double, _store(tmp_path), wall_now=clocks.wall
        )
        # Final folded event is the last CHECK_OUT: anchor from it,
        # session closed.
        assert report.check_out_anchors_restored == (_EMPLOYEE,)
        assert report.open_sessions_restored == ()
        assert double.anchor_calls == [(_EMPLOYEE, 60.0)]
        assert double.session_calls == []

    def test_equal_timestamp_out_after_in_restores_out_anchor(
        self, tmp_path
    ) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=60)
        _write_journal(
            tmp_path,
            [
                _record_line(_temporary_event("ev-in", _EMPLOYEE, at)),
                _record_line(
                    _temporary_event(
                        "ev-out", _EMPLOYEE, at,
                        event_type=EventType.CHECK_OUT,
                    )
                ),
            ],
        )
        double = RecordingP26Restore()
        report = recover_session_state(
            double, _store(tmp_path), wall_now=clocks.wall
        )
        # Folded latest = the later-positioned CHECK_OUT.
        assert report.open_sessions_restored == ()
        assert report.check_out_anchors_restored == (_EMPLOYEE,)
        assert double.session_calls == []
        assert double.anchor_calls == [(_EMPLOYEE, 60.0)]

    def test_future_dated_records_are_clamped_to_zero(self, tmp_path) -> None:
        clocks = FakeClocks()
        _write_journal(
            tmp_path,
            [
                _record_line(
                    _temporary_event(
                        "ev-out", "E-A", clocks.wall + timedelta(seconds=45),
                        event_type=EventType.CHECK_OUT,
                    )
                ),
                _record_line(
                    _temporary_event(
                        "ev-in", "E-B", clocks.wall + timedelta(seconds=45)
                    )
                ),
            ],
        )
        double = RecordingP26Restore()
        report = recover_session_state(
            double, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.future_timestamps_clamped == 2
        assert double.session_calls == [("E-B", 0.0)]
        assert double.anchor_calls == [("E-A", 0.0)]

    def test_check_in_only_journal_recovers_sessions_per_employee(
        self, tmp_path
    ) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=30)
        _write_journal(
            tmp_path,
            [
                _record_line(_temporary_event("ev-1", "EMP-A", at)),
                _record_line(_temporary_event("ev-2", "EMP-B", at)),
            ],
        )
        h = _engine(clocks)
        report = recover_session_state(
            h, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.open_sessions_restored == ("EMP-A", "EMP-B")
        assert report.check_out_anchors_restored == ()
        # And the long-established cooldown replay is unaffected:
        cooldown_report = recover_cooldown_anchors(
            h, _store(tmp_path), wall_now=clocks.wall
        )
        assert cooldown_report.anchors_restored == ("EMP-A", "EMP-B")

    def test_store_contract_and_wall_now_validated_before_any_load(
        self, tmp_path
    ) -> None:
        with pytest.raises(AttendanceInputError):
            recover_session_state(object(), object(), wall_now=_T0)
        with pytest.raises(AttendanceInputError):
            recover_session_state(
                RecordingP26Restore(),
                _store(tmp_path),
                wall_now=_T0.replace(tzinfo=None),  # naive
            )


# ---------------------------------------------------------------------------
# P2-6 S3: journal-backed open_session_lookup
# ---------------------------------------------------------------------------


class TestJournalOpenSessionLookup:
    def test_latest_check_in_returns_true(self, tmp_path) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=30)
        _write_journal(
            tmp_path,
            [_record_line(_temporary_event("ev-in", _EMPLOYEE, at))],
        )
        lookup = make_journal_open_session_lookup(_store(tmp_path))
        assert lookup(_EMPLOYEE) is True

    def test_latest_check_out_returns_false(self, tmp_path) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=30)
        _write_journal(
            tmp_path,
            [
                _record_line(_temporary_event("ev-in", _EMPLOYEE, at)),
                _record_line(
                    _temporary_event(
                        "ev-out", _EMPLOYEE, at,
                        event_type=EventType.CHECK_OUT,
                    )
                ),
            ],
        )
        lookup = make_journal_open_session_lookup(_store(tmp_path))
        assert lookup(_EMPLOYEE) is False

    def test_unknown_employee_returns_false(self, tmp_path) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=30)
        _write_journal(
            tmp_path,
            [_record_line(_temporary_event("ev-in", "OTHER", at))],
        )
        lookup = make_journal_open_session_lookup(_store(tmp_path))
        assert lookup(_EMPLOYEE) is False

    def test_fresh_load_every_call_no_stale_caching(self, tmp_path) -> None:
        clocks = FakeClocks()
        store = _store(tmp_path)
        at = clocks.wall - timedelta(seconds=30)
        _write_journal(
            tmp_path,
            [_record_line(_temporary_event("ev-1", _EMPLOYEE, at))],
        )
        lookup = make_journal_open_session_lookup(store)
        assert lookup(_EMPLOYEE) is True
        # A checkout appended after the first lookup is visible on the
        # very next call (the same store instance, no cached snapshot).
        _append_record(
            tmp_path,
            _temporary_event(
                "ev-2", _EMPLOYEE, clocks.wall,
                event_type=EventType.CHECK_OUT,
            ),
        )
        assert lookup(_EMPLOYEE) is False

    def test_corrupt_journal_error_propagates_never_false(
        self, tmp_path
    ) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=30)
        _write_journal(
            tmp_path,
            [
                _record_line(_temporary_event("ev-in", _EMPLOYEE, at)),
                '{"broken json',
            ],
        )
        lookup = make_journal_open_session_lookup(_store(tmp_path))
        with pytest.raises(CorruptAttendanceJournalError):
            lookup(_EMPLOYEE)

    def test_loader_io_error_propagates_unchanged(self) -> None:
        boom = CorruptAttendanceJournalError("unreadable journal")
        lookup = make_journal_open_session_lookup(ExplodingLoader(error=boom))
        with pytest.raises(CorruptAttendanceJournalError) as excinfo:
            lookup(_EMPLOYEE)
        assert excinfo.value is boom

    def test_invalid_or_malformed_records_fail_closed_via_store(
        self, tmp_path
    ) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=30)
        # An unknown event-type string must fail closed exactly as the
        # store loader does for any other consumer.
        _write_journal(
            tmp_path,
            [_record_line(_temporary_event("ev-x", _EMPLOYEE, at))],
        )
        payload = json.loads(
            _record_line(_temporary_event("ev-x", _EMPLOYEE, at))
        )
        payload["event_type"] = "check_pause"
        _write_journal(tmp_path, [json.dumps(payload)])
        lookup = make_journal_open_session_lookup(_store(tmp_path))
        with pytest.raises(CorruptAttendanceJournalError):
            lookup(_EMPLOYEE)

    def test_lookup_never_mutates_engine_state(self, tmp_path) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=30)
        _write_journal(
            tmp_path,
            [_record_line(_temporary_event("ev-in", _EMPLOYEE, at))],
        )
        h = _engine(clocks)
        lookup = make_journal_open_session_lookup(_store(tmp_path))
        assert lookup(_EMPLOYEE) is True
        # Direct hook calls perform no engine work.
        assert h.tracked_employee_count() == 0
        assert (
            h.update(_verified_result()).status
            is AttendanceDecisionStatus.EVENT_CREATED  # engine own state
        )

    def test_factory_and_hook_input_validation(self, tmp_path) -> None:
        with pytest.raises(AttendanceInputError):
            make_journal_open_session_lookup(object())
        lookup = make_journal_open_session_lookup(_store(tmp_path))
        for employee_id in ("", None, 7):
            with pytest.raises(AttendanceInputError):
                lookup(employee_id)


# ---------------------------------------------------------------------------
# P2-6 S4: combined single-snapshot startup recovery
# ---------------------------------------------------------------------------


class CountingLoadStore:
    """Delegating store double counting validated journal loads."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.loads = 0

    def load_events(self):
        self.loads += 1
        return self._inner.load_events()


def _hook_out_engine(clocks: FakeClocks) -> AttendanceEngine:
    """Engine with an always-True hook (checkout admission upstream)."""
    return AttendanceEngine(
        config=AttendanceConfig(
            check_in_cooldown_seconds=_COOLDOWN,
            check_out_cooldown_seconds=_COOLDOWN,
        ),
        clock_wall=clocks.utc_now,
        monotonic_clock=clocks.mono_now,
        open_session_lookup=lambda _id: True,
        event_id_factory=lambda: "ev-fixed",
    )


class TestRecoverAttendanceStartupState:
    def test_single_journal_load_restores_both_phases(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        at_in = clocks.wall - timedelta(seconds=60)
        at_out = clocks.wall - timedelta(seconds=30)
        _write_journal(
            tmp_path,
            [
                _record_line(_temporary_event("ev-in", _EMPLOYEE, at_in)),
                _record_line(
                    _temporary_event(
                        "ev-out", _EMPLOYEE, at_out,
                        event_type=EventType.CHECK_OUT,
                    )
                ),
            ],
        )
        h = _engine(clocks)
        counting = CountingLoadStore(_store(tmp_path))
        report = recover_attendance_startup_state(
            h, counting, wall_now=clocks.wall
        )
        assert isinstance(report, AttendanceStartupRecoveryReport)
        # Exactly ONE validated journal load backs both phases.
        assert counting.loads == 1
        # P2-3 phase: check-in anchor from the latest CHECK_IN.
        assert report.cooldown.anchors_restored == (_EMPLOYEE,)
        assert report.cooldown.expired_anchors == 0
        assert report.cooldown.events_scanned == 1
        # P2-6 phase: latest folded event is CHECK_OUT → no session,
        # unexpired checkout anchor restored.
        assert report.session.open_sessions_restored == ()
        assert report.session.check_out_anchors_restored == (_EMPLOYEE,)
        assert report.session.expired_check_out_anchors == 0
        assert report.session.events_scanned == 2

    def test_restored_startup_state_behaves_like_the_individual_replays(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        at_in = clocks.wall - timedelta(seconds=60)
        at_out = clocks.wall - timedelta(seconds=30)
        _write_journal(
            tmp_path,
            [
                _record_line(_temporary_event("ev-in", _EMPLOYEE, at_in)),
                _record_line(
                    _temporary_event(
                        "ev-out", _EMPLOYEE, at_out,
                        event_type=EventType.CHECK_OUT,
                    )
                ),
            ],
        )
        # Check-in anchor: suppressed until the boundary (age 60).
        h = _engine(clocks)
        recover_attendance_startup_state(
            h, _store(tmp_path), wall_now=clocks.wall
        )
        assert (
            h.update(_verified_result()).status
            is AttendanceDecisionStatus.SUPPRESSED
        )
        clocks.advance(mono_seconds=240.0)
        assert (
            h.update(_verified_result()).status
            is AttendanceDecisionStatus.EVENT_CREATED
        )
        # Session is closed: a hookless checkout is honestly rejected.
        h2 = _engine(clocks)
        recover_attendance_startup_state(
            h2, _store(tmp_path), wall_now=clocks.wall
        )
        assert (
            h2.update(_verified_result(), event_type=EventType.CHECK_OUT)
            .status
            is AttendanceDecisionStatus.SUPPRESSED
        )
        # Checkout anchor: hook-admitted checkout suppressed until the
        # anchor's boundary. The shared clock is at mono 240 here (the
        # check-in boundary advance above), so the age-30 restore
        # places the anchor at mono 210 → boundary at mono 510.
        h3 = _hook_out_engine(clocks)
        recover_attendance_startup_state(
            h3, _store(tmp_path), wall_now=clocks.wall
        )
        assert (
            h3.update(_verified_result(), event_type=EventType.CHECK_OUT)
            .status
            is AttendanceDecisionStatus.SUPPRESSED
        )
        clocks.mono = 509.0  # 299 s elapsed
        assert (
            h3.update(_verified_result(), event_type=EventType.CHECK_OUT)
            .status
            is AttendanceDecisionStatus.SUPPRESSED
        )
        clocks.mono = 510.0  # exactly 300 s elapsed → expired
        assert (
            h3.update(_verified_result(), event_type=EventType.CHECK_OUT)
            .status
            is AttendanceDecisionStatus.EVENT_CREATED
        )

    def test_check_in_only_journal_matches_the_individual_replays(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        at = clocks.wall - timedelta(seconds=30)
        _write_journal(
            tmp_path,
            [
                _record_line(_temporary_event("ev-1", "EMP-A", at)),
                _record_line(_temporary_event("ev-2", "EMP-B", at)),
            ],
        )
        h = _engine(clocks)
        report = recover_attendance_startup_state(
            h, _store(tmp_path), wall_now=clocks.wall
        )
        assert report.cooldown.anchors_restored == ("EMP-A", "EMP-B")
        assert report.session.open_sessions_restored == ("EMP-A", "EMP-B")
        assert report.session.check_out_anchors_restored == ()
        assert report.session.events_scanned == 2

    def test_corrupt_journal_fails_closed_without_partial_state(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        good = _temporary_event(
            "ev-good", _EMPLOYEE, clocks.wall - timedelta(seconds=15)
        )
        _write_journal(
            tmp_path,
            [_record_line(good), '{"schema_version": 1, "event_id": ""}'],
        )
        h = _engine(clocks)
        with pytest.raises(CorruptAttendanceJournalError):
            recover_attendance_startup_state(
                h, _store(tmp_path), wall_now=clocks.wall
            )
        # All-or-nothing: nothing restored by the failed load.
        assert (
            h.update(_verified_result()).status
            is AttendanceDecisionStatus.EVENT_CREATED
        )

    def test_collaborator_validation_precedes_any_journal_access(
        self, tmp_path: pathlib.Path
    ) -> None:
        clocks = FakeClocks()
        counting = CountingLoadStore(_store(tmp_path))
        # Engine missing any one of the three restore callables.
        with pytest.raises(AttendanceInputError):
            recover_attendance_startup_state(
                RecordingRestore(), counting, wall_now=clocks.wall
            )
        assert counting.loads == 0
        # Naive wall_now.
        with pytest.raises(AttendanceInputError):
            recover_attendance_startup_state(
                _engine(clocks),
                counting,
                wall_now=clocks.wall.replace(tzinfo=None),
            )
        assert counting.loads == 0
