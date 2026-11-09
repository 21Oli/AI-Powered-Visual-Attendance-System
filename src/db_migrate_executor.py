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
* the four fixed ``pg_catalog`` reads of :mod:`src.db_schema_catalog` (only
  when the caller asks :meth:`~PsycopgMigrationExecutor.verify_schema_locked`
  for a physical-schema assessment; static text, no interpolation)

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
* **One connection for the whole G2 startup verification**, owned by
  :meth:`~PsycopgMigrationExecutor.verify_schema_locked`: it takes the
  session lock, reads the snapshot and evaluates it on the same
  connection, then releases the lock, confirms the release and closes.
  The operation is additive (the six-method ABC is unchanged) precisely
  because the lock connection and the work connection above cannot be
  the same connection through the separate methods.
* **One connection for the combined G2+G8 verification**: the same method,
  given a ``catalog_probe``, reads the physical catalog on that connection
  **inside the same** ``REPEATABLE READ READ ONLY`` transaction, so the
  migration history and the physical facts come from one consistent snapshot.
  There is no second connection, transaction or lock for the catalog.

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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from src.db_migrate import (
    ADVISORY_LOCK_KEY,
    LEDGER_TABLE,
    MigrationCleanupError,
    MigrationContentionError,
    MigrationError,
    MigrationExecutor,
    MigrationIntegrityError,
    MigrationSnapshot,
)
from src.db_schema_catalog import CatalogProbeError, CatalogSnapshot
from src.physical_schema import SchemaEvaluation, evaluate_physical_schema

__all__ = [
    "LockedSchemaVerification",
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


@dataclass(frozen=True)
class LockedSchemaVerification:
    """Combined G2 + G8 result from **one** locked read-only verification.

    Returned by :meth:`PsycopgMigrationExecutor.verify_schema_locked` only when
    the caller supplied a ``catalog_probe``; the default call keeps returning
    the bare :class:`~src.db_migrate.MigrationSnapshot`, so every existing G2
    caller is unaffected.

    ``migration`` is the history snapshot G2 evaluated and ``physical`` is the
    immutable S3 evaluation of the catalog facts read from the *same*
    transaction. Neither field carries SQL, driver text, a DSN or a credential.
    """

    migration: MigrationSnapshot
    physical: SchemaEvaluation


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


def _finish_verification(connection: Any, *, lock_held: bool) -> Exception | None:
    """Release the verification lock (confirmed) and close; never raises.

    Returns the cleanup failure, or ``None`` when both the release and the
    close succeeded. The ``pg_advisory_unlock`` result is **required to be
    ``True``**: a ``False`` means PostgreSQL did not release the key, so
    reporting success would be a false claim (G2 contract).

    The caller decides how to report a failure: a note on an exception in
    flight, or the returned typed error when nothing else failed.
    """
    release_failure: Exception | None = None
    if lock_held:
        try:
            row = connection.execute(_UNLOCK_SQL, (ADVISORY_LOCK_KEY,)).fetchone()
            if not (row is not None and row[0] is True):
                release_failure = MigrationCleanupError(
                    "the schema-verification advisory lock release could not "
                    "be confirmed"
                )
        except Exception as exc:
            error = MigrationCleanupError(
                "the schema-verification advisory lock could not be released"
            )
            error.__cause__ = exc
            release_failure = error

    close_failure: Exception | None = None
    try:
        connection.close()
    except Exception as exc:
        error = MigrationCleanupError(
            "the schema-verification connection could not be closed"
        )
        error.__cause__ = exc
        close_failure = error

    if release_failure is not None:
        if close_failure is not None:
            release_failure.add_note(
                f"closing the connection also failed "
                f"({type(close_failure).__name__})"
            )
        return release_failure
    return close_failure


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

    def _read_snapshot_on(
        self, connection: Any, *, validate_ledger_shape: bool = False
    ) -> MigrationSnapshot:
        """Read the migration snapshot on a **caller-owned** connection.

        Shared by :meth:`read_snapshot` (which owns and closes its own
        connection) and :meth:`verify_schema_locked` (which already holds the
        one verification connection), so the snapshot SQL exists exactly once.
        The caller is responsible for rolling back a failed transaction and for
        closing the connection; this helper only issues the statements.

        ``validate_ledger_shape`` adds the existing ledger-shape query to the
        same read-only transaction. It is opt-in because ``read_snapshot``'s
        ``status`` behaviour is an established contract; the startup gate
        needs it to distinguish a malformed ledger from an unavailable one.
        """
        connection.execute(_BEGIN_SNAPSHOT_SQL)
        snapshot = self._read_snapshot_statements(
            connection, validate_ledger_shape=validate_ledger_shape
        )
        connection.execute(_ROLLBACK_SQL)
        return snapshot

    def _read_snapshot_statements(
        self, connection: Any, *, validate_ledger_shape: bool = False
    ) -> MigrationSnapshot:
        """Issue the snapshot reads **inside an already-open transaction**.

        Shared by :meth:`_read_snapshot_on` (which owns the ``BEGIN`` /
        ``ROLLBACK`` pair) and by the combined G2+G8 verification in
        :meth:`verify_schema_locked`, which must read the physical catalog
        inside the *same* transaction and therefore cannot let this helper open
        or end it. Statement order is identical in both callers: ledger
        presence, optional ledger shape, tables, applied rows.
        """
        row = connection.execute(_LEDGER_PRESENT_SQL).fetchone()
        ledger_present = row is not None and row[0] is not None

        if validate_ledger_shape and ledger_present:
            # Refuses a malformed ledger before its rows are interpreted, so
            # the refusal is a typed integrity error rather than a driver
            # failure on the row query.
            self._validate_ledger_shape(connection)

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

        return MigrationSnapshot(
            tables=tables, ledger_present=ledger_present, applied=applied
        )

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
            return self._read_snapshot_on(connection)
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

    def verify_schema_locked(
        self,
        evaluate: Callable[[MigrationSnapshot], None],
        *,
        catalog_probe: Callable[[Any], CatalogSnapshot] | None = None,
        physical_evaluator: Callable[[CatalogSnapshot], SchemaEvaluation] = (
            evaluate_physical_schema
        ),
    ) -> MigrationSnapshot | LockedSchemaVerification:
        """Lock, snapshot, evaluate, unlock and close on **one** connection.

        G2's startup gate must hold the advisory lock across the snapshot *and*
        the evaluation. :meth:`acquire_advisory_lock` and
        :meth:`read_snapshot` cannot express that: each owns a different
        connection and the snapshot method closes its own, so sharing one is
        impossible without changing its ownership contract. This additive
        concrete-class operation keeps that contract intact and reuses the same
        statements, the same snapshot reader and the same lock key.

        Sequence (any deviation is a refusal, never a retry):

        1. one connection, ``autocommit`` on;
        2. ``pg_try_advisory_lock`` — ``False`` →
           :class:`~src.db_migrate.MigrationContentionError`;
        3. the read-only ``REPEATABLE READ`` snapshot **on that connection**,
           including the ledger-shape check;
        4. ``evaluate(snapshot)`` while the lock is still held (it may raise a
           typed refusal, which propagates unchanged);
        5. ``pg_advisory_unlock`` with its boolean **required** to be ``True``;
        6. close, and only then return.

        A cleanup failure never masks an exception already in flight (G7 scope:
        ``Exception``): it is attached as a note and the primary failure —
        including a refusal raised by ``evaluate`` — is preserved.

        Combined physical verification (G8 S4)
        -------------------------------------
        Supplying ``catalog_probe`` (normally
        :func:`src.db_schema_catalog.read_public_catalog`) adds the physical
        read to that same transaction and returns a
        :class:`LockedSchemaVerification`. The order is contractual:

        1. one connection, ``autocommit`` on, **one** advisory-lock attempt;
        2. **one** ``REPEATABLE READ READ ONLY`` transaction;
        3. the migration snapshot, reusing the G2 read statements;
        4. ``evaluate(snapshot)`` — G2 **first**: a history refusal raises
           before a single catalog statement runs, so a physical result can
           never pre-empt or mask a G2 refusal;
        5. ``catalog_probe(connection)`` on the same connection, still inside
           that transaction, so history and catalog share one snapshot;
        6. ``physical_evaluator(catalog)`` (pure; the default is
           :func:`src.physical_schema.evaluate_physical_schema`);
        7. ``ROLLBACK`` ends the transaction, then the confirmed unlock and
           close described above.

        A probe failure is :class:`~src.db_schema_catalog.CatalogProbeError`;
        it is re-raised as a fixed-message :class:`MigrationDriverError` with
        the cause preserved, so no partial snapshot and no raw driver text
        leaves this boundary. Without ``catalog_probe`` the method is the G2
        operation it has always been and returns the bare
        :class:`~src.db_migrate.MigrationSnapshot`.
        """
        if catalog_probe is not None and not callable(catalog_probe):
            raise TypeError("catalog_probe must be callable")
        if not callable(physical_evaluator):
            raise TypeError("physical_evaluator must be callable")

        connection = self._opener(self._dsn)  # availability errors propagate
        primary: BaseException | None = None
        lock_held = False
        try:
            connection.autocommit = True
            row = connection.execute(
                _TRY_LOCK_SQL, (ADVISORY_LOCK_KEY,)
            ).fetchone()
            if not (row is not None and row[0] is True):
                raise MigrationContentionError(
                    "another migration run holds the advisory lock; refusing "
                    "to verify the schema (fail-fast, by contract)"
                )
            lock_held = True

            if catalog_probe is None:
                try:
                    snapshot = self._read_snapshot_on(
                        connection, validate_ledger_shape=True
                    )
                except MigrationError:
                    raise
                except Exception as exc:
                    raise MigrationDriverError(
                        "the migration state snapshot could not be read"
                    ) from exc

                evaluate(snapshot)
                return snapshot

            connection.execute(_BEGIN_SNAPSHOT_SQL)
            try:
                snapshot = self._read_snapshot_statements(
                    connection, validate_ledger_shape=True
                )
            except MigrationError:
                raise
            except Exception as exc:
                raise MigrationDriverError(
                    "the migration state snapshot could not be read"
                ) from exc

            # G2 precedence: nothing is probed unless the history is valid.
            evaluate(snapshot)

            try:
                catalog = catalog_probe(connection)
            except CatalogProbeError as exc:
                raise MigrationDriverError(
                    "the physical schema catalog could not be read for "
                    "verification"
                ) from exc

            physical = physical_evaluator(catalog)

            try:
                connection.execute(_ROLLBACK_SQL)
            except MigrationError:
                raise
            except Exception as exc:
                raise MigrationDriverError(
                    "the schema verification transaction could not be ended"
                ) from exc

            return LockedSchemaVerification(
                migration=snapshot, physical=physical
            )
        except BaseException as exc:
            primary = exc
            raise
        finally:
            _rollback_quietly(connection)
            cleanup_error = _finish_verification(connection, lock_held=lock_held)
            if cleanup_error is not None:
                if primary is not None:
                    primary.add_note(
                        "schema-verification cleanup also failed "
                        f"({type(cleanup_error).__name__}); the advisory lock "
                        "may still be held"
                    )
                else:
                    raise cleanup_error

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
