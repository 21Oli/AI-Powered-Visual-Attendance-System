"""Unknown-face handling for the AI-Powered Visual Attendance System
(Phase 1 — Step 12).

Makes **conservative, explicit, in-memory** decisions about how an
unidentified or uncertain face should be handled, building strictly on
the approved Step 10 (recognition) and Step 11 (temporal verification)
contracts. This module:

* never attempts to identify an unknown person beyond the existing
  recognition service — no speculative identity, no names, no
  similarity/inference of any kind;
* never records attendance, generates attendance events, writes
  database/CSV/file data, persists crops, images, or embeddings, adds
  tracking, cross-camera matching, notifications, or network calls;
* never bypasses :class:`~src.temporal_verification.TemporalVerifier`
  — the only path that treats an identity as *temporally verified* is
  a temporal result whose ``status`` is exactly ``VERIFIED``;
* retains **no biometric data**: outcomes carry statuses, small
  counters, and short reason strings only — never employee IDs,
  scores, embeddings, frames, or crops — and the retained per-session
  state is a tiny episode record (bool-like flags and a frame count).

Trust boundary (documented)
---------------------------

Two entry points exist, deliberately distinct:

* :meth:`UnknownFaceHandler.observe_temporal` — the **authoritative**
  path. A ``TemporalVerificationResult`` with status ``VERIFIED`` (and
  a valid matched ID per the Step 11 contract) is the *only* signal
  that resolves an unresolved episode as "a known, temporally verified
  employee is present" and yields ``NO_ACTION``/``EPISODE_ENDED``.
  ``COLLECTING`` and ``INTERRUPTED`` are handled conservatively as
  **unverified** observations (an unverified face may still turn out
  to be an enrolled employee mid-verification — it may equally be a
  stranger); they never produce or preserve an identity claim.
* :meth:`UnknownFaceHandler.observe_recognition` — the
  **observability** path for raw Step 10 results when the caller wants
  unknown-face handling without a temporal result in the same call. A
  raw ``MATCHED`` result is classified
  ``MATCH_UNVERIFIED`` — a single-frame match is explicitly NOT a
  temporally verified identity and is handled as conservatively as any
  other unresolved face. No entry point of this module can ever turn
  any recognition result into an identity.

Outcome status semantics
------------------------

``NO_ACTION``
    The frame resolved to a temporally verified identity (no
    unknown-face action required) and no unresolved episode needed
    ending.
``OBSERVED``
    A new unresolved episode just became active: this observation is
    the episode's one "alert" (the moment to handle — log count,
    review, etc. — at the caller's discretion).
``SUPPRESSED``
    An unresolved observation arrived while an episode was already
    active within the cooldown: repeated handling is suppressed
    (bounded; the suppressed-frame count is reported).
``EPISODE_ENDED``
    A temporally verified identity resolved an episode that was
    active; nothing unknown remains to handle for that session.

Session, cooldown and expiry policy (deterministic)
----------------------------------------------------

* State is per **caller-managed session key**. The application has no
  stable face-track identifier, so a session key is a caller
  assertion ("this frame came from the same camera/tracked face I
  consider one session"), not proof that repeated frames show the same
  person — suppression is a bounded unresolved-session policy, and
  multiple unknown people within one caller session cannot be
  distinguished (documented limitation; no tracker is added here).
* An unresolved episode opens on the **first** unresolved
  observation in a session (``OBSERVED`` — the single "alert").
  Further unresolved observations within ``episode_cooldown_seconds``
  of the alert instant are ``SUPPRESSED`` (each suppressed frame
  increments the episode's counter) and do **not** refresh the
  cooldown — the episode expires deterministically at
  ``alert_instant + cooldown`` (lazy expiry, checked against the
  injectable clock; a later unresolved observation after expiry opens
  a fresh episode, so stale state can never suppress observations
  indefinitely).
* A ``VERIFIED`` temporal result ends an active episode immediately
  (``EPISODE_ENDED``); while the temporal verifier keeps reporting the
  same verified identity, subsequent verified frames report
  ``NO_ACTION``.
* :meth:`UnknownFaceHandler.reset` clears one session (by key) or
  every session (``session_key=None``) and returns how many active
  unresolved episodes were ended.
* Memory is bounded: at most ``max_sessions`` episodes are retained
  (LRU eviction of the least-recently-seen session beyond the cap;
  worst case after eviction is one extra alert episode). The retained
  record per session is one small value — no unbounded history
  anywhere.

Configured via the frozen :class:`~src.config.UnknownFaceConfig`
(strict validation; see its docstring for the provisional-defaults
honesty note). ``clock`` is an injectable monotonic-seconds callable
(default ``time.monotonic``) so tests never sleep.

Logging privacy: log lines carry outcome statuses and counts only —
never employee IDs, session keys, scores, or any biometric data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Final

from src.config import UNKNOWN_FACE, UnknownFaceConfig
from src.face_recognition import RecognitionResult, RecognitionStatus
from src.logging_setup import get_logger
from src.temporal_verification import (
    TemporalVerificationResult,
    TemporalVerificationStatus,
)

__all__ = [
    "UnknownFaceError",
    "UnknownFaceInputError",
    "UnknownFaceStatus",
    "UnresolvedObservation",
    "UnknownFaceOutcome",
    "UnknownFaceHandler",
]

_logger = get_logger(__name__)

DEFAULT_SESSION_KEY: Final[str] = "default"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnknownFaceError(Exception):
    """Base class for unknown-face-handling errors."""


class UnknownFaceInputError(UnknownFaceError, ValueError):
    """Raised for invalid handler configuration or malformed results.

    Includes results that violate their own Step 10/11 contracts
    (e.g. ``MATCHED``/``VERIFIED`` without a valid identity) — such
    inconsistencies fail safely with a typed error and **never** get
    silently interpreted as unresolved observations or identities.
    """


# ---------------------------------------------------------------------------
# Outcome types (frozen; no biometric data ever carried)
# ---------------------------------------------------------------------------


class UnknownFaceStatus(Enum):
    """Explicit unknown-face handling outcome — never a bare bool.

    ``NO_ACTION``      — temporally verified identity; nothing to handle.
    ``OBSERVED``       — unresolved episode started; this is its one alert.
    ``SUPPRESSED``     — unresolved observation within an active
        episode's cooldown; repeated handling suppressed.
    ``EPISODE_ENDED``  — a temporally verified identity resolved an
        episode that was active; nothing unknown remains.
    """

    NO_ACTION = "no_action"
    OBSERVED = "observed"
    SUPPRESSED = "suppressed"
    EPISODE_ENDED = "episode_ended"


class UnresolvedObservation(Enum):
    """Which of the approved contracts marked the face unresolved.

    Mirrors the input statuses one-to-one so no distinct source is
    collapsed away:

    ``UNKNOWN``           — recognition status ``UNKNOWN``.
    ``AMBIGUOUS``         — recognition status ``AMBIGUOUS``.
    ``NO_CANDIDATES``     — recognition status ``NO_CANDIDATES``.
    ``MATCH_UNVERIFIED``  — raw recognition ``MATCHED`` without
        temporal verification (observability path only; never an
        identity).
    ``TEMPORAL_UNVERIFIED`` — temporal ``COLLECTING`` or
        ``INTERRUPTED`` (possibly an enrolled employee mid-verification
        — possibly a stranger; conservatively unresolved either way).
    """

    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"
    NO_CANDIDATES = "no_candidates"
    MATCH_UNVERIFIED = "match_unverified"
    TEMPORAL_UNVERIFIED = "temporal_unverified"


@dataclass(frozen=True)
class UnknownFaceOutcome:
    """Outcome of feeding one result to the unknown-face handler.

    Attributes:
        status: NO_ACTION / OBSERVED / SUPPRESSED / EPISODE_ENDED.
        observation: :class:`UnresolvedObservation` when the frame was
            unresolved (``OBSERVED``/``SUPPRESSED``), ``None`` when the
            frame resolved to a verified identity or nothing remains.
        suppressed_frames: Number of frames suppressed within the
            currently active episode (includes this frame when the
            status is ``SUPPRESSED``; ``0`` otherwise).
        reason: Short diagnostic string (never biometric detail, never
            an identity).
    """

    status: UnknownFaceStatus
    observation: UnresolvedObservation | None = None
    suppressed_frames: int = 0
    reason: str | None = None


@dataclass
class _Episode:
    """Bounded per-session unresolved-episode record (private)."""

    observation: UnresolvedObservation
    alert_instant: float
    suppressed_frames: int


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class UnknownFaceHandler:
    """Conservative unknown-face handling over approved contract types.

    Construction performs no side effects (no camera, no model, no
    files, no network). State is in-memory, per caller-managed session
    key, bounded (``max_sessions``, LRU eviction), and private to this
    handler instance — instances share nothing.

    Args:
        config: ``UnknownFaceConfig`` (defaults to the project-wide
            frozen constant).
        clock: Injectable ``() -> float`` monotonic-seconds callable
            (default ``time.monotonic``) so cooldown boundaries are
            deterministic in tests without sleeping.

    Raises:
        UnknownFaceInputError: config is not an ``UnknownFaceConfig``
            or clock is not callable.
    """

    def __init__(
        self,
        *,
        config: UnknownFaceConfig = UNKNOWN_FACE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(config, UnknownFaceConfig):
            raise UnknownFaceInputError(
                "config must be an UnknownFaceConfig, got "
                f"{type(config).__name__}"
            )
        if not callable(clock):
            raise UnknownFaceInputError(
                "clock must be a callable returning monotonic seconds, "
                f"got {type(clock).__name__}"
            )
        self._config = config
        self._clock = clock
        self._cooldown = float(config.episode_cooldown_seconds)
        self._episodes: dict[str, _Episode] = {}

    # ------------------------------------------------------------------
    # Authoritative path (temporal-verification trust boundary)
    # ------------------------------------------------------------------

    def observe_temporal(
        self,
        result: TemporalVerificationResult,
        *,
        session_key: str = DEFAULT_SESSION_KEY,
    ) -> UnknownFaceOutcome:
        """Consume one Step 11 temporal result (authoritative path).

        ``VERIFIED`` is the only temporal status treated as a temporally
        verified identity: it yields ``NO_ACTION`` (or
        ``EPISODE_ENDED`` when an episode was active) and the outcome
        carries **no** identity — the handler exists for unknown-face
        handling, so verified frames need nothing more here.
        ``COLLECTING``/``INTERRUPTED`` are conservatively unresolved
        observations and follow the episode/cooldown policy.

        Raises:
            UnknownFaceInputError: ``result`` is not a temporal result,
                its status is not a real ``TemporalVerificationStatus``,
                a ``VERIFIED`` result carries no valid identity
                (contract violation), or the session key is invalid.
        """
        if not isinstance(result, TemporalVerificationResult):
            raise UnknownFaceInputError(
                "result must be a TemporalVerificationResult, got "
                f"{type(result).__name__}"
            )
        if not isinstance(result.status, TemporalVerificationStatus):
            # Step 11's dataclass does not type-check the status field;
            # a synthetic malformed status must fail safely.
            raise UnknownFaceInputError(
                "temporal result status is not a TemporalVerificationStatus, "
                f"got {type(result.status).__name__}"
            )

        session = self._validated_key(session_key)

        if result.status is TemporalVerificationStatus.VERIFIED:
            matched_id = result.employee_id
            if not isinstance(matched_id, str) or not matched_id:
                raise UnknownFaceInputError(
                    "VERIFIED temporal result carries an invalid "
                    "employee_id; the Step 11 contract guarantees an "
                    "identity on VERIFIED — failing safely instead of "
                    "accepting an unanchored verification"
                )
            episode = self._episodes.get(session)
            if episode is None:
                _logger.debug(
                    "verified identity present; no unknown-face action "
                    "(suppressed %d frames over the episode)",
                    episode.suppressed_frames if episode else 0,
                )
                return UnknownFaceOutcome(
                    status=UnknownFaceStatus.NO_ACTION,
                    observation=None,
                    suppressed_frames=0,
                    reason="temporally verified identity; no unknown-face "
                    "handling required",
                )
            # A known, verified identity resolves the unresolved episode.
            suppressed = episode.suppressed_frames
            del self._episodes[session]
            _logger.debug(
                "verified identity ended the unresolved episode "
                "(%d frames had been suppressed)",
                suppressed,
            )
            return UnknownFaceOutcome(
                status=UnknownFaceStatus.EPISODE_ENDED,
                observation=None,
                suppressed_frames=suppressed,
                reason="temporally verified identity resolved the "
                "unresolved episode",
            )

        # COLLECTING / INTERRUPTED: conservative unresolved observation
        # (may be an enrolled employee mid-verification — may be a
        # stranger; the handler never guesses which).
        return self._observe_unresolved(
            UnresolvedObservation.TEMPORAL_UNVERIFIED,
            session=session,
            reason=(
                f"temporal status {result.status.value}; not a verified "
                "identity"
            ),
        )

    # ------------------------------------------------------------------
    # Observability path (raw Step 10 results; never an identity)
    # ------------------------------------------------------------------

    def observe_recognition(
        self,
        result: RecognitionResult,
        *,
        session_key: str = DEFAULT_SESSION_KEY,
    ) -> UnknownFaceOutcome:
        """Consume one raw Step 10 recognition result (observability).

        Classification is one-to-one with the recognition statuses:
        ``UNKNOWN`` / ``AMBIGUOUS`` / ``NO_CANDIDATES`` map to their
        own :class:`UnresolvedObservation` values; a raw ``MATCHED``
        is classified ``MATCH_UNVERIFIED`` — a single-frame match is
        explicitly **not** a temporally verified identity and is
        handled as conservatively as every other unresolved face.

        Raises:
            UnknownFaceInputError: ``result`` is not a recognition
                result, its status is not a real ``RecognitionStatus``,
                a ``MATCHED`` result carries no valid identity
                (contract violation), or the session key is invalid.
        """
        if not isinstance(result, RecognitionResult):
            raise UnknownFaceInputError(
                "result must be a RecognitionResult, got "
                f"{type(result).__name__}"
            )
        if not isinstance(result.status, RecognitionStatus):
            # Step 10's frozen dataclass does not type-check the status
            # field; synthetic malformed statuses fail safely.
            raise UnknownFaceInputError(
                "recognition result status is not a RecognitionStatus, "
                f"got {type(result.status).__name__}"
            )

        session = self._validated_key(session_key)

        if result.status is RecognitionStatus.MATCHED:
            matched_id = result.employee_id
            if not isinstance(matched_id, str) or not matched_id:
                raise UnknownFaceInputError(
                    "MATCHED recognition result carries an invalid "
                    "employee_id; the Step 10 contract guarantees an "
                    "identity on MATCHED — failing safely"
                )
            return self._observe_unresolved(
                UnresolvedObservation.MATCH_UNVERIFIED,
                session=session,
                reason=(
                    "raw MATCHED result; not temporally verified — "
                    "single-frame matches are never identities here"
                ),
            )

        return self._observe_unresolved(
            UnresolvedObservation(result.status.value),
            session=session,
            reason=f"recognition status {result.status.value}",
        )

    # ------------------------------------------------------------------
    # Reset & introspection
    # ------------------------------------------------------------------

    def reset(self, session_key: str | None = None) -> int:
        """End unresolved episode state explicitly.

        Args:
            session_key: Reset a single session's episode, or ``None``
                (documented) to clear **every** session.

        Returns:
            The number of active unresolved episodes that were ended
            (0 when nothing was active).

        Raises:
            UnknownFaceInputError: a session key is provided but
                invalid.
        """
        if session_key is None:
            ended = len(self._episodes)
            self._episodes.clear()
            _logger.debug("all unresolved-episode state cleared")
            return ended
        session = self._validated_key(session_key)
        if session in self._episodes:
            del self._episodes[session]
            _logger.debug("unresolved-episode state cleared for one session")
            return 1
        return 0

    def active_episode_count(self) -> int:
        """Number of sessions currently holding an active episode."""
        return len(self._episodes)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validated_key(self, session_key: object) -> str:
        """Validate a caller-managed session key (strict; no invention)."""
        if not isinstance(session_key, str) or not session_key.strip():
            raise UnknownFaceInputError(
                "session_key must be a non-empty str, got "
                f"{type(session_key).__name__}"
            )
        if len(session_key) > self._config.max_session_key_length:
            raise UnknownFaceInputError(
                "session_key longer than max_session_key_length "
                f"({self._config.max_session_key_length}); refusing it "
                "rather than truncating"
            )
        return session_key

    def _observe_unresolved(
        self,
        observation: UnresolvedObservation,
        *,
        session: str,
        reason: str,
    ) -> UnknownFaceOutcome:
        """Apply the bounded episode/cooldown policy (deterministic)."""
        now = self._clock()
        episode = self._episodes.get(session)
        if episode is not None and now < episode.alert_instant + self._cooldown:
            # Active within cooldown: suppress (cooldown NOT refreshed —
            # deterministic expiry even while frames keep flowing).
            episode.suppressed_frames += 1
            return UnknownFaceOutcome(
                status=UnknownFaceStatus.SUPPRESSED,
                observation=observation,
                suppressed_frames=episode.suppressed_frames,
                reason=reason,
            )
        # No active episode (none, expired, or evicted): open a fresh one.
        if episode is not None:
            # The previously stored episode expired lazily here.
            _logger.debug(
                "unresolved episode expired by cooldown (%d frames were "
                "suppressed); new episode begins",
                episode.suppressed_frames,
            )
        if len(self._episodes) >= self._config.max_sessions:
            if session not in self._episodes:
                # LRU eviction: drop the least-recently-SEEN session.
                # Worst case after eviction is one extra alert episode;
                # no evidence is lost — the handler retains no frames.
                oldest = min(
                    self._episodes,
                    key=lambda key: self._episodes[key].alert_instant,
                )
                del self._episodes[oldest]
                _logger.debug(
                    "session state evicted (bounded memory; max_sessions=%d)",
                    self._config.max_sessions,
                )
        self._episodes[session] = _Episode(
            observation=observation, alert_instant=now, suppressed_frames=0
        )
        return UnknownFaceOutcome(
            status=UnknownFaceStatus.OBSERVED,
            observation=observation,
            suppressed_frames=0,
            reason=reason,
        )
