"""PostgreSQL wiring for the employee-metadata repository (Phase 3 — Step 1.5c-3a).

The *only* thing this module does is bind the approved
:class:`~src.employee_repository.EmployeeRepository` to the approved lazy
connection layer (:func:`src.db.open_connection`). It defines **no SQL**,
no second persistence implementation, no retry, and no fallback source —
the repository keeps sole ownership of the statements and of the typed
error taxonomy.

Import hygiene
--------------

Nothing here connects, reads a DSN, or imports psycopg at module level:
``src.db`` (and therefore psycopg) is imported inside the default opener,
on the first real use. Importing this module is safe for validation-only
CLI paths and ordinary application startup.

Connection and transaction lifecycle (one operation = one connection)
---------------------------------------------------------------------

Every repository call acquires a **fresh** connection through the scope
below and the scope owns the whole lifecycle:

1. open the connection (availability failures surface unchanged as
   :class:`~src.db.DatabaseUnavailableError`);
2. for a read-only repository, enforce ``read_only`` **before the caller
   can issue any statement** (see below);
3. yield the connection to the repository's single ``execute`` call;
4. ``commit()`` on the success path (a read-only transaction commits as a
   no-op — the server has already refused any write);
5. ``rollback()`` when the body raises, so a failed transaction is never
   left open — and the original exception propagates unchanged (its type
   is the importer's stop reason);
6. always ``close()``, on every path.

Cleanup never hides the real failure: a rollback error is swallowed while
another exception is propagating, and a close error is attached as a note
to that propagating exception instead of replacing it. A close error with
nothing else in flight is raised, because a leaked connection is a real
fault rather than a footnote.

Read-only dry runs
------------------

The importer's dry run may read existing rows to classify records but must
never write. The wiring enforces that **server-side** for
``read_only=True``: psycopg's ``Connection.read_only`` is set before the
first statement and read back, so PostgreSQL refuses any write the code
might attempt. A connection whose read-only mode cannot be set or does not
report back is refused (fail closed) — "only SELECTs are sent" is not
treated as equivalent to enforced read-only.

Trust boundary
--------------

``read_only`` and ``connection_opener`` are ordinary in-process arguments:
this is a fail-closed mistake guard for callers, not a defence against
malicious code running in the same interpreter. The production CLI passes
``read_only=not apply``; ``connection_opener`` exists so offline tests can
observe the lifecycle without a server (it defaults to the real lazily
imported opener).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Final

from src.employee_repository import EmployeeRepository

__all__ = [
    "EmployeeRepositoryWiringError",
    "open_employee_repository",
]

#: Bounded, credential-free wording for every read-only refusal. The
#: refusal never echoes a DSN, a driver message, or a row value.
_READ_ONLY_SET_FAILED: Final[str] = (
    "the connection could not be put in read-only mode; refusing to run a "
    "dry-run query"
)

_READ_ONLY_NOT_REPORTED: Final[str] = (
    "the connection did not report read-only mode after it was requested; "
    "refusing to run a dry-run query"
)


class EmployeeRepositoryWiringError(RuntimeError):
    """The wiring contract was violated before any statement ran.

    Raised for a non-boolean ``read_only`` and for a connection that
    cannot be positively verified as read-only. Deliberately **not** an
    availability error: the caller asked for something the wiring cannot
    guarantee, so it fails closed instead of guessing.
    """


def _enforce_read_only(connection: Any) -> None:
    """Put the connection in read-only mode and verify it took effect.

    Mirrors :func:`src.test_database_guard._enforce_read_only`: the mode is
    requested on the driver object and then read back, because the
    contract is *enforced* read-only rather than asserted read-only.
    """
    try:
        connection.read_only = True
    except Exception:
        raise EmployeeRepositoryWiringError(_READ_ONLY_SET_FAILED) from None
    if getattr(connection, "read_only", None) is not True:
        raise EmployeeRepositoryWiringError(_READ_ONLY_NOT_REPORTED)


def _rollback_quietly(connection: Any) -> None:
    """End the failed transaction; never mask the exception in flight."""
    rollback = getattr(connection, "rollback", None)
    if rollback is None:
        return
    try:
        rollback()
    except Exception:
        pass


def _close(connection: Any, failure: BaseException | None) -> None:
    """Close the connection, never replacing an exception in flight."""
    close = getattr(connection, "close", None)
    if close is None:
        return
    if failure is None:
        close()
        return
    try:
        close()
    except Exception as exc:  # pragma: no cover - driver-specific path
        # Preserve the primary failure; keep the cleanup fault observable.
        failure.add_note(f"closing the connection also failed: {type(exc).__name__}")


def _default_opener(dsn: str | None) -> Any:
    """Real opener, imported lazily so this module stays psycopg-free.

    ``dsn=None`` deliberately leaves resolution to
    :func:`src.db.open_connection`, which re-reads
    ``ATTENDANCE_DATABASE_URL`` on every call (never cached here).
    """
    from src.db import open_connection  # lazy: psycopg only on real use

    return open_connection(dsn)


@contextmanager
def _connection_scope(
    opener: Callable[[str | None], Any],
    dsn: str | None,
    *,
    read_only: bool,
) -> Iterator[Any]:
    """One operation's connection: commit or roll back, then always close."""
    connection = opener(dsn)
    failure: BaseException | None = None
    try:
        if read_only:
            _enforce_read_only(connection)
        yield connection
        connection.commit()
    except BaseException as exc:  # includes KeyboardInterrupt: roll back first
        failure = exc
        _rollback_quietly(connection)
        raise
    finally:
        _close(connection, failure)


def open_employee_repository(
    dsn: str | None = None,
    *,
    read_only: bool = False,
    connection_opener: Callable[[str | None], Any] | None = None,
) -> EmployeeRepository:
    """Build the approved repository over the PostgreSQL connection layer.

    Parameters
    ----------
    dsn:
        Explicit DSN, or ``None`` to resolve
        ``ATTENDANCE_DATABASE_URL`` at each connection attempt (the
        connection layer's documented behaviour). No connection is opened
        here — the first repository call does that.
    read_only:
        ``True`` for the importer's dry run: every connection is put in
        server-enforced read-only mode before any statement runs.
    connection_opener:
        Test seam, defaulting to the lazily imported
        :func:`src.db.open_connection`. Production callers never pass it.

    Returns
    -------
    EmployeeRepository
        A repository whose connection factory satisfies the documented
        repository contract (one context-managed connection per call).

    Raises
    ------
    EmployeeRepositoryWiringError
        If ``read_only`` is not a real boolean.
    """
    if not isinstance(read_only, bool):
        raise EmployeeRepositoryWiringError(
            "read_only must be a real boolean, got "
            f"{type(read_only).__name__}"
        )
    opener = _default_opener if connection_opener is None else connection_opener
    return EmployeeRepository(
        functools.partial(_connection_scope, opener, dsn, read_only=read_only)
    )
