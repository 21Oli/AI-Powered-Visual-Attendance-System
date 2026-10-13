"""Temporal (multi-frame) verification for the AI-Powered Visual
Attendance System (Phase 1 — Step 11).

Combines successive :class:`~src.face_recognition.RecognitionResult`
values from the existing recognition service (Step 10) into an explicit
temporal decision: an identity is only reported as *temporally
verified* after a configurable number of **consecutive, consistent**
``MATCHED`` frames for that **same** employee ID.

This module:

* builds on the existing recognition-result contract (it imports and
  requires ``RecognitionResult`` instances — it never duplicates cosine
  similarity, embedding loading, manifest validation, or thresholds);
* does **not** replace single-frame recognition, add face tracking,
  cross-camera tracking, persistence, attendance events, CSV/database
  writes, frames, face crops, embeddings, names, or any personal data
  beyond the exact matched employee ID string and counters;
* performs **no** camera work, model loading, biometric-file reads, or
  network access at import or construction time (mirrors every previous
  component's lifecycle convention).

Temporal policy (documented, conservative, deterministic)
---------------------------------------------------------

* Evidence unit: one ``RecognitionResult`` at a time, fed in temporal
  order by the caller (successive frames of one tracked face; the
  caller maintains one :class:`TemporalVerifier` instance per
  independent tracking session — no state is shared between instances).
* Positive evidence: **only** ``RecognitionStatus.MATCHED`` frames with
  a matched ``employee_id``. ``UNKNOWN``, ``AMBIGUOUS``, and
  ``NO_CANDIDATES`` are never positive evidence, exactly like their
  single-frame semantics.
* Interruption (documented conservative rule): an instant of
  uncertainty may not contribute to a verified identity, so **any**
  non-MATCHED frame **breaks the streak** — the accumulated evidence
  for the previous candidate identity is interrupted (status
  ``INTERRUPTED``, no employee_id reported, evidence recovery starts
  from zero matches). A ``MATCHED`` frame for a **different** employee
  ID likewise breaks the streak and starts a new candidate streak for
  the new ID (cross-ID evidence is never combined).
* Batch-expiry vs. streak: the configured window
  (``window_frames``) bounds *retained* evidence (a bounded deque used
  for deterministic reporting counters; never unbounded history). A
  continuing streak is always the *newest* frames, so window
  advancement drops only older evidence, deterministically
  (oldest-first, one entry per frame).
* Verification: ``VERIFIED(employee_id)`` as soon as
  ``streak_frames >= min_consistent_frames`` for one uninterrupted
  same-ID streak. Semantics of repeated processing after verification
  are **idempotent**: additional consecutive same-ID frames keep the
  status ``VERIFIED`` (no separate per-frame "verification events" are
  emitted — the result reports current temporal state; callers that
  need event-on-transition semantics must track transitions
  themselves). New evidence is always required to keep verification:
  the very next non-MATCHED frame drops the status back to
  ``INTERRUPTED``.
* An interrupted or identity-changed sequence can never report the
  *previous* evidence as a newly verified identity: ``INTERRUPTED``
  carries ``employee_id=None`` and a zero streak.
* Identities are preserved exactly (leading zeros, punctuation —
  ``"007"``, ``"EMP-0042"``): the matched ID string is stored and
  reported verbatim, with no normalization or coercion.
* Reset: :meth:`TemporalVerifier.reset` clears **all** temporal state
  (streak and window) when the tracked face disappears, the camera
  session changes, or the caller wants a fresh verification window.

Defaults are initial engineering starting points (not calibrated
production values); see ``TemporalVerificationConfig`` and the
Phase 2 calibration note in ARCHITECTURE.md D5.

Logging privacy: log lines carry counters, statuses, and reasons
only — never employee IDs, never biometric data.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum

from src.config import TEMPORAL_VERIFICATION, TemporalVerificationConfig
from src.face_recognition import RecognitionResult, RecognitionStatus
from src.logging_setup import get_logger

__all__ = [
    "TemporalVerificationError",
    "TemporalVerificationInputError",
    "TemporalVerificationStatus",
    "TemporalVerificationResult",
    "TemporalVerifier",
]

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TemporalVerificationError(Exception):
    """Base class for temporal-verification errors."""


class TemporalVerificationInputError(
    TemporalVerificationError, ValueError
):
    """Raised for invalid verifer configuration or invalid input results.

    Includes ``MATCHED`` results whose ``employee_id`` is not a
    non-empty string — Step 10's contract guarantees a matched result
    carries an identity, and silently accepting a violation would risk
    counting an identity-less frame as positive evidence.
    """


# ---------------------------------------------------------------------------
# Result types (frozen dataclasses; no biometric data ever carried)
# ---------------------------------------------------------------------------


class TemporalVerificationStatus(Enum):
    """Explicit temporal outcome — never surfaced as a bare bool.

    ``COLLECTING``  — same-ID matched streak is building but has not
        reached ``min_consistent_frames`` yet.
    ``VERIFIED``    — ``min_consistent_frames`` consecutive ``MATCHED``
        frames for the **same** employee ID; the result identifies
        exactly one employee (the streak's ID).
    ``INTERRUPTED`` — the latest frame was non-MATCHED (``UNKNOWN`` /
        ``AMBIGUOUS`` / ``NO_CANDIDATES``) or the matched ID changed:
        prior evidence is invalidated, no identity is reported.
    """

    COLLECTING = "collecting"
    VERIFIED = "verified"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class TemporalVerificationResult:
    """Outcome of feeding one recognition frame to the verifier.

    Attributes:
        status: COLLECTING / VERIFIED / INTERRUPTED (see enum).
        employee_id: Exact matched employee ID **only** on ``VERIFIED``
            and only for the streak's own single identity; ``None`` for
            ``COLLECTING`` and ``INTERRUPTED`` (uncertain or interrupted
            evidence never implies an identity).
        streak_frames: Length of the current uninterrupted same-ID
            matched streak (``0`` after an interruption).
        window_frames_used: Number of evidence frames currently
            retained in the bounded window (never exceeds the
            configured ``window_frames``).
        reason: Short diagnostic code (never biometric detail, never
            an employee ID).
    """

    status: TemporalVerificationStatus
    employee_id: str | None = None
    streak_frames: int = 0
    window_frames_used: int = 0
    reason: str | None = None


# ---------------------------------------------------------------------------
# Verifier (one instance per tracked face / camera session)
# ---------------------------------------------------------------------------


class TemporalVerifier:
    """Combine successive recognition results into a temporal decision.

    Construction performs no side effects at all (no camera, no model,
    no files, no network). The verifier manages **one** tracking
    sequence at a time: callers must create a separate instance for
    each independent tracked face or camera session — instances share
    no state (:meth:`reset` clears only this verifier).

    Evidence state retained is minimal and bounded: the current streak
    (candidate ID + counter), the last interruption reason, and a
    bounded deque of recent evidence statuses (maxlen =
    ``window_frames``) used only for deterministic window accounting.
    No frames, crops, embeddings, scores, or names are retained.

    Args:
        config: ``TemporalVerificationConfig`` (validated at
            construction; defaults to the project-wide frozen
            constant).

    Raises:
        TemporalVerificationInputError: config is not a
            ``TemporalVerificationConfig``.
    """

    def __init__(
        self,
        *,
        config: TemporalVerificationConfig = TEMPORAL_VERIFICATION,
    ) -> None:
        if not isinstance(config, TemporalVerificationConfig):
            raise TemporalVerificationInputError(
                "config must be a TemporalVerificationConfig, got "
                f"{type(config).__name__}"
            )
        self._config = config
        self._min_consistent = config.min_consistent_frames
        # Bounded evidence window: never exceeds window_frames entries.
        self._window: deque[RecognitionStatus] = deque(
            maxlen=config.window_frames
        )
        self._streak_id: str | None = None
        self._streak_frames = 0
        self._last_reason: str | None = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def update(self, result: RecognitionResult) -> TemporalVerificationResult:
        """Consume one recognition result and report the temporal state.

        Args:
            result: A :class:`~src.face_recognition.RecognitionResult`
                from the Step 10 recognition service, fed in temporal
                frame order (the caller's responsibility — this module
                invents no frame sequence numbers; out-of-order feeding
                is simply the evidence in the order supplied).

        Returns:
            A frozen :class:`TemporalVerificationResult`.

        Raises:
            TemporalVerificationInputError: ``result`` is not a
                ``RecognitionResult``, or a ``MATCHED`` result carries
                no non-empty string ``employee_id`` (contract
                violation — never silently treated as non-evidence).
        """
        if not isinstance(result, RecognitionResult):
            raise TemporalVerificationInputError(
                "result must be a RecognitionResult, got "
                f"{type(result).__name__}"
            )

        if result.status is RecognitionStatus.MATCHED:
            matched_id = result.employee_id
            if not isinstance(matched_id, str) or not matched_id:
                raise TemporalVerificationInputError(
                    "MATCHED recognition result carries "
                    f"{'no' if matched_id is None else 'an empty/invalid'} "
                    "employee_id; the Step 10 contract guarantees an "
                    "identity on MATCHED — refusing to count this frame"
                )
            if matched_id == self._streak_id:
                # Continuing the same identity's streak (or re-verifying
                # it — idempotent while the evidence persists).
                self._streak_frames += 1
            else:
                # Identity change: the old streak is discarded; the new
                # ID starts its own streak from zero matches (cross-ID
                # evidence is never combined).
                _logger.debug(
                    "temporal streak switched identity (streak was %d "
                    "frames; old streak excluded from decision)",
                    self._streak_frames,
                )
                self._streak_id = matched_id
                self._streak_frames = 1
            self._append_window(result.status)

            if self._streak_frames >= self._min_consistent:
                self._last_reason = "consistent matched streak long enough"
                return TemporalVerificationResult(
                    status=TemporalVerificationStatus.VERIFIED,
                    employee_id=self._streak_id,
                    streak_frames=self._streak_frames,
                    window_frames_used=len(self._window),
                    reason=self._last_reason,
                )
            self._last_reason = "collecting consistent matched frames"
            return TemporalVerificationResult(
                status=TemporalVerificationStatus.COLLECTING,
                employee_id=None,
                streak_frames=self._streak_frames,
                window_frames_used=len(self._window),
                reason=self._last_reason,
            )

        # -- Non-matched statuses: never positive evidence --------------
        # (UNKNOWN / AMBIGUOUS / NO_CANDIDATES) — conservative rule: the
        # uncertain instant interrupts the accumulated streak so that no
        # identity can be verified across an uncertainty gap.
        self._append_window(result.status)
        _logger.debug(
            "temporal streak interrupted by %s frame (dropped %d "
            "accumulated frames)",
            result.status.value,
            self._streak_frames,
        )
        self._streak_id = None
        self._streak_frames = 0
        self._last_reason = f"streak interrupted by {result.status.value} frame"
        return TemporalVerificationResult(
            status=TemporalVerificationStatus.INTERRUPTED,
            employee_id=None,
            streak_frames=0,
            window_frames_used=len(self._window),
            reason=self._last_reason,
        )

    def reset(self) -> None:
        """Clear ALL temporal evidence for this verifier (streak + window).

        Called when the tracked face disappears, the camera session
        changes, or a fresh verification window is wanted. Affects only
        this verifier instance.
        """
        self._window.clear()
        self._streak_id = None
        self._streak_frames = 0
        self._last_reason = None
        _logger.debug(
            "temporal verifier state reset (window capacity %d)",
            self._config.window_frames,
        )

    @property
    def window_frames_used(self) -> int:
        """Evidence frames currently retained (never exceeds the cap)."""
        return len(self._window)

    @property
    def streak_frames(self) -> int:
        """Current uninterrupted same-ID matched streak length."""
        return self._streak_frames

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append_window(self, status: RecognitionStatus) -> None:
        """Record one bounded evidence entry (defaultdict-free; maxlen)."""
        self._window.append(status)
