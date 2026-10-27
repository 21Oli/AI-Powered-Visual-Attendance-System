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
    cooldown_out: float = 10.0,
    max_tracked: int = 64,
    **kwargs,
) -> AttendanceEngine:
    return AttendanceEngine(
        config=AttendanceConfig(
            check_in_cooldown_seconds=cooldown,
            check_out_cooldown_seconds=cooldown_out,
            max_tracked_employees=max_tracked,
        ),
        monotonic_clock=clocks.mono_now,
        clock_wall=kwargs.pop("clock_wall", clocks.utc_now),
        **kwargs,
    )


def checkout(engine: AttendanceEngine, employee_id: str = "007"):
    return engine.update(
        verified(employee_id), event_type=EventType.CHECK_OUT
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


# ---------------------------------------------------------------------------
# P2-3: restore_suppression_anchor (restart recovery, additive API)
# ---------------------------------------------------------------------------


class TestRestoreSuppressionAnchor:
    def test_restores_anchor_in_current_monotonic_domain(self, clocks) -> None:
        h = engine(clocks, cooldown=10.0)
        clocks.mono = 100.0
        # Age 4 s of a 10 s cooldown → anchor at 96 → expires at 106.
        assert h.restore_suppression_anchor("007", elapsed_seconds=4.0)
        assert h.tracked_employee_count() == 1
        clocks.mono = 105.0
        assert h.update(verified()).status is AttendanceDecisionStatus.SUPPRESSED
        clocks.mono = 106.0
        assert h.update(verified()).status is AttendanceDecisionStatus.EVENT_CREATED

    def test_expired_age_returns_false_and_touches_nothing(self, clocks) -> None:
        h = engine(clocks, cooldown=10.0)
        clocks.mono = 100.0
        assert h.restore_suppression_anchor("007", elapsed_seconds=10.0) is False
        assert h.restore_suppression_anchor("007", elapsed_seconds=99.0) is False
        assert h.tracked_employee_count() == 0
        # No anchor → immediate fresh event, exactly as after expiry.
        assert h.update(verified()).status is AttendanceDecisionStatus.EVENT_CREATED

    @pytest.mark.parametrize(
        "elapsed",
        [-1.0, float("nan"), float("inf"), float("-inf"), True, "5", None],
    )
    def test_invalid_elapsed_rejected_before_any_state(
        self, clocks, elapsed
    ) -> None:
        h = engine(clocks, cooldown=10.0)
        with pytest.raises(AttendanceInputError):
            h.restore_suppression_anchor("007", elapsed_seconds=elapsed)
        assert h.tracked_employee_count() == 0

    @pytest.mark.parametrize("employee_id", ["", "   ", None, 7])
    def test_invalid_employee_id_rejected(self, clocks, employee_id) -> None:
        h = engine(clocks, cooldown=10.0)
        with pytest.raises(AttendanceInputError):
            h.restore_suppression_anchor(employee_id, elapsed_seconds=1.0)

    def test_restore_beyond_capacity_evicts_least_recently_anchored(
        self, clocks
    ) -> None:
        h = engine(clocks, cooldown=100.0, max_tracked=2)
        clocks.mono = 0.0
        assert h.update(verified("A")).status is AttendanceDecisionStatus.EVENT_CREATED
        clocks.mono = 10.0
        assert h.update(verified("B")).status is AttendanceDecisionStatus.EVENT_CREATED
        # Restoring C (age 1) at mono 20 evicts A (oldest anchor).
        clocks.mono = 20.0
        assert h.restore_suppression_anchor("C", elapsed_seconds=1.0)
        assert h.tracked_employee_count() == 2
        clocks.mono = 30.0
        assert h.update(verified("A")).status is AttendanceDecisionStatus.EVENT_CREATED
        clocks.mono = 40.0
        assert h.update(verified("C")).status is AttendanceDecisionStatus.SUPPRESSED

    def test_double_restore_is_a_deterministic_overwrite(self, clocks) -> None:
        h = engine(clocks, cooldown=10.0)
        clocks.mono = 100.0
        h.restore_suppression_anchor("007", elapsed_seconds=8.0)
        h.restore_suppression_anchor("007", elapsed_seconds=1.0)
        # Age 8 anchor replaced by age 1 → expires at 109, not 102.
        clocks.mono = 105.0
        assert h.update(verified()).status is AttendanceDecisionStatus.SUPPRESSED
        clocks.mono = 109.0
        assert h.update(verified()).status is AttendanceDecisionStatus.EVENT_CREATED

    def test_restore_creates_no_event_and_reads_no_wall_clock(self, clocks) -> None:
        def exploding_wall() -> datetime:
            raise AssertionError("restore must not read the wall clock")

        h = engine(clocks, cooldown=10.0, clock_wall=exploding_wall)
        clocks.mono = 100.0
        assert h.restore_suppression_anchor("007", elapsed_seconds=2.0)
        assert h.tracked_employee_count() == 1


# ---------------------------------------------------------------------------
# P2-6: CHECK_OUT event type, open-session policy, independent bounded maps
# ---------------------------------------------------------------------------


class TestP26EventTypes:
    def test_check_out_is_valid_and_check_in_value_unchanged(self) -> None:
        """CHECK_OUT supplements the enum; CHECK_IN's serialized value
        ("check_in") is byte-for-byte unchanged."""
        assert EventType.CHECK_IN.value == "check_in"
        assert EventType.CHECK_OUT.value == "check_out"
        assert {t.value for t in EventType} == {"check_in", "check_out"}

    def test_check_out_decision_uses_same_event_schema(self, clocks) -> None:
        h = engine(clocks)
        h.update(verified("007"))
        decision = h.update(verified("007"), event_type=EventType.CHECK_OUT)
        assert decision.status is AttendanceDecisionStatus.EVENT_CREATED
        assert decision.event is not None
        assert decision.event.event_type is EventType.CHECK_OUT
        assert decision.event.employee_id == "007"
        assert decision.event.occurred_at.utcoffset() == timedelta(0)


class RecordingHook:
    """Authoritative open-session lookup that records each employee."""

    def __init__(self, answer: bool = True) -> None:
        self.calls: list[str] = []
        self.answer = answer

    def __call__(self, employee_id: str) -> bool:
        self.calls.append(employee_id)
        return self.answer


class TestP26OpenSessionPolicy:
    def test_checkout_succeeds_after_valid_open_session(self, clocks) -> None:
        h = engine(clocks)
        assert h.update(verified("007")).status is (
            AttendanceDecisionStatus.EVENT_CREATED
        )
        out = checkout(h)
        assert out.status is AttendanceDecisionStatus.EVENT_CREATED
        assert out.event is not None
        assert out.event.event_type is EventType.CHECK_OUT
        assert h._open_sessions == {}  # session closed by acceptance
        assert h._check_out_anchors["007"] == 0.0

    def test_independent_checkin_and_checkout_cooldowns(self, clocks) -> None:
        """The check-out gate never consults the check-in anchor."""
        h = engine(clocks, cooldown=10.0, cooldown_out=10.0)
        h.update(verified("007"))  # in anchor @0
        clocks.advance(5.0)
        assert h.update(verified("007")).status is (
            AttendanceDecisionStatus.SUPPRESSED  # check-in still cooled down
        )
        assert checkout(h).status is AttendanceDecisionStatus.EVENT_CREATED

    def test_checkout_without_open_session_rejected_no_event_no_state(
        self, clocks
    ) -> None:
        h = engine(clocks)
        decision = checkout(h)
        assert decision.status is AttendanceDecisionStatus.SUPPRESSED
        assert decision.event is None
        assert decision.reason is not None
        assert "no open session" in decision.reason or (
            "no in-memory open-session marker" in decision.reason
        )
        assert h._open_sessions == {}
        assert h._check_out_anchors == {}
        assert h._check_in_anchors == {}

    def test_rejected_checkout_via_hook_leaves_all_maps_unchanged(
        self, clocks
    ) -> None:
        """Ghost employee (marker absent) + hook False: nothing is
        created or mutated — including the other employee's maps."""
        h = engine(clocks, open_session_lookup=RecordingHook(answer=False))
        h.update(verified("A"))  # A: in anchor + session marker
        decision = checkout(h, "GHOST")
        assert decision.status is AttendanceDecisionStatus.SUPPRESSED
        assert decision.event is None
        assert len(h._check_in_anchors) == 1  # A's anchor untouched
        assert len(h._open_sessions) == 1  # A's marker untouched
        assert h._check_out_anchors == {}

    def test_failed_event_id_factory_leaves_session_intact(self, clocks) -> None:
        """A factory failure on check-out must not close the session."""
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("rng failure")  # only the check-out fails
            return uuid.uuid4().hex

        h = engine(clocks, event_id_factory=flaky)
        h.update(verified("007"))
        with pytest.raises(Exception):
            checkout(h)
        assert h._open_sessions != {}  # marker survived the failure
        out = checkout(h)  # third factory call succeeds
        assert out.status is AttendanceDecisionStatus.EVENT_CREATED
        assert h._open_sessions == {}  # closed exactly once, by acceptance

    def test_checkout_cooldown_rejection_preserves_session_and_anchor(
        self, clocks
    ) -> None:
        h = engine(clocks, cooldown=10.0, cooldown_out=100.0)
        h.update(verified("007"))  # in @0, marker @0
        clocks.advance(1.0)
        assert checkout(h).status is AttendanceDecisionStatus.EVENT_CREATED
        assert h._check_out_anchors == {"007": 1.0}
        assert h._open_sessions == {}
        clocks.advance(9.0)  # mono 10: new check-in re-opens the session
        assert h.update(verified("007")).status is (
            AttendanceDecisionStatus.EVENT_CREATED
        )
        clocks.advance(2.0)  # mono 12: out elapsed 11 < 100
        again = checkout(h)
        assert again.status is AttendanceDecisionStatus.SUPPRESSED
        assert again.event is None
        assert h._open_sessions["007"] == 10.0  # session preserved
        assert h._check_out_anchors == {"007": 1.0}  # anchor not refreshed
        clocks.mono = 101.0  # out elapsed 100 == cooldown
        assert checkout(h).status is AttendanceDecisionStatus.EVENT_CREATED

    def test_checkout_does_not_clear_check_in_anchor(self, clocks) -> None:
        h = engine(clocks, cooldown=10.0, cooldown_out=10.0)
        h.update(verified("007"))  # in @0
        clocks.advance(5.0)
        assert checkout(h).status is AttendanceDecisionStatus.EVENT_CREATED
        clocks.advance(4.5)  # mono 9.5 < in cooldown
        assert h.update(verified("007")).status is (
            AttendanceDecisionStatus.SUPPRESSED  # in anchor untouched
        )
        clocks.advance(0.5)  # exactly 10.0 since the check-in
        assert h.update(verified("007")).status is (
            AttendanceDecisionStatus.EVENT_CREATED
        )

    def test_new_checkin_refreshes_reopens_session_marker(self, clocks) -> None:
        """Accepted new check-in re-opens (last-writer-wins) the session."""
        h = engine(clocks, cooldown=10.0, cooldown_out=10.0)
        h.update(verified("007"))
        clocks.advance(1.0)
        assert checkout(h).status is AttendanceDecisionStatus.EVENT_CREATED
        assert h._open_sessions == {}
        clocks.advance(9.0)  # mono 10: re-check-in after in cooldown
        assert h.update(verified("007")).status is (
            AttendanceDecisionStatus.EVENT_CREATED
        )
        assert h._open_sessions == {"007": 10.0}  # marker replaced
        clocks.advance(1.0)  # mono 11: out anchor @1, elapsed 10
        assert checkout(h).status is AttendanceDecisionStatus.EVENT_CREATED
        assert h._open_sessions == {}

    @pytest.mark.parametrize(
        "bad_result",
        [
            TemporalVerificationResult(
                status=TemporalVerificationStatus.COLLECTING, employee_id=None
            ),
        ],
    )
    def test_nonverified_checkout_never_creates(self, clocks, bad_result) -> None:
        decision = engine(clocks).update(
            bad_result, event_type=EventType.CHECK_OUT
        )
        assert decision.status is AttendanceDecisionStatus.NOT_VERIFIED
        assert decision.event is None

    def test_inactive_employee_checkout_rejected_like_checkin(self, clocks) -> None:
        h = engine(clocks, active_lookup=lambda emp: emp != "IDLE-1")
        decision = checkout(h, "IDLE-1")
        assert decision.status is AttendanceDecisionStatus.INACTIVE_EMPLOYEE
        assert decision.event is None
        assert h._open_sessions == {}
        assert h._check_out_anchors == {}

    def test_invalid_event_type_argument_rejected(self, clocks) -> None:
        h = engine(clocks)
        with pytest.raises(AttendanceInputError):
            h.update(verified("007"), event_type="check_out")  # type: ignore
        assert h.tracked_employee_count() == 0
        assert h._open_sessions == {}

    def test_flag_off_evicted_session_rejected_honestly(self, clocks) -> None:
        """Without a hook, an evicted marker is a bounded-memory miss:
        check-out is rejected, never claimed as a closed session."""
        h = engine(clocks, cooldown=10.0, cooldown_out=10.0, max_tracked=1)
        h.update(verified("A"))  # marker A@0
        clocks.advance(1.0)
        h.update(verified("B"))  # evicts A's marker (bounded state)
        decision = checkout(h, "A")
        assert decision.status is AttendanceDecisionStatus.SUPPRESSED
        assert decision.event is None
        assert decision.reason is not None
        assert "eviction" in decision.reason
        assert "bounded" in decision.reason
        assert h._check_out_anchors == {}  # rejection created nothing
        assert h._open_sessions == {"B": 1.0}


class TestP26HookContract:
    def test_hook_true_adjudicates_missing_marker(self, clocks) -> None:
        hook = RecordingHook()
        h = engine(clocks, open_session_lookup=hook)
        h.update(verified("007"))
        clocks.advance(1.0)  # marker present: hook stays silent
        assert h.update(verified("007")).status is (
            AttendanceDecisionStatus.SUPPRESSED
        )
        h._open_sessions.clear()  # simulate bounded eviction
        out = checkout(h)
        assert out.status is AttendanceDecisionStatus.EVENT_CREATED
        assert hook.calls == ["007"]  # adjudicated exactly once, exact ID

    def test_hook_not_called_when_marker_in_memory(self, clocks) -> None:
        hook = RecordingHook()
        h = engine(clocks, open_session_lookup=hook)
        h.update(verified("007"))
        assert checkout(h).status is AttendanceDecisionStatus.EVENT_CREATED
        assert hook.calls == []  # in-memory hit is authoritative

    def test_non_boolean_hook_return_raises_typed_error(self, clocks) -> None:
        h = engine(clocks, open_session_lookup=lambda e: "yes")
        h.update(verified("007"))
        # 007's marker is in memory (hook would not run); the ghost
        # employee's missing marker triggers the hook return validation:
        with pytest.raises(AttendanceInputError):
            checkout(h, "GHOST")
        # No state mutation from the invalid return:
        assert h._open_sessions == {"007": 0.0}
        assert h._check_out_anchors == {}
        assert h._check_in_anchors == {"007": 0.0}

    def test_hook_exception_propagates_without_state(self, clocks) -> None:
        def exploding(employee_id: str) -> bool:
            raise RuntimeError("journal unavailable")

        h = engine(clocks, open_session_lookup=exploding)
        h.update(verified("007"))
        with pytest.raises(RuntimeError):
            checkout(h, "GHOST")  # missing marker → hook consulted
        assert h._open_sessions == {"007": 0.0}
        assert h._check_out_anchors == {}
        assert h._check_in_anchors == {"007": 0.0}

    def test_non_callable_open_session_lookup_rejected(self) -> None:
        with pytest.raises(AttendanceInputError):
            AttendanceEngine(open_session_lookup="bools-only")


class TestP26IndependentBoundedMaps:
    """Each map is bounded by max_tracked_employees and LRU-evicted by
    its own smallest stored instant; eviction in one map never removes
    entries from the others. Sequencing uses explicit mono advances so
    every map's LRU order is independently observable.
    """

    def test_eviction_is_isolated_per_map(self, clocks) -> None:
        h = engine(clocks, cooldown=10.0, cooldown_out=10.0, max_tracked=2)
        h.update(verified("A"))  # in{A:0} sess{A:0}
        clocks.advance(1.0)
        h.update(verified("B"))  # in{A:0,B:1} sess{A:0,B:1}
        clocks.advance(1.0)
        assert checkout(h, "A").status is (
            AttendanceDecisionStatus.EVENT_CREATED
        )  # out{A:2}; A's session closed legitimately
        clocks.advance(1.0)
        h.update(verified("C"))  # in evicts A ->{B:1,C:3}; sess inserts C
        assert h._check_in_anchors == {"B": 1.0, "C": 3.0}
        assert h._open_sessions == {"B": 1.0, "C": 3.0}
        assert h._check_out_anchors == {"A": 2.0}  # untouched by C's insert
        clocks.advance(1.0)
        assert checkout(h, "C").status is (
            AttendanceDecisionStatus.EVENT_CREATED
        )  # sess{B:1}; out{A:2,C:4}
        clocks.advance(1.0)
        h.update(verified("D"))  # in evicts B; sess{B:1}+D insert (no evict)
        assert h._check_in_anchors == {"C": 3.0, "D": 5.0}
        assert h._open_sessions == {"B": 1.0, "D": 5.0}
        # B's in-anchor was evicted by capacity, but B's session marker
        # survived: in-map eviction never touches the session map.
        clocks.advance(1.0)
        assert checkout(h, "B").status is (
            AttendanceDecisionStatus.EVENT_CREATED  # marker hit, no hook
        )
        # B's out acceptance evicted the least-recent out anchor (A):
        assert h._check_out_anchors == {"C": 4.0, "B": 6.0}
        assert h._open_sessions == {"D": 5.0}  # untouched by out eviction
        assert h._check_in_anchors == {"C": 3.0, "D": 5.0}  # untouched

    def test_session_map_eviction_keeps_checkin_anchors(self, clocks) -> None:
        h = engine(clocks, cooldown=10.0, cooldown_out=10.0, max_tracked=2)
        h.update(verified("A"))
        clocks.advance(1.0)
        h.update(verified("B"))  # both maps full: {A:0, B:1}
        clocks.advance(1.0)
        h.update(verified("C"))  # session map evicts A; in map evicts A
        assert h._open_sessions == {"B": 1.0, "C": 2.0}
        assert h._check_in_anchors == {"B": 1.0, "C": 2.0}
        # B's check-in anchor survived the session-map insert of C:
        assert h.update(verified("B")).status is (
            AttendanceDecisionStatus.SUPPRESSED
        )

    def test_checkout_rejection_kernels_do_not_refresh_out_anchor(
        self, clocks
    ) -> None:
        h = engine(clocks, cooldown=10.0, cooldown_out=6.0)
        h.update(verified("007"))
        clocks.advance(1.0)
        assert checkout(h).status is AttendanceDecisionStatus.EVENT_CREATED
        # Missing marker admitted by the hook, then blocked by cooldown:
        hook = RecordingHook()
        h = engine(clocks, cooldown_out=6.0, open_session_lookup=hook)
        assert checkout(h).status is (
            AttendanceDecisionStatus.EVENT_CREATED  # hook True @5: out anchor
        )
        assert h._check_out_anchors == {"007": 1.0}
        clocks.advance(4.0)  # mono 9: out elapsed 4 < 6
        decision = checkout(h)  # marker still absent: hook adjudicates again
        assert decision.status is AttendanceDecisionStatus.SUPPRESSED
        assert "suppression interval" in decision.reason
        assert h._check_out_anchors == {"007": 1.0}  # not refreshed
        assert h._open_sessions == {}  # hook path mutated nothing
        assert hook.calls == ["007", "007"]


# ---------------------------------------------------------------------------
# P2-6 S3: additive restoration APIs (journal-backed recovery support)
# ---------------------------------------------------------------------------


class TestRestoreCheckOutAnchor:
    def test_restored_anchor_places_state_and_suppresses(self, clocks) -> None:
        h = engine(clocks, cooldown_out=10.0)
        clocks.mono = 100.0
        assert h.restore_check_out_anchor("007", elapsed_seconds=3.0)
        assert h._check_out_anchors == {"007": 97.0}
        # The open-session map and the check-in map stay untouched.
        assert h._open_sessions == {}
        assert h.tracked_employee_count() == 0
        # Recovery is not a session: without a marker/hook a checkout
        # is still rejected (recovery of the anchor != open session).
        assert checkout(h).status is AttendanceDecisionStatus.SUPPRESSED
        # With an admitting hook, the restored anchor suppresses: the
        # anchor sits at mono 100-3=97, so the boundary is mono 107.
        h = engine(
            clocks,
            cooldown_out=10.0,
            open_session_lookup=lambda _id: True,
        )
        clocks.mono = 100.0
        h.restore_check_out_anchor("007", elapsed_seconds=3.0)
        clocks.mono = 106.0
        assert checkout(h).status is AttendanceDecisionStatus.SUPPRESSED
        clocks.mono = 107.0
        assert checkout(h).status is AttendanceDecisionStatus.EVENT_CREATED

    def test_age_at_or_beyond_check_out_cooldown_restores_nothing(
        self, clocks
    ) -> None:
        h = engine(clocks, cooldown_out=10.0)
        clocks.mono = 50.0
        assert not h.restore_check_out_anchor("007", elapsed_seconds=10.0)
        assert not h.restore_check_out_anchor("007", elapsed_seconds=600.0)
        assert h._check_out_anchors == {}  # expired: nothing stored

    def test_expiry_uses_the_check_out_cooldown_not_the_check_in_cooldown(
        self, clocks
    ) -> None:
        h = engine(clocks, cooldown=2.0, cooldown_out=50.0)
        clocks.mono = 10.0
        # Age 3 >= check-in cooldown would be expired for check-ins,
        # but the check-out cooldown governs here.
        assert h.restore_check_out_anchor("007", elapsed_seconds=3.0)
        assert h._check_out_anchors == {"007": 7.0}

    def test_invalid_arguments_rejected_before_any_state_change(
        self, clocks
    ) -> None:
        h = engine(clocks, cooldown_out=10.0)
        clocks.mono = 100.0
        for elapsed in (-3.0, float("nan"), float("inf"), "3", True):
            with pytest.raises(AttendanceInputError):
                h.restore_check_out_anchor("007", elapsed_seconds=elapsed)
        for employee_id in ("", "   ", None, 7):
            with pytest.raises(AttendanceInputError):
                h.restore_check_out_anchor(
                    employee_id, elapsed_seconds=1.0
                )
        assert h._check_out_anchors == {}  # invalid args mutate nothing


class TestRestoreOpenSession:
    def test_restored_marker_allows_checkout_to_proceed(self, clocks) -> None:
        h = engine(clocks, cooldown_out=10.0)
        clocks.mono = 100.0
        assert h.restore_open_session("007", elapsed_seconds=30.0)
        assert h._open_sessions == {"007": 70.0}
        # No anchors were touched by the session restore.
        assert h.tracked_employee_count() == 0
        assert h._check_out_anchors == {}
        # The restored marker authorizes the check-out (no hook needed).
        assert checkout(h).status is AttendanceDecisionStatus.EVENT_CREATED
        assert h._open_sessions == {}  # accepted checkout closed it

    def test_restored_marker_is_authoritative_over_any_hook(
        self, clocks
    ) -> None:
        hook = RecordingHook()
        h = engine(clocks, cooldown_out=10.0, open_session_lookup=hook)
        clocks.mono = 100.0
        h.restore_open_session("007", elapsed_seconds=30.0)
        assert checkout(h).status is AttendanceDecisionStatus.EVENT_CREATED
        assert hook.calls == []  # in-memory marker authoritative

    def test_marker_eviction_order_follows_restored_age(self, clocks) -> None:
        h = engine(clocks, cooldown_out=10.0, max_tracked=2)
        clocks.mono = 100.0
        h.restore_open_session("A", elapsed_seconds=50.0)  # marker @50
        h.restore_open_session("B", elapsed_seconds=5.0)   # marker @95
        # Placing C evicts A (least-recently-opened session).
        assert h.restore_open_session("C", elapsed_seconds=0.0)
        assert set(h._open_sessions) == {"B", "C"}

    def test_invalid_arguments_rejected_before_any_state_change(
        self, clocks
    ) -> None:
        h = engine(clocks, cooldown_out=10.0)
        clocks.mono = 100.0
        for elapsed in (-1.0, float("nan"), float("inf"), None, True):
            with pytest.raises(AttendanceInputError):
                h.restore_open_session("007", elapsed_seconds=elapsed)
        for employee_id in ("", "   ", None, 5):
            with pytest.raises(AttendanceInputError):
                h.restore_open_session(employee_id, elapsed_seconds=1.0)
        assert h._open_sessions == {}  # invalid args mutate nothing
