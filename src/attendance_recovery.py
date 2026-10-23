"""Restart recovery of attendance cooldown anchors (Phase 2 — P2-3).

:func:`recover_cooldown_anchors` replays previously persisted
attendance events into a fresh :class:`~src.attendance.AttendanceEngine`
so duplicate suppression survives a process restart. Replay is
**read-only**: the journal is loaded through the P2-1 store's existing
``load_events()`` (the same validation, torn-tail reporting, and typed
errors as any load), nothing is ever appended, rewritten, or repaired
by this module, and no new attendance events are produced.

No second source of cooldown truth
------------------------------------

Recovery never keeps its own cooldown state and never duplicates the
cooldown policy: per employee it selects the latest persisted
``CHECK_IN``, measures the wall-clock age, and hands the age to the
engine's additive
:meth:`~src.attendance.AttendanceEngine.restore_suppression_anchor`,
which alone decides expiry (against the engine's configured cooldown)
and places the anchor in the run's monotonic domain. This module holds
no anchors, no cooldown constant, and no timers.

Wall-clock versus monotonic (never conflated)
----------------------------------------------

The engine suppresses on a monotonic timeline; the journal stores
UTC wall-clock timestamps. Recovery measures the event's age
**wall-to-wall** (``wall_now - occurred_at``, both UTC-aware), then
translates that age once into the current run's monotonic domain via
the engine's restore call (``anchor = monotonic_now - age``). A wall
timestamp is never treated as a monotonic instant, and no wall value
is ever written into an anchor.

Clock-skew policy (explicit, conservative, documented)
-------------------------------------------------------

Cross-run cooldown precision is bounded by wall-clock reliability
between runs; within a run, suppression remains monotonic-exact. The
explicit policy at the restart boundary:

* **Wall clock moved backward** (the journal's latest event is dated
  *after* ``wall_now``): the negative age is **clamped to zero** — the
  event is treated as having happened just now and the employee is
  suppressed for the full cooldown from the recovery instant. This
  errs on the no-duplicate side (a clamp can only *delay* a legitimate
  re-check-in by at most one cooldown; it can never create a duplicate
  event) and preserves the engine's invariant that an anchor never
  sits in the future of the monotonic domain.
* **Wall clock moved forward**: the computed age is larger than the
  real elapsed time, so a cooldown may appear already expired. The
  anchor is then simply not restored (expired-at-recovery semantics
  below) — the worst case is one additional check-in event, exactly
  matching the engine's documented bounded-state worst case. Forward
  drift is never "compensated"; no invented adjustment is applied.

Deterministic replay policy
---------------------------

* Only ``EventType.CHECK_IN`` records are attendance events for
  check-in cooldown recovery (an explicit allowlist: future event
  types must never silently affect check-in suppression).
* Records are read in journal (append) order. Per employee the
  **latest** event wins: greater UTC ``occurred_at`` replaces the
  candidate; equal timestamps are resolved by journal position (the
  later record wins). This is deterministic under out-of-order
  timestamps and under the store's documented duplicate-``event_id``
  limitation (a retry after an uncertain append can duplicate a line;
  duplicates carry identical timestamps and collapse to one anchor).
* Restores are applied in first-seen journal order of employees —
  deterministic even where LRU eviction at ``max_tracked_employees``
  can make application order observable.
* A cooldown that is already elapsed at recovery (age ≥ cooldown) is
  **skipped, not restored** — expired anchors are equivalent to no
  anchor; the engine will simply create a fresh event on the next
  verified result.
* An incomplete trailing record (torn final line) is excluded by the
  store's documented load semantics and therefore never restores an
  anchor; the ``incomplete_trailing_record`` flag is passed through
  on the report so the caller can see the journal was torn.

Failure semantics (never a false success)
------------------------------------------

The journal is loaded **in full before any engine state changes**, so
a failed load can never leave a partially recovered engine:

* ``CorruptAttendanceJournalError`` /
  ``UnsupportedJournalSchemaVersionError`` (and any other store-load
  error) propagate unchanged — a failed load is never treated as an
  empty journal and never swallowed.
* The helper validates its collaborators structurally (callable
  ``restore_suppression_anchor`` / ``load_events``) and a timezone-
  aware ``wall_now``; invalid inputs raise
  :class:`~src.attendance.AttendanceInputError` before any load.

Privacy: reports carry employee identifiers only where the caller
needs them (the restored IDs); log lines carry counts and statuses
only — never employee IDs, never biometric content.

No filesystem side effects at import or before the explicit load call;
the store creates nothing (loading a missing journal is the normalized
empty history, per the P2-1 contract).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from src.attendance import (
    AttendanceInputError,
    AttendanceEvent,
    EventType,
)
from src.logging_setup import get_logger

__all__ = [
    "CooldownRecoveryReport",
    "recover_cooldown_anchors",
]

_logger = get_logger(__name__)


@dataclass(frozen=True)
class CooldownRecoveryReport:
    """Explicit result of one read-only cooldown replay.

    Attributes:
        anchors_restored: Employee IDs whose cooldown anchors were
            restored (in first-seen journal order). Empty when nothing
            was restored — never a bare bool.
        expired_anchors: Number of employees whose latest event was
            already at or beyond the cooldown at recovery (skipped —
            expired anchors are equivalent to no anchor).
        future_timestamps_clamped: Number of restores whose wall-clock
            age was negative (journal event dated after ``wall_now`` —
            clock moved backward across the restart) and was clamped
            to zero per the documented skew policy.
        events_scanned: Number of qualifying ``CHECK_IN`` records
            examined from the journal (before per-employee selection).
        incomplete_trailing_record: Pass-through of the store's load
            flag — ``True`` when the journal ended in a torn final
            fragment (excluded from replay, never silently).
    """

    anchors_restored: tuple[str, ...]
    expired_anchors: int
    future_timestamps_clamped: int
    events_scanned: int
    incomplete_trailing_record: bool


def recover_cooldown_anchors(
    engine: object,
    store: object,
    *,
    wall_now: datetime,
) -> CooldownRecoveryReport:
    """Replay persisted attendance events into engine cooldown anchors.

    The journal is loaded in full through the store's
    ``load_events()``; per employee the latest ``CHECK_IN`` by UTC
    ``occurred_at`` (ties by journal position) is translated into a
    wall-clock age and restored via the engine's
    ``restore_suppression_anchor``. Nothing is appended; no attendance
    event is created; no cooldown decision is made here (the engine's
    restore call owns expiry against its configured cooldown).

    Args:
        engine: The decision authority whose anchors are restored —
            by contract an object exposing a callable
            ``restore_suppression_anchor`` (the P2-3
            :class:`~src.attendance.AttendanceEngine` API), or any
            injected double of the same shape.
        store: The persistence authority — by contract an object
            exposing a callable ``load_events()`` (the P2-1
            :class:`~src.attendance_store.AttendanceEventStore`
            contract), or any injected double.
        wall_now: The current wall-clock instant (timezone-aware;
            normalized to UTC). Supplied by the caller so recovery is
            deterministic and never reads a hidden clock; a live
            composition passes the same wall clock it gives the
            engine.

    Returns:
        A frozen :class:`CooldownRecoveryReport`.

    Raises:
        AttendanceInputError: a collaborator lacks the required
            callable, or ``wall_now`` is naive / not a datetime.
            Raised before any journal access.
        CorruptAttendanceJournalError: the journal fails load
            validation (propagated unchanged, never swallowed).
        UnsupportedJournalSchemaVersionError: a record carries an
            unsupported schema version (propagated unchanged).
    """
    restore = getattr(engine, "restore_suppression_anchor", None)
    if not callable(restore):
        raise AttendanceInputError(
            "engine must provide a callable restore_suppression_anchor "
            "(the P2-3 AttendanceEngine contract), got "
            f"{type(engine).__name__}"
        )
    load_events = getattr(store, "load_events", None)
    if not callable(load_events):
        raise AttendanceInputError(
            "store must provide a callable load_events() (the P2-1 "
            f"AttendanceEventStore contract), got {type(store).__name__}"
        )
    if not isinstance(wall_now, datetime):
        raise AttendanceInputError(
            "wall_now must be a datetime, got "
            f"{type(wall_now).__name__}"
        )
    if wall_now.tzinfo is None or wall_now.utcoffset() is None:
        raise AttendanceInputError(
            "wall_now must be timezone-aware, got naive "
            f"{wall_now!r}"
        )
    wall_now_utc = wall_now.astimezone(UTC)

    # Load in full before any engine mutation: a failed load can never
    # leave a partially recovered engine, and load errors propagate.
    load_result = load_events()

    # Select per employee: latest CHECK_IN by UTC timestamp; equal
    # timestamps resolved by journal position (later wins).
    latest: dict[str, AttendanceEvent] = {}
    first_seen: list[str] = []
    scanned = 0
    for event in load_result.events:
        if event.event_type is not EventType.CHECK_IN:
            # Explicit allowlist: only attendance check-ins drive the
            # check-in cooldown (future event types never silently do).
            continue
        scanned += 1
        employee_id = event.employee_id
        if employee_id not in latest:
            first_seen.append(employee_id)
            latest[employee_id] = event
        else:
            current = latest[employee_id]
            if event.occurred_at >= current.occurred_at:
                latest[employee_id] = event

    restored: list[str] = []
    expired = 0
    clamped = 0
    for employee_id in first_seen:
        event = latest[employee_id]
        age = (wall_now_utc - event.occurred_at).total_seconds()
        if age < 0:
            # Clock moved backward across the restart: clamp to zero
            # (treat the event as just-happened; suppress for the full
            # cooldown). Documented, conservative, never inverted.
            clamped += 1
            age = 0.0
        if restore(employee_id, elapsed_seconds=age):
            restored.append(employee_id)
        else:
            expired += 1
    _logger.info(
        "cooldown recovery replayed: %d records scanned, %d anchors "
        "restored, %d expired, %d future-dated clamped "
        "(incomplete_trailing=%s)",
        scanned,
        len(restored),
        expired,
        clamped,
        load_result.incomplete_trailing_record,
    )
    return CooldownRecoveryReport(
        anchors_restored=tuple(restored),
        expired_anchors=expired,
        future_timestamps_clamped=clamped,
        events_scanned=scanned,
        incomplete_trailing_record=(
            load_result.incomplete_trailing_record
        ),
    )
