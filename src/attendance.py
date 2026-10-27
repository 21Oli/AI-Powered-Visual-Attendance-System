"""Attendance-event engine for the AI-Powered Visual Attendance System
(Phase 1 — Step 13).

Converts a **valid temporally verified employee identity** (exactly the
:class:`~src.temporal_verification.TemporalVerificationResult` values
the approved Step 11 verifier produces) into a structured attendance
event when the configured policy permits it — and never otherwise.

Identity / recognition / attendance stay three separate components:
this engine performs **no** face matching, no similarity computation,
no embedding or manifest reads, no camera work, and no persistence.
It adds exactly one new decision per event type — whether the
attendance policy (a finite cooldown per employee per event type, plus
an open session for check-out) permits emitting the event — and
returns an explicit, structured decision.

Trust boundary (documented, enforced)
-------------------------------------

* Only a structurally valid ``VERIFIED`` temporal result may authorize
  an event decision. ``COLLECTING`` and ``INTERRUPTED`` yield
  ``NOT_VERIFIED`` decisions with no event.
* The authoritative identity comes from ``result.employee_id`` on
  ``VERIFIED`` results only, preserved exactly (``"007"``,
  ``"EMP-0042"``). A raw ``RecognitionResult`` (any status, including
  ``MATCHED``) is **not** an accepted input and cannot authorize an
  event — this module never reconstructs identity from recognition.
* Malformed or inconsistent inputs (wrong type, synthetic bogus
  status, ``VERIFIED`` without a valid employee ID) fail safely with
  typed errors and never produce events or state changes.
* Employee active status: when the caller supplies an
  ``active_lookup`` callable (the same injectable hook pattern the
  Step 10 recognition service already uses — **no** new manifest or
  storage reads), an inactive employee yields an ``INACTIVE_EMPLOYEE``
  decision with no event. With ``active_lookup=None`` the engine does
  **not** invent any active status — active gating is that hook's (or
  the recognition stage's) responsibility, delegated by the caller.

Duplicate-suppression policy (in-memory, bounded, documented)
--------------------------------------------------------------

* Exactly one ``CHECK_IN`` per employee per
  ``AttendanceConfig.check_in_cooldown_seconds``: the first eligible
  verified result creates the event and pins a non-refreshing
  suppression anchor (an injectable **monotonic** clock instant).
* P2-6 adds ``CHECK_OUT`` with the symmetric, fully independent
  bounded state described in the engine class documentation
  (``check_out_cooldown_seconds``, open-session markers; the same
  LRU/eviction policy per map).
* Repeated verified results for the same employee with elapsed
  monotonic time **strictly below** the cooldown are ``SUPPRESSED``
  (no event exposed). Boundary is exact and documented: at
  ``elapsed == cooldown`` a new event is created again. For
  ``CHECK_OUT`` decisions the same status is the explicit "policy
  declined this check-out" outcome (no open session, or within the
  check-out cooldown) — the ``reason`` field names the exact cause.
* Suppression anchors are **NOT refreshed** by repeated frames — the
  interval expires deterministically even under a continuous stream,
  so another event can occur after each full cooldown.
* Different employees are tracked independently (per-ID anchors).
* State is bounded: at most ``AttendanceConfig.max_tracked_employees``
  per-ID anchors, LRU-evicted by anchor instant (worst case after
  eviction: one additional event). :meth:`AttendanceEngine.reset`
  clears one or all anchors explicitly.

**Restart limitation (never overclaim):** duplicate protection holds
only for the *lifetime of this engine instance* and only as far as its
anchors are retained. Restarting the process loses it; nothing in
Step 13 is a database-level or cross-process exactly-once mechanism,
and no durable audit trail is created here.

Timestamp and clock semantics (explicitly separated)
-----------------------------------------------------

* **Event timestamp** — the injectable ``clock_wall`` (default
  ``datetime.now(UTC)``) provides the event's ``occurred_at``. It must
  return a timezone-aware ``datetime``; a naive one is rejected with a
  typed error (never silently substituted), and the value is
  normalized to UTC on the event.
* **Suppression clock** — the injectable ``monotonic_clock`` (default
  ``time.monotonic``) provides elapsed-time governance. Backward wall
  clock movement therefore cannot bypass duplicate protection or make
  anchors expire early; anchors never use wall clock in Phase 1, and
  this separation is asserted in tests.
* A non-finite/non-numeric monotonic value is rejected with a typed
  error *before* any decision is made — a clock failure can never
  return a fabricated successful event.

Privacy: decisions and events carry IDs, types, timestamps, and
short reasons only — no crops, frames, embeddings, scores, names, or
unnecessary personal data. Logs record outcome counts and statuses
only, never employee IDs or biometric details. No audit trail, no
shift/lateness logic, no persistence.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Callable

from src.config import ATTENDANCE, AttendanceConfig
from src.logging_setup import get_logger
from src.temporal_verification import (
    TemporalVerificationResult,
    TemporalVerificationStatus,
)

__all__ = [
    "AttendanceError",
    "AttendanceInputError",
    "EventType",
    "AttendanceEvent",
    "AttendanceDecisionStatus",
    "AttendanceDecision",
    "AttendanceEngine",
    "default_event_id_factory",
]

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AttendanceError(Exception):
    """Base class for attendance-event-engine errors."""


class AttendanceInputError(AttendanceError, ValueError):
    """Raised for invalid engine configuration, clocks, or results.

    Includes structurally malformed ``VERIFIED`` temporal results
    (missing/invalid employee ID), invalid clock outputs (naive or
    non-datetime wall values, non-finite monotonic values), and
    malformed ``active_lookup`` returns — all fail safely with no
    event and no state change.
    """


def default_event_id_factory() -> str:
    """Default event identifier: ``uuid.uuid4().hex`` — globally unique.

    Documented contract: a non-empty lowercase hex ``str`` derived from
    a UUID4, unique across events; tests assert uniqueness/non-emptiness
    rather than exact values. The engine validates (and may wrap) any
    injected factory's output.
    """
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Event schema (frozen; no biometric data ever carried)
# ---------------------------------------------------------------------------


class EventType(Enum):
    """Attendance event type. Step 13 defined ``CHECK_IN``; P2-6 adds
    ``CHECK_OUT`` (bathroom/symmetric serialized-value style — the
    lowercase ``str`` value is the journal encoding). No shift,
    lateness, overtime, leave, or payroll logic belongs here."""

    CHECK_IN = "check_in"
    CHECK_OUT = "check_out"


@dataclass(frozen=True)
class AttendanceEvent:
    """One accepted attendance event (immutable, minimal schema).

    Attributes:
        event_id: Unique identifier (``default_event_id_factory``
            produces ``uuid4().hex``; any injected factory's value is
            validated as a non-empty ``str``).
        employee_id: Exact verified employee identifier (no coercion;
            ``"007"`` and ``"EMP-0042"`` stay verbatim).
        event_type: :class:`EventType` — ``CHECK_IN`` (Step 13) or
            ``CHECK_OUT`` (P2-6).
        occurred_at: A timezone-aware ``datetime``, normalized to UTC
            at construction. Naive datetimes are rejected (fail
            fast) — the field is always comparable across events.

    No face images, crops, embeddings, similarity diagnostics, or
    unknown-person info belong here.
    """

    event_id: str
    employee_id: str
    event_type: EventType
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id:
            raise AttendanceInputError(
                "event_id must be a non-empty str, got "
                f"{type(self.event_id).__name__}"
            )
        if not isinstance(self.employee_id, str) or not self.employee_id:
            raise AttendanceInputError(
                "employee_id must be a non-empty str, got "
                f"{type(self.employee_id).__name__}"
            )
        if not isinstance(self.event_type, EventType):
            raise AttendanceInputError(
                "event_type must be an EventType, got "
                f"{type(self.event_type).__name__}"
            )
        if not isinstance(self.occurred_at, datetime):
            raise AttendanceInputError(
                "occurred_at must be a datetime, got "
                f"{type(self.occurred_at).__name__}"
            )
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            # Naive timestamps are ambiguous across deployments; the
            # engine requires an aware instant and normalizes to UTC.
            raise AttendanceInputError(
                "occurred_at must be timezone-aware (e.g. datetime.now(UTC)), "
                f"got naive datetime {self.occurred_at!r}"
            )
        object.__setattr__(
            self, "occurred_at", self.occurred_at.astimezone(UTC)
        )


# ---------------------------------------------------------------------------
# Decision types
# ---------------------------------------------------------------------------


class AttendanceDecisionStatus(Enum):
    """Explicit event-decision outcome — never a bare bool.

    ``EVENT_CREATED``      — policy permitted; ``decision.event`` holds
        the one accepted event.
    ``NOT_VERIFIED``       — the temporal result did not establish a
        verified identity (``COLLECTING``/``INTERRUPTED`` or a
        non-``VERIFIED`` input classification); no event.
    ``INACTIVE_EMPLOYEE``  — the injectable ``active_lookup``
        authoritative hook reports the verified employee inactive; no
        event. With no hook supplied this status never occurs (the
        engine invents no active status).
    ``SUPPRESSED``         — an equivalent recent event exists within
        the configured cooldown for this employee; no event.
    """

    EVENT_CREATED = "event_created"
    NOT_VERIFIED = "not_verified"
    INACTIVE_EMPLOYEE = "inactive_employee"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class AttendanceDecision:
    """Outcome of feeding one temporal result to the engine.

    Attributes:
        status: :class:`AttendanceDecisionStatus` (never ambiguous).
        event: The accepted :class:`AttendanceEvent` **only** when the
            status is ``EVENT_CREATED``; otherwise ``None`` —
            suppressed or unverified decisions never expose a
            fabricated event.
        reason: Short diagnostic string (never biometric detail).
    """

    status: AttendanceDecisionStatus
    event: AttendanceEvent | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

_UTC_NOW: Callable[[], datetime] = lambda: datetime.now(UTC)


class AttendanceEngine:
    """Turn verified temporal identities into bounded attendance events.

    Construction performs no side effects (no camera, model, files,
    network, threads). Per-employee bounded maps are the only state;
    decisions for different employees are independent and instances
    share nothing.

    P2-6 bounded-state layout (three physically separate dicts, each
    independently bounded by ``max_tracked_employees`` and evicted by
    its own LRU rule of the smallest stored monotonic instant):

    * ``_check_in_anchors`` — last accepted ``CHECK_IN`` (check-in
      duplicate suppression; byte-for-byte semantics of the pre-P2-6
      ``_anchors`` map).
    * ``_check_out_anchors`` — last accepted ``CHECK_OUT`` (check-out
      duplicate suppression). An accepted check-out never clears or
      resets the check-in anchor, and a check-out cooldown rejection
      never closes the session nor refreshes the anchor.
    * ``_open_sessions`` — the open session's latest accepted
      ``CHECK_IN`` instant. An accepted check-in opens/refreshes the
      marker (last-writer-wins); an accepted check-out closes it.

    Eviction in one map is physically incapable of touching the others
    (separate dicts). A missed open-session marker is treated purely as
    a bounded-memory miss, never as evidence a session is closed:
    with a configured ``open_session_lookup`` the hook adjudicates; in
    flag-off in-memory operation a later check-out is honestly rejected
    (``SUPPRESSED`` with a reason naming the eviction limitation) and
    no closed-session conclusion is ever drawn.

    Decision-time state ownership (unchanged engine convention):
    state changes only when a decision is accepted, immediately before
    persistence is attempted by the caller — a rejected decision (no
    open session, cooldown suppression, inactive, unverified) never
    mutates any map, and an event-ID factory failure mutates nothing.

    Args:
        config: ``AttendanceConfig`` (defaults to the project-wide
            frozen constant).
        clock_wall: Injectable timezone-aware UTC wall clock used for
            event timestamps (default ``datetime.now`` UTC).
        monotonic_clock: Injectable monotonically non-decreasing
            elapsed-seconds callable used for duplicate suppression
            (default ``time.monotonic``).
        active_lookup: Optional authoritative hook delegating "is this
            verified employee currently active?" (same injectable
            pattern as the Step 10 recognition service). ``None``
            delegates active gating upward — nothing is invented.
        open_session_lookup: Optional authoritative hook adjudicating
            "does this employee hold a currently open session?" for
            check-out decisions when the in-memory marker is absent or
            previously evicted. Contract: ``True`` — a session is open;
            ``False`` — no open session; a non-``bool`` return raises
            :class:`AttendanceInputError` with no state change; hook
            exceptions propagate. Not called while the marker is in
            memory, and never mutated into ``False``. This is an
            injectable interface for later durable integration (S3/S4);
            no journal access belongs to this engine.
        event_id_factory: Injectable unique-ID producer (default
            ``uuid.uuid4().hex``); its output is validated.

    Raises:
        AttendanceInputError: invalid config object, non-callable
            clock/factory/lookup parameters.
    """

    def __init__(
        self,
        *,
        config: AttendanceConfig = ATTENDANCE,
        clock_wall: Callable[[], datetime] = _UTC_NOW,
        monotonic_clock: Callable[[], float] = time.monotonic,
        active_lookup: Callable[[str], bool] | None = None,
        open_session_lookup: Callable[[str], bool] | None = None,
        event_id_factory: Callable[[], str] = default_event_id_factory,
    ) -> None:
        if not isinstance(config, AttendanceConfig):
            raise AttendanceInputError(
                "config must be an AttendanceConfig, got "
                f"{type(config).__name__}"
            )
        if not callable(clock_wall):
            raise AttendanceInputError(
                "clock_wall must be a callable returning timezone-aware "
                f"datetimes, got {type(clock_wall).__name__}"
            )
        if not callable(monotonic_clock):
            raise AttendanceInputError(
                "monotonic_clock must be a callable returning elapsed "
                f"seconds, got {type(monotonic_clock).__name__}"
            )
        if not callable(event_id_factory):
            raise AttendanceInputError(
                "event_id_factory must be a callable returning a unique "
                f"ID str, got {type(event_id_factory).__name__}"
            )
        if active_lookup is not None and not callable(active_lookup):
            raise AttendanceInputError(
                "active_lookup must be a callable taking the employee ID "
                f"and returning a bool, got {type(active_lookup).__name__}"
            )
        if (
            open_session_lookup is not None
            and not callable(open_session_lookup)
        ):
            raise AttendanceInputError(
                "open_session_lookup must be a callable taking the employee "
                f"ID and returning a bool, got "
                f"{type(open_session_lookup).__name__}"
            )
        self._config = config
        self._clock_wall = clock_wall
        self._monotonic_clock = monotonic_clock
        self._active_lookup = active_lookup
        self._open_session_lookup = open_session_lookup
        self._event_id_factory = event_id_factory
        self._cooldown = float(config.check_in_cooldown_seconds)
        self._out_cooldown = float(config.check_out_cooldown_seconds)
        # P2-6: three physically separate bounded maps. Eviction in one
        # map can never touch the others; each is LRU-evicted by the
        # smallest stored monotonic instant, independently, at
        # max_tracked_employees.
        # employee_id (exact) -> monotonic instant of the last accepted
        # CHECK_IN (check-in duplicate suppression; non-refreshing).
        self._check_in_anchors: dict[str, float] = {}
        # employee_id -> monotonic instant of the last accepted
        # CHECK_OUT (check-out duplicate suppression; non-refreshing).
        self._check_out_anchors: dict[str, float] = {}
        # employee_id -> monotonic instant of the open session's latest
        # accepted CHECK_IN (last-writer-wins refresh; closed by the
        # next accepted CHECK_OUT). Its absence means "consult the
        # open_session_lookup hook if configured", never "no session".
        self._open_sessions: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def update(
        self,
        result: TemporalVerificationResult,
        *,
        event_type: EventType = EventType.CHECK_IN,
    ) -> AttendanceDecision:
        """Decide one attendance event from one temporal result.

        Args:
            result: A :class:`~src.temporal_verification.
                TemporalVerificationResult` from the approved Step 11
                verifier. Raw recognition results must **not** be fed
                here — they never authorize events.
            event_type: The intent of this decision (keyword-only,
                P2-6). ``EventType.CHECK_IN`` (default) keeps the
                original Step 13 policy unchanged; ``EventType.CHECK_OUT``
                requests a check-out: the same verified/active gates
                apply, then — in order — the open-session adjudication
                (in-memory marker first; the ``open_session_lookup``
                hook only when the marker is absent; honest rejection
                without the hook) and the independent
                ``check_out_cooldown_seconds`` gate. An accepted
                check-out pins the check-out anchor and closes the
                in-memory session without touching the check-in
                anchor; a rejected check-out creates no event and
                mutates no map.

        Returns:
            A frozen :class:`AttendanceDecision`; ``decision.event``
            is non-``None`` only for ``EVENT_CREATED``.

        Raises:
            AttendanceInputError: malformed result/status or ``event_type``,
                ``VERIFIED`` without a valid employee ID, an invalid
                clock output, an invalid ``active_lookup`` or
                ``open_session_lookup`` return, or an event-ID
                factory failure — in every case no event is created
                and no suppression/session state changes.
        """
        if not isinstance(result, TemporalVerificationResult):
            raise AttendanceInputError(
                "result must be a TemporalVerificationResult, got "
                f"{type(result).__name__}"
            )
        if not isinstance(event_type, EventType):
            raise AttendanceInputError(
                "event_type must be an EventType, got "
                f"{type(event_type).__name__}"
            )
        if not isinstance(result.status, TemporalVerificationStatus):
            # Step 11's dataclass does not type-check the status field;
            # synthetic malformed statuses must fail safely here.
            raise AttendanceInputError(
                "temporal result status is not a "
                f"TemporalVerificationStatus, got "
                f"{type(result.status).__name__}"
            )

        if result.status is not TemporalVerificationStatus.VERIFIED:
            # COLLECTING / INTERRUPTED: identity not yet established —
            # never an attendance event (recognition evidence alone is
            # also never sufficient; raw recognition is not accepted).
            return AttendanceDecision(
                status=AttendanceDecisionStatus.NOT_VERIFIED,
                event=None,
                reason=(
                    f"temporal status {result.status.value}; a check-in "
                    "requires a VERIFIED identity"
                ),
            )

        # -- VERIFIED: the only attendance-authorizing status -----------
        employee_id = result.employee_id
        if not isinstance(employee_id, str) or not employee_id.strip():
            # Whitespace-only IDs are rejected (validated, never
            # transformed — the exact string is preserved verbatim).
            raise AttendanceInputError(
                "VERIFIED temporal result carries an invalid employee_id; "
                "the Step 11 contract guarantees an identity on VERIFIED "
                "— failing safely instead of accepting an unanchored "
                "verification"
            )

        # -- Active gate (injectable authority only; never invented) ---
        if self._active_lookup is not None:
            is_active = self._active_lookup(employee_id)
            if not isinstance(is_active, bool):
                raise AttendanceInputError(
                    f"employee {employee_id!r}: active_lookup returned "
                    f"{type(is_active).__name__}, expected bool"
                )
            if not is_active:
                _logger.debug(
                    "check-in withheld: verified employee inactive per "
                    "the authoritative hook"
                )
                return AttendanceDecision(
                    status=AttendanceDecisionStatus.INACTIVE_EMPLOYEE,
                    event=None,
                    reason="verified employee is inactive per the "
                    "authoritative active-status hook",
                )

        # -- Policy gates (monotonic; boundary exact) -------------------
        now_monotonic = self._monotonic_now()
        if event_type is EventType.CHECK_OUT:
            # P2-6: open-session + independent out-cooldown gates; the
            # decision body is symmetric to the check-in policy but
            # touches only their own bounded maps.
            return self._decide_check_out(employee_id, now_monotonic)

        # -- Check-in duplicate suppression (unchanged policy) ----------
        anchor = self._check_in_anchors.get(employee_id)
        if anchor is not None:
            elapsed = now_monotonic - anchor
            if elapsed < self._cooldown:
                # Non-refreshing anchor: repeated frames never extend
                # the suppression window.
                _logger.debug(
                    "check-in suppressed within cooldown (elapsed %.1f "
                    "of %.1f seconds)",
                    elapsed,
                    self._cooldown,
                )
                return AttendanceDecision(
                    status=AttendanceDecisionStatus.SUPPRESSED,
                    event=None,
                    reason=(
                        "a check-in exists within the suppression "
                        f"interval ({elapsed:.1f} < {self._cooldown:.1f} "
                        "seconds elapsed)"
                    ),
                )
            # elapsed >= cooldown: the anchor expired deterministically
            # (never refreshed by suppressed frames); a new event may
            # now be created below.

        # -- Create the event (all validation BEFORE any state change) --
        event = self._build_event(employee_id, EventType.CHECK_IN)
        if len(self._check_in_anchors) >= self._config.max_tracked_employees:
            if employee_id not in self._check_in_anchors:
                self._evict_lru_anchor(self._check_in_anchors)
        self._check_in_anchors[employee_id] = now_monotonic
        # P2-6: the accepted check-in opens/last-writer-wins refreshes
        # the bounded open-session marker. Its map is independently
        # LRU-bounded; its eviction never touches the anchor maps.
        if len(self._open_sessions) >= self._config.max_tracked_employees:
            if employee_id not in self._open_sessions:
                self._evict_lru_marker(self._open_sessions)
        self._open_sessions[employee_id] = now_monotonic
        _logger.info(
            "attendance %s event created (site totals: %d tracked "
            "employees)",
            event.event_type.value,
            len(self._check_in_anchors),
        )
        return AttendanceDecision(
            status=AttendanceDecisionStatus.EVENT_CREATED,
            event=event,
            reason="verified identity accepted by the check-in policy",
        )

    def _decide_check_out(
        self, employee_id: str, now_monotonic: float
    ) -> AttendanceDecision:
        """Apply the P2-6 check-out policy after the shared gates.

        Order: open-session adjudication first, then the independent
        check-out cooldown, then acceptance. Every rejection below
        creates no event and mutates no map. Rejections reuse the
        ``SUPPRESSED`` status (documented "policy declined this
        check-out" outcome) with a ``reason`` naming the exact cause —
        no new status vocabulary was added this phase.
        """

        # -- Open-session adjudication ---------------------------------
        if employee_id in self._open_sessions:
            # In-memory marker present: the hook is never consulted and
            # the session identity is authoritative here.
            session_open = True
        elif self._open_session_lookup is None:
            # Honest in-memory miss: without the durable lookup the
            # engine cannot distinguish "never checked in" from "the
            # bounded session map evicted the marker" — an eviction is
            # a memory miss, never evidence that a durable session is
            # closed. Reject; no journal access belongs to the engine.
            _logger.debug(
                "check-out withheld: no in-memory open-session marker "
                "and no open_session_lookup hook (bounded-memory miss "
                "cannot be reconstructed in this configuration)"
            )
            return AttendanceDecision(
                status=AttendanceDecisionStatus.SUPPRESSED,
                event=None,
                reason=(
                    "no in-memory open-session marker for this employee; "
                    "without an open_session_lookup hook the bounded "
                    "session state cannot be reconstructed, so the "
                    "check-out is rejected (in-memory eviction is not "
                    "treated as evidence a durable session is closed)"
                ),
            )
        else:
            # Missing marker + configured hook: adjudicate upward.
            # Hook exceptions propagate untouched; a non-bool return
            # raises a typed error and nothing is mutated.
            hook_result = self._open_session_lookup(employee_id)
            if not isinstance(hook_result, bool):
                raise AttendanceInputError(
                    f"employee {employee_id!r}: open_session_lookup "
                    f"returned {type(hook_result).__name__}, expected bool"
                )
            if not hook_result:
                _logger.debug(
                    "check-out withheld: no open session per the "
                    "authoritative open_session_lookup hook"
                )
                return AttendanceDecision(
                    status=AttendanceDecisionStatus.SUPPRESSED,
                    event=None,
                    reason=(
                        "no open session for this employee per the "
                        "authoritative open_session_lookup hook; a "
                        "check-out requires an open check-in"
                    ),
                )

        # -- Check-out cooldown (independent of the check-in gate) ------
        out_anchor = self._check_out_anchors.get(employee_id)
        if out_anchor is not None:
            out_elapsed = now_monotonic - out_anchor
            if out_elapsed < self._out_cooldown:
                # Non-refreshing anchor; a rejection here closes no
                # session and refreshes no anchor.
                _logger.debug(
                    "check-out suppressed within cooldown (elapsed %.1f "
                    "of %.1f seconds)",
                    out_elapsed,
                    self._out_cooldown,
                )
                return AttendanceDecision(
                    status=AttendanceDecisionStatus.SUPPRESSED,
                    event=None,
                    reason=(
                        "a check-out exists within the suppression "
                        "interval (%.1f < %.1f seconds elapsed)"
                        % (out_elapsed, self._out_cooldown)
                    ),
                )
            # elapsed >= out cooldown: deterministically expired.

        # -- Create the event (all validation BEFORE any state change) --
        event = self._build_event(employee_id, EventType.CHECK_OUT)
        if (
            len(self._check_out_anchors) >= self._config.max_tracked_employees
        ):
            if employee_id not in self._check_out_anchors:
                self._evict_lru_anchor(self._check_out_anchors)
        self._check_out_anchors[employee_id] = now_monotonic
        # Closing the session is part of accepting the check-out (the
        # hook may have adjudicated the marker, so removal is guarded):
        self._open_sessions.pop(employee_id, None)
        # The check-in anchor is deliberately untouched: a check-out
        # never clears or resets check-in suppression.
        _logger.info(
            "attendance %s event created (site totals: %d tracked "
            "check-outs)",
            event.event_type.value,
            len(self._check_out_anchors),
        )
        return AttendanceDecision(
            status=AttendanceDecisionStatus.EVENT_CREATED,
            event=event,
            reason=(
                "verified identity accepted by the check-out policy; "
                "open session closed"
            ),
        )

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def reset(self, employee_id: str | None = None) -> int:
        """Clear check-in duplicate-suppression anchors explicitly.

        Scoped to the check-in anchor map (the P2-3 recovery contract
        and every existing caller); open-session markers and check-out
        anchors are separate bounded maps and are not cleared here.

        Args:
            employee_id: Clear one employee's anchor (exact match), or
                ``None`` (documented) to clear **every** anchor.

        Returns:
            The number of anchors cleared (0 when nothing was held).

        Raises:
            AttendanceInputError: a non-empty ``str`` ID is required
                when one is provided.
        """
        if employee_id is None:
            cleared = len(self._check_in_anchors)
            self._check_in_anchors.clear()
            _logger.debug("all suppression anchors cleared (%d)", cleared)
            return cleared
        if not isinstance(employee_id, str) or not employee_id:
            raise AttendanceInputError(
                "employee_id must be a non-empty str or None, got "
                f"{type(employee_id).__name__}"
            )
        if employee_id in self._check_in_anchors:
            del self._check_in_anchors[employee_id]
            _logger.debug("suppression anchor cleared for one employee")
            return 1
        return 0

    def tracked_employee_count(self) -> int:
        """Number of employees currently holding a check-in
        suppression anchor (the check-out anchors and open-session
        markers are separate bounded maps, not counted here)."""
        return len(self._check_in_anchors)

    def restore_suppression_anchor(
        self, employee_id: str, *, elapsed_seconds: float
    ) -> bool:
        """Restore one cooldown anchor from a previous run (P2-3 only).

        Restart recovery translates a persisted wall-clock event
        timestamp into this run's monotonic domain **once**, here: the
        anchor is placed at ``monotonic_now - elapsed_seconds`` so a
        suppressed decision expires exactly
        ``check_in_cooldown_seconds - elapsed_seconds`` from now. A
        wall timestamp is never stored as, or compared as, a monotonic
        value — the age is measured wall-to-wall by the caller and
        handed over as an elapsed-seconds quantity.

        This is deliberately additive to :meth:`update`: the engine
        remains the single cooldown authority (its configured
        ``check_in_cooldown_seconds`` decides expiry here too), the
        anchor dict is never bypassed, and the bounded-state policy is
        identical (restoring beyond ``max_tracked_employees`` evicts
        the least-recently-anchored employee, exactly as a new event
        would).

        Args:
            employee_id: Exact employee identifier (never transformed).
            elapsed_seconds: Wall-clock seconds elapsed since the
                persisted event being restored. Must be a real, finite,
                **non-negative** number — a negative value would place
                the anchor in the future of the monotonic domain and is
                rejected (callers clamp future-dated records before
                calling; see ``src/attendance_recovery.py`` for the
                documented clock-skew policy).

        Returns:
            ``True`` when an (unexpired) anchor was restored;
            ``False`` when ``elapsed_seconds`` is already at or beyond
            the configured cooldown — the cooldown has deterministically
            expired across the restart, there is nothing left to
            restore, and the engine state is left untouched.

        Raises:
            AttendanceInputError: invalid employee ID or elapsed value.
                Validation happens **before** any state change.
        """
        if not isinstance(employee_id, str) or not employee_id.strip():
            raise AttendanceInputError(
                "employee_id must be a non-empty str, got "
                f"{type(employee_id).__name__}"
            )
        if (
            not isinstance(elapsed_seconds, (int, float))
            or isinstance(elapsed_seconds, bool)
            or not math.isfinite(elapsed_seconds)
            or elapsed_seconds < 0
        ):
            raise AttendanceInputError(
                "elapsed_seconds must be a real finite non-negative "
                f"number of seconds, got {elapsed_seconds!r}"
            )
        age = float(elapsed_seconds)
        if age >= self._cooldown:
            # Expired across the restart (deterministic): a restored
            # anchor older than the cooldown is indistinguishable from
            # no anchor — nothing is stored, nothing evicted.
            _logger.debug(
                "anchor restore skipped: %.1f s >= cooldown %.1f s "
                "(expired across the restart)",
                age,
                self._cooldown,
            )
            return False
        now_monotonic = self._monotonic_now()
        if len(self._check_in_anchors) >= self._config.max_tracked_employees:
            if employee_id not in self._check_in_anchors:
                self._evict_lru_anchor(self._check_in_anchors)
        self._check_in_anchors[employee_id] = now_monotonic - age
        _logger.debug(
            "suppression anchor restored (%.1f s of %.1f s cooldown "
            "remaining)",
            self._cooldown - age,
            self._cooldown,
        )
        return True

    def restore_check_out_anchor(
        self, employee_id: str, *, elapsed_seconds: float
    ) -> bool:
        """Restore one check-out cooldown anchor from a previous run
        (P2-6 S3, additive; P2-3 contract untouched).

        Exact mirror of :meth:`restore_suppression_anchor` for the
        independent P2-6 check-out state: the wall-clock age measured
        by the caller is translated **once** into this run's monotonic
        domain (``anchor = monotonic_now - age``) and expiry is decided
        here against the engine's configured
        ``check_out_cooldown_seconds`` — never against the check-in
        cooldown. A wall timestamp is never stored as, or compared as,
        a monotonic value.

        Args:
            employee_id: Exact employee identifier (never transformed).
            elapsed_seconds: Wall-clock seconds elapsed since the
                persisted ``CHECK_OUT`` being restored. Must be real,
                finite, and **non-negative** (callers clamp future-
                dated records first, per the documented skew policy).

        Returns:
            ``True`` when an (unexpired) anchor was restored; ``False``
            when the age already reached the configured check-out
            cooldown (nothing is stored, nothing evicted, no state
            change).

        Raises:
            AttendanceInputError: invalid employee ID or elapsed value;
                validation happens before any state change.
        """
        if not isinstance(employee_id, str) or not employee_id.strip():
            raise AttendanceInputError(
                "employee_id must be a non-empty str, got "
                f"{type(employee_id).__name__}"
            )
        if (
            not isinstance(elapsed_seconds, (int, float))
            or isinstance(elapsed_seconds, bool)
            or not math.isfinite(elapsed_seconds)
            or elapsed_seconds < 0
        ):
            raise AttendanceInputError(
                "elapsed_seconds must be a real finite non-negative "
                f"number of seconds, got {elapsed_seconds!r}"
            )
        age = float(elapsed_seconds)
        if age >= self._out_cooldown:
            # Expired across the restart (deterministic): nothing is
            # stored, nothing is evicted, state is untouched.
            _logger.debug(
                "check-out anchor restore skipped: %.1f s >= cooldown "
                "%.1f s (expired across the restart)",
                age,
                self._out_cooldown,
            )
            return False
        now_monotonic = self._monotonic_now()
        if (
            len(self._check_out_anchors) >= self._config.max_tracked_employees
        ):
            if employee_id not in self._check_out_anchors:
                self._evict_lru_anchor(self._check_out_anchors)
        self._check_out_anchors[employee_id] = now_monotonic - age
        _logger.debug(
            "check-out anchor restored (%.1f s of %.1f s cooldown "
            "remaining)",
            self._out_cooldown - age,
            self._out_cooldown,
        )
        return True

    def restore_open_session(
        self, employee_id: str, *, elapsed_seconds: float
    ) -> bool:
        """Restore one open-session marker from a previous run (P2-6
        S3, additive).

        The folded journal authority says the employee's latest event
        is ``CHECK_IN`` — i.e. an open session survives the restart.
        This method places the bounded open-session marker, translated
        once into this run's monotonic domain (``marker = monotonic_now
        - age``) so the marker's LRU eviction ordering stays aligned
        with the actual session-start recency. A wall timestamp is
        never stored as a monotonic value.

        Sessions never expire inside the engine (an open session stays
        open until an accepted ``CHECK_OUT``), so unlike the anchor
        restores this has no elapsed-at-recovery ``False`` case: the
        marker is always placed and ``True`` is returned.

        Args:
            employee_id: Exact employee identifier (never transformed).
            elapsed_seconds: Wall-clock seconds elapsed since the open
                session's latest persisted ``CHECK_IN``. Real, finite,
                non-negative (callers clamp future-dated records).

        Returns:
            ``True`` — the marker was placed (restoring beyond
            ``max_tracked_employees`` evicts the least-recently-opened
            session, exactly as an accepted check-in would).

        Raises:
            AttendanceInputError: invalid employee ID or elapsed value;
                validation happens before any state change.
        """
        if not isinstance(employee_id, str) or not employee_id.strip():
            raise AttendanceInputError(
                "employee_id must be a non-empty str, got "
                f"{type(employee_id).__name__}"
            )
        if (
            not isinstance(elapsed_seconds, (int, float))
            or isinstance(elapsed_seconds, bool)
            or not math.isfinite(elapsed_seconds)
            or elapsed_seconds < 0
        ):
            raise AttendanceInputError(
                "elapsed_seconds must be a real finite non-negative "
                f"number of seconds, got {elapsed_seconds!r}"
            )
        age = float(elapsed_seconds)
        now_monotonic = self._monotonic_now()
        if len(self._open_sessions) >= self._config.max_tracked_employees:
            if employee_id not in self._open_sessions:
                self._evict_lru_marker(self._open_sessions)
        self._open_sessions[employee_id] = now_monotonic - age
        _logger.debug(
            "open-session marker restored (age %.1f s at the recovery "
            "instant)",
            age,
        )
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _monotonic_now(self) -> float:
        """Read and validate the injected monotonic clock (no state)."""
        value = self._monotonic_clock()
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise AttendanceInputError(
                "monotonic_clock must return a real finite number of "
                f"seconds, got {value!r}"
            )
        return float(value)

    def _build_event(
        self, employee_id: str, event_type: EventType
    ) -> AttendanceEvent:
        """Materialize one validated event (no state mutation here).

        Every input to the event — wall timestamp, event ID — is
        validated here *before* the caller records any suppression
        state, so a clock/factory failure can never leave state and
        can never yield a fabricated event.
        """
        wall_now = self._clock_wall()
        if not isinstance(wall_now, datetime):
            raise AttendanceInputError(
                "clock_wall must return a datetime, got "
                f"{type(wall_now).__name__}"
            )
        if wall_now.tzinfo is None or wall_now.utcoffset() is None:
            raise AttendanceInputError(
                "clock_wall must return a timezone-aware datetime, got "
                f"naive {wall_now!r}"
            )
        try:
            event_id = self._event_id_factory()
        except Exception as exc:  # noqa: BLE001 - wrap factory failure
            raise AttendanceInputError(
                f"event_id_factory failed: {exc}"
            ) from exc
        if not isinstance(event_id, str) or not event_id:
            raise AttendanceInputError(
                "event_id_factory must return a non-empty str, got "
                f"{type(event_id).__name__}"
            )
        return AttendanceEvent(
            event_id=event_id,
            employee_id=employee_id,
            event_type=event_type,
            occurred_at=wall_now,
        )

    def _evict_lru_anchor(self, anchors: dict[str, float]) -> None:
        """Drop the least-recently-anchored employee from one bounded
        anchor map (bounded state; never touches the other maps)."""
        oldest = min(anchors, key=lambda key: anchors[key])
        del anchors[oldest]
        _logger.debug(
            "suppression anchor evicted (bounded memory; "
            "max_tracked_employees=%d)",
            self._config.max_tracked_employees,
        )

    def _evict_lru_marker(self, sessions: dict[str, float]) -> None:
        """Drop the least-recently-opened session marker from the
        bounded open-session map (its own eviction policy).

        Documented semantics: the evicted marker becomes a pure
        in-memory miss for the open-session lookup path; it is never
        treated as evidence that the employee has no open session, and
        eviction here never removes entries from the anchor maps.
        """
        oldest = min(sessions, key=lambda key: sessions[key])
        del sessions[oldest]
        _logger.debug(
            "open-session marker evicted (bounded memory; "
            "open-session state is a miss until adjudicated upstream; "
            "max_tracked_employees=%d)",
            self._config.max_tracked_employees,
        )
