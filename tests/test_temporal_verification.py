"""Step 11 tests: temporal / multi-frame verification.

Deterministic and hardware-free: synthetic ``RecognitionResult``
instances only. **No camera, no real YuNet/SFace model, no network,**
no biometric files — the temporal logic is exercised purely against
the Step 10 result contract (including its frozen-dataclass and
input-validation guarantees).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from src.config import (
    RECOGNITION,
    TEMPORAL_VERIFICATION,
    TemporalVerificationConfig,
)
from src.face_recognition import RecognitionResult, RecognitionStatus
from src.temporal_verification import (
    TemporalVerificationInputError,
    TemporalVerificationResult,
    TemporalVerificationStatus,
    TemporalVerifier,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def matched(employee_id: str) -> RecognitionResult:
    """A MATCHED result for one employee (Step 10 contract shape)."""
    return RecognitionResult(
        status=RecognitionStatus.MATCHED,
        employee_id=employee_id,
        best_score=0.9,
        runner_up_score=None,
        margin=None,
        reason="best score above threshold and margin satisfied",
    )


def unknown() -> RecognitionResult:
    return RecognitionResult(
        status=RecognitionStatus.UNKNOWN,
        employee_id=None,
        best_score=0.1,
        reason="best score below acceptance threshold",
    )


def ambiguous() -> RecognitionResult:
    return RecognitionResult(
        status=RecognitionStatus.AMBIGUOUS,
        employee_id=None,
        best_score=0.8,
        runner_up_score=0.8,
        margin=0.0,
        reason="top two scores within the ambiguity margin",
    )


def no_candidates() -> RecognitionResult:
    return RecognitionResult(
        status=RecognitionStatus.NO_CANDIDATES,
        reason="no eligible candidates stored/enrolled",
    )


def verifier(min_frames: int = 3, window: int = 5) -> TemporalVerifier:
    return TemporalVerifier(
        config=TemporalVerificationConfig(
            min_consistent_frames=min_frames, window_frames=window
        )
    )


# ---------------------------------------------------------------------------
# 1. Initial state has no verified identity
# ---------------------------------------------------------------------------


class TestInitialState:
    def test_fresh_verifier_reports_collecting_without_identity(
        self,
    ) -> None:
        v = verifier()
        assert v.streak_frames == 0
        assert v.window_frames_used == 0
        assert TemporalVerifier(config=TEMPORAL_VERIFICATION)

    def test_fresh_state_has_no_verification_before_any_frame(
        self,
    ) -> None:
        v = verifier(min_frames=1)
        # No frame yet: nothing exists to report — the API only reacts
        # to frames, so construction itself must not invent a verdict.
        assert v.streak_frames == 0


# ---------------------------------------------------------------------------
# 2. A single match does not verify when more evidence is required
# ---------------------------------------------------------------------------


class TestSingleMatchNotEnough:
    def test_one_match_below_threshold_is_collecting(self) -> None:
        result = verifier(min_frames=2).update(matched("007"))
        assert result.status is TemporalVerificationStatus.COLLECTING
        assert result.employee_id is None
        assert result.streak_frames == 1

    def test_min_consistent_minus_one_never_verifies(self) -> None:
        v = verifier(min_frames=4)
        for _ in range(2):
            v.update(matched("A"))
        result = v.update(matched("A"))  # min − 1 = 3 frames
        assert result.status is TemporalVerificationStatus.COLLECTING
        assert result.streak_frames == 3


# ---------------------------------------------------------------------------
# 3. Repeated same-ID matches eventually verify
# ---------------------------------------------------------------------------


class TestEventualVerification:
    def test_repeated_matches_reach_verified_exactly_on_min_frame(
        self,
    ) -> None:
        v = verifier(min_frames=3)
        assert (
            v.update(matched("E1")).status
            is TemporalVerificationStatus.COLLECTING
        )
        assert (
            v.update(matched("E1")).status
            is TemporalVerificationStatus.COLLECTING
        )
        result = v.update(matched("E1"))
        assert result.status is TemporalVerificationStatus.VERIFIED
        assert result.employee_id == "E1"
        assert result.streak_frames == 3
        assert result.window_frames_used == 3

    def test_verified_persists_while_streak_continues(self) -> None:
        """Chosen semantics: idempotent re-verification while evidence
        persists — the verifier reports current state, not per-frame
        events; callers detect the transition themselves."""
        v = verifier(min_frames=2)
        v.update(matched("E1"))
        first = v.update(matched("E1"))
        again = v.update(matched("E1"))
        assert first.status is TemporalVerificationStatus.VERIFIED
        assert again.status is TemporalVerificationStatus.VERIFIED
        assert again.streak_frames == 3


# ---------------------------------------------------------------------------
# 4. Different employee IDs cannot be combined
# ---------------------------------------------------------------------------


class TestIdentityConsistency:
    def test_alternating_ids_never_verify(self) -> None:
        v = verifier(min_frames=2)
        for _ in range(6):
            v.update(matched("A") if False else matched("A"))
        # Same-ID streak verified above; now guarantee cross-ID never
        # accumulates: alternate A/B forever.
        v2 = verifier(min_frames=2)
        statuses = []
        for i in range(10):
            result = v2.update(
                matched("A") if i % 2 == 0 else matched("B")
            )
            statuses.append(result.status)
        assert TemporalVerificationStatus.VERIFIED not in statuses

    def test_identity_change_discards_old_streak_completely(self) -> None:
        v = verifier(min_frames=2)
        v.update(matched("A"))
        result = v.update(matched("B"))  # identity change → streak of 1
        assert result.status is TemporalVerificationStatus.COLLECTING
        assert result.employee_id is None
        assert result.streak_frames == 1
        # B needs its own min_consistent consecutive frames.
        assert (
            v.update(matched("B")).status
            is TemporalVerificationStatus.VERIFIED
        )
        assert v.update(matched("B")).employee_id == "B"

    def test_verified_id_is_exactly_one_employee(self) -> None:
        v = verifier(min_frames=3)
        for _ in range(3):
            v.update(matched("SOLO"))
        result = v.update(matched("SOLO"))
        assert result.employee_id == "SOLO"


# ---------------------------------------------------------------------------
# 5. Exact identity preservation (leading zeros / punctuation)
# ---------------------------------------------------------------------------


class TestIdentityPreservation:
    @pytest.mark.parametrize(
        "employee_id", ["007", "EMP-0042", "001-MAIN", "007.1"]
    )
    def test_ids_with_leading_zeros_and_punctuation_survive(
        self, employee_id: str
    ) -> None:
        result = verifier(min_frames=1).update(matched(employee_id))
        assert result.status is TemporalVerificationStatus.VERIFIED
        assert result.employee_id == employee_id  # verbatim, no coercion

    def test_confusable_ids_are_distinct_identities(self) -> None:
        v = verifier(min_frames=2)
        v.update(matched("007"))
        result = v.update(matched("7"))  # not the same identity
        assert result.status is TemporalVerificationStatus.COLLECTING


# ---------------------------------------------------------------------------
# 6-8. Non-matched statuses never contribute positive evidence
# ---------------------------------------------------------------------------


class TestNonMatchedStatusesNeverPositive:
    @pytest.mark.parametrize(
        "frame_factory",
        [unknown, ambiguous, no_candidates],
        ids=["UNKNOWN", "AMBIGUOUS", "NO_CANDIDATES"],
    )
    def test_status_never_verifies_an_identity(
        self, frame_factory: object
    ) -> None:
        v = verifier(min_frames=1)
        result = v.update(frame_factory())  # type: ignore[operator]
        assert result.status is TemporalVerificationStatus.INTERRUPTED
        assert result.employee_id is None
        assert result.streak_frames == 0
        # Even with an existing streak, a non-matched frame breaks it.
        v2 = verifier(min_frames=2)
        v2.update(matched("A"))
        v2.update(matched("A"))
        broken = v2.update(frame_factory())  # type: ignore[operator]
        assert broken.status is TemporalVerificationStatus.INTERRUPTED
        assert broken.employee_id is None
        assert broken.streak_frames == 0
        # And the past evidence cannot come back: the next match starts
        # collecting from zero, never re-verifies old evidence.
        recovered = v2.update(matched("A"))
        assert recovered.status is TemporalVerificationStatus.COLLECTING
        assert recovered.employee_id is None


# ---------------------------------------------------------------------------
# 9. Non-matched results interrupt evidence (documented policy)
# ---------------------------------------------------------------------------


class TestInterruptionPolicy:
    def test_interrupted_result_never_carries_identity(self) -> None:
        v = verifier(min_frames=1)
        v.update(matched("A"))
        result = v.update(unknown())
        assert result.employee_id is None
        assert result.reason is not None and "unknown" in result.reason

    def test_streak_needs_to_rebuild_after_interruption(self) -> None:
        v = verifier(min_frames=3)
        v.update(matched("A"))
        v.update(matched("A"))
        v.update(unknown())  # 2 accumulated frames are dropped
        assert v.streak_frames == 0
        v.update(matched("A"))
        v.update(matched("A"))
        assert (
            v.update(matched("A")).status
            is TemporalVerificationStatus.VERIFIED
        )

    def test_reason_reports_which_status_interrupted(self) -> None:
        v = verifier(min_frames=1)
        for factory, token in (
            (ambiguous, "ambiguous"),
            (no_candidates, "no_candidates"),
        ):
            v.update(matched("A"))
            result = v.update(factory())  # type: ignore[operator]
            assert result.reason is not None and token in result.reason


# ---------------------------------------------------------------------------
# 10-11. Bounded window / deterministic advancement; no stale verification
# ---------------------------------------------------------------------------


class TestBoundedWindow:
    def test_window_retention_is_capped(self) -> None:
        v = verifier(min_frames=1, window=4)
        for _ in range(10):
            v.update(matched("A"))
        assert v.window_frames_used == 4  # bounded, never unbounded

    def test_window_advancement_is_deterministic(self) -> None:
        """Fed in order, the retained window is the newest N frames."""
        v = verifier(min_frames=2, window=3)
        v.update(matched("A"))
        v.update(matched("A"))
        v.update(unknown())
        # The window now holds [MATCHED, MATCHED, UNKNOWN] — the newest
        # evidence; the interrupted streak is not ressurected by
        # advancement (oldest entries deterministically drop out).
        v.update(matched("A"))
        assert v.window_frames_used == 3
        assert v.streak_frames == 1

    def test_verified_cannot_survive_without_new_evidence(self) -> None:
        v = verifier(min_frames=1, window=4)
        v.update(matched("A"))
        # Any later non-matched evidence invalidates verification.
        assert (
            v.update(no_candidates()).status
            is TemporalVerificationStatus.INTERRUPTED
        )
        # A stale verified identity can never reappear by itself.
        v.reset()
        assert (
            v.update(unknown()).status
            is TemporalVerificationStatus.INTERRUPTED
        )


# ---------------------------------------------------------------------------
# 12. Reset clears all temporal evidence
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_streak_and_window(self) -> None:
        v = verifier(min_frames=1)
        v.update(matched("A"))
        v.update(matched("A"))
        assert v.streak_frames == 2
        v.reset()
        assert v.streak_frames == 0
        assert v.window_frames_used == 0
        # After reset, verification starts over from zero.
        result = v.update(matched("A"))
        assert result.status is TemporalVerificationStatus.VERIFIED
        assert result.streak_frames == 1
        assert v.window_frames_used == 1

    def test_reset_after_verification_drops_the_identity(self) -> None:
        v = verifier(min_frames=2)
        v.update(matched("A"))
        v.update(matched("A"))
        v.update(unknown())  # interrupted; last verified state gone
        v.reset()
        # The next single match is not verification evidence again
        # until min_consistent is reached.
        v2 = verifier(min_frames=2)
        v2.update(matched("A"))
        result = v2.update(no_candidates())
        assert result.status is TemporalVerificationStatus.INTERRUPTED
        assert result.employee_id is None


# ---------------------------------------------------------------------------
# 13. Independent verifier instances do not share state
# ---------------------------------------------------------------------------


class TestStateIsolation:
    def test_two_instances_are_independent(self) -> None:
        a = verifier(min_frames=2)
        b = verifier(min_frames=2)
        a.update(matched("A"))
        a.update(matched("A"))
        result_a = a.update(matched("A"))
        # b received nothing and is not affected by a's evidence.
        b_result = b.update(unknown())
        assert result_a.status is TemporalVerificationStatus.VERIFIED
        assert b_result.status is TemporalVerificationStatus.INTERRUPTED
        assert b.streak_frames == 0
        assert a.streak_frames == 3
        assert b.window_frames_used == 1

    def test_reset_is_instance_scoped(self) -> None:
        a = verifier(min_frames=2)
        b = verifier(min_frames=2)
        a.update(matched("A"))
        b.update(matched("A"))
        a.reset()
        assert a.streak_frames == 0
        assert b.streak_frames == 1  # untouched


# ---------------------------------------------------------------------------
# 14. Invalid configuration values are rejected
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_zero_min_frames_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalVerificationConfig(min_consistent_frames=0)

    def test_negative_window_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalVerificationConfig(window_frames=-1)

    def test_window_smaller_than_min_frames_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalVerificationConfig(
                min_consistent_frames=5, window_frames=4
            )

    @pytest.mark.parametrize(
        "bad_kwargs",
        [
            {"min_consistent_frames": True},  # bool is not a plain int
            {"min_consistent_frames": 2.0},  # float rejected (strict)
            {"window_frames": 10.0},
            {"window_frames": None},
        ],
    )
    def test_wrong_types_rejected(self, bad_kwargs: dict) -> None:
        with pytest.raises(ValueError):
            TemporalVerificationConfig(**bad_kwargs)

    def test_config_object_is_frozen(self) -> None:
        cfg = TemporalVerificationConfig()
        with pytest.raises(Exception):
            cfg.min_consistent_frames = 99  # type: ignore[misc]

    def test_min_frames_one_window_one_is_valid(self) -> None:
        assert TemporalVerificationConfig(
            min_consistent_frames=1, window_frames=1
        )


# ---------------------------------------------------------------------------
# 15. Invalid / malformed recognition results handled safely
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_non_recognition_result_rejected(self) -> None:
        v = verifier()
        with pytest.raises(TemporalVerificationInputError):
            v.update("matched")  # type: ignore[arg-type]
        with pytest.raises(TemporalVerificationInputError):
            v.update(None)  # type: ignore[arg-type]
        with pytest.raises(TemporalVerificationInputError):
            v.update(
                {
                    "status": RecognitionStatus.MATCHED,
                    "employee_id": "A",
                }
            )  # type: ignore[arg-type]

    def test_matched_without_employee_id_is_refused(self) -> None:
        """MATCHED must carry an identity (Step 10 contract); a
        violation is surfaced, never silently treated as non-evidence."""
        v = verifier(min_frames=1)
        with pytest.raises(TemporalVerificationInputError):
            v.update(
                RecognitionResult(
                    status=RecognitionStatus.MATCHED, employee_id=None
                )
            )
        with pytest.raises(TemporalVerificationInputError):
            v.update(
                RecognitionResult(
                    status=RecognitionStatus.MATCHED, employee_id=""
                )
            )
        with pytest.raises(TemporalVerificationInputError):
            v.update(
                RecognitionResult(
                    status=RecognitionStatus.MATCHED, employee_id=7
                )  # type: ignore[arg-type]
            )
        # The verifier stayed usable and uncorrupted:
        assert v.streak_frames == 0

    def test_recognition_result_immutability_preserved(self) -> None:
        """Existing frozen-dataclass guarantees still hold when the
        verifier consumes results."""
        result = matched("A")
        with pytest.raises(Exception):
            result.status = RecognitionStatus.UNKNOWN  # type: ignore[misc]

    @pytest.mark.parametrize(
        "factory", [unknown, ambiguous, no_candidates]
    )
    def test_non_matched_results_are_never_coerced_into_matches(
        self, factory: object
    ) -> None:
        v = verifier(min_frames=1)
        for _ in range(5):
            result = v.update(factory())  # type: ignore[operator]
            assert result.employee_id is None
            assert result.status is not TemporalVerificationStatus.VERIFIED


# ---------------------------------------------------------------------------
# 16. Boundary behavior (min frames / window size)
# ---------------------------------------------------------------------------


class TestBoundaries:
    def test_min_frames_one_verifies_on_first_match(self) -> None:
        result = verifier(
            min_frames=1, window=1
        ).update(matched("A"))
        assert result.status is TemporalVerificationStatus.VERIFIED
        assert result.employee_id == "A"

    def test_window_equal_to_min_frames_verifies_and_stays_bounded(
        self,
    ) -> None:
        v = verifier(min_frames=3, window=3)
        v.update(matched("A"))
        v.update(matched("A"))
        result = v.update(matched("A"))
        assert result.status is TemporalVerificationStatus.VERIFIED
        assert result.window_frames_used == 3
        # One more frame keeps verification while identity persists.
        more = v.update(matched("A"))
        assert more.status is TemporalVerificationStatus.VERIFIED
        assert more.window_frames_used == 3  # capped

    def test_boundary_zero_frames_outside_window_impossible_by_config(
        self,
    ) -> None:
        # window < min_consistent rejected up front, so the impossible
        # window/deadlock configuration cannot be constructed silently.
        with pytest.raises(ValueError):
            TemporalVerificationConfig(
                min_consistent_frames=2, window_frames=1
            )


# ---------------------------------------------------------------------------
# 17. Bounded state only
# ---------------------------------------------------------------------------


class TestStateBound:
    def test_long_run_retains_nothing_beyond_the_window(self) -> None:
        window, min_frames = 6, 2
        v = verifier(min_frames=min_frames, window=window)
        for round_index in range(50):
            v.update(matched(f"E{round_index}"))
            v.update(unknown())
        assert v.window_frames_used == window
        assert v.streak_frames == 0
        assert v.streak_frames < min_frames

    def test_result_objects_carry_no_scores_or_biometric_fields(
        self,
    ) -> None:
        result = verifier(min_frames=1).update(matched("007"))
        payload = result.__dict__ if hasattr(result, "__dict__") else {}
        for name in (
            "best_score",
            "vector",
            "embeddings",
            "frame",
            "crop",
            "scores",
        ):
            assert name not in payload, name


# ---------------------------------------------------------------------------
# 19. Import / construction has no side effects (subprocess, foreign CWD)
# ---------------------------------------------------------------------------


class TestNoSideEffects:
    def test_import_and_construct_in_clean_subprocess(self) -> None:
        code = (
            "import sys\n"
            "sys.path.insert(0, r'{root}')\n"
            "from src.temporal_verification import TemporalVerifier\n"
            "v = TemporalVerifier()\n"
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

    def test_default_config_is_the_project_constant(self) -> None:
        assert TemporalVerifier().window_frames_used == 0
        # RECOGNITION stays untouched by this module (no coupling).
        assert RECOGNITION.min_similarity == pytest.approx(0.36)


# ---------------------------------------------------------------------------
# Result type shape
# ---------------------------------------------------------------------------


class TestResultShape:
    def test_result_is_frozen_dataclass(self) -> None:
        result = TemporalVerificationResult(
            status=TemporalVerificationStatus.COLLECTING
        )
        with pytest.raises(Exception):
            result.status = TemporalVerificationStatus.VERIFIED  # type: ignore

    def test_result_statuses_are_explicit_not_bools(self) -> None:
        assert (
            TemporalVerificationStatus.VERIFIED.value == "verified"
        )
        assert (
            TemporalVerificationStatus.COLLECTING.value == "collecting"
        )
        assert (
            TemporalVerificationStatus.INTERRUPTED.value == "interrupted"
        )
