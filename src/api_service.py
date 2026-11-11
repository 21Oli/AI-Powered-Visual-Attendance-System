"""Phase 3 Step 40 — API service layer for the eight-endpoint contract.

This module holds the composition the FastAPI routes must **not** contain:
the route functions in :mod:`src.api` translate HTTP to a service call and a
service result to an HTTP response, and nothing else. Every decision lives
either here or in the existing component that already owns it.

What this module is allowed to decide (and nothing more)
-------------------------------------------------------

* **Employee metadata** — it owns no SQL and no validation rules. Every
  operation delegates to :class:`src.employee_repository.EmployeeRepository`
  (which owns the statements and the typed persistence-state errors) and the
  caller supplies the already-validated :class:`src.employee.Employee` model
  where one is needed. The service adds only the API-shaped questions the
  repository deliberately leaves upstream: "missing means 404", "duplicate
  means 409", and the keyset page envelope.
* **Attendance reads** — it owns no journal semantics. Reads go through
  :meth:`src.attendance_store.AttendanceEventStore.load_events`, so the store's
  validation, torn-tail reporting and typed corruption errors apply unchanged.
  A corrupt or unreadable journal is **never** repaired, rewritten, or
  presented as an empty history.
* **Checkout** — see the explicit note below.

Checkout: operator-authorized append, never a fabricated verification
--------------------------------------------------------------------

The checkout endpoint is an **operator action**, recorded through the approved
**additive engine path**: :meth:`src.attendance.AttendanceEngine.operator_checkout`,
which requires an explicit :class:`src.attendance.OperatorAuthorization`. The
camera-authorized ``engine.update`` path is untouched and still requires a
``VERIFIED`` :class:`src.temporal_verification.TemporalVerificationResult`.

* **No fabricated verification.** This module never constructs, mocks, or
  synthesizes a ``TemporalVerificationResult``: inventing a ``VERIFIED`` result
  would claim biometric verification that never happened. The authorizing
  object is an ``OperatorAuthorization``, which by construction cannot carry the
  biometric source, and the engine answers ``AUTHORIZATION_REQUIRED`` when no
  valid authorization is supplied.
* **The rules live in the engine, not here.** The engine applies the shared
  active gate, the open-session adjudication and the independent
  ``check_out_cooldown_seconds`` window, using the same code as the biometric
  path. This module only (a) resolves the employee (404 is an API concern),
  (b) loads the journal once through the store's validated ``load_events`` and
  folds it with :func:`src.attendance_recovery.fold_latest_events`, (c) rebuilds
  the engine's bounded session and check-out anchors from that fold with
  ``restore_open_session`` / ``restore_check_out_anchor`` so a per-request
  engine instance still honours the cooldown, and (d) maps the engine's stable
  ``reason_code`` onto the API's business-refusal codes.
* **Durable append is the success boundary.** The accepted ``CHECK_OUT`` is
  appended through the existing store, whose ``fsync`` makes it durable, and
  the event is returned only after that append returned. A refusal appends
  nothing.

Concurrency (process-local serialization, documented)
----------------------------------------------------

The journal load, the policy decision and the durable append are one **logical**
operation, but they are three separate calls, so two overlapping check-out
requests could otherwise both observe the same open session and both append.
``checkout`` therefore runs that sequence inside a module-level
``threading.Lock`` (``_CHECKOUT_CRITICAL_SECTION``): the second request re-reads
the journal after the first append and is then refused by the engine
(``NO_OPEN_SESSION`` or ``CHECKOUT_COOLDOWN``) with nothing appended.

Scope, stated honestly: this is **process-local**. The API is a single process
by contract (Windows single-process startup guard), so it closes the reachable
in-process race — Starlette runs synchronous endpoints in a thread pool, which
is what makes the overlap real. It does **not** serialize another process
against this journal, and the store keeps its documented cross-process
no-exactly-once disclaimer unchanged; a multi-process deployment would need a
store-level lock, which is a separate decision. Journal format, validation,
fsync durability and the store's own behaviour are untouched, and no automatic
reconciliation of residual open sessions is performed anywhere.

Error taxonomy
--------------

The service raises only what the existing components raise plus
:class:`CheckoutRejected` for a **business** refusal (inactive employee, no open
session). Availability and corruption errors propagate unchanged so the route
layer can map them to their distinct documented statuses; nothing here catches
broadly, retries, or repairs.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from src.attendance import (
    REASON_CHECKOUT_COOLDOWN,
    REASON_INACTIVE_EMPLOYEE,
    REASON_NO_OPEN_SESSION,
    AttendanceDecisionStatus,
    AttendanceEngine,
    AttendanceEvent,
    EventType,
    OperatorAuthorization,
)
from src.attendance_recovery import fold_latest_events
from src.attendance_store import AttendanceEventStore
from src.config import ATTENDANCE, PAGINATION, AppSettings
from src.employee import Employee
from src.employee_repository import EmployeeRepository

__all__ = [
    "CHECKOUT_COOLDOWN",
    "CHECKOUT_EMPLOYEE_INACTIVE",
    "CHECKOUT_NO_OPEN_SESSION",
    "CHECKOUT_NO_EMPLOYEE",
    "OPERATOR_CHECKOUT_AUTHORIZATION",
    "AttendanceQueryService",
    "CheckoutRejected",
    "EmployeeQueryService",
    "EmployeePage",
]


# ---------------------------------------------------------------------------
# Business-refusal codes (stable machine surface; never a driver message)
# ---------------------------------------------------------------------------

#: The employee does not exist in the metadata store (route maps this to 404).
CHECKOUT_NO_EMPLOYEE: Final[str] = "EMPLOYEE_NOT_FOUND"
#: The employee exists but is deactivated (409).
CHECKOUT_EMPLOYEE_INACTIVE: Final[str] = "EMPLOYEE_INACTIVE"
#: The journal shows no open session for this employee (409).
CHECKOUT_NO_OPEN_SESSION: Final[str] = "NO_OPEN_SESSION"
#: The engine's ``check_out_cooldown_seconds`` window still applies (409).
CHECKOUT_COOLDOWN: Final[str] = "CHECKOUT_COOLDOWN"

#: The operator identity recorded for API-originated check-outs. This names the
#: **caller**, never an employee, and the engine refuses to label it as
#: biometric verification.
OPERATOR_CHECKOUT_AUTHORITY: Final[str] = "attendance_api"

#: The one authorizing object the API supplies. Built at import time (pure
#: validation, no I/O); it can never be constructed with the biometric source.
OPERATOR_CHECKOUT_AUTHORIZATION: Final[OperatorAuthorization] = OperatorAuthorization(
    operator=OPERATOR_CHECKOUT_AUTHORITY
)

#: Serializes the check-out critical section (journal read → decision → durable
#: append) inside this process.
#:
#: Why it exists: the journal load and the append are two operations, so without
#: this lock two concurrent check-out requests could both observe the same open
#: session and both append a ``CHECK_OUT``. Starlette runs synchronous endpoints
#: in a thread pool, so that overlap is reachable in one server process with no
#: second process involved. The lock is deliberately minimal — it covers only
#: this phase's operator check-out, adds no dependency, changes no journal byte
#: and weakens no validation — and its scope is stated honestly below.
#:
#: Scope (documented, never overclaimed): **process-local**. It does not
#: serialize other processes, and the store keeps its documented cross-process
#: disclaimer and its no-locking policy. The deployment is a single API process
#: by contract (the Windows single-process startup guard), so this is the whole
#: in-process race; a multi-process deployment would need a store-level lock and
#: is out of scope here.
_CHECKOUT_CRITICAL_SECTION: Final[threading.Lock] = threading.Lock()

#: Engine ``reason_code`` → stable API business-refusal code.
_CHECKOUT_BY_REASON_CODE: Final[dict[str, str]] = {
    REASON_INACTIVE_EMPLOYEE: CHECKOUT_EMPLOYEE_INACTIVE,
    REASON_NO_OPEN_SESSION: CHECKOUT_NO_OPEN_SESSION,
    REASON_CHECKOUT_COOLDOWN: CHECKOUT_COOLDOWN,
}

_CHECKOUT_MESSAGES: Final[dict[str, str]] = {
    CHECKOUT_NO_EMPLOYEE: "No employee exists for the requested identifier.",
    CHECKOUT_EMPLOYEE_INACTIVE: (
        "An inactive employee cannot check out."
    ),
    CHECKOUT_NO_OPEN_SESSION: (
        "No open check-in session exists for this employee; a check-out "
        "requires an open session."
    ),
    CHECKOUT_COOLDOWN: (
        "A check-out was recorded for this employee within the check-out "
        "cooldown window; this check-out is declined."
    ),
}


class CheckoutRejected(RuntimeError):
    """A check-out was refused by business policy (never by infrastructure).

    ``code`` is a stable machine value from the module's ``CHECKOUT_*``
    constants and ``message`` is a fixed sanitized sentence; neither is ever
    built from a driver error, a DSN, or a row value.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        self.message = _CHECKOUT_MESSAGES[code]
        super().__init__(f"{code}: {self.message}")


# ---------------------------------------------------------------------------
# Employee layer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmployeePage:
    """One keyset page plus the cursor a client needs to continue.

    ``next_after`` is the last ``employee_id`` of the page when the page is
    **full** (the client can ask for more), and ``None`` when the page is empty
    or short — a short page means the keyset walk is exhausted. It is derived,
    never guessed, and the repository still owns ordering and clamping.
    """

    employees: tuple[Employee, ...]
    limit: int
    next_after: str | None


class EmployeeQueryService:
    """API-shaped facade over the approved employee repository.

    Holds no SQL, no validation rules and no connection: every call delegates
    to the injected repository, whose documented errors
    (:class:`~src.employee_repository.EmployeeNotFound`,
    :class:`~src.employee_repository.EmployeeAlreadyExists`,
    :class:`~src.employee.EmployeeValidationError`, and the availability family)
    propagate unchanged for the route layer to map.
    """

    #: The five callables the approved repository contract provides. Checked
    #: structurally (the same injectable-double pattern the attendance engine
    #: uses for its clocks and lookups) so offline tests can supply a fake
    #: without the production class, while a genuinely wrong collaborator is
    #: still refused at construction.
    _REQUIRED = ("create", "get", "list", "update_full_name", "deactivate")

    def __init__(self, repository: object) -> None:
        missing = [
            name for name in self._REQUIRED
            if not callable(getattr(repository, name, None))
        ]
        if missing:
            raise TypeError(
                "repository must provide the approved EmployeeRepository "
                f"operations (missing: {', '.join(missing)}), got "
                f"{type(repository).__name__}"
            )
        self._repository = repository

    def create(self, employee: Employee) -> Employee:
        """Insert the validated model; duplicate IDs raise unchanged."""
        return self._repository.create(employee)

    def get(self, employee_id: str) -> Employee:
        """Return the employee or raise ``EmployeeNotFound``."""
        employee = self._repository.get(employee_id)
        if employee is None:
            from src.employee_repository import EmployeeNotFound

            raise EmployeeNotFound(
                "no employee exists for the requested identifier"
            )
        return employee

    def list(self, *, after: str | None = None, limit: int | None = None) -> EmployeePage:
        """Return one keyset page plus its continuation cursor."""
        resolved = PAGINATION.default_page_size if limit is None else limit
        effective = resolved if resolved < 1 else min(resolved, PAGINATION.max_page_size)
        employees = self._repository.list(after=after, limit=resolved)
        next_after = (
            employees[-1].employee_id
            if employees and len(employees) == effective
            else None
        )
        return EmployeePage(
            employees=tuple(employees), limit=effective, next_after=next_after
        )

    def rename(self, employee_id: str, full_name: str) -> Employee:
        """Update ``full_name`` only (validation stays in the model)."""
        return self._repository.update_full_name(employee_id, full_name)

    def deactivate(self, employee_id: str) -> Employee:
        """Set ``is_active = FALSE`` and return the read-back row."""
        return self._repository.deactivate(employee_id)


# ---------------------------------------------------------------------------
# Attendance layer
# ---------------------------------------------------------------------------


class AttendanceQueryService:
    """Journal reads and the operator check-out append.

    Args:
        store: The persistence authority (the existing
            :class:`~src.attendance_store.AttendanceEventStore` contract:
            callable ``load_events`` and ``append``). Injected so offline
            tests can drive a temporary journal.
        clock_wall: Injectable timezone-aware UTC clock for the appended
            event's ``occurred_at`` (default ``datetime.now(UTC)``). A naive
            return value is rejected by ``AttendanceEvent`` itself.
        event_id_factory: Injectable unique-ID producer (default
            ``uuid.uuid4().hex``); validated by ``AttendanceEvent``.
    """

    def __init__(
        self,
        store: object,
        *,
        clock_wall: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        load_events = getattr(store, "load_events", None)
        append = getattr(store, "append", None)
        if not callable(load_events) or not callable(append):
            raise TypeError(
                "store must provide callable load_events() and append(event) "
                f"(the AttendanceEventStore contract), got "
                f"{type(store).__name__}"
            )
        self._store = store
        self._clock_wall = clock_wall or (lambda: datetime.now(UTC))
        self._event_id_factory = event_id_factory or (lambda: uuid.uuid4().hex)

    # -- reads ---------------------------------------------------------

    def list_events(self) -> tuple[tuple[AttendanceEvent, ...], bool]:
        """Every validated journal event in append order, plus the torn flag.

        Delegates entirely to the store's ``load_events``: this method never
        repairs a torn tail, never skips a corrupt line, and never turns an
        unreadable journal into an empty history. Attendance has no pagination
        contract (``PaginationConfig`` is explicitly employee-only), so the
        whole validated journal is returned.
        """
        result = self._store.load_events()
        return (tuple(result.events), bool(result.incomplete_trailing_record))

    def latest_events(self) -> dict[str, AttendanceEvent]:
        """Per-employee folded latest event (the session authority)."""
        events, _ = self.list_events()
        return fold_latest_events(events)

    # -- the operator check-out ----------------------------------------

    def checkout(
        self,
        employee_id: str,
        *,
        employee_lookup: Callable[[str], Employee | None],
    ) -> AttendanceEvent:
        """Append one operator-authorized ``CHECK_OUT`` through the engine.

        The employee must exist (404 is this layer's concern). Every other rule
        — active status, open session, ``check_out_cooldown_seconds`` — is
        decided by :meth:`src.attendance.AttendanceEngine.operator_checkout`,
        so no copy of the policy lives here. A refusal appends **nothing**.

        Args:
            employee_id: Requested identifier (whitespace stripped, otherwise
                verbatim — the model's own normalization never runs here).
            employee_lookup: Authoritative status lookup (the repository). Must
                return an ``Employee`` or ``None``; anything else is a
                programming error and raises ``TypeError``.

        Returns:
            The appended :class:`~src.attendance.AttendanceEvent` — returned
            **only after** the store's durable append returned.

        Raises:
            CheckoutRejected: business refusal (unknown employee, inactive
                employee, no open session, check-out cooldown). The message is
                fixed and sanitized.
            TypeError: ``employee_lookup`` returned a non-``Employee``.
            src.attendance_store.CorruptAttendanceJournalError: the journal is
                unreadable or a record fails validation (propagated, so the
                route layer can answer its distinct unavailable state).
            src.attendance_store.AttendanceStorePersistenceError: the append
                failed; the record's persistence is unconfirmed and no retry is
                attempted.
            src.db.DatabaseUnavailableError: the metadata store could not be
                reached while looking the employee up.
        """
        if not isinstance(employee_id, str) or not employee_id.strip():
            # A blank identifier cannot name an employee; treat it as absent
            # rather than raising a contract error the API would have to
            # translate twice.
            raise CheckoutRejected(CHECKOUT_NO_EMPLOYEE)
        key = employee_id.strip()

        employee = employee_lookup(key)
        if employee is None:
            raise CheckoutRejected(CHECKOUT_NO_EMPLOYEE)
        if not isinstance(employee, Employee):
            raise TypeError(
                "employee_lookup must return an Employee or None, got "
                f"{type(employee).__name__}"
            )
        # Load → decide → append is one logical operation; the lock keeps a
        # second in-process request from observing the same open session.
        with _CHECKOUT_CRITICAL_SECTION:
            return self._check_out_under_lock(employee)

    def _check_out_under_lock(self, employee: Employee) -> AttendanceEvent:
        """The serialized check-out critical section (see the module docstring)."""
        events, _ = self.list_events()
        engine = self._operator_engine(employee, events)
        decision = engine.operator_checkout(
            employee.employee_id,
            authorization=OPERATOR_CHECKOUT_AUTHORIZATION,
        )

        if (
            decision.status is AttendanceDecisionStatus.EVENT_CREATED
            and decision.event is not None
        ):
            # Durable append is the success boundary: nothing is reported as a
            # check-out until the store's fsync has returned.
            self._store.append(decision.event)
            return decision.event

        code = _CHECKOUT_BY_REASON_CODE.get(decision.reason_code or "")
        if code is None:
            # AUTHORIZATION_REQUIRED / NOT_VERIFIED / any future status: this
            # service always authorizes and the engine never asks for biometric
            # evidence here, so reaching this line is a programming error and
            # must never be reported as a business refusal or a success.
            raise RuntimeError(
                "operator check-out invariant violated: decision status "
                f"{decision.status.value} with reason_code "
                f"{decision.reason_code!r}"
            )
        raise CheckoutRejected(code)

    def _operator_engine(
        self, employee: Employee, events: Sequence[AttendanceEvent]
    ) -> AttendanceEngine:
        """Build the engine state this one request needs, from the journal.

        The engine keeps its bounded state in memory, so a per-request instance
        starts empty. Both facts the check-out policy needs are therefore
        replayed from the folded journal authority — the open session (when the
        employee's latest event is a ``CHECK_IN``) and the last check-out
        instant (for the cooldown window) — through the engine's own documented
        restore methods, which keep expiry decisions inside the engine. Wall
        timestamps are converted to an elapsed-seconds age here, once; a
        future-dated record is clamped to zero per the project's clock-skew
        policy, and a wall timestamp is never stored as a monotonic value.
        """
        latest = fold_latest_events(events)
        engine = AttendanceEngine(
            config=ATTENDANCE,
            clock_wall=self._clock_wall,
            event_id_factory=self._event_id_factory,
            # The repository row this request already read is the active-status
            # authority; the engine invents nothing and issues no I/O.
            active_lookup=lambda employee_id: (
                employee_id == employee.employee_id and employee.is_active
            ),
            # Journal-derived session authority (the same fold
            # ``make_journal_open_session_lookup`` delegates to), computed once
            # from the events already loaded for this request.
            open_session_lookup=lambda employee_id: _has_open_session(
                latest, employee_id
            ),
        )
        now_wall = self._clock_wall()
        current = latest.get(employee.employee_id)
        if current is not None and current.event_type is EventType.CHECK_IN:
            engine.restore_open_session(
                employee.employee_id,
                elapsed_seconds=_elapsed_seconds(now_wall, current.occurred_at),
            )
        last_check_out = _latest_event_of_type(
            events, employee.employee_id, EventType.CHECK_OUT
        )
        if last_check_out is not None:
            engine.restore_check_out_anchor(
                employee.employee_id,
                elapsed_seconds=_elapsed_seconds(
                    now_wall, last_check_out.occurred_at
                ),
            )
        return engine


# ---------------------------------------------------------------------------
# Operator check-out helpers (pure; no I/O, no policy)
# ---------------------------------------------------------------------------


def _has_open_session(
    latest: dict[str, AttendanceEvent], employee_id: str
) -> bool:
    """Whether the folded journal's latest event for ``employee_id`` is a
    ``CHECK_IN`` (the same rule ``make_journal_open_session_lookup`` applies)."""
    state = latest.get(employee_id)
    return state is not None and state.event_type is EventType.CHECK_IN


def _elapsed_seconds(now: datetime, then: datetime) -> float:
    """Wall-clock age, clamped at zero for future-dated records."""
    delta = (now - then).total_seconds()
    return 0.0 if delta < 0.0 else delta


def _latest_event_of_type(
    events: Sequence[AttendanceEvent],
    employee_id: str,
    event_type: EventType,
) -> AttendanceEvent | None:
    """The employee's latest event of one type (latest timestamp, then
    later journal position — the fold's ordering rule)."""
    latest: AttendanceEvent | None = None
    for event in events:
        if event.employee_id != employee_id or event.event_type is not event_type:
            continue
        if latest is None or event.occurred_at >= latest.occurred_at:
            latest = event
    return latest


def employee_payload(employee: Employee) -> dict[str, object]:
    """Explicit four-field JSON shape for one employee (no extra keys)."""
    return {
        "employee_id": employee.employee_id,
        "full_name": employee.full_name,
        "is_active": employee.is_active,
        "created_at": (
            employee.created_at.isoformat() if employee.created_at else None
        ),
    }


def event_payload(event: AttendanceEvent) -> dict[str, object]:
    """Explicit five-field JSON shape for one attendance event."""
    return {
        "event_id": event.event_id,
        "employee_id": event.employee_id,
        "event_type": event.event_type.value,
        "occurred_at": event.occurred_at.isoformat(),
    }


def build_default_services(
    settings: AppSettings,
) -> tuple[EmployeeQueryService, AttendanceQueryService]:
    """Build the production services lazily (no connection, no filesystem).

    Imported inside the function so :mod:`src.api` keeps its documented
    import-hygiene contract: importing (or building the app for) the API pulls
    in no database driver, no attendance runtime and no camera stack.
    ``open_employee_repository`` connects nothing at construction, and
    ``AttendanceEventStore`` performs no filesystem work until a real append.
    """
    # Availability is checked *before* the driver is touched: with no
    # application target configured this raises the connection layer's typed
    # availability error, and ``src.db_employee_repository`` (and therefore
    # psycopg) is never imported. That keeps the API's documented offline
    # import-hygiene guarantee true for the request path too, not just for
    # ``import src.api``.
    from src.db import DatabaseUnavailableError

    if not isinstance(settings.database_url, str) or not settings.database_url.strip():
        raise DatabaseUnavailableError(
            "No application database is configured for the API."
        )

    from src.db_employee_repository import open_employee_repository

    return (
        EmployeeQueryService(open_employee_repository()),
        AttendanceQueryService(AttendanceEventStore(settings)),
    )
