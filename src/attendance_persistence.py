"""Composition of attendance decisions with durable persistence
(Phase 2 — P2-2).

`DurableAttendanceService` composes the two existing, unchanged
components — the Step 13 :class:`~src.attendance.AttendanceEngine`
(decision authority) and the P2-1
:class:`~src.attendance_store.AttendanceEventStore` (persistence and
validation authority) — into one service that turns a temporal
verification result into a decision *and* durably journals the event
when (and only when) the engine creates one.

This module adds **no** decision logic: check-in cooldown, duplicate
suppression, active-employee gating, clock separation, and event
construction remain exactly the engine's. It adds **no** storage
semantics: schema, validation, append durability, and torn-tail
recovery remain exactly the store's. The single new decision here is
*what to do when the engine's event fails to persist* — answered
explicitly below, never by silently presenting an uncertain write as a
durable check-in and never by automatically retrying (the journal may
already contain the event after an I/O failure; a retry could
duplicate it — retry policy belongs to an operator/composition layer
above this one).

Outcome contract (exactly four states, never ambiguous)
---------------------------------------------------------

* ``NO_EVENT`` — the engine produced a decision without an event
  (``NOT_VERIFIED`` / ``INACTIVE_EMPLOYEE`` / ``SUPPRESSED``).
  **Nothing is appended**; the exact engine decision is carried
  unchanged on the outcome.
* ``PERSISTED`` — the engine created an event and
  ``AttendanceEventStore.append()`` returned: the record is durable
  (fsync completed).
* ``PERSISTENCE_REJECTED`` — the engine created an event but the
  store raised ``AttendanceStoreInputError``: a **confirmed**
  failure. By the store's documented contract its input rejections
  occur before any write, so the journal is provably unchanged and
  the record is definitively absent. The outcome carries the
  original exception object and the exact event; nothing is retried
  (the caller fixes the input and retries deliberately).
* ``PERSISTENCE_UNCONFIRMED`` — the engine created an event but
  ``append()`` raised any other ``AttendanceStoreError``
  (``AttendanceStorePersistenceError`` in practice): an
  **uncertain** outcome — bytes may or may not have reached the
  journal. The outcome carries the original exception object (cause
  chain intact) and the exact event, plus a short reason naming the
  uncertainty. Nothing is retried (the journal may already contain
  the event; a retry could duplicate it — reconciliation by
  ``event_id`` is the caller's tool, not a silent re-append).

Failure classification (never mislabeled)
------------------------------------------

* Only ``AttendanceStoreError`` (the store's documented error family)
  maps to a persistence outcome, split by the store's own semantics:
  ``AttendanceStoreInputError`` (pre-write rejection) maps to the
  confirmed ``PERSISTENCE_REJECTED``; every other store error maps to
  the uncertain ``PERSISTENCE_UNCONFIRMED``. Any other exception
  raised by the store is a programming error and propagates
  unchanged.
* Engine exceptions (typed ``AttendanceInputError`` from the Step 13
  contract) propagate unchanged — no event, no append, no outcome.
* Construction validates its collaborators structurally (callable
  ``update`` / ``append``) — the same injectable-double pattern the
  engine uses for clocks and hooks — and performs **no** filesystem
  work at import or construction time, per the project lifecycle
  convention.

Privacy: outcomes carry decisions, events (IDs, types, timestamps),
statuses, and short reasons only — never crops, frames, embeddings,
scores, unknown-person data; log lines carry statuses and counts only,
never employee IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.attendance import (
    AttendanceDecision,
    AttendanceInputError,
    AttendanceEvent,
)
from src.attendance_store import (
    AttendanceEventStore,
    AttendanceStoreError,
    AttendanceStoreInputError,
)
from src.logging_setup import get_logger

__all__ = [
    "DurableAttendanceStatus",
    "DurableAttendanceOutcome",
    "DurableAttendanceService",
]

_logger = get_logger(__name__)


class DurableAttendanceStatus(Enum):
    """Explicit compose-and-persist outcome — never a bare bool.

    ``NO_EVENT``                 — the engine decided without creating
        an event; nothing was appended.
    ``PERSISTED``                — the engine's event was durably
        journalled (``fsync`` completed inside ``append``).
    ``PERSISTENCE_REJECTED``    — the engine's event was rejected by
        the store *before any write* (``AttendanceStoreInputError``);
        the journal is provably unchanged and the record is
        definitively absent. Confirmed failure; no automatic retry.
    ``PERSISTENCE_UNCONFIRMED`` — the engine's event could not be
        confirmed persisted (any other ``AttendanceStoreError``);
        the journal may or may not already contain the record — no
        automatic retry is attempted.
    """

    NO_EVENT = "no_event"
    PERSISTED = "persisted"
    PERSISTENCE_REJECTED = "persistence_rejected"
    PERSISTENCE_UNCONFIRMED = "persistence_unconfirmed"


@dataclass(frozen=True)
class DurableAttendanceOutcome:
    """Result of one process cycle (frozen; explicit, never ambiguous).

    Attributes:
        status: :class:`DurableAttendanceStatus` — one of exactly
            four states (see the enum and module docstring).
        decision: The engine's decision, **verbatim and unchanged** —
            this service never rewrites decision semantics.
        event: The engine's event when one was created (mirror of
            ``decision.event``; ``None`` otherwise). Present on both
            ``PERSISTED`` and ``PERSISTENCE_UNCONFIRMED`` — the event
            exists as a decision even when its persistence is
            unconfirmed.
        reason: Short diagnostic string (no biometric content).
        store_error: The original ``AttendanceStoreError`` raised by
            the store, **only** when the status is
            ``PERSISTENCE_REJECTED`` or ``PERSISTENCE_UNCONFIRMED``;
            ``None`` otherwise. The exception object is preserved
            unmodified (cause chain intact) so the caller can inspect
            and act on it.
    """

    status: DurableAttendanceStatus
    decision: AttendanceDecision
    event: AttendanceEvent | None
    reason: str | None
    store_error: AttendanceStoreError | None = None


class DurableAttendanceService:
    """Compose the decision engine with the durable event store.

    Args:
        engine: The decision authority — by default the Step 13
            :class:`~src.attendance.AttendanceEngine`, or any injected
            double exposing a callable ``update`` (structural check
            only, mirroring the engine's injectable-clock/hook
            pattern). Its decision semantics are consumed as-is.
        store: The persistence authority — by default the P2-1
            :class:`~src.attendance_store.AttendanceEventStore`, or
            any injected double exposing a callable ``append``.

    Raises:
        AttendanceInputError: a collaborator lacks the required
            callable. No filesystem work happens at construction.
    """

    def __init__(self, engine: object, store: object) -> None:
        update = getattr(engine, "update", None)
        if not callable(update):
            raise AttendanceInputError(
                "engine must provide a callable update(result) "
                f"(the Step 13 AttendanceEngine contract), got "
                f"{type(engine).__name__}"
            )
        append = getattr(store, "append", None)
        if not callable(append):
            raise AttendanceInputError(
                "store must provide a callable append(event) "
                "(the P2-1 AttendanceEventStore contract), got "
                f"{type(store).__name__}"
            )
        self._engine = engine
        self._store = store

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def process(self, result: object) -> DurableAttendanceOutcome:
        """Decide one attendance outcome and persist its event, if any.

        The temporal verification result is passed to the engine
        unmodified; its decision is consumed exactly as produced. When
        the decision carries an event, that exact event object is
        appended once.

        Args:
            result: The value the engine's ``update`` accepts (in
                production, a Step 11
                :class:`~src.temporal_verification.
                TemporalVerificationResult`).

        Returns:
            A frozen :class:`DurableAttendanceOutcome` in one of the
            four documented states.

        Raises:
            AttendanceInputError: re-raised unchanged from the engine
                (its documented contract for malformed inputs — no
                event, no append, no state change).
            Exception: any non-``AttendanceStoreError`` store failure
                propagates unchanged (a programming error is never
                mislabeled as a persistence outcome).
        """
        decision = self._engine.update(result)  # engine contract intact
        event = decision.event
        if event is None:
            _logger.info(
                "durable attendance: no event (%s); nothing appended",
                decision.status.value,
            )
            return DurableAttendanceOutcome(
                status=DurableAttendanceStatus.NO_EVENT,
                decision=decision,
                event=None,
                reason=(
                    f"engine decision {decision.status.value} created "
                    "no event; nothing was appended"
                ),
            )
        try:
            self._store.append(event)
        except AttendanceStoreInputError as exc:
            # Confirmed rejection: by the store's documented contract,
            # input errors are raised before any journal write, so the
            # journal is unchanged and the record is definitively
            # absent. Still no automatic retry — correcting the input
            # and re-appending is the caller's deliberate decision.
            _logger.error(
                "durable attendance: event rejected before any write "
                "(%s); journal unchanged; no automatic retry",
                type(exc).__name__,
            )
            return DurableAttendanceOutcome(
                status=DurableAttendanceStatus.PERSISTENCE_REJECTED,
                decision=decision,
                event=event,
                reason=(
                    "the engine created the event but the store rejected "
                    f"it before any write ({type(exc).__name__}); the "
                    "journal is unchanged and no automatic retry was "
                    "attempted"
                ),
                store_error=exc,
            )
        except AttendanceStoreError as exc:
            # The event exists as a decision but its persistence is
            # unconfirmed: the journal may already contain it (append
            # cannot retract buffered data) or may not. No retry here —
            # an automatic re-append could duplicate the record. The
            # original exception object is preserved on the outcome.
            _logger.error(
                "durable attendance: persistence unconfirmed "
                "(%s); no automatic retry",
                type(exc).__name__,
            )
            return DurableAttendanceOutcome(
                status=DurableAttendanceStatus.PERSISTENCE_UNCONFIRMED,
                decision=decision,
                event=event,
                reason=(
                    "the engine created the event but the store raised "
                    f"{type(exc).__name__}; persistence is unconfirmed "
                    "and no automatic retry was attempted"
                ),
                store_error=exc,
            )
        _logger.info(
            "durable attendance: event persisted (status=%s)",
            decision.status.value,
        )
        return DurableAttendanceOutcome(
            status=DurableAttendanceStatus.PERSISTED,
            decision=decision,
            event=event,
            reason="engine event durably journalled (fsync confirmed)",
        )
