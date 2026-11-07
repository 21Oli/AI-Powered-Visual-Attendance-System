"""Real psycopg-backed migration executor (Phase 3 — Step 1.5b-6).

Implements the six-method :class:`src.db_migrate.MigrationExecutor` contract
against a real PostgreSQL connection, plus one method the runner does not
need: :meth:`PsycopgMigrationExecutor.read_snapshot`, which reads the
migration-relevant state from a single ``REPEATABLE READ READ ONLY``
transaction for the read-only ``status`` command.

What this module may execute
----------------------------
Exactly six statements and one transaction-control pair, all static text:

* ``SELECT pg_try_advisory_lock(%s)`` / ``SELECT pg_advisory_unlock(%s)``
* ``SELECT to_regclass('public.schema_version')``
* ``CREATE TABLE IF NOT EXISTS schema_version (...)`` (only when absent)
* ``SELECT column_name, data_type, is_nullable FROM information_schema.columns``
* ``SELECT relname FROM pg_class ...`` (public tables/partitioned tables)
* ``SELECT version, checksum FROM schema_version``
* ``INSERT INTO schema_version (version, checksum, applied_at) VALUES (%s, %s, %s)``
* ``BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY`` / ``ROLLBACK``

Migration SQL itself is passed through verbatim (no splitting, no
interpolation, no parameters), so psycopg uses the simple-query path and
PostgreSQL runs the file's statements inside the transaction this executor
opened — that is what makes "DDL and ledger row commit together" true.

Connection ownership
--------------------
* **One dedicated lock connection**, opened by
  :meth:`~PsycopgMigrationExecutor.acquire_advisory_lock`, kept open for the
  whole run (session-scoped lock), and closed by
  :meth:`~PsycopgMigrationExecutor.release_advisory_lock`.
* **One short-lived work connection per call** for ``ensure_ledger``,
  ``list_tables``, ``fetch_applied``, ``apply_migration`` and
  ``read_snapshot``. Every one is closed on every path.

Target discipline
-----------------
The constructor requires an explicit, non-blank DSN and there is **no
default**: :func:`src.db.open_connection` falls back to
``ATTENDANCE_DATABASE_URL`` when given ``None``, so a default here could
silently retarget a caller. A blank target is refused before any connection.

Error discipline
----------------
* ``DatabaseUnavailableError`` / ``MalformedDsnError`` from the connection
  layer propagate **unchanged** (availability vs. caller-value contract).
* Unexpected driver failures become :class:`MigrationDriverError` (a
  :class:`~src.db_migrate.MigrationError`), with a fixed message and the
  original exception preserved as ``__cause__``. Raw driver text never
  reaches a user-facing message.
* **Primary exceptions are never masked** by cleanup when the executor
  handles the error (an ordinary ``Exception``): a failed ``rollback`` is
  swallowed while another exception propagates; a failed ``close`` is
  attached as a note to that exception instead of replacing it; and a
  failed ``close`` with nothing else in flight is raised as
  :class:`MigrationDriverError` (a leaked connection is a fault, not a
  footnote). Known limitation **G7**: this guarantee is scoped to
  ``Exception``. A ``BaseException`` that is not an ``Exception`` (e.g.
  ``SystemExit``) is not caught, so no cleanup failure is recorded against
  it — and if the final close then also fails, that
  :class:`MigrationDriverError` replaces the propagating ``BaseException``.
  ``acquire_advisory_lock`` likewise closes its connection only on
  ``Exception``, and the pre-existing ``migrate()`` cleanup path catches
  only ``Exception`` from ``release_advisory_lock``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Final

from src.db_migrate import (
    ADVISORY_LOCK_KEY,
    LEDGER_TABLE,
    MigrationError,
    MigrationExecutor,
    MigrationIntegrityError,
    MigrationSnapshot,
)

__all__ = [
    "MigrationDriverError",
    "MigrationTargetError",
    "PsycopgMigrationExecutor",
]


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class MigrationTargetError(MigrationError):
    """The migration target is unusable before any statement runs.

    Configuration-class: no explicit DSN, or a target the caller cannot name
    unambiguously. The CLI maps this to its configuration exit code.
    """


class MigrationDriverError(MigrationError):
    """The database driver failed unexpectedly.

    Infrastructure-class. Messages are fixed, sanitized strings; the original
    driver exception is preserved as ``__cause__`` for trusted diagnostics.
    """


# ---------------------------------------------------------------------------
# Fixed statements (static text; the ledger name is a module constant)
# ---------------------------------------------------------------------------

_TRY_LOCK_SQL: Final[str] = "SELECT pg_try_advisory_lock(%s)"
_UNLOCK_SQL: Final[str] = "SELECT pg_advisory_unlock(%s)"

_LEDGER_PRESENT_SQL: Final[str] = f"SELECT to_regclass('public.{LEDGER_TABLE}')"

_CREATE_LEDGER_SQL: Final[str] = (
    f"CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} ("
    "version INTEGER NOT NULL PRIMARY KEY, "
    "checksum TEXT NOT NULL, "
    "applied_at TIMESTAMPTZ NOT NULL)"
)

_LEDGER_SHAPE_SQL: Final[str] = (
    "SELECT column_name, data_type, is_nullable "
    "FROM information_schema.columns "
    f"WHERE table_schema = 'public' AND table_name = '{LEDGER_TABLE}' "
    "ORDER BY ordinal_position"
)

#: The documented ledger shape (DB_DESIGN.md Section 3.1), in column order.
_EXPECTED_LEDGER_SHAPE: Final[tuple[tuple[str, str, str], ...]] = (
    ("version", "integer", "NO"),
    ("checksum", "text", "NO"),
    ("applied_at", "timestamp with time zone", "NO"),
)

_TABLES_SQL: Final[str] = (
    "SELECT c.relname FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
)

_APPLIED_SQL: Final[str] = f"SELECT version, checksum FROM {LEDGER_TABLE}"

_INSERT_LEDGER_SQL: Final[str] = (
    f"INSERT INTO {LEDGER_TABLE} (version, checksum, applied_at) "
    "VALUES (%s, %s, %s)"
)

_BEGIN_SNAPSHOT_SQL: Final[str] = (
    "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
)
_ROLLBACK_SQL: Final[str] = "ROLLBACK"


# ---------------------------------------------------------------------------
# Cleanup helpers (mirror src/db_employee_repository.py's discipline)
# ---------------------------------------------------------------------------


def _rollback_quietly(connection: Any) -> None:
    """End a failed transaction; never mask the exception in flight."""
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
        try:
            close()
        except Exception as exc:
            raise MigrationDriverError(
                "the migration connection could not be closed"
            ) from exc
        return
    try:
        close()
    except Exception as exc:  # pragma: no cover - driver-specific path
        failure.add_note(
            f"closing the connection also failed: {type(exc).__name__}"
        )


def _default_opener(dsn: str) -> Any:
    """Open a real connection, importing the driver lazily."""
    from src.db import open_connection  # lazy: module import stays driver-free

    return open_connection(dsn)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class PsycopgMigrationExecutor(MigrationExecutor):
    """The runner's real database seam (one operation = one connection).

    Parameters
    ----------
    dsn:
        The explicit PostgreSQL target. Required and non-blank: there is no
        fallback to the configured application DSN, because that fallback is
        exactly how a test-target run could reach the wrong database.
    connection_opener:
        Offline-test seam; defaults to the lazily imported
        :func:`src.db.open_connection`. Production callers never pass it.
    """

    def __init__(
        self,
        dsn: str,
        *,
        connection_opener: Callable[[str], Any] | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise MigrationTargetError(
                "an explicit, non-blank PostgreSQL DSN is required to build "
                "the migration executor"
            )
        self._dsn = dsn.strip()
        self._opener = _default_opener if connection_opener is None else connection_opener
        self._lock_connection: Any | None = None
        self._lock_held = False

    def __repr__(self) -> str:  # never reveal the DSN or credentials
        return "PsycopgMigrationExecutor(<redacted target>)"

    # -- Option B lock lifecycle ------------------------------------------

    def acquire_advisory_lock(self) -> bool:
        """Acquire the session-scoped lock on a dedicated connection.

        Nonblocking, never retried. ``False`` leaves **nothing** open: the
        freshly opened connection is closed before returning, so a refused
        run holds no connection and no lock.
        """
        if self._lock_connection is not None:
            # Idempotent: the runner calls this once, but a repeat must not
            # open a second session (which could deadlock on its own lock).
            return True

        connection = self._opener(self._dsn)  # availability errors propagate
        try:
            connection.autocommit = True
            row = connection.execute(
                _TRY_LOCK_SQL, (ADVISORY_LOCK_KEY,)
            ).fetchone()
        except Exception as exc:
            error = MigrationDriverError(
                "the migration advisory lock could not be requested"
            )
            _close(connection, error)
            raise error from exc

        if not (row is not None and row[0] is True):
            _close(connection, None)  # nothing held, nothing left open
            return False

        self._lock_connection = connection
        self._lock_held = True
        return True

    def release_advisory_lock(self) -> None:
        """Release the key and close the dedicated connection.

        Idempotent and no-op safe: never acquired, not held, already closed,
        or a second call all return silently. A genuine failure is raised
        **after** the close, so the runner can report it without masking a
        primary failure.
        """
        connection, self._lock_connection = self._lock_connection, None
        held, self._lock_held = self._lock_held, False
        if connection is None:
            return

        release_failure: Exception | None = None
        try:
            if held:
                connection.execute(_UNLOCK_SQL, (ADVISORY_LOCK_KEY,))
        except Exception as exc:
            release_failure = exc

        close_failure: Exception | None = None
        try:
            connection.close()
        except Exception as exc:
            close_failure = exc

        if release_failure is not None:
            raise MigrationDriverError(
                "the migration advisory lock could not be released"
            ) from release_failure
        if close_failure is not None:
            raise MigrationDriverError(
                "the migration lock connection could not be closed"
            ) from close_failure

    # -- ledger ------------------------------------------------------------

    def ensure_ledger(self) -> None:
        """Create the ledger only when it is absent; validate it otherwise.

        The presence check comes first by contract: a role without ``CREATE``
        must still be able to run a no-op provisioning pass on a database
        whose ledger already exists. When the ledger is present its documented
        column structure is verified and a divergence is a typed integrity
        refusal, never a silent repair.
        """
        connection = self._opener(self._dsn)
        failure: BaseException | None = None
        try:
            row = connection.execute(_LEDGER_PRESENT_SQL).fetchone()
            present = row is not None and row[0] is not None
            if present:
                self._validate_ledger_shape(connection)
                _rollback_quietly(connection)
                return
            connection.execute(_CREATE_LEDGER_SQL)
            connection.commit()
        except MigrationError:
            failure = None
            _rollback_quietly(connection)
            raise
        except Exception as exc:
            _rollback_quietly(connection)
            error = MigrationDriverError(
                "the migration ledger could not be created"
            )
            failure = error
            raise error from exc
        finally:
            _close(connection, failure)

    def _validate_ledger_shape(self, connection: Any) -> None:
        """Refuse a ledger whose columns/types are not the documented shape."""
        rows = connection.execute(_LEDGER_SHAPE_SQL).fetchall()
        shape = tuple(
            (str(row[0]).lower(), str(row[1]).lower(), str(row[2]).upper())
            for row in rows
        )
        if shape != _EXPECTED_LEDGER_SHAPE:
            raise MigrationIntegrityError(
                f"{LEDGER_TABLE} does not have the documented column "
                "structure (version, checksum, applied_at); refusing to run"
            )

    # -- state reads -------------------------------------------------------

    def list_tables(self) -> frozenset[str]:
        """Return the ordinary and partitioned table names in schema public."""
        connection = self._opener(self._dsn)
        failure: BaseException | None = None
        try:
            rows = connection.execute(_TABLES_SQL).fetchall()
            return frozenset(str(row[0]) for row in rows)
        except Exception as exc:
            error = MigrationDriverError(
                "the existing tables could not be listed"
            )
            failure = error
            raise error from exc
        finally:
            _close(connection, failure)

    def fetch_applied(self) -> Sequence[tuple[int, str]]:
        """Return the ``(version, checksum)`` rows recorded in the ledger."""
        connection = self._opener(self._dsn)
        failure: BaseException | None = None
        try:
            rows = connection.execute(_APPLIED_SQL).fetchall()
            return tuple((int(row[0]), str(row[1])) for row in rows)
        except Exception as exc:
            error = MigrationDriverError(
                "the applied migration ledger could not be read"
            )
            failure = error
            raise error from exc
        finally:
            _close(connection, failure)

    def read_snapshot(self) -> MigrationSnapshot:
        """Read tables, ledger presence and ledger rows from **one** snapshot.

        Not part of the runner's contract (the ABC is unchanged): ``status``
        needs a consistent read, and separate connections cannot provide one
        while a runner is committing.

        The transaction is server-side read-only and ``REPEATABLE READ``, so
        all three reads share one MVCC snapshot and the ledger query is issued
        only when that same snapshot reported the ledger present. The
        transaction is always ended with ``ROLLBACK`` and the connection is
        always closed; nothing is ever written and the advisory lock is not
        taken.
        """
        connection = self._opener(self._dsn)
        failure: BaseException | None = None
        try:
            connection.autocommit = True
            connection.execute(_BEGIN_SNAPSHOT_SQL)

            row = connection.execute(_LEDGER_PRESENT_SQL).fetchone()
            ledger_present = row is not None and row[0] is not None

            tables = frozenset(
                str(table_row[0])
                for table_row in connection.execute(_TABLES_SQL).fetchall()
            )

            applied: tuple[tuple[int, str], ...] = ()
            if ledger_present:
                applied = tuple(
                    (int(applied_row[0]), str(applied_row[1]))
                    for applied_row in connection.execute(_APPLIED_SQL).fetchall()
                )

            connection.execute(_ROLLBACK_SQL)
            return MigrationSnapshot(
                tables=tables, ledger_present=ledger_present, applied=applied
            )
        except MigrationError:
            _rollback_quietly(connection)
            raise
        except Exception as exc:
            _rollback_quietly(connection)
            error = MigrationDriverError(
                "the migration state snapshot could not be read"
            )
            failure = error
            raise error from exc
        finally:
            _close(connection, failure)

    # -- apply -------------------------------------------------------------

    def apply_migration(
        self,
        version: int,
        sql_text: str,
        checksum: str,
        applied_at: datetime,
    ) -> None:
        """Run one migration file and record its ledger row, atomically.

        The file's SQL is executed **verbatim and parameterless** (psycopg's
        simple-query path), then the ledger insert runs in the same
        transaction; a failure at either step rolls the whole transaction
        back, so a half-applied schema with a recorded version is impossible.
        """
        connection = self._opener(self._dsn)
        failure: BaseException | None = None
        try:
            connection.autocommit = False
            connection.execute(sql_text)
            connection.execute(
                _INSERT_LEDGER_SQL, (version, checksum, applied_at)
            )
            connection.commit()
        except MigrationError:
            _rollback_quietly(connection)
            raise
        except Exception as exc:
            _rollback_quietly(connection)
            error = MigrationDriverError(
                f"migration {version} could not be applied"
            )
            failure = error
            raise error from exc
        finally:
            _close(connection, failure)
