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

Shared event fold (P2-6 S3)
---------------------------

:func:`fold_latest_events` is the single reusable, **pure** helper the
loop above uses for per-employee latest-event selection (the fold is
what implements the "latest ``CHECK_IN`` wins" policy after the
``CHECK_IN``-only allowlist filter). P2-6 S3 reuses the same helper for:

* open-session recovery (:func:`recover_session_state` — folded latest
  event of *any* type decides the session state),
* journal-backed ``open_session_lookup``
  (:func:`make_journal_open_session_lookup`), and
* the existing check-in anchor recovery above (fold over the filtered
  ``CHECK_IN`` sequence — the allowlist contract is untouched).

The fold compares ``(occurred_at, journal_position)`` tuples: a later
UTC timestamp wins; equal timestamps are resolved by the event's later
position in the validated journal sequence. It never loads files, never
mutates engine state, never catches store errors, and is deterministic
for the same input sequence.

"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Sequence

from src.attendance import (
    AttendanceInputError,
    AttendanceEvent,
    EventType,
)
from src.logging_setup import get_logger

__all__ = [
    "CooldownRecoveryReport",
    "SessionRecoveryReport",
    "fold_latest_events",
    "recover_cooldown_anchors",
    "recover_session_state",
    "make_journal_open_session_lookup",
]

_logger = get_logger(__name__)


@dataclass(frozen=True)
class SessionRecoveryReport:
    """Explicit result of one read-only P2-6 session-state replay.

    Attributes:
        open_sessions_restored: Employee IDs whose latest folded event
            is ``CHECK_IN`` — an open session — whose in-memory marker
            was restored (in first-seen journal order).
        check_out_anchors_restored: Employee IDs whose latest folded
            event is ``CHECK_OUT`` whose unexpired check-out cooldown
            anchor was restored (in first-seen journal order).
        expired_check_out_anchors: Number of ``CHECK_OUT``-latest
            employees whose wall-clock age already reached the
            configured ``check_out_cooldown_seconds`` at recovery
            (skipped — equivalent to no anchor).
        future_timestamps_clamped: Number of restorations (sessions
            and check-out anchors combined) whose wall-clock age was
            negative (journal event dated after ``wall_now`` — clock
            moved backward) and was clamped to zero per the documented
            skew policy.
        events_scanned: Number of validated events examined from the
            journal (before per-employee selection).
        incomplete_trailing_record: Pass-through of the store's load
            flag — ``True`` when the journal ended in a torn final
            fragment (excluded from replay, never silently).
    """

    open_sessions_restored: tuple[str, ...]
    check_out_anchors_restored: tuple[str, ...]
    expired_check_out_anchors: int
    future_timestamps_clamped: int
    events_scanned: int
    incomplete_trailing_record: bool


def fold_latest_events(
    events: Sequence[AttendanceEvent],
) -> dict[str, AttendanceEvent]:
    """Pure per-employee fold: the latest event, never ambiguous.

    Selection key is the tuple ``(event.occurred_at, journal_position)``:
    a later UTC timestamp wins; equal timestamps are resolved by the
    event's later position in the validated journal sequence. Because
    the fold iterates the sequence in journal order, comparing
    ``event.occurred_at >= current.occurred_at`` implements exactly that
    maximum (``current`` always precedes ``event`` positionally) — no
    reliance on dictionary iteration order, sort stability, or an
    unordered collection for correctness.

    ``occurred_at`` values are timezone-aware, UTC-normalized datetimes
    by the :class:`~src.attendance.AttendanceEvent` contract, so the
    comparison is datetime ordering — never timestamp-string
    lexicography.

    Purity contract: no files, no I/O, no engine state, no store calls,
    no error handling. Already-validated events in; deterministic
    result out (same input, same output).

    Returns:
        ``employee_id -> latest`` with a **documented** iteration order:
        first-seen journal order (guaranteed by construction), which
        callers use for a deterministic restore-application order.
    """
    latest: dict[str, AttendanceEvent] = {}
    for position, event in enumerate(events):
        employee_id = event.employee_id
        current = latest.get(employee_id)
        if current is None or event.occurred_at >= current.occurred_at:
            latest[employee_id] = event
    return latest


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

    # Select per employee with the shared pure fold over the
    # CHECK_IN-only allowlist (the events are already validated): a
    # later timestamp wins and equal timestamps resolve by journal
    # position — exactly the established selection semantics, now
    # implemented once. The fold's iteration order is first-seen
    # journal order (its documented contract), so restore-application
    # order remains deterministic.
    check_in_events = tuple(
        event
        for event in load_result.events
        if event.event_type is EventType.CHECK_IN
    )
    latest = fold_latest_events(check_in_events)
    scanned = len(check_in_events)

    restored: list[str] = []
    expired = 0
    clamped = 0
    for employee_id, event in latest.items():
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


def recover_session_state(
    engine: object,
    store: object,
    *,
    wall_now: datetime,
) -> SessionRecoveryReport:
    """Replay the P2-6 open-session and check-out cooldown state.

    The journal is loaded **in full** through the store's
    ``load_events()`` (same validation, torn-tail reporting, and typed
    errors as any load) before any engine state changes — a failed load
    can never leave a partially recovered engine. The single shared
    fold (:func:`fold_latest_events`) selects each employee's latest
    event over **all** validated events; the folded event type alone
    decides the state:

    * latest ``CHECK_IN`` → an open session: restored through the
      engine's ``restore_open_session``.
    * latest ``CHECK_OUT`` → no open session, and the check-out
      cooldown is eligible for restoration: the engine's
      ``restore_check_out_anchor`` decides the age against the
      engine's configured ``check_out_cooldown_seconds`` (expired
      anchors are skipped, never restored).
    * no events → no open session, nothing restored.

    The wall-clock-to-monotonic conversion reuses the established P2-3
    policy exactly (see the module docstring): age measured wall-to-
    wall, negative ages clamped to zero (conservative, never
    inverted), wall timestamps never treated as monotonic instants.

    Args:
        engine: The decision authority whose P2-6 state is restored —
            by contract an object exposing callables
            ``restore_open_session`` and ``restore_check_out_anchor``
            (the P2-6 additions to the
            :class:`~src.attendance.AttendanceEngine` API), or an
            injected double of the same shape.
        store: The persistence authority — by contract an object
            exposing a callable ``load_events()`` (the P2-1 store
            contract), or any injected double.
        wall_now: The current wall-clock instant (timezone-aware;
            normalized to UTC). Supplied by the caller so recovery is
            deterministic and never reads a hidden clock.

    Returns:
        A frozen :class:`SessionRecoveryReport`.

    Raises:
        AttendanceInputError: a collaborator lacks a required callable,
            or ``wall_now`` is naive / not a datetime. Raised before
            any journal access.
        CorruptAttendanceJournalError: the journal fails load
            validation (propagated unchanged, never swallowed; no
            partial restoration).
        UnsupportedJournalSchemaVersionError: a record carries an
            unsupported schema version (propagated unchanged).
    """
    restore_session = getattr(engine, "restore_open_session", None)
    if not callable(restore_session):
        raise AttendanceInputError(
            "engine must provide a callable restore_open_session "
            "(the P2-6 AttendanceEngine contract), got "
            f"{type(engine).__name__}"
        )
    restore_anchor = getattr(engine, "restore_check_out_anchor", None)
    if not callable(restore_anchor):
        raise AttendanceInputError(
            "engine must provide a callable restore_check_out_anchor "
            "(the P2-6 AttendanceEngine contract), got "
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

    # Load in full before any engine mutation (same atomicity as the
    # P2-3 recovery): a failed load can never leave a partially
    # recovered engine, and load errors propagate unchanged.
    load_result = load_events()

    latest = fold_latest_events(load_result.events)

    open_sessions: list[str] = []
    out_anchors: list[str] = []
    expired_out = 0
    clamped = 0
    for employee_id, event in latest.items():  # first-seen journal order
        age = (wall_now_utc - event.occurred_at).total_seconds()
        if age < 0:
            # Same documented clamp policy: a future-dated event is
            # treated as just-happened (never inverted, never an
            # anchor ahead of the monotonic domain).
            clamped += 1
            age = 0.0
        if event.event_type is EventType.CHECK_IN:
            # An open session: place the marker (sessions never expire
            # in-engine; the engine decides marker placement).
            restore_session(employee_id, elapsed_seconds=age)
            open_sessions.append(employee_id)
        else:
            # Latest event is CHECK_OUT: no open session; restore the
            # check-out cooldown anchor only if the engine deems it
            # still unexpired (age < check_out_cooldown_seconds).
            if restore_anchor(employee_id, elapsed_seconds=age):
                out_anchors.append(employee_id)
            else:
                expired_out += 1
    _logger.info(
        "session recovery replayed: %d events scanned, %d open "
        "sessions restored, %d check-out anchors restored, %d "
        "check-out anchors expired, %d future-dated clamped "
        "(incomplete_trailing=%s)",
        len(load_result.events),
        len(open_sessions),
        len(out_anchors),
        expired_out,
        clamped,
        load_result.incomplete_trailing_record,
    )
    return SessionRecoveryReport(
        open_sessions_restored=tuple(open_sessions),
        check_out_anchors_restored=tuple(out_anchors),
        expired_check_out_anchors=expired_out,
        future_timestamps_clamped=clamped,
        events_scanned=len(load_result.events),
        incomplete_trailing_record=(
            load_result.incomplete_trailing_record
        ),
    )


def make_journal_open_session_lookup(
    store: object,
) -> Callable[[str], bool]:
    """Build a journal-backed ``open_session_lookup`` callable (P2-6 S3).

    The returned callable satisfies the engine's S2 hook contract:
    given an employee ID it returns ``True`` only when that employee's
    latest **folded** journal event is ``CHECK_IN`` (an open session),
    and ``False`` when the employee has no event or their latest event
    is ``CHECK_OUT``.

    Contract:

    * The journal is loaded through the store's existing
      ``load_events()`` on **every** call — full store validation and
      corruption rules apply each time, and no stale snapshot is ever
      cached across calls (a newly appended event is visible on the
      very next lookup).
    * Loading invalidates nothing: store errors (including
      :class:`~src.attendance_store.CorruptAttendanceJournalError` and
      I/O errors) **propagate unchanged** — they are never converted to
      ``False`` and never swallowed.
    * The callable performs no engine access, mutates no engine state,
      and keeps no state of its own — it is a pure adapter over the
      store plus the shared fold.

    Args:
        store: The persistence authority — by contract an object
            exposing a callable ``load_events()`` (the P2-1
            :class:`~src.attendance_store.AttendanceEventStore`
            contract), or any injected double.

    Returns:
        The hook callable, ready to pass to the engine's
        ``open_session_lookup`` constructor parameter (wiring it into
        the live composition is a later, separate step).

    Raises:
        AttendanceInputError: the store lacks a callable
            ``load_events()`` — raised before any journal access.
    """
    load_events = getattr(store, "load_events", None)
    if not callable(load_events):
        raise AttendanceInputError(
            "store must provide a callable load_events() (the P2-1 "
            f"AttendanceEventStore contract), got {type(store).__name__}"
        )

    def lookup(employee_id: str) -> bool:
        if not isinstance(employee_id, str) or not employee_id:
            raise AttendanceInputError(
                "employee_id must be a non-empty str, got "
                f"{type(employee_id).__name__}"
            )
        # Fresh validated load on every call; store errors propagate.
        load_result = load_events()
        latest = fold_latest_events(load_result.events)
        event = latest.get(employee_id)
        return event is not None and event.event_type is EventType.CHECK_IN

    return lookup
