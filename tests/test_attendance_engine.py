"""Step 13 tests: attendance-event engine.

Deterministic and hardware-free: synthetic Step 11 temporal results,
injectable fake wall/monotonic clocks (no sleeping), and injected
event-ID factories. **No camera, no YuNet/SFace model, no network,
no biometric files.**
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.attendance import (
    AttendanceDecision,
    AttendanceDecisionStatus,
    AttendanceEngine,
    AttendanceError,
    AttendanceEvent,
    AttendanceInputError,
    EventType,
)
from src.config import ATTENDANCE, AttendanceConfig
from src.face_recognition import RecognitionResult, RecognitionStatus
from src.temporal_verification import (
    TemporalVerificationResult,
    TemporalVerificationStatus,
)

# ---------------------------------------------------------------------------
# Helpers (fake clocks & synthetic results)
# ---------------------------------------------------------------------------


class FakeClocks:
    """Injectable wall + monotonic clocks, advanced independently."""

    def __init__(self) -> None:
        self.mono = 0.0
        self.wall = datetime(2026, 10, 13, 9, 0, 0, tzinfo=UTC)

    def utc_now(self) -> datetime:
        return self.wall

    def mono_now(self) -> float:
        return self.mono

    def advance(self, seconds: float = 0.0, wall_seconds: float = 0.0) -> None:
        self.mono += seconds
        self.wall = self.wall + timedelta(seconds=wall_seconds)


@pytest.fixture()
def clocks() -> FakeClocks:
    return FakeClocks()


def engine(
    clocks: FakeClocks,
    *,
    cooldown: float = 10.0,
    max_tracked: int = 64,
    **kwargs,
) -> AttendanceEngine:
    return AttendanceEngine(
        config=AttendanceConfig(
            check_in_cooldown_seconds=cooldown,
            max_tracked_employees=max_tracked,
        ),
        monotonic_clock=clocks.mono_now,
        clock_wall=kwargs.pop("clock_wall", clocks.utc_now),
        **kwargs,
    )


def verified(employee_id: str = "007") -> TemporalVerificationResult:
    return TemporalVerificationResult(
        status=TemporalVerificationStatus.VERIFIED,
        employee_id=employee_id,
        streak_frames=3,
        window_frames_used=3,
        reason="consistent matched streak long enough",
    )


def creating(engine: AttendanceEngine, employee_id: str) -> AttendanceDecision:
    decision = engine.update(verified(employee_id))
    assert decision.status is AttendanceDecisionStatus.EVENT_CREATED
    assert decision.event is not None
    return decision


# ---------------------------------------------------------------------------
# Trust boundary (requirements 1-9)
# ---------------------------------------------------------------------------


class TestTrustBoundary:
    def test_valid_verified_creates_check_in(self, clocks) -> None:
        decision = engine(clocks).update(verified("007"))
        assert decision.status is AttendanceDecisionStatus.EVENT_CREATED
        assert decision.event is not None
        assert decision.event.event_type is EventType.CHECK_IN
        assert decision.event.employee_id == "007"

    @pytest.mark.parametrize(
        "status",
        [
            TemporalVerificationStatus.COLLECTING,
            TemporalVerificationStatus.INTERRUPTED,
        ],
    )
    def test_non_verified_never_creates(
        self, clocks, status: TemporalVerificationStatus
    ) -> None:
        result = TemporalVerificationResult(status=status, employee_id=None)
        decision = engine(clocks).update(result)
        assert decision.status is AttendanceDecisionStatus.NOT_VERIFIED
        assert decision.event is None

    def test_raw_match_cannot_authorize_an_event(self, clocks) -> None:
        """A raw Step 10 result is not an accepted input — temporal
        verification is the only attendance boundary."""
        h = engine(clocks)
        with pytest.raises(AttendanceInputError):
            h.update(
                RecognitionResult(
                    status=RecognitionStatus.MATCHED, employee_id="007"
                )
            )
        assert h.tracked_employee_count() == 0

    @pytest.mark.parametrize(
        "status",
        [
            RecognitionStatus.UNKNOWN,
            RecognitionStatus.AMBIGUOUS,
            RecognitionStatus.NO_CANDIDATES,
        ],
        ids=["UNKNOWN", "AMBIGUOUS", "NO_CANDIDATES"],
    )
    def test_recognition_outcomes_cannot_authorize(
        self, clocks, status: RecognitionStatus
    ) -> None:
        h = engine(clocks)
        with pytest.raises(AttendanceInputError):
            h.update(
                RecognitionResult(status=status, employee_id=None)
            )
        assert h.tracked_employee_count() == 0

    @pytest.mark.parametrize("bad_id", [None, "", 7, "  "],
                             ids=["None", "empty", "int", "blank"])
    def test_missing_or_empty_ids_rejected_safely(self, clocks, bad_id) -> None:
        h = engine(clocks)
        with pytest.raises(AttendanceInputError):
            h.update(
                TemporalVerificationResult(
                    status=TemporalVerificationStatus.VERIFIED,
                    employee_id=bad_id,
                )
            )
        assert h.tracked_employee_count() == 0

    def test_malformed_results_do_not_produce_events(self, clocks) -> None:
        h = engine(clocks)
        # Wrong container type / None / bogus structures:
        for bad in ("verified", None, 42, {"status": "VERIFIED"}):
            with pytest.raises(AttendanceInputError):
                h.update(bad)
        # Synthetic result whose status field is not the real enum
        # (the frozen dataclass does not type-check it):
        bogus = TemporalVerificationResult.__new__(TemporalVerificationResult)
        object.__setattr__(bogus, "status", "verified")
        with pytest.raises(AttendanceInputError):
            h.update(bogus)
        assert h.tracked_employee_count() == 0

    def test_inactive_rejected_with_authoritative_hook(self, clocks) -> None:
        h = engine(clocks, active_lookup=lambda emp: emp != "IDLE-1")
        h.update(verified("ACTIVE"))  # gate passes for active
        clocks.advance()
        decision = h.update(verified("IDLE-1"))
        assert decision.status is AttendanceDecisionStatus.INACTIVE_EMPLOYEE
        assert decision.event is None

    def test_no_active_invention_without_hook(self, clocks) -> None:
        """Without a hook the engine never invents active status."""
        decision = engine(clocks).update(verified())
        assert decision.event is not None  # active gating was delegated

    def test_inputs_are_never_mutated(self, clocks) -> None:
        result = verified("007")
        before = (
            result.status, result.employee_id, result.streak_frames,
            result.window_frames_used, result.reason,
        )
        engine(clocks).update(result)
        assert (
            result.status, result.employee_id, result.streak_frames,
            result.window_frames_used, result.reason,
        ) == before
        # Frozen dataclass immutability still enforced:
        with pytest.raises(Exception):
            result.status = TemporalVerificationStatus.COLLECTING  # type: ignore


# ---------------------------------------------------------------------------
# Event schema (requirements 10-14)
# ---------------------------------------------------------------------------


class TestEventSchema:
    def test_event_and_decision_are_frozen(self, clocks) -> None:
        decision = creating(engine(clocks), "007")
        event = decision.event
        with pytest.raises(Exception):
            event.employee_id = "OTHER"  # type: ignore
        with pytest.raises(Exception):
            decision.status = AttendanceDecisionStatus.SUPPRESSED  # type: ignore

    @pytest.mark.parametrize("employee_id", ["007", "EMP-0042", "001-MAIN"])
    def test_ids_preserved_exactly(self, clocks, employee_id: str) -> None:
        event = creating(engine(clocks), employee_id).event
        assert event is not None and event.employee_id == employee_id

    def test_timestamp_is_aware_and_utc_normalized(self, clocks) -> None:
        aware = timezone(timedelta(hours=2))
        wall = lambda: datetime(2026, 10, 13, 11, 30, tzinfo=aware)
        event = creating(engine(clocks, clock_wall=wall), "007").event
        assert event is not None
        assert event.occurred_at.utcoffset() == timedelta(0)  # UTC
        assert event.occurred_at == datetime(2026, 10, 13, 9, 30, tzinfo=UTC)

    def test_naive_wall_timestamp_rejected(self, clocks) -> None:
        h = engine(clocks, clock_wall=lambda: datetime(2026, 10, 13, 9, 0))
        with pytest.raises(AttendanceInputError):
            h.update(verified())
        assert h.tracked_employee_count() == 0  # no event, no state

    def test_non_datetime_wall_rejected(self, clocks) -> None:
        h = engine(clocks, clock_wall=lambda: 12345.0)
        with pytest.raises(AttendanceInputError):
            h.update(verified())

    def test_event_ids_unique_and_nonempty(self, clocks) -> None:
        h = engine(clocks)
        ids = [creating(h, f"E{i}").event.event_id for i in range(10)]
        assert all(isinstance(i, str) and i for i in ids)
        assert len(set(ids)) == 10  # unique across events
        assert len(ids[0]) == 32  # documented uuid4().hex contract

    def test_event_has_no_biometric_fields(self, clocks) -> None:
        event = creating(engine(clocks), "007").event
        for name in ("vector", "embedding", "crop", "frame", "score"):
            assert name not in event.__dict__, name
        decision = engine(clocks).update(
            TemporalVerificationResult(status=TemporalVerificationStatus.COLLECTING)
        )
        for name in ("vector", "embedding", "crop", "frame", "score"):
            assert name not in decision.__dict__, name


# ---------------------------------------------------------------------------
# Duplicate suppression (requirements 15-21)
# ---------------------------------------------------------------------------


class TestDuplicateSuppression:
    def test_first_eligible_creates_event(self, clocks) -> None:
        decision = engine(clocks).update(verified("007"))
        assert decision.status is AttendanceDecisionStatus.EVENT_CREATED

    def test_repeats_within_interval_suppressed(self, clocks) -> None:
        h = engine(clocks, cooldown=10.0)
        first = h.update(verified("007"))
        assert first.event is not None
        for steps in (1, 2, 3):
            clocks.advance()
            again = h.update(verified("007"))
            assert again.status is AttendanceDecisionStatus.SUPPRESSED
            assert again.event is None  # never a fabricated event

    def test_different_employee_independent(self, clocks) -> None:
        h = engine(clocks, cooldown=1000.0)
        h.update(verified("A"))
        other = h.update(verified("B"))
        assert other.status is AttendanceDecisionStatus.EVENT_CREATED
        suppressed = h.update(verified("A"))
        assert suppressed.status is AttendanceDecisionStatus.SUPPRESSED

    def test_exact_boundary(self, clocks) -> None:
        """elapsed < cooldown suppressed; elapsed == cooldown allowed."""
        h = engine(clocks, cooldown=10.0)
        h.update(verified("007"))
        clocks.advance(9.5)
        below = h.update(verified("007"))
        assert below.status is AttendanceDecisionStatus.SUPPRESSED
        clocks.advance(0.5)  # exactly 10.0 since the created event
        at_boundary = h.update(verified("007"))
        assert at_boundary.status is AttendanceDecisionStatus.EVENT_CREATED
        assert at_boundary.event is not None

    def test_repeats_do_not_refresh_interval(self, clocks) -> None:
        """The anchor is the created event's instant — suppressed
        repeats never extend it (deterministic re-eligibilty)."""
        h = engine(clocks, cooldown=10.0)
        h.update(verified("007"))
        for _ in range(9):
            clocks.advance(1.0)
            assert h.update(verified("007")).status is (
                AttendanceDecisionStatus.SUPPRESSED
            )
        assert clocks.mono == 9.0 < 10.0
        clocks.advance(1.0)  # now exactly 10.0
        re_event = h.update(verified("007"))
        assert re_event.status is AttendanceDecisionStatus.EVENT_CREATED

    @pytest.mark.parametrize(
        "bad_kwargs",
        [
            {"check_in_cooldown_seconds": 0},
            {"check_in_cooldown_seconds": -5},
            {"check_in_cooldown_seconds": float("inf")},
            {"check_in_cooldown_seconds": float("nan")},
            {"check_in_cooldown_seconds": True},   # bool rejected
            {"check_in_cooldown_seconds": "30"},   # str rejected
            {"max_tracked_employees": 0},
            {"max_tracked_employees": 2.5},        # float rejected
            {"max_tracked_employees": True},       # bool rejected
            {"max_tracked_employees": None},
        ],
    )
    def test_invalid_config_rejected(self, bad_kwargs: dict) -> None:
        with pytest.raises(ValueError):
            AttendanceConfig(**bad_kwargs)

    def test_config_frozen_and_valid_default(self) -> None:
        cfg = ATTENDANCE
        assert cfg.check_in_cooldown_seconds > 0
        with pytest.raises(Exception):
            cfg.max_tracked_employees = 99999  # type: ignore


class TestClockReliability:

    def test_nonfinite_and_bool_monotonic_rejected_safely(
        self, clocks
    ) -> None:
        """Invalid clock values fail safely: no event, no state."""
        for bad_value in (float("nan"), float("inf"), "0.0", True):
            h = AttendanceEngine(
                config=AttendanceConfig(check_in_cooldown_seconds=10.0),
                monotonic_clock=lambda v=bad_value: v,
                clock_wall=clocks.utc_now,
            )
            with pytest.raises(AttendanceInputError):
                h.update(verified())
            assert h.tracked_employee_count() == 0

    def test_backwards_wall_clock_cannot_bypass_suppression(
        self, clocks: FakeClocks
    ) -> None:
        """Suppression is governed by the monotonic clock: wall time
        jumping backwards leaves duplicate protection intact."""
        h = engine(clocks, cooldown=100.0)
        h.update(verified("007"))
        clocks.advance(wall_seconds=-3600.0)  # wall moves backward
        again = h.update(verified("007"))
        # Still suppressed — anchors never use wall clock in Phase 1.
        assert again.status is AttendanceDecisionStatus.SUPPRESSED
        assert again.event is None

    def test_backward_wall_clock_does_not_create_contradictory_stamps(
        self, clocks
    ) -> None:
        clocks2 = FakeClocks()
        h = engine(clocks2, cooldown=10.0)
        clocks2.advance(wall_seconds=-30.0)  # wall jumps backward first
        d = h.update(verified("001"))
        assert d.status is AttendanceDecisionStatus.EVENT_CREATED
        assert d.event.occurred_at == datetime(2026, 10, 13, 8, 59, 30, tzinfo=UTC)
        # The engine did not crash or emit duplicate protection breaks;
        # suppression still uses monotonic time:
        clocks2.mono = 10.0
        assert h.update(verified("001")).status is (
            AttendanceDecisionStatus.EVENT_CREATED
        )

    def test_reset_single_and_all(self, clocks) -> None:
        h = engine(clocks, max_tracked=4)
        h.update(verified("A"))
        h.update(verified("B"))
        assert h.reset("A") == 1
        assert h.reset("A") == 0
        assert h.tracked_employee_count() == 1
        # A's suppression is gone: immediate event again.
        assert h.update(verified("A")).event is not None
        assert h.reset(None) == 2
        assert h.reset(None) == 0

    def test_reset_requires_valid_id(self, clocks) -> None:
        h = engine(clocks)
        with pytest.raises(AttendanceInputError):
            h.reset(7)  # type: ignore[arg-type]
        with pytest.raises(AttendanceInputError):
            h.reset("")

    def test_independent_instances_share_nothing(self, clocks) -> None:
        a = engine(clocks, cooldown=1000.0)
        b = engine(clocks, cooldown=1000.0)
        a.update(verified("007"))
        decide_b = b.update(verified("007"))
        assert decide_b.status is AttendanceDecisionStatus.EVENT_CREATED
        assert a.tracked_employee_count() == 1
        assert b.tracked_employee_count() == 1

    def test_engine_error_types_hierarchy(self) -> None:
        assert issubclass(AttendanceInputError, AttendanceError)
        assert issubclass(AttendanceInputError, ValueError)

    def test_non_callable_params_rejected(self, clocks) -> None:
        with pytest.raises(AttendanceInputError):
            AttendanceEngine(monotonic_clock="x")
        with pytest.raises(AttendanceInputError):
            AttendanceEngine(clock_wall=42)  # type: ignore[arg-type]
        with pytest.raises(AttendanceInputError):
            AttendanceEngine(event_id_factory=True)
        with pytest.raises(AttendanceInputError):
            AttendanceEngine(active_lookup="bools-only")


class TestBoundedStateAndHygiene:
    def test_event_id_factory_failure_never_leaves_state(self, clocks) -> None:
        class Flaky:
            def __init__(self) -> None:
                self.calls = 0

            def __call__(self) -> str:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("rng failure")
                return uuid.uuid4().hex

        flaky = Flaky()
        h = engine(clocks, cooldown=10.0, event_id_factory=flaky)
        with pytest.raises(Exception):
            h.update(verified())
        assert h.tracked_employee_count() == 0  # nothing recorded
        decision = h.update(verified())  # engine remains usable
        assert decision.event is not None and flaky.calls == 2

    def test_state_bounded_by_lru_eviction(self, clocks) -> None:
        h = engine(clocks, cooldown=1000.0, max_tracked=3)
        for emp in ("A", "B", "C"):
            h.update(verified(emp))
            clocks.advance()
        assert h.tracked_employee_count() == 3
        # D evicts the least-recently-anchored (A).
        h.update(verified("D"))
        assert h.tracked_employee_count() == 3
        # A's anchor was evicted: worst case of one extra event.
        clocks.advance()
        decision = h.update(verified("A"))
        assert decision.status is AttendanceDecisionStatus.EVENT_CREATED

    def test_suppression_state_bounded_over_long_run(self, clocks) -> None:
        h = engine(clocks, cooldown=2.0)
        for _ in range(200):
            h.update(verified("A"))
            clocks.advance(1.0)
        assert h.tracked_employee_count() == 1

    def test_import_and_construct_in_clean_subprocess(self) -> None:
        code = (
            "import sys\n"
            "sys.path.insert(0, r'{root}')\n"
            "from src.attendance import AttendanceEngine\n"
            "e = AttendanceEngine()\n"
            "print('clean')\n"
        ).format(root=Path(__file__).resolve().parent.parent)
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            cwd="/",
        )
        assert completed.returncode == 0
        assert "clean" in completed.stdout
