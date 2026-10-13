"""Step 12 tests: unknown-face handling.

Deterministic and hardware-free: synthetic Step 10/11 result objects
and an injected fake clock (no sleeping). **No camera, no YuNet/SFace
model, no network, no biometric files.**
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.config import (
    UNKNOWN_FACE,
    UnknownFaceConfig,
)
from src.face_recognition import RecognitionResult, RecognitionStatus
from src.temporal_verification import (
    TemporalVerificationResult,
    TemporalVerificationStatus,
)
from src.unknown_face_handler import (
    DEFAULT_SESSION_KEY,
    UnknownFaceHandler,
    UnknownFaceInputError,
    UnknownFaceOutcome,
    UnknownFaceStatus,
    UnresolvedObservation,
)


# ---------------------------------------------------------------------------
# Helpers (fake clock & synthetic results)
# ---------------------------------------------------------------------------


class FakeClock:
    """Injectable monotonic-seconds clock, advanced in whole seconds."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


def handler(clock: FakeClock, **kwargs) -> UnknownFaceHandler:
    cfg_kwargs = {
        "episode_cooldown_seconds": kwargs.pop("cooldown", 30.0),
    }
    cfg_kwargs.update(kwargs)
    return UnknownFaceHandler(
        config=UnknownFaceConfig(**cfg_kwargs), clock=clock
    )


def recognized(status: RecognitionStatus, employee_id: str | None = None):
    return RecognitionResult(
        status=status,
        employee_id=employee_id,
        best_score=0.9 if status is RecognitionStatus.MATCHED else None,
        reason="synthetic",
    )


def temporal(status: TemporalVerificationStatus, employee_id: str | None = None):
    return TemporalVerificationResult(
        status=status,
        employee_id=employee_id,
        streak_frames=2 if status is TemporalVerificationStatus.VERIFIED else 1,
        window_frames_used=3,
        reason="synthetic",
    )


def verified(employee_id: str = "007"):
    return temporal(TemporalVerificationStatus.VERIFIED, employee_id)


# ---------------------------------------------------------------------------
# 1. Verified identities are not classified as unknown
# ---------------------------------------------------------------------------


class TestVerifiedIsNotUnknown:
    def test_verified_temporal_is_no_action(self, clock: FakeClock) -> None:
        result = handler(clock).observe_temporal(verified("007"))
        assert result.status is UnknownFaceStatus.NO_ACTION
        assert result.observation is None
        assert result.suppressed_frames == 0

    def test_verified_never_carries_or_implies_identity(
        self, clock: FakeClock
    ) -> None:
        outcome = handler(clock).observe_temporal(verified("SECRET-1"))
        assert outcome.observation is None
        fields = outcome.__dict__
        for name in ("employee_id", "identity", "score", "vector", "frame"):
            assert name not in fields, name


# ---------------------------------------------------------------------------
# 2-4. Recognition resolutions never produce an identity
# ---------------------------------------------------------------------------


class TestRecognitionNeverIdentity:
    @pytest.mark.parametrize(
        "status,expected_class",
        [
            (RecognitionStatus.UNKNOWN, UnresolvedObservation.UNKNOWN),
            (RecognitionStatus.AMBIGUOUS, UnresolvedObservation.AMBIGUOUS),
            (
                RecognitionStatus.NO_CANDIDATES,
                UnresolvedObservation.NO_CANDIDATES,
            ),
        ],
        ids=["UNKNOWN", "AMBIGUOUS", "NO_CANDIDATES"],
    )
    def test_status_classes_one_to_one_and_identityless(
        self,
        clock: FakeClock,
        status: RecognitionStatus,
        expected_class: UnresolvedObservation,
    ) -> None:
        outcome = handler(clock).observe_recognition(
            recognized(status, employee_id=None)
        )
        assert outcome.status is UnknownFaceStatus.OBSERVED
        assert outcome.observation is expected_class
        # No identity can leak from an unresolved outcome:
        assert outcome.__dict__.get("employee_id", None) is None
        assert outcome.suppressed_frames == 0

    def test_matched_without_temporal_verification_is_unverified(
        self, clock: FakeClock
    ) -> None:
        """A single-frame MATCHED is explicitly NOT verified here."""
        outcome = handler(clock).observe_recognition(
            recognized(RecognitionStatus.MATCHED, employee_id="007")
        )
        assert outcome.observation is UnresolvedObservation.MATCH_UNVERIFIED
        assert outcome.status is UnknownFaceStatus.OBSERVED


# ---------------------------------------------------------------------------
# 5-6. COLLECTING / INTERRUPTED never establish identity
# ---------------------------------------------------------------------------


class TestTemporalUnverified:
    def test_collecting_is_unresolved_not_identity(self, clock) -> None:
        result = handler(clock).observe_temporal(
            temporal(TemporalVerificationStatus.COLLECTING)
        )
        assert result.observation is UnresolvedObservation.TEMPORAL_UNVERIFIED
        assert result.status is UnknownFaceStatus.OBSERVED

    def test_interrupted_is_unresolved_not_identity(self, clock) -> None:
        result = handler(clock).observe_temporal(
            temporal(TemporalVerificationStatus.INTERRUPTED)
        )
        assert result.observation is UnresolvedObservation.TEMPORAL_UNVERIFIED
        assert result.status is UnknownFaceStatus.OBSERVED


# ---------------------------------------------------------------------------
# 7. Only VERIFIED counts as verified
# ---------------------------------------------------------------------------


class TestOnlyVerifiedIsVerified:
    @pytest.mark.parametrize(
        "status",
        [
            TemporalVerificationStatus.COLLECTING,
            TemporalVerificationStatus.INTERRUPTED,
        ],
    )
    def test_temporal_unresolved_cannot_yield_no_action(
        self, clock, status
    ) -> None:
        outcome = handler(clock).observe_temporal(temporal(status))
        assert outcome.status is not UnknownFaceStatus.NO_ACTION
        assert outcome.status is not UnknownFaceStatus.EPISODE_ENDED
        assert outcome.observation is not None

    def test_raw_match_stays_observation_not_no_action(
        self, clock: FakeClock
    ) -> None:
        result = handler(clock).observe_recognition(
            recognized(RecognitionStatus.MATCHED, "007")
        )
        assert result.status is UnknownFaceStatus.OBSERVED
        assert result.observation is UnresolvedObservation.MATCH_UNVERIFIED


# ---------------------------------------------------------------------------
# 8. Malformed or inconsistent results fail safely
# ---------------------------------------------------------------------------


class TestFailSafeInputs:
    def test_wrong_types_rejected(self, clock: FakeClock) -> None:
        h = handler(clock)
        with pytest.raises(UnknownFaceInputError):
            h.observe_temporal("verified")  # type: ignore[arg-type]
        with pytest.raises(UnknownFaceInputError):
            h.observe_temporal(None)  # type: ignore[arg-type]
        with pytest.raises(UnknownFaceInputError):
            h.observe_recognition({"status": "matched"})  # type: ignore[arg-type]
        with pytest.raises(UnknownFaceInputError):
            h.observe_recognition("0.99")  # type: ignore[arg-type]

    def test_synthetic_bogus_statuses_fail_safely(self, clock) -> None:
        """The underlying dataclasses do not type-check statuses; the
        handler must catch malformed objects instead of guessing."""
        bogus_recognition = RecognitionResult.__new__(RecognitionResult)
        object.__setattr__(bogus_recognition, "status", "matched")
        bogus_temporal = TemporalVerificationResult.__new__(
            TemporalVerificationResult
        )
        object.__setattr__(bogus_temporal, "status", "verified")
        h = handler(clock)
        with pytest.raises(UnknownFaceInputError):
            h.observe_recognition(bogus_recognition)
        with pytest.raises(UnknownFaceInputError):
            h.observe_temporal(bogus_temporal)

    def test_inconsistent_contracts_fail_safely(self, clock) -> None:
        h = handler(clock)
        with pytest.raises(UnknownFaceInputError):
            # MATCHED without identity violates the Step 10 contract.
            h.observe_recognition(recognized(RecognitionStatus.MATCHED, None))
        with pytest.raises(UnknownFaceInputError):
            h.observe_recognition(recognized(RecognitionStatus.MATCHED, ""))
        with pytest.raises(UnknownFaceInputError):
            h.observe_recognition(recognized(RecognitionStatus.MATCHED, 7))
        with pytest.raises(UnknownFaceInputError):
            # VERIFIED without identity violates the Step 11 contract.
            h.observe_temporal(temporal(TemporalVerificationStatus.VERIFIED, None))
        # The handler stays usable afterwards and the failed frames
        # never opened an episode.
        assert h.active_episode_count() == 0

    def test_non_callable_clock_rejected(self) -> None:
        with pytest.raises(UnknownFaceInputError):
            UnknownFaceHandler(clock="a string")

    def test_non_config_object_rejected(self, clock: FakeClock) -> None:
        with pytest.raises(UnknownFaceInputError):
            UnknownFaceHandler(config={"cooldown": 1}, clock=clock)


# ---------------------------------------------------------------------------
# 9-12. Cooldown / deduplication policy and expiry
# ---------------------------------------------------------------------------


class TestCooldownPolicy:
    def test_first_unresolved_is_observed_then_suppressed(
        self, clock: FakeClock
    ) -> None:
        h = handler(clock, cooldown=10.0)
        first = h.observe_temporal(temporal(TemporalVerificationStatus.COLLECTING))
        assert first.status is UnknownFaceStatus.OBSERVED
        for expected_suppressed in (1, 2, 3):
            clock.advance(1.0)
            outcome = h.observe_recognition(
                recognized(RecognitionStatus.UNKNOWN)
            )
            assert outcome.status is UnknownFaceStatus.SUPPRESSED
            assert outcome.suppressed_frames == expected_suppressed

    def test_cooldown_does_not_refresh_while_suppressed(
        self, clock: FakeClock
    ) -> None:
        """Deterministic expiry: the alert instant is pinned; repeated
        suppressed frames never extend the episode."""
        h = handler(clock, cooldown=10.0)
        h.observe_temporal(temporal(TemporalVerificationStatus.COLLECTING))
        for _ in range(5):
            clock.advance(1.0)
            result = h.observe_recognition(recognized(RecognitionStatus.UNKNOWN))
        assert clock.now == 5.0
        clock.advance(5.0)  # now at 10.0 = alert + cooldown
        result = h.observe_recognition(recognized(RecognitionStatus.UNKNOWN))
        assert result.status is UnknownFaceStatus.OBSERVED  # fresh episode
        assert result.suppressed_frames == 0
        assert h.active_episode_count() == 1

    def test_cooldown_boundary_exact_expiry(self, clock: FakeClock) -> None:
        """< cooldown inside the episode; == cooldown expired."""
        h = handler(clock, cooldown=10.0)
        h.observe_recognition(recognized(RecognitionStatus.AMBIGUOUS))
        clock.advance(9.5)
        within = h.observe_recognition(recognized(RecognitionStatus.AMBIGUOUS))
        assert within.status is UnknownFaceStatus.SUPPRESSED
        clock.advance(0.5)  # exactly 10.0 since the alert
        at_expiry = h.observe_recognition(recognized(RecognitionStatus.AMBIGUOUS))
        assert at_expiry.status is UnknownFaceStatus.OBSERVED

    def test_verified_ends_active_episode_immediately(
        self, clock: FakeClock
    ) -> None:
        h = handler(clock, cooldown=30.0)
        h.observe_recognition(recognized(RecognitionStatus.UNKNOWN))
        result = h.observe_temporal(verified("007"))
        assert result.status is UnknownFaceStatus.EPISODE_ENDED
        assert result.observation is None
        assert h.active_episode_count() == 0
        # The next verified frame is plain NO_ACTION.
        assert (
            h.observe_temporal(verified("007")).status
            is UnknownFaceStatus.NO_ACTION
        )

    def test_expired_state_does_not_suppress_indefinitely(
        self, clock: FakeClock
    ) -> None:
        h = handler(clock, cooldown=5.0)
        h.observe_temporal(temporal(TemporalVerificationStatus.INTERRUPTED))
        clock.advance(100.0)  # long silence, no new evidence
        late = h.observe_recognition(recognized(RecognitionStatus.NO_CANDIDATES))
        assert late.status is UnknownFaceStatus.OBSERVED  # stale state gone
        assert h.active_episode_count() == 1


# ---------------------------------------------------------------------------
# 11. Reset behavior
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_single_session(self, clock: FakeClock) -> None:
        h = handler(clock)
        h.observe_recognition(recognized(RecognitionStatus.UNKNOWN, None))
        assert h.reset(DEFAULT_SESSION_KEY) == 1
        assert h.active_episode_count() == 0
        assert h.reset(DEFAULT_SESSION_KEY) == 0  # already ended
        # After reset the next unresolved frame is observed again.
        outcome = h.observe_recognition(recognized(RecognitionStatus.UNKNOWN))
        assert outcome.status is UnknownFaceStatus.OBSERVED

    def test_reset_all_sessions(self, clock: FakeClock) -> None:
        h = handler(clock, max_sessions=4)
        h.observe_recognition(recognized(RecognitionStatus.UNKNOWN, None))
        h.observe_recognition(
            recognized(RecognitionStatus.UNKNOWN, None), session_key="cam-2"
        )
        h.reset(None)
        assert h.active_episode_count() == 0
        assert h.active_episode_count() == 0  # stable double-check

    def test_reset_empty_handler_returns_zero(self, clock: FakeClock) -> None:
        assert handler(clock).reset(None) == 0


# ---------------------------------------------------------------------------
# 13. Bounded memory / state
# ---------------------------------------------------------------------------


class TestBoundedState:
    def test_many_suppressed_frames_over_time_stay_bounded(self, clock) -> None:
        h = handler(clock, cooldown=10.0)
        for _ in range(200):
            clock.advance(1.0)
            h.observe_recognition(recognized(RecognitionStatus.UNKNOWN))
        assert h.active_episode_count() == 1  # one bounded episode struct

    def test_session_eviction_beyond_max_sessions(self, clock) -> None:
        h = handler(clock, max_sessions=3, cooldown=1000.0)
        h.observe_recognition(recognized(RecognitionStatus.UNKNOWN), session_key="s1")
        clock.advance(1.0)
        h.observe_recognition(recognized(RecognitionStatus.UNKNOWN), session_key="s2")
        clock.advance(1.0)
        h.observe_recognition(recognized(RecognitionStatus.UNKNOWN), session_key="s3")
        assert h.active_episode_count() == 3
        clock.advance(1.0)
        # s1 is least-recently seen → evicted when s4 arrives.
        h.observe_recognition(recognized(RecognitionStatus.UNKNOWN), session_key="s4")
        assert h.active_episode_count() == 3
        with pytest.raises(UnknownFaceInputError):
            h.observe_recognition(
                recognized(RecognitionStatus.UNKNOWN, None),
                session_key=("x" * 129)[:129],
            )
        # Re-observing s1 opens a fresh episode (state was bounded away,
        # nothing merged or corrupted).
        clock.advance(1.0)
        again = h.observe_recognition(
            recognized(RecognitionStatus.UNKNOWN, None), session_key="s1"
        )
        assert again.status is UnknownFaceStatus.OBSERVED
        assert h.active_episode_count() == 3

    def test_outcome_objects_carry_no_biometric_fields(self, clock) -> None:
        outcome = handler(clock).observe_recognition(
            recognized(RecognitionStatus.MATCHED, "007")
        )
        for name in ("vector", "embedding", "crop", "frame", "scores"):
            assert name not in outcome.__dict__, name


# ---------------------------------------------------------------------------
# 14-16. Session isolation & key validation
# ---------------------------------------------------------------------------


class TestSessionIsolation:
    def test_instances_share_nothing(self, clock: FakeClock) -> None:
        a = handler(clock, cooldown=100.0)
        b = handler(clock, cooldown=100.0)
        a.observe_recognition(recognized(RecognitionStatus.UNKNOWN))
        a.reset(None)
        seen_b = b.observe_recognition(
            recognized(RecognitionStatus.UNKNOWN), session_key="other"
        )
        assert seen_b.status is UnknownFaceStatus.OBSERVED
        assert b.active_episode_count() == 1
        assert a.active_episode_count() == 0

    def test_sessions_are_isolated(self, clock: FakeClock) -> None:
        h = handler(clock, cooldown=100.0)
        h.observe_recognition(
            recognized(RecognitionStatus.AMBIGUOUS), session_key="cam-A"
        )
        other = h.observe_recognition(
            recognized(RecognitionStatus.AMBIGUOUS), session_key="cam-B"
        )
        assert other.status is UnknownFaceStatus.OBSERVED  # not shared
        suppressed = h.observe_recognition(
            recognized(RecognitionStatus.AMBIGUOUS), session_key="cam-A"
        )
        assert suppressed.status is UnknownFaceStatus.SUPPRESSED

    def test_default_session_key_is_explicit_and_documented(
        self, clock: FakeClock
    ) -> None:
        h = handler(clock)
        h.observe_recognition(recognized(RecognitionStatus.UNKNOWN, None))
        with_default = h.observe_recognition(
            recognized(RecognitionStatus.UNKNOWN, None)
        )
        assert with_default.status is UnknownFaceStatus.SUPPRESSED

    @pytest.mark.parametrize(
        "bad_key", [None, "", 7, " ", "x" * 129],
        ids=["None", "empty", "int", "whitespace-space", "too-long"],
    )
    def test_invalid_session_keys_rejected(self, clock, bad_key) -> None:
        h = handler(clock)
        with pytest.raises(UnknownFaceInputError):
            h.observe_recognition(
                recognized(RecognitionStatus.UNKNOWN, None),
                session_key=bad_key,  # type: ignore[arg-type]
            )
        if bad_key is None:
            # reset(None) is the documented "clear every session" call.
            h.reset(None)  # type: ignore[arg-type]
            assert h.active_episode_count() == 0
        else:
            with pytest.raises(UnknownFaceInputError):
                h.reset(bad_key)  # type: ignore[arg-type]
        assert h.active_episode_count() == 0

    def test_max_session_key_length_is_configurable(self, clock) -> None:
        h = handler(clock, cooldown=1.0, max_session_key_length=4)
        h.observe_recognition(
            recognized(RecognitionStatus.UNKNOWN, None), session_key="abcd"
        )
        with pytest.raises(UnknownFaceInputError):
            h.reset("abcde")

    def test_reset_accepts_only_str_keys(self, clock: FakeClock) -> None:
        h = handler(clock)
        with pytest.raises(UnknownFaceInputError):
            h.reset(3)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 17. Configuration validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_frozen_config_instance_matches_module_default(self) -> None:
        assert UNKNOWN_FACE.max_sessions >= 1
        assert UNKNOWN_FACE.episode_cooldown_seconds > 0

    @pytest.mark.parametrize(
        "bad_kwargs",
        [
            {"episode_cooldown_seconds": 0},
            {"episode_cooldown_seconds": -1.5},
            {"episode_cooldown_seconds": True},   # bool, not a number
            {"episode_cooldown_seconds": float("inf")},
            {"episode_cooldown_seconds": float("nan")},
            {"episode_cooldown_seconds": "30"},
            {"max_sessions": 0},
            {"max_sessions": -1},
            {"max_sessions": 2.0},                # float rejected
            {"max_sessions": True},               # bool rejected
            {"max_session_key_length": 0},
            {"max_session_key_length": True},
        ],
    )
    def test_invalid_values_rejected(self, bad_kwargs: dict) -> None:
        with pytest.raises(ValueError):
            UnknownFaceConfig(**bad_kwargs)

    def test_config_object_is_frozen(self) -> None:
        cfg = UnknownFaceConfig()
        with pytest.raises(Exception):
            cfg.max_sessions = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 20. Approved contracts unchanged (unaffected guards)
# ---------------------------------------------------------------------------


class TestContractsUnchanged:
    def test_recognition_status_values_not_altered(self) -> None:
        assert {s.value for s in RecognitionStatus} == {
            "matched",
            "unknown",
            "ambiguous",
            "no_candidates",
        }

    def test_temporal_status_values_not_altered(self) -> None:
        assert TemporalVerificationStatus.VERIFIED.value == "verified"
        # The handler never mutates results it observes.
        t = temporal(TemporalVerificationStatus.COLLECTING)
        before = (t.status, t.employee_id, t.streak_frames)
        handler(FakeClock()).observe_temporal(t)
        assert (t.status, t.employee_id, t.streak_frames) == before


# ---------------------------------------------------------------------------
# 19. Import / construction has no side effects (fresh subprocess)
# ---------------------------------------------------------------------------


class TestNoSideEffects:
    def test_import_and_construct_in_clean_subprocess(self) -> None:
        code = (
            "import sys\n"
            "sys.path.insert(0, r'{root}')\n"
            "from src.unknown_face_handler import UnknownFaceHandler\n"
            "h = UnknownFaceHandler()\n"
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
# Outcome type shape
# ---------------------------------------------------------------------------


class TestOutcomeShape:
    def test_outcome_is_frozen(self) -> None:
        outcome = UnknownFaceOutcome(status=UnknownFaceStatus.OBSERVED)
        with pytest.raises(Exception):
            outcome.status = UnknownFaceStatus.NO_ACTION  # type: ignore

    def test_status_values_are_explicit_strings(self) -> None:
        assert UnknownFaceStatus.OBSERVED.value == "observed"
        assert UnknownFaceStatus.SUPPRESSED.value == "suppressed"
        assert UnknownFaceStatus.NO_ACTION.value == "no_action"
        assert UnknownFaceStatus.EPISODE_ENDED.value == "episode_ended"
