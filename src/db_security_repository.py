"""PostgreSQL wiring for the Phase 5 identity repositories (P5.2).

The only thing this module does is bind :class:`src.user_repository.UserRepository`
and :class:`src.session_repository.SessionRepository` to the approved lazy
connection layer (:func:`src.db.open_connection`). It defines **no** SQL, no
second persistence implementation, no retry and no fallback source.

Import hygiene
--------------

Nothing here connects, reads a DSN or imports psycopg at module level:
``src.db`` (and therefore psycopg) is imported inside the default opener, on
the first real use, mirroring :mod:`src.db_employee_repository`.

Connection lifecycle (one operation = one connection)
-----------------------------------------------------

The scope below mirrors the employee wiring exactly: open, yield, ``commit()``
on success, ``rollback()`` when the body raises (the original exception
propagates unchanged), and always ``close()``. Cleanup never hides the real
failure: a rollback error is swallowed while another exception is in flight,
and a close error is attached as a note instead of replacing it.

Unlike the employee wiring there is no ``read_only`` mode: the identity
repositories are read/write by design (user creation, role/status updates,
session insert/touch/revoke) and no P5.2 caller performs a dry run. A future
milestone that needs one should add it deliberately rather than inherit an
unused flag here.

Trust boundary
--------------

``connection_opener`` is an ordinary in-process test seam; production callers
never pass it. It is not a defence against malicious code in the same
interpreter.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from src.session_repository import SessionRepository
from src.user_repository import UserRepository

__all__ = [
    "open_session_repository",
    "open_user_repository",
]


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
        failure.add_note(f"closing the connection also failed: {type(exc).__name__}")


def _default_opener(dsn: str | None) -> Any:
    """Real opener, imported lazily so this module stays psycopg-free."""
    from src.db import open_connection  # lazy: psycopg only on real use

    return open_connection(dsn)


@contextmanager
def _connection_scope(
    opener: Callable[[str | None], Any], dsn: str | None
) -> Iterator[Any]:
    """One operation's connection: commit or roll back, then always close."""
    connection = opener(dsn)
    failure: BaseException | None = None
    try:
        yield connection
        connection.commit()
    except BaseException as exc:  # includes KeyboardInterrupt: roll back first
        failure = exc
        _rollback_quietly(connection)
        raise
    finally:
        _close(connection, failure)


def open_user_repository(
    dsn: str | None = None,
    *,
    connection_opener: Callable[[str | None], Any] | None = None,
) -> UserRepository:
    """Build a :class:`UserRepository` over the PostgreSQL connection layer.

    No connection is opened here — the first repository call does that.
    ``dsn=None`` lets :func:`src.db.open_connection` resolve
    ``ATTENDANCE_DATABASE_URL`` on every attempt (never cached here).
    ``connection_opener`` is an offline-test seam.
    """
    opener = _default_opener if connection_opener is None else connection_opener
    return UserRepository(functools.partial(_connection_scope, opener, dsn))


def open_session_repository(
    dsn: str | None = None,
    *,
    connection_opener: Callable[[str | None], Any] | None = None,
) -> SessionRepository:
    """Build a :class:`SessionRepository` over the same connection layer."""
    opener = _default_opener if connection_opener is None else connection_opener
    return SessionRepository(functools.partial(_connection_scope, opener, dsn))
