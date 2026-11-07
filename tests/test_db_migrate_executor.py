"""Offline tests for the real migration executor and the read-only state reader
(Phase 3 — Step 1.5b-6).

Everything here is fake-driven: a recording connection stands in for psycopg,
so every statement, parameter tuple, commit, rollback and close is observed.
No database is contacted.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.db import DatabaseUnavailableError
from src.db_migrate import (
    ADVISORY_LOCK_KEY,
    MigrationChecksumError,
    MigrationIntegrityError,
    MigrationSnapshot,
    load_migrations,
    read_state,
)
from src.db_migrate_executor import (
    MigrationDriverError,
    MigrationTargetError,
    PsycopgMigrationExecutor,
)

DSN = "postgresql://tester:super-secret@localhost:5432/migration_db"

EXPECTED_SHAPE = [
    ("version", "integer", "NO"),
    ("checksum", "text", "NO"),
    ("applied_at", "timestamp with time zone", "NO"),
]


# ---------------------------------------------------------------------------
# Recording fakes
# ---------------------------------------------------------------------------


class Result:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class FakeConnection:
    def __init__(
        self,
        handler,
        *,
        close_error: Exception | None = None,
        rollback_error: Exception | None = None,
    ) -> None:
        self.handler = handler
        self.autocommit = False
        self.statements: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self.close_error = close_error
        self.rollback_error = rollback_error

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return Result(self.handler(sql, params))

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def make_handler(
    *,
    ledger_present: bool = False,
    tables: tuple[str, ...] = (),
    applied: tuple[tuple[int, str], ...] = (),
    shape: list[tuple] | None = None,
    lock_result: bool = True,
    fail: tuple[str, Exception] | None = None,
):
    effective_shape = EXPECTED_SHAPE if shape is None else shape

    def handle(sql: str, params: object):
        if fail is not None and fail[0] in sql:
            raise fail[1]
        if "to_regclass" in sql:
            return [("public.schema_version",)] if ledger_present else [(None,)]
        if "pg_try_advisory_lock" in sql:
            return [(lock_result,)]
        if "pg_advisory_unlock" in sql:
            return [(True,)]
        if "information_schema.columns" in sql:
            return list(effective_shape)
        if "pg_class" in sql:
            return [(name,) for name in tables]
        if "SELECT version, checksum" in sql:
            return list(applied)
        return []

    return handle


def make_opener(handler, *, close_error=None, rollback_error=None):
    connections: list[FakeConnection] = []

    def opener(dsn: str) -> FakeConnection:
        connection = FakeConnection(
            handler, close_error=close_error, rollback_error=rollback_error
        )
        connections.append(connection)
        return connection

    return opener, connections


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dsn", ["", "   ", None, 42])
def test_constructor_requires_an_explicit_dsn(dsn: object) -> None:
    called: list[str] = []
    with pytest.raises(MigrationTargetError):
        PsycopgMigrationExecutor(dsn, connection_opener=lambda value: called.append(value))
    assert called == []


def test_constructor_opens_nothing() -> None:
    opened: list[str] = []
    PsycopgMigrationExecutor(DSN, connection_opener=lambda value: opened.append(value))
    assert opened == []


def test_repr_never_reveals_the_dsn() -> None:
    text = repr(PsycopgMigrationExecutor(DSN))
    assert "super-secret" not in text
    assert "postgresql://" not in text


# ---------------------------------------------------------------------------
# Advisory lock lifecycle
# ---------------------------------------------------------------------------


def test_acquire_takes_the_documented_key_on_an_autocommit_connection() -> None:
    opener, connections = make_opener(make_handler(lock_result=True))
    executor = PsycopgMigrationExecutor(DSN, connection_opener=opener)

    assert executor.acquire_advisory_lock() is True

    connection = connections[0]
    assert connection.autocommit is True
    assert connection.statements == [
        ("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    ]
    assert connection.closed == 0  # stays open for the whole run


def test_acquire_refusal_closes_the_connection_and_holds_nothing() -> None:
    opener, connections = make_opener(make_handler(lock_result=False))
    executor = PsycopgMigrationExecutor(DSN, connection_opener=opener)

    assert executor.acquire_advisory_lock() is False
    assert connections[0].closed == 1


def test_acquire_is_idempotent_and_opens_no_second_session() -> None:
    opener, connections = make_opener(make_handler())
    executor = PsycopgMigrationExecutor(DSN, connection_opener=opener)

    assert executor.acquire_advisory_lock() is True
    assert executor.acquire_advisory_lock() is True
    assert len(connections) == 1


def test_acquire_driver_failure_is_typed_and_closes() -> None:
    opener, connections = make_opener(
        make_handler(fail=("pg_try_advisory_lock", RuntimeError("driver boom")))
    )
    executor = PsycopgMigrationExecutor(DSN, connection_opener=opener)

    with pytest.raises(MigrationDriverError) as excinfo:
        executor.acquire_advisory_lock()
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "driver boom" not in str(excinfo.value)
    assert connections[0].closed == 1


def test_acquire_availability_error_propagates_unchanged() -> None:
    def opener(dsn: str):
        raise DatabaseUnavailableError("no server")

    executor = PsycopgMigrationExecutor(DSN, connection_opener=opener)
    with pytest.raises(DatabaseUnavailableError):
        executor.acquire_advisory_lock()


def test_release_unlocks_then_closes() -> None:
    opener, connections = make_opener(make_handler())
    executor = PsycopgMigrationExecutor(DSN, connection_opener=opener)
    executor.acquire_advisory_lock()

    executor.release_advisory_lock()

    connection = connections[0]
    assert connection.statements[-1] == (
        "SELECT pg_advisory_unlock(%s)",
        (ADVISORY_LOCK_KEY,),
    )
    assert connection.closed == 1


def test_release_is_idempotent_and_safe_before_acquire() -> None:
    opener, connections = make_opener(make_handler())
    executor = PsycopgMigrationExecutor(DSN, connection_opener=opener)

    executor.release_advisory_lock()  # never acquired: silent no-op
    executor.release_advisory_lock()

    executor.acquire_advisory_lock()
    executor.release_advisory_lock()
    statements_before = len(connections[0].statements)
    executor.release_advisory_lock()  # already closed: silent no-op

    assert len(connections) == 1
    assert len(connections[0].statements) == statements_before
    assert connections[0].closed == 1


def test_release_failure_is_typed_but_still_closes() -> None:
    opener, connections = make_opener(
        make_handler(fail=("pg_advisory_unlock", RuntimeError("unlock boom")))
    )
    executor = PsycopgMigrationExecutor(DSN, connection_opener=opener)
    executor.acquire_advisory_lock()

    with pytest.raises(MigrationDriverError):
        executor.release_advisory_lock()
    assert connections[0].closed == 1


def test_release_close_failure_is_typed() -> None:
    opener, _ = make_opener(make_handler(), close_error=OSError("cannot close"))
    executor = PsycopgMigrationExecutor(DSN, connection_opener=opener)
    executor.acquire_advisory_lock()

    with pytest.raises(MigrationDriverError):
        executor.release_advisory_lock()


# ---------------------------------------------------------------------------
# Ledger creation and validation
# ---------------------------------------------------------------------------


def test_ensure_ledger_creates_only_when_absent() -> None:
    opener, connections = make_opener(make_handler(ledger_present=False))
    PsycopgMigrationExecutor(DSN, connection_opener=opener).ensure_ledger()

    connection = connections[0]
    assert "to_regclass" in connection.statements[0][0]
    assert any(sql.startswith("CREATE TABLE IF NOT EXISTS") for sql, _ in connection.statements)
    assert connection.commits == 1
    assert connection.closed == 1


def test_ensure_ledger_present_issues_no_ddl_and_validates_shape() -> None:
    opener, connections = make_opener(make_handler(ledger_present=True))
    PsycopgMigrationExecutor(DSN, connection_opener=opener).ensure_ledger()

    connection = connections[0]
    assert not any(sql.startswith("CREATE TABLE") for sql, _ in connection.statements)
    assert any("information_schema.columns" in sql for sql, _ in connection.statements)
    assert connection.commits == 0
    assert connection.closed == 1


@pytest.mark.parametrize(
    "shape",
    [
        EXPECTED_SHAPE[:2],  # missing column
        [("version", "text", "NO")] + EXPECTED_SHAPE[1:],  # wrong type
        list(reversed(EXPECTED_SHAPE)),  # wrong order
        [("version", "integer", "YES")] + EXPECTED_SHAPE[1:],  # nullable
    ],
)
def test_ensure_ledger_refuses_a_divergent_shape_without_writing(shape) -> None:
    opener, connections = make_opener(make_handler(ledger_present=True, shape=shape))

    with pytest.raises(MigrationIntegrityError):
        PsycopgMigrationExecutor(DSN, connection_opener=opener).ensure_ledger()

    connection = connections[0]
    assert not any(sql.startswith("CREATE TABLE") for sql, _ in connection.statements)
    assert connection.commits == 0
    assert connection.closed == 1


def test_ensure_ledger_driver_failure_rolls_back_closes_and_is_typed() -> None:
    opener, connections = make_opener(
        make_handler(fail=("CREATE TABLE", RuntimeError("ddl boom")))
    )

    with pytest.raises(MigrationDriverError) as excinfo:
        PsycopgMigrationExecutor(DSN, connection_opener=opener).ensure_ledger()

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert connections[0].rollbacks >= 1
    assert connections[0].closed == 1


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_list_tables_returns_a_frozenset_and_closes() -> None:
    opener, connections = make_opener(make_handler(tables=("employee", "schema_version")))
    tables = PsycopgMigrationExecutor(DSN, connection_opener=opener).list_tables()

    assert tables == frozenset({"employee", "schema_version"})
    assert "relkind IN ('r', 'p')" in connections[0].statements[0][0]
    assert connections[0].closed == 1


def test_fetch_applied_converts_rows_and_closes() -> None:
    opener, connections = make_opener(make_handler(applied=((1, "abc"),)))
    rows = PsycopgMigrationExecutor(DSN, connection_opener=opener).fetch_applied()

    assert rows == ((1, "abc"),)
    assert connections[0].closed == 1


# ---------------------------------------------------------------------------
# Apply: statement order, atomicity, rollback
# ---------------------------------------------------------------------------


def test_apply_runs_sql_then_ledger_insert_and_commits_once() -> None:
    opener, connections = make_opener(make_handler())
    applied_at = datetime.now(UTC)

    PsycopgMigrationExecutor(DSN, connection_opener=opener).apply_migration(
        1, "CREATE TABLE employee (employee_id TEXT);", "checksum-1", applied_at
    )

    connection = connections[0]
    assert connection.statements[0] == (
        "CREATE TABLE employee (employee_id TEXT);",
        None,
    )
    assert connection.statements[1][0].startswith("INSERT INTO schema_version")
    assert connection.statements[1][1] == (1, "checksum-1", applied_at)
    assert connection.autocommit is False
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed == 1


def test_apply_rolls_back_when_the_migration_sql_fails() -> None:
    opener, connections = make_opener(
        make_handler(fail=("CREATE TABLE employee", RuntimeError("ddl boom")))
    )

    with pytest.raises(MigrationDriverError) as excinfo:
        PsycopgMigrationExecutor(DSN, connection_opener=opener).apply_migration(
            1, "CREATE TABLE employee (x INT);", "c", datetime.now(UTC)
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    connection = connections[0]
    assert connection.rollbacks == 1
    assert connection.commits == 0
    assert connection.closed == 1


def test_apply_rolls_back_when_the_ledger_insert_fails() -> None:
    opener, connections = make_opener(
        make_handler(fail=("INSERT INTO schema_version", RuntimeError("insert boom")))
    )

    with pytest.raises(MigrationDriverError):
        PsycopgMigrationExecutor(DSN, connection_opener=opener).apply_migration(
            1, "SELECT 1;", "c", datetime.now(UTC)
        )

    assert connections[0].rollbacks == 1
    assert connections[0].commits == 0
    assert connections[0].closed == 1


def test_close_failure_never_masks_the_primary_exception() -> None:
    opener, connections = make_opener(
        make_handler(fail=("CREATE TABLE employee", RuntimeError("primary boom"))),
        close_error=OSError("close boom"),
    )

    with pytest.raises(MigrationDriverError) as excinfo:
        PsycopgMigrationExecutor(DSN, connection_opener=opener).apply_migration(
            1, "CREATE TABLE employee (x INT);", "c", datetime.now(UTC)
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    notes = getattr(excinfo.value, "__notes__", [])
    assert any("closing the connection also failed" in note for note in notes)
    assert connections[0].closed == 1


def test_close_failure_with_nothing_in_flight_is_raised() -> None:
    opener, _ = make_opener(make_handler(), close_error=OSError("close boom"))

    with pytest.raises(MigrationDriverError):
        PsycopgMigrationExecutor(DSN, connection_opener=opener).apply_migration(
            1, "SELECT 1;", "c", datetime.now(UTC)
        )


def test_rollback_failure_never_masks_the_primary_exception() -> None:
    opener, connections = make_opener(
        make_handler(fail=("CREATE TABLE employee", RuntimeError("primary boom"))),
        rollback_error=OSError("rollback boom"),
    )

    with pytest.raises(MigrationDriverError) as excinfo:
        PsycopgMigrationExecutor(DSN, connection_opener=opener).apply_migration(
            1, "CREATE TABLE employee (x INT);", "c", datetime.now(UTC)
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert connections[0].closed == 1


# ---------------------------------------------------------------------------
# Snapshot reading
# ---------------------------------------------------------------------------


def test_read_snapshot_uses_one_read_only_repeatable_read_transaction() -> None:
    opener, connections = make_opener(
        make_handler(ledger_present=False, tables=("employee",))
    )

    snapshot = PsycopgMigrationExecutor(DSN, connection_opener=opener).read_snapshot()

    connection = connections[0]
    statements = [sql for sql, _ in connection.statements]
    assert statements == [
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY",
        "SELECT to_regclass('public.schema_version')",
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')",
        "ROLLBACK",
    ]
    assert snapshot == MigrationSnapshot(
        tables=frozenset({"employee"}), ledger_present=False, applied=()
    )
    assert connection.commits == 0
    assert connection.closed == 1


def test_read_snapshot_reads_the_ledger_only_when_present() -> None:
    opener, connections = make_opener(
        make_handler(ledger_present=True, tables=("employee",), applied=((1, "abc"),))
    )

    snapshot = PsycopgMigrationExecutor(DSN, connection_opener=opener).read_snapshot()

    assert snapshot.ledger_present is True
    assert snapshot.applied == ((1, "abc"),)
    assert [sql for sql, _ in connections[0].statements] == [
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY",
        "SELECT to_regclass('public.schema_version')",
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')",
        "SELECT version, checksum FROM schema_version",
        "ROLLBACK",
    ]


def test_read_snapshot_failure_rolls_back_closes_and_is_typed() -> None:
    opener, connections = make_opener(
        make_handler(fail=("SELECT c.relname", RuntimeError("catalog boom")))
    )

    with pytest.raises(MigrationDriverError):
        PsycopgMigrationExecutor(DSN, connection_opener=opener).read_snapshot()

    assert connections[0].rollbacks == 1
    assert connections[0].closed == 1


# ---------------------------------------------------------------------------
# read_state: pure derivation
# ---------------------------------------------------------------------------


def test_read_state_reports_pending_on_a_pristine_target() -> None:
    state = read_state(
        MigrationSnapshot(tables=frozenset(), ledger_present=False, applied=())
    )

    assert state.ledger_present is False
    assert state.previous_version == 0
    assert state.applied == ()
    assert state.pending == (1,)


def test_read_state_reports_up_to_date_for_the_real_v001() -> None:
    script = load_migrations()[0]
    state = read_state(
        MigrationSnapshot(
            tables=frozenset({"employee", "schema_version"}),
            ledger_present=True,
            applied=((script.version, script.checksum),),
        )
    )

    assert state.previous_version == 1
    assert state.pending == ()


def test_read_state_refuses_employee_without_a_ledger() -> None:
    with pytest.raises(MigrationIntegrityError):
        read_state(
            MigrationSnapshot(
                tables=frozenset({"employee"}), ledger_present=False, applied=()
            )
        )


def test_read_state_refuses_an_inconsistent_snapshot() -> None:
    with pytest.raises(MigrationIntegrityError):
        read_state(
            MigrationSnapshot(
                tables=frozenset({"employee"}),
                ledger_present=False,
                applied=((1, "abc"),),
            )
        )


def test_read_state_delegates_checksum_detection(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "migrations" / "V001__employee.sql"
    (tmp_path / "V001__employee.sql").write_text(
        source.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8"
    )

    with pytest.raises(MigrationChecksumError):
        read_state(
            MigrationSnapshot(
                tables=frozenset({"employee", "schema_version"}),
                ledger_present=True,
                applied=((1, "stale-checksum"),),
            ),
            migrations_dir=tmp_path,
        )


# ---------------------------------------------------------------------------
# Import hygiene
# ---------------------------------------------------------------------------


def test_import_hygiene_fresh_process() -> None:
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys, src.db_migrate_executor as m;"
        "print('psycopg' in sys.modules, 'src.db' in sys.modules, "
        "m.PsycopgMigrationExecutor.__name__)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split()[:2] == ["False", "False"]
