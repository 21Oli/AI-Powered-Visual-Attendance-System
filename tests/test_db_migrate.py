"""Phase 3, Step 1.5b — offline migration-runner tests (``src/db_migrate.py``).

Everything here runs against **fake executors and temporary
directories only** — no PostgreSQL connection, no driver import, no
real runtime data (repository gate 1.5b restriction).

Coverage (authorized matrix):
discovery/ordering, empty directory, invalid/duplicate versions,
checksum generation + detection of modified applied migrations,
inconsistent ledger rows, missing scripts, sequence gaps,
advisory-lock contention (fail-fast, no inference, nothing after),
transaction failure aborting the run, prohibited statements, integrity
refusal (employee table without ledger), naive-timestamp refusal,
sanitized error text, and the fresh-process import-hygiene contract.

Option B (H-3) lock lifecycle — session-scoped advisory lock on a
dedicated connection: acquisition before every state check, exclusion of
a second simulated runner through shared fake server state, the lock
observed as *held* during each state check and each per-migration
transaction, per-migration commit granularity with earlier commits
surviving a later failure, and release/close on success, typed failure,
unexpected exception, and failed acquisition — plus cleanup-failure
reporting that never masks the original error.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from src.db_migrate import (
    ADVISORY_LOCK_KEY,
    MigrationApplicationError,
    MigrationChecksumError,
    MigrationCleanupError,
    MigrationContentionError,
    MigrationError,
    MigrationExecutor,
    MigrationFileError,
    MigrationGapError,
    MigrationIntegrityError,
    MigrationMissingScriptError,
    load_migrations,
    migrate,
)

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)

V001_SQL = "CREATE TABLE employee (\n    employee_id TEXT PRIMARY KEY,\n    full_name   TEXT NOT NULL,\n    is_active   BOOLEAN NOT NULL DEFAULT TRUE,\n    created_at  TIMESTAMPTZ NOT NULL\n);\n"
V002_SQL = "CREATE TABLE demo_two (id INTEGER);\n"


def write_migration(directory: Path, version: int, name: str, sql: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"V{version}__{name}.sql"
    path.write_text(sql, encoding="utf-8")
    return path


class FakeLockServer:
    """Shared stand-in for **one** database server's advisory-lock state.

    Two fake executors built over the same server model two runners
    against the same database: the session lock belongs to whoever took
    it, and a second ``try_lock`` fails while it is held (approved
    Option B semantics, ``pg_try_advisory_lock``).
    """

    def __init__(self) -> None:
        self.held_by: object | None = None

    def try_lock(self, owner: object) -> bool:
        if self.held_by is not None:
            return False
        self.held_by = owner
        return True

    def unlock(self, owner: object) -> None:
        # Only the owner releases; a non-owner release is a silent no-op,
        # exactly like ``pg_advisory_unlock`` for a lock you do not hold.
        if self.held_by is owner:
            self.held_by = None


class FakeExecutor(MigrationExecutor):
    """In-memory executor implementing the documented contract, including
    the transactional-atomicity shape of ``apply_migration`` and the
    Option B dedicated-lock-connection lifecycle.

    The lock is a real piece of shared state (:class:`FakeLockServer`),
    not a flag: every state call and every migration application records
    whether the *server* still reports this executor as the holder, so
    "the lock covered the run" is observed, never merely asserted.
    """

    def __init__(
        self,
        *,
        lock_available: bool = True,
        tables: frozenset[str] = frozenset(),
        ledger: dict[int, str] | None = None,
        fail_at_version: int | None = None,
        lock_server: FakeLockServer | None = None,
        acquire_error: Exception | None = None,
        release_error: Exception | None = None,
        fetch_error: Exception | None = None,
    ) -> None:
        self.lock_available = lock_available
        self.tables = set(tables)
        self.ledger: dict[int, str] = dict(ledger or {})
        self.fail_at_version = fail_at_version
        self.calls: list[tuple[str, int | None]] = []
        self.ensure_ledger_count = 0
        self.chaos = RuntimeError("synthetic failure inside the transaction")

        # --- Option B lock lifecycle state -------------------------------
        self.server = lock_server if lock_server is not None else FakeLockServer()
        #: The dedicated lock connection exists (opened at acquisition).
        self.lock_connection_open = False
        #: This executor's view of whether it holds the session lock.
        self.lock_held = False
        self.release_count = 0
        self.acquire_error = acquire_error
        self.release_error = release_error
        self.fetch_error = fetch_error
        #: Which calls observed the session lock genuinely held, in order.
        self.locked_calls: list[str] = []
        #: Versions whose transaction committed (one entry per migration).
        self.commits: list[int] = []

    def _record(self, name: str, version: int | None = None) -> None:
        self.calls.append((name, version))
        if self.lock_held and name in (
            "ensure_ledger",
            "list_tables",
            "fetch_applied",
            "apply",
        ):
            self.locked_calls.append(name)

    def acquire_advisory_lock(self) -> bool:
        self.calls.append(("acquire", None))
        if self.acquire_error is not None:
            # The dedicated connection was opened, then acquisition blew
            # up: cleanup must still be attempted by the runner.
            self.lock_connection_open = True
            raise self.acquire_error
        if not self.lock_available:
            return False  # nothing opened, nothing held (contract)
        self.lock_connection_open = True
        self.lock_held = self.server.try_lock(self)
        return self.lock_held

    def release_advisory_lock(self) -> None:
        self.calls.append(("release", None))
        self.release_count += 1
        if self.release_error is not None:
            raise self.release_error
        # Idempotent and no-op safe by contract: works when the lock was
        # never taken and when the connection is already closed.
        self.server.unlock(self)
        self.lock_held = False
        self.lock_connection_open = False

    def ensure_ledger(self) -> None:
        self._record("ensure_ledger")
        self.tables.add("schema_version")  # runner-owned materialization
        self.ensure_ledger_count += 1

    def list_tables(self) -> frozenset[str]:
        self._record("list_tables")
        return frozenset(self.tables)

    def fetch_applied(self) -> list[tuple[int, str]]:
        self._record("fetch_applied")
        if self.fetch_error is not None:
            raise self.fetch_error
        return sorted(self.ledger.items())

    def apply_migration(
        self,
        version: int,
        sql_text: str,
        checksum: str,
        applied_at: datetime,
    ) -> None:
        self._record("apply", version)
        if self.fail_at_version == version:
            raise self.chaos  # transaction rolls back; no ledger change
        self.tables.update(
            part.strip() for part in ("employee",) if version == 1
        )
        self.ledger[version] = checksum
        self.commits.append(version)  # this migration's own COMMIT


def test_discovery_orders_by_version_deterministically(tmp_path: Path) -> None:
    write_migration(tmp_path, 2, "second", V002_SQL)
    write_migration(tmp_path, 1, "first", V001_SQL)
    scripts = load_migrations(tmp_path)
    assert [script.version for script in scripts] == [1, 2]
    assert scripts[0].name == "V1__first.sql"
    assert len(scripts[0].checksum) == 64  # sha256 hex digest


def test_discovery_tolerates_empty_directory(tmp_path: Path) -> None:
    assert load_migrations(tmp_path) == []


def test_discovery_tolerates_absent_directory(tmp_path: Path) -> None:
    assert load_migrations(tmp_path / "nope") == []


def test_unparseable_sql_filename_refused(tmp_path: Path) -> None:
    (tmp_path / "0042_employee.sql").write_text(V001_SQL, encoding="utf-8")
    with pytest.raises(MigrationFileError) as err:
        load_migrations(tmp_path)
    assert "0042_employee.sql" in str(err.value)
    assert str(tmp_path) not in str(err.value)  # no absolute paths


def test_duplicate_version_refused(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "one", V001_SQL)
    write_migration(tmp_path, 1, "another", V001_SQL)
    with pytest.raises(MigrationFileError) as err:
        load_migrations(tmp_path)
    assert "duplicate migration version 1" in str(err.value)


def test_zero_version_refused(tmp_path: Path) -> None:
    write_migration(tmp_path, 0, "zero", V001_SQL)
    with pytest.raises(MigrationFileError):
        load_migrations(tmp_path)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE DATABASE attendance;\n",
        "create   database attendance;\n",
        "CREATE\tDATABASE attendance;\n",
        "CREATE\n    DATABASE attendance;\n",
        "CREATE INDEX CONCURRENTLY idx ON employee (employee_id);\n",
        "create index concurrently idx ON employee (employee_id);\n",
        # H-2 audit bypasses: the UNIQUE form is equally non-transactional.
        "CREATE UNIQUE INDEX CONCURRENTLY idx ON employee (employee_id);\n",
        "create unique   index\n concurrently idx ON employee (employee_id);\n",
        # ... and a comment may not be used to split the keyword sequence.
        "CREATE UNIQUE INDEX /*x*/ CONCURRENTLY idx ON employee (employee_id);\n",
        "CREATE INDEX /* multi\n line */ CONCURRENTLY idx ON employee (employee_id);\n",
        "CREATE /*x*/ DATABASE attendance;\n",
        "CREATE\n-- sneaky comment\nDATABASE attendance;\n",
    ],
)
def test_prohibited_statements_refused(tmp_path: Path, sql: str) -> None:
    write_migration(tmp_path, 1, "bad", sql)
    with pytest.raises(MigrationFileError):
        load_migrations(tmp_path)


def test_prohibited_statement_error_names_no_file_or_sql(tmp_path: Path) -> None:
    write_migration(
        tmp_path, 1, "bad", "CREATE UNIQUE INDEX CONCURRENTLY i ON t (c);\n"
    )
    with pytest.raises(MigrationFileError) as err:
        load_migrations(tmp_path)
    text = str(err.value)
    assert str(tmp_path) not in text
    assert "idx" not in text
    assert "CREATE UNIQUE" not in text


def test_comment_mentioning_prohibited_statement_is_allowed(
    tmp_path: Path,
) -> None:
    # A comment (the repository's own documentation style) must not
    # false-match the prohibited-statement scanner: comments are
    # stripped before the scan, statements are not.
    write_migration(
        tmp_path,
        1,
        "commented",
        "-- create index concurrently is documented as prohibited above\n"
        "CREATE TABLE referenced (id INTEGER);\n",
    )
    assert [script.version for script in load_migrations(tmp_path)] == [1]


def test_checksum_is_line_ending_independent(tmp_path: Path) -> None:
    """M-4: LF and CRLF copies of identical SQL share one checksum."""
    lf_dir = tmp_path / "lf"
    crlf_dir = tmp_path / "crlf"
    lf_dir.mkdir()
    crlf_dir.mkdir()
    sql_lf = "CREATE TABLE t (a int);\nCREATE TABLE u (b int);\n"
    sql_crlf = sql_lf.replace("\n", "\r\n")
    (lf_dir / "V1__demo.sql").write_bytes(sql_lf.encode("utf-8"))
    (crlf_dir / "V1__demo.sql").write_bytes(sql_crlf.encode("utf-8"))
    lf_script = load_migrations(lf_dir)[0]
    crlf_script = load_migrations(crlf_dir)[0]
    assert lf_script.checksum == crlf_script.checksum


def test_checksum_ignores_byte_order_mark(tmp_path: Path) -> None:
    plain_dir = tmp_path / "plain"
    bom_dir = tmp_path / "bom"
    plain_dir.mkdir()
    bom_dir.mkdir()
    sql = "CREATE TABLE t (a int);\n"
    (plain_dir / "V1__demo.sql").write_bytes(sql.encode("utf-8"))
    (bom_dir / "V1__demo.sql").write_bytes(b"\xef\xbb\xbf" + sql.encode("utf-8"))
    assert (
        load_migrations(plain_dir)[0].checksum
        == load_migrations(bom_dir)[0].checksum
    )


def test_checksum_changes_with_meaningful_content(tmp_path: Path) -> None:
    """M-4: canonicalization must not make the digest content-blind."""
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    (before_dir / "V1__demo.sql").write_text(
        "CREATE TABLE t (a INTEGER);\n", encoding="utf-8"
    )
    (after_dir / "V1__demo.sql").write_text(
        "CREATE TABLE t (a BIGINT);\n", encoding="utf-8"
    )
    assert (
        load_migrations(before_dir)[0].checksum
        != load_migrations(after_dir)[0].checksum
    )


def test_checksum_survives_crlf_round_trip_of_the_real_v001(tmp_path: Path) -> None:
    """The shipped V001 migration keeps its checksum across a CRLF
    checkout, which is the operational scenario M-4 protects."""
    from src.db_migrate import DEFAULT_MIGRATIONS_DIR

    original = load_migrations(DEFAULT_MIGRATIONS_DIR)[0]
    crlf_path = tmp_path / "V1__employee.sql"
    crlf_path.write_bytes(
        original.sql_text.replace("\r\n", "\n").replace("\n", "\r\n").encode(
            "utf-8"
        )
    )
    assert load_migrations(tmp_path)[0].checksum == original.checksum


def test_first_run_applies_and_records_checksum(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "employee", V001_SQL)
    executor = FakeExecutor()
    outcome = migrate(executor, tmp_path, now=NOW)
    assert outcome.previous_version == 0
    assert outcome.applied_versions == (1,)
    assert outcome.current_version == 1
    # Acquisition first, release last, and every state/migration call in
    # between happened while the session lock was genuinely held.
    assert executor.calls == [
        ("acquire", None),
        ("ensure_ledger", None),
        ("list_tables", None),
        ("fetch_applied", None),
        ("apply", 1),
        ("release", None),
    ]
    assert executor.locked_calls == [
        "ensure_ledger",
        "list_tables",
        "fetch_applied",
        "apply",
    ]
    assert executor.ledger[1] == load_migrations(tmp_path)[0].checksum


def test_second_run_is_idempotent_no_rerun(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "employee", V001_SQL)
    script = load_migrations(tmp_path)[0]
    executor = FakeExecutor(ledger={1: script.checksum})
    outcome = migrate(executor, tmp_path, now=NOW)
    assert outcome.applied_versions == ()
    assert outcome.current_version == 1
    assert ("apply", 1) not in executor.calls  # never re-runnable


def test_modified_applied_migration_refused(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "employee", V001_SQL)
    script = load_migrations(tmp_path)[0]
    executor = FakeExecutor(ledger={1: "0" * 64})
    with pytest.raises(MigrationChecksumError) as err:
        migrate(executor, tmp_path, now=NOW)
    assert "checksum mismatch" in str(err.value)
    assert ("apply", 1) not in executor.calls
    assert V001_SQL not in str(err.value)


def test_applied_version_missing_its_script_refused(tmp_path: Path) -> None:
    executor = FakeExecutor(ledger={1: "0" * 64})
    with pytest.raises(MigrationMissingScriptError) as err:
        migrate(executor, tmp_path, now=NOW)
    assert "no migration file provides it" in str(err.value)


def test_employee_table_without_ledger_row_refused(tmp_path: Path) -> None:
    executor = FakeExecutor(tables={"employee"})
    with pytest.raises(MigrationIntegrityError) as err:
        migrate(executor, tmp_path, now=NOW)
    assert "no applied migration" in str(err.value)


def test_duplicate_ledger_rows_refused(tmp_path: Path) -> None:
    class DupLedgerExecutor(FakeExecutor):
        def fetch_applied(self) -> list[tuple[int, str]]:
            return sorted(self.ledger.items()) + list(self.ledger.items())

    write_migration(tmp_path, 1, "employee", V001_SQL)
    real_checksum = load_migrations(tmp_path)[0].checksum
    with pytest.raises(MigrationIntegrityError):
        migrate(
            DupLedgerExecutor(ledger={1: real_checksum}), tmp_path, now=NOW
        )


def test_noncontiguous_ledger_refused(tmp_path: Path) -> None:
    class GappedLedgerExecutor(FakeExecutor):
        def fetch_applied(self) -> list[tuple[int, str]]:
            return self.gapped_rows

    write_migration(tmp_path, 1, "employee", V001_SQL)
    write_migration(tmp_path, 2, "two", V002_SQL)
    write_migration(tmp_path, 3, "three", V002_SQL)
    scripts = {script.version: script for script in load_migrations(tmp_path)}
    executor = GappedLedgerExecutor(ledger={})
    executor.gapped_rows = [
        (1, scripts[1].checksum),
        (3, scripts[3].checksum),
    ]
    with pytest.raises(MigrationIntegrityError):
        migrate(executor, tmp_path, now=NOW)


def test_sequence_gap_refused_before_any_application(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "employee", V001_SQL)
    write_migration(tmp_path, 3, "third", V002_SQL)
    executor = FakeExecutor()
    with pytest.raises(MigrationGapError):
        migrate(executor, tmp_path, now=NOW)
    assert ("apply", 1) not in executor.calls  # refused before applying


def test_advisory_lock_failure_stops_everything(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "employee", V001_SQL)
    executor = FakeExecutor(lock_available=False)
    with pytest.raises(MigrationContentionError):
        migrate(executor, tmp_path, now=NOW)
    # Nothing is read or written: the only calls are the acquisition
    # attempt and the mandatory cleanup (Option B release in ``finally``).
    assert executor.calls == [("acquire", None), ("release", None)]
    assert executor.ensure_ledger_count == 0
    assert executor.ledger == {}


def test_lock_wins_and_uses_documented_fixed_key(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "employee", V001_SQL)
    executor = FakeExecutor()
    migrate(executor, tmp_path, now=NOW)
    assert ("acquire", None) == executor.calls[0]
    assert ADVISORY_LOCK_KEY > 0


def test_transaction_failure_aborts_run(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "employee", V001_SQL)
    write_migration(tmp_path, 2, "two", V002_SQL)
    executor = FakeExecutor(fail_at_version=1)
    with pytest.raises(MigrationApplicationError) as err:
        migrate(executor, tmp_path, now=NOW)
    assert "migration 1 failed" in str(err.value)
    assert executor.chaos is err.value.__cause__  # preserved, chain, not text
    assert executor.ledger == {}  # the failed transaction left nothing
    assert ("apply", 2) not in executor.calls  # later versions never attempted


def test_naive_now_refused(tmp_path: Path) -> None:
    from datetime import datetime as dt

    executor = FakeExecutor()
    with pytest.raises(ValueError):
        migrate(executor, tmp_path, now=dt(2026, 1, 1))
    # Argument validation precedes the lock: no acquisition, no release.
    assert executor.calls == []


def test_error_messages_never_leak_paths_or_sql(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "employee", V001_SQL)
    with pytest.raises(MigrationError) as modified:
        migrate(FakeExecutor(ledger={1: "0" * 64}), tmp_path, now=NOW)
    text = str(modified.value)
    # Sanitization: no filesystem paths and no file contents survive into
    # the typed error text (only the version number and reason phrase).
    assert str(tmp_path) not in text
    assert tmp_path.name not in text
    assert "CREATE TABLE" not in text
    assert "employee_id" not in text


def test_file_validation_error_never_reaches_the_executor(
    tmp_path: Path,
) -> None:
    """File-name/content validation happens before any executor call."""
    (tmp_path / "not_a_migration.sql").write_text(V001_SQL, encoding="utf-8")
    executor = FakeExecutor()
    with pytest.raises(MigrationFileError):
        migrate(executor, tmp_path, now=NOW)
    assert executor.calls == []


def test_sequence_gap_is_detected_after_lock_acquisition(
    tmp_path: Path,
) -> None:
    """A *gap* is a database-state problem, not a file-name error: the
    advisory lock is taken first (fail-fast ordering) and the gap is then
    detected before any migration is applied."""
    write_migration(tmp_path, 2, "skip_will_fail", V001_SQL)
    executor = FakeExecutor()
    with pytest.raises(MigrationGapError):
        migrate(executor, tmp_path, now=NOW)
    assert executor.calls[0] == ("acquire", None)
    assert all(call[0] != "apply" for call in executor.calls)


def test_import_hygiene_fresh_process() -> None:
    """Importing the module must not load any driver/attendance stack nor
    attempt any connection (fresh subprocess proof per repository pattern)."""
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "import src.db_migrate;"
        "banned = {'psycopg', 'cv2', 'fastapi', 'uvicorn',"
        "'src.db', 'src.attendance', 'src.attendance_store'};"
        "loaded = banned & set(sys.modules);"
        "assert not loaded, f'unexpected imports: {loaded}';"
        "from src.db_migrate import DEFAULT_MIGRATIONS_DIR as d;"
        "assert d.name == 'migrations', d;"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")


# ---------------------------------------------------------------------------
# Option B (H-3): session-scoped advisory lock on a dedicated connection
# ---------------------------------------------------------------------------


def test_lock_is_acquired_before_any_state_check(tmp_path: Path) -> None:
    """Ordering, not just presence: acquisition is the first executor
    call, and the ledger is not even created before it."""
    write_migration(tmp_path, 1, "employee", V001_SQL)
    write_migration(tmp_path, 2, "two", V002_SQL)
    executor = FakeExecutor()

    migrate(executor, tmp_path, now=NOW)

    assert executor.calls[0] == ("acquire", None)
    first_state_call = next(
        index
        for index, (name, _) in enumerate(executor.calls)
        if name in ("ensure_ledger", "list_tables", "fetch_applied", "apply")
    )
    assert first_state_call == 1  # immediately after acquisition


def test_lock_is_released_once_and_last_on_success(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "employee", V001_SQL)
    executor = FakeExecutor()

    migrate(executor, tmp_path, now=NOW)

    assert executor.calls[-1] == ("release", None)
    assert executor.release_count == 1
    assert executor.lock_held is False
    assert executor.lock_connection_open is False
    assert executor.server.held_by is None


def test_second_runner_cannot_pass_state_checks_while_first_holds(
    tmp_path: Path,
) -> None:
    """Two runners, one fake server: the second is refused before it can
    read or write any migration state, and its cleanup must not drop the
    first runner's lock."""
    write_migration(tmp_path, 1, "employee", V001_SQL)
    server = FakeLockServer()
    first = FakeExecutor(lock_server=server)
    second = FakeExecutor(lock_server=server)

    assert first.acquire_advisory_lock() is True  # first runner owns it

    with pytest.raises(MigrationContentionError):
        migrate(second, tmp_path, now=NOW)

    assert second.calls == [("acquire", None), ("release", None)]
    assert second.ensure_ledger_count == 0
    assert second.ledger == {}
    assert all(name != "apply" for name, _ in second.calls)
    # Contention is symmetric: the refused runner's release is a no-op,
    # so the lock is still the first runner's.
    assert server.held_by is first
    assert first.lock_held is True

    first.release_advisory_lock()
    assert server.held_by is None


def test_lock_held_across_every_state_check_and_transaction(
    tmp_path: Path,
) -> None:
    """The session lock is *observed held* through all state checks and
    every per-migration transaction — the whole run, not one statement."""
    write_migration(tmp_path, 1, "employee", V001_SQL)
    write_migration(tmp_path, 2, "two", V002_SQL)
    write_migration(tmp_path, 3, "three", V002_SQL)
    executor = FakeExecutor()

    outcome = migrate(executor, tmp_path, now=NOW)

    assert outcome.applied_versions == (1, 2, 3)
    assert executor.locked_calls == [
        "ensure_ledger",
        "list_tables",
        "fetch_applied",
        "apply",
        "apply",
        "apply",
    ]
    # Per-migration transactions, not one outer transaction.
    assert executor.commits == [1, 2, 3]


def test_each_migration_keeps_its_own_transaction_and_ledger_row(
    tmp_path: Path,
) -> None:
    write_migration(tmp_path, 1, "employee", V001_SQL)
    write_migration(tmp_path, 2, "two", V002_SQL)
    executor = FakeExecutor()

    migrate(executor, tmp_path, now=NOW)

    scripts = {script.version: script for script in load_migrations(tmp_path)}
    assert executor.commits == [1, 2]
    assert executor.ledger == {
        1: scripts[1].checksum,
        2: scripts[2].checksum,
    }


def test_later_failure_preserves_earlier_committed_migrations(
    tmp_path: Path,
) -> None:
    """Approved Option B behaviour: a failure at V2 must not roll back
    V1, which already committed in its own transaction."""
    write_migration(tmp_path, 1, "employee", V001_SQL)
    write_migration(tmp_path, 2, "two", V002_SQL)
    write_migration(tmp_path, 3, "three", V002_SQL)
    scripts = {script.version: script for script in load_migrations(tmp_path)}
    executor = FakeExecutor(fail_at_version=2)

    with pytest.raises(MigrationApplicationError):
        migrate(executor, tmp_path, now=NOW)

    assert executor.commits == [1]  # V1 stayed committed
    assert executor.ledger == {1: scripts[1].checksum}
    assert ("apply", 3) not in executor.calls  # later versions never tried
    assert executor.calls[-1] == ("release", None)  # cleanup still ran
    assert executor.lock_connection_open is False


def test_lock_released_after_typed_refusal(tmp_path: Path) -> None:
    """A typed failure raised by a *state check* (not a migration) also
    releases the lock and closes the dedicated connection."""
    write_migration(tmp_path, 1, "employee", V001_SQL)
    executor = FakeExecutor(ledger={1: "0" * 64})  # checksum mismatch

    with pytest.raises(MigrationChecksumError):
        migrate(executor, tmp_path, now=NOW)

    assert executor.release_count == 1
    assert executor.lock_held is False
    assert executor.lock_connection_open is False


def test_lock_released_after_unexpected_exception(tmp_path: Path) -> None:
    """A non-typed, unexpected exception propagates unchanged — and the
    cleanup path still runs."""
    write_migration(tmp_path, 1, "employee", V001_SQL)
    boom = RuntimeError("synthetic: connection dropped during state check")
    executor = FakeExecutor(fetch_error=boom)

    with pytest.raises(RuntimeError) as err:
        migrate(executor, tmp_path, now=NOW)

    assert err.value is boom  # never translated or wrapped
    assert executor.release_count == 1
    assert executor.lock_connection_open is False


def test_cleanup_attempted_when_acquisition_itself_fails(
    tmp_path: Path,
) -> None:
    """Acquisition opening a connection and then failing must still get
    the cleanup call (the executor cannot safely release on its own)."""
    write_migration(tmp_path, 1, "employee", V001_SQL)
    boom = RuntimeError("synthetic: lock statement failed")
    executor = FakeExecutor(acquire_error=boom)

    with pytest.raises(RuntimeError) as err:
        migrate(executor, tmp_path, now=NOW)

    assert err.value is boom
    assert executor.release_count == 1
    assert executor.ensure_ledger_count == 0
    assert all(name != "apply" for name, _ in executor.calls)


def test_cleanup_failure_never_masks_the_primary_failure(
    tmp_path: Path,
) -> None:
    write_migration(tmp_path, 1, "employee", V001_SQL)
    release_boom = RuntimeError("synthetic: release statement failed")
    executor = FakeExecutor(
        fail_at_version=1, release_error=release_boom
    )

    with pytest.raises(MigrationApplicationError) as err:
        migrate(executor, tmp_path, now=NOW)

    # The migration failure is still the error the caller sees ...
    assert executor.chaos is err.value.__cause__
    # ... and the cleanup failure is attached, observable, not swallowed.
    notes = getattr(err.value, "__notes__", [])
    assert any("cleanup also failed" in note for note in notes)
    assert any("RuntimeError" in note for note in notes)
    assert err.value.__cause__ is not release_boom


def test_cleanup_failure_without_primary_failure_is_reported(
    tmp_path: Path,
) -> None:
    """When nothing else failed, a release failure is its own typed
    error: the run's outcome is unknown, so silence would be wrong."""
    write_migration(tmp_path, 1, "employee", V001_SQL)
    executor = FakeExecutor(
        release_error=RuntimeError("synthetic: unlock failed")
    )

    with pytest.raises(MigrationCleanupError) as err:
        migrate(executor, tmp_path, now=NOW)

    assert "could not be released" in str(err.value)
    assert isinstance(err.value.__cause__, RuntimeError)
    assert executor.release_count == 1
    # The migration itself did commit before cleanup failed (documented
    # "unknown outcome" semantics — no automatic retry or rollback).
    assert executor.commits == [1]


def test_release_is_idempotent_and_safe_when_nothing_was_acquired() -> None:
    """The documented release contract: callable twice, and callable
    without ever holding anything — never an error, never a
    closed-connection crash."""
    never_acquired = FakeExecutor()
    never_acquired.release_advisory_lock()  # no-op, no raise
    assert never_acquired.lock_held is False
    assert never_acquired.server.held_by is None

    executor = FakeExecutor()
    assert executor.acquire_advisory_lock() is True
    executor.release_advisory_lock()
    executor.release_advisory_lock()  # second release is still safe
    assert executor.lock_held is False
    assert executor.lock_connection_open is False
    assert executor.server.held_by is None


def test_noop_run_takes_and_releases_the_lock_without_applying(
    tmp_path: Path,
) -> None:
    write_migration(tmp_path, 1, "employee", V001_SQL)
    script = load_migrations(tmp_path)[0]
    executor = FakeExecutor(ledger={1: script.checksum})

    outcome = migrate(executor, tmp_path, now=NOW)

    assert outcome.applied_versions == ()
    assert executor.locked_calls == [
        "ensure_ledger",
        "list_tables",
        "fetch_applied",
    ]
    assert executor.release_count == 1
    assert executor.commits == []
