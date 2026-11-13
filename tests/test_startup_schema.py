"""Offline G2 startup schema-gate tests (no database is contacted).

Every database interaction here is a recording fake connection; migration files
are real temporary files, because G2's rules are about the shipped files versus
one snapshot. Nothing in this module imports ``psycopg`` or opens a socket.

Covered: the policy table (one exact-match allow plus every refusal code), the
target-safety rules (both DSNs mandatory, malformed/ambiguous, and the
application target never being the test target), the one-connection lifecycle
(connect → lock → snapshot → evaluate → confirmed unlock → close), its cleanup
combinations, and the translation of executor failures into diagnostic codes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import src.startup_schema as gate
from src.config import AppSettings
from src.db import DatabaseUnavailableError
from src.db_migrate import (
    MigrationCleanupError,
    MigrationContentionError,
    MigrationSnapshot,
    load_migrations,
)
from src.db_migrate_executor import (
    MigrationDriverError,
    MigrationIntegrityError,
    PsycopgMigrationExecutor,
)
from src.startup_schema import (
    DATABASE_CONFIGURATION_INVALID,
    DATABASE_TARGET_INVALID,
    DATABASE_UNAVAILABLE,
    MIGRATIONS_PENDING,
    MIGRATION_CHECKSUM_MISMATCH,
    MIGRATION_HISTORY_INVALID,
    MIGRATION_LEDGER_MISSING,
    MIGRATION_LOCK_RELEASE_FAILED,
    MIGRATION_LOCK_UNAVAILABLE,
    MIGRATION_SET_EMPTY,
    SCHEMA_VERIFICATION_FAILED,
    TEST_DATABASE_TARGET_REJECTED,
    UNKNOWN_MIGRATION,
    StartupSchemaError,
    evaluate_migration_state,
    resolve_startup_targets,
    verify_startup_schema,
)

APP_DSN = "postgresql://app_user:app-secret@localhost:5432/attendance"
TEST_DSN = "postgresql://tester:test-secret@127.0.0.1:5432/attendance_test"

MIGRATION_SQL = "CREATE TABLE employee (id INTEGER PRIMARY KEY);\n"

EXPECTED_SHAPE = [
    ("version", "integer", "NO"),
    ("checksum", "text", "NO"),
    ("applied_at", "timestamp with time zone", "NO"),
]

#: Healthy physical catalog rows (G8). The gate now also verifies the physical
#: schema, so the shared fake connection must answer the four S2 statements;
#: these rows describe exactly the contracted schema.
CATALOG_RELATIONS = [
    ("public", "employee", "r"),
    ("public", "employee_pkey", "i"),
    ("public", "app_user", "r"),
    ("public", "app_user_pkey", "i"),
    ("public", "app_user_username_key_key", "i"),
    ("public", "user_session", "r"),
    ("public", "user_session_pkey", "i"),
    ("public", "user_session_user_id_idx", "i"),
    ("public", "user_session_expires_at_idx", "i"),
    ("public", "schema_version", "r"),
    ("public", "schema_version_pkey", "i"),
]
CATALOG_COLUMNS = [
    ("public", "employee", "employee_id", "text", True, None, "", "", 1),
    ("public", "employee", "full_name", "text", True, None, "", "", 2),
    ("public", "employee", "is_active", "boolean", True, "true", "", "", 3),
    (
        "public",
        "employee",
        "created_at",
        "timestamp with time zone",
        True,
        None,
        "",
        "",
        4,
    ),
    ("public", "app_user", "user_id", "text", True, None, "", "", 1),
    ("public", "app_user", "username", "text", True, None, "", "", 2),
    ("public", "app_user", "username_key", "text", True, None, "", "", 3),
    ("public", "app_user", "password_hash", "text", True, None, "", "", 4),
    ("public", "app_user", "role", "text", True, None, "", "", 5),
    ("public", "app_user", "is_active", "boolean", True, "true", "", "", 6),
    (
        "public",
        "app_user",
        "created_at",
        "timestamp with time zone",
        True,
        None,
        "",
        "",
        7,
    ),
    (
        "public",
        "app_user",
        "updated_at",
        "timestamp with time zone",
        True,
        None,
        "",
        "",
        8,
    ),
    (
        "public",
        "user_session",
        "session_token_hash",
        "character(64)",
        True,
        None,
        "",
        "",
        1,
    ),
    ("public", "user_session", "user_id", "text", True, None, "", "", 2),
    (
        "public",
        "user_session",
        "issued_at",
        "timestamp with time zone",
        True,
        None,
        "",
        "",
        3,
    ),
    (
        "public",
        "user_session",
        "expires_at",
        "timestamp with time zone",
        True,
        None,
        "",
        "",
        4,
    ),
    (
        "public",
        "user_session",
        "absolute_expires_at",
        "timestamp with time zone",
        True,
        None,
        "",
        "",
        5,
    ),
    (
        "public",
        "user_session",
        "revoked_at",
        "timestamp with time zone",
        False,
        None,
        "",
        "",
        6,
    ),
    (
        "public",
        "user_session",
        "last_seen_at",
        "timestamp with time zone",
        True,
        None,
        "",
        "",
        7,
    ),
    ("public", "schema_version", "version", "integer", True, None, "", "", 1),
    ("public", "schema_version", "checksum", "text", True, None, "", "", 2),
    (
        "public",
        "schema_version",
        "applied_at",
        "timestamp with time zone",
        True,
        None,
        "",
        "",
        3,
    ),
]
CATALOG_CONSTRAINTS = [
    (
        "public",
        "employee",
        "employee_pkey",
        "p",
        "PRIMARY KEY (employee_id)",
        ["employee_id"],
    ),
    (
        "public",
        "app_user",
        "app_user_pkey",
        "p",
        "PRIMARY KEY (user_id)",
        ["user_id"],
    ),
    (
        "public",
        "user_session",
        "user_session_pkey",
        "p",
        "PRIMARY KEY (session_token_hash)",
        ["session_token_hash"],
    ),
    (
        "public",
        "schema_version",
        "schema_version_pkey",
        "p",
        "PRIMARY KEY (version)",
        ["version"],
    ),
]
CATALOG_INDEXES = [
    (
        "public",
        "employee",
        "employee_pkey",
        True,
        True,
        "CREATE UNIQUE INDEX employee_pkey ON public.employee USING btree (employee_id)",
        ["employee_id"],
    ),
    (
        "public",
        "schema_version",
        "schema_version_pkey",
        True,
        True,
        "CREATE UNIQUE INDEX schema_version_pkey ON public.schema_version USING btree (version)",
        ["version"],
    ),
]


# ---------------------------------------------------------------------------
# Recording fakes
# ---------------------------------------------------------------------------


class Result:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)


def _label(sql: str) -> str:
    if "pg_try_advisory_lock" in sql:
        return "try_lock"
    if "pg_advisory_unlock" in sql:
        return "unlock"
    if sql.startswith("BEGIN ISOLATION"):
        return "begin"
    if "to_regclass" in sql:
        return "ledger"
    if "information_schema.columns" in sql:
        return "shape"
    if "pg_class" in sql:
        return "tables"
    if "SELECT version, checksum" in sql:
        return "applied"
    if sql.strip().upper() == "ROLLBACK":
        return "rollback"
    return sql


class RecordingConnection:
    """Minimal psycopg-shaped connection that records operation order."""

    def __init__(self, handler, events: list[str], *, close_error=None) -> None:
        self.handler = handler
        self.events = events
        self.autocommit = False
        self.closed = 0
        self.rollbacks = 0
        self.close_error = close_error

    def execute(self, sql, params=None):
        self.events.append(_label(sql))
        return Result(self.handler(sql, params))

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1
        self.events.append("close")
        if self.close_error is not None:
            raise self.close_error


def _handler(
    *,
    ledger_present: bool = True,
    tables: tuple[str, ...] = (),
    applied: tuple[tuple[int, str], ...] = (),
    shape: list | None = None,
    lock_result: bool = True,
    unlock_result: bool = True,
    fail_in: str | None = None,
    raise_on_unlock: bool = False,
):
    effective_shape = EXPECTED_SHAPE if shape is None else shape

    def handle(sql: str, params=None):
        if "pg_advisory_unlock" in sql and raise_on_unlock:
            raise RuntimeError("driver exploded on unlock")
        if fail_in is not None and fail_in in sql:
            raise RuntimeError("driver exploded")
        if "to_regclass" in sql:
            return [("public.schema_version",)] if ledger_present else [(None,)]
        if "pg_try_advisory_lock" in sql:
            return [(lock_result,)]
        if "pg_advisory_unlock" in sql:
            return [(unlock_result,)]
        if "information_schema.columns" in sql:
            return list(effective_shape)
        # G8 catalog statements (checked before the generic ``pg_class``
        # branch, which would otherwise answer them with table names).
        if "format_type" in sql:
            return list(CATALOG_COLUMNS)
        if "pg_constraint" in sql:
            return list(CATALOG_CONSTRAINTS)
        if "pg_index" in sql:
            return list(CATALOG_INDEXES)
        if "n.nspname, c.relname, c.relkind" in sql:
            return list(CATALOG_RELATIONS)
        if "pg_class" in sql:
            return [(name,) for name in tables]
        if "SELECT version, checksum" in sql:
            return list(applied)
        return []

    return handle


def _executor(handler, *, close_error=None):
    events: list[str] = []
    opened: list[tuple[str, RecordingConnection]] = []

    def opener(dsn: str) -> RecordingConnection:
        connection = RecordingConnection(handler, events, close_error=close_error)
        opened.append((dsn, connection))
        events.append("connect")
        return connection

    executor = PsycopgMigrationExecutor(APP_DSN, connection_opener=opener)
    return executor, events, opened


def _settings(**overrides) -> AppSettings:
    values = {"database_url": APP_DSN, "test_database_url": TEST_DSN}
    values.update(overrides)
    return AppSettings(**values)


def _write_migrations(directory: Path, *versions: int) -> dict[int, str]:
    directory.mkdir(parents=True, exist_ok=True)
    for version in versions:
        (directory / f"V{version:03d}__step_{version}.sql").write_text(
            MIGRATION_SQL, encoding="utf-8"
        )
    return {script.version: script.checksum for script in load_migrations(directory)}


def _snapshot(
    *,
    tables: tuple[str, ...] = (),
    ledger_present: bool = True,
    applied: tuple[tuple[int, str], ...] = (),
) -> MigrationSnapshot:
    return MigrationSnapshot(
        tables=frozenset(tables), ledger_present=ledger_present, applied=tuple(applied)
    )


def _gate(
    directory: Path,
    handler,
    *,
    close_error=None,
    settings: AppSettings | None = None,
    record_dsn: list[str] | None = None,
):
    executor, events, opened = _executor(handler, close_error=close_error)

    def factory(dsn: str):
        if record_dsn is not None:
            record_dsn.append(dsn)
        return executor

    verify_startup_schema(
        settings or _settings(),
        migrations_dir=directory,
        executor_factory=factory,
    )
    return events, opened


# ---------------------------------------------------------------------------
# Policy table
# ---------------------------------------------------------------------------


def test_exact_matching_state_permits_startup(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory, 1)
    state = evaluate_migration_state(
        _snapshot(applied=((1, checksums[1]),)), directory
    )
    assert state.ledger_present is True
    assert state.previous_version == 1
    assert state.pending == ()


def test_empty_migration_set_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(_snapshot(), directory)
    assert excinfo.value.code == MIGRATION_SET_EMPTY


def test_absent_migration_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(_snapshot(), tmp_path / "missing")
    assert excinfo.value.code == MIGRATION_SET_EMPTY


def test_missing_ledger_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    _write_migrations(directory, 1)
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(_snapshot(ledger_present=False), directory)
    assert excinfo.value.code == MIGRATION_LEDGER_MISSING


def test_empty_ledger_with_expected_migrations_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    _write_migrations(directory, 1)
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(_snapshot(), directory)
    assert excinfo.value.code == MIGRATIONS_PENDING


def test_pending_migration_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory, 1, 2)
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(_snapshot(applied=((1, checksums[1]),)), directory)
    assert excinfo.value.code == MIGRATIONS_PENDING


def test_checksum_mismatch_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    _write_migrations(directory, 1)
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(_snapshot(applied=((1, "0" * 64),)), directory)
    assert excinfo.value.code == MIGRATION_CHECKSUM_MISMATCH


def test_unknown_recorded_migration_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory, 1)
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(
            _snapshot(applied=((1, checksums[1]), (2, "f" * 64))), directory
        )
    assert excinfo.value.code == UNKNOWN_MIGRATION


def test_non_contiguous_history_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory, 1, 3)
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(_snapshot(applied=((1, checksums[1]),)), directory)
    assert excinfo.value.code == MIGRATION_HISTORY_INVALID


def test_duplicate_ledger_rows_are_refused(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory, 1)
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(
            _snapshot(applied=((1, checksums[1]), (1, checksums[1]))), directory
        )
    assert excinfo.value.code == MIGRATION_HISTORY_INVALID


def test_migration_set_not_beginning_at_v001_is_refused(tmp_path: Path) -> None:
    """The audit's gap: files [2] plus a ledger [2] reconciles cleanly."""
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory, 2)
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(_snapshot(applied=((2, checksums[2]),)), directory)
    assert excinfo.value.code == MIGRATION_HISTORY_INVALID


def test_malformed_migration_file_name_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    _write_migrations(directory, 1)
    (directory / "not_a_migration.sql").write_text(MIGRATION_SQL, encoding="utf-8")
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(_snapshot(), directory)
    assert excinfo.value.code == MIGRATION_HISTORY_INVALID


def test_employee_table_without_ledger_is_refused(tmp_path: Path) -> None:
    """The pre-existing documented integrity check is preserved."""
    directory = tmp_path / "migrations"
    _write_migrations(directory, 1)
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(
            _snapshot(tables=("employee",), ledger_present=False), directory
        )
    assert excinfo.value.code == MIGRATION_HISTORY_INVALID


def test_unexpected_state_failure_is_refused(tmp_path: Path, monkeypatch) -> None:
    directory = tmp_path / "migrations"
    _write_migrations(directory, 1)

    def explode(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(gate, "read_state", explode)
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(_snapshot(), directory)
    assert excinfo.value.code == SCHEMA_VERIFICATION_FAILED


@pytest.mark.parametrize(
    "build",
    [
        lambda directory, checksums: _snapshot(ledger_present=False),
        lambda directory, checksums: _snapshot(),
        lambda directory, checksums: _snapshot(applied=((1, "0" * 64),)),
    ],
)
def test_refusal_messages_are_sanitized(tmp_path: Path, build) -> None:
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory, 1)
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(build(directory, checksums), directory)
    text = str(excinfo.value)
    assert excinfo.value.code in text
    for fragment in ("postgresql", "app-secret", "test-secret", "Traceback", "SELECT"):
        assert fragment not in text


# ---------------------------------------------------------------------------
# Target safety
# ---------------------------------------------------------------------------


def test_missing_application_dsn_is_refused() -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        resolve_startup_targets(_settings(database_url=None))
    assert excinfo.value.code == DATABASE_CONFIGURATION_INVALID


def test_missing_test_dsn_is_refused() -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        resolve_startup_targets(_settings(test_database_url=None))
    assert excinfo.value.code == DATABASE_CONFIGURATION_INVALID


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_dsn_is_refused(blank: str) -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        resolve_startup_targets(_settings(database_url=blank))
    assert excinfo.value.code == DATABASE_CONFIGURATION_INVALID
    with pytest.raises(StartupSchemaError) as excinfo:
        resolve_startup_targets(_settings(test_database_url=blank))
    assert excinfo.value.code == DATABASE_CONFIGURATION_INVALID


def test_malformed_application_dsn_is_refused() -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        resolve_startup_targets(_settings(database_url="postgresql://[::1"))
    assert excinfo.value.code == DATABASE_TARGET_INVALID


def test_malformed_test_dsn_is_refused() -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        resolve_startup_targets(_settings(test_database_url="postgresql://[::1"))
    assert excinfo.value.code == DATABASE_TARGET_INVALID


def test_unidentifiable_application_target_is_refused() -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        resolve_startup_targets(_settings(database_url="dbname=attendance"))
    assert excinfo.value.code == DATABASE_TARGET_INVALID


def test_unidentifiable_test_target_is_refused() -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        resolve_startup_targets(_settings(test_database_url="dbname=attendance_test"))
    assert excinfo.value.code == DATABASE_TARGET_INVALID


@pytest.mark.parametrize(
    "test_dsn",
    [
        "postgresql://other:other@localhost:5432/attendance",
        "postgresql://other:other@127.0.0.1/attendance",
        "postgresql://other:other@[::1]:5432/attendance",
        "host=127.0.0.1 dbname=attendance user=other",
    ],
)
def test_identical_normalized_targets_are_rejected(test_dsn: str) -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        resolve_startup_targets(_settings(test_database_url=test_dsn))
    assert excinfo.value.code == TEST_DATABASE_TARGET_REJECTED


def test_distinct_targets_resolve_to_the_application_dsn() -> None:
    assert resolve_startup_targets(_settings()) == APP_DSN


def test_test_dsn_never_reaches_a_connection_opener(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory, 1)
    dsns: list[str] = []
    events, opened = _gate(
        directory,
        _handler(applied=((1, checksums[1]),)),
        record_dsn=dsns,
    )
    assert dsns == [APP_DSN]
    assert opened and all(dsn == APP_DSN for dsn, _ in opened)
    assert TEST_DSN not in {dsn for dsn, _ in opened}


# ---------------------------------------------------------------------------
# One-connection lifecycle
# ---------------------------------------------------------------------------


def test_lifecycle_uses_one_connection_in_the_approved_order(tmp_path: Path) -> None:
    executor, events, opened = _executor(_handler())
    seen: dict[str, bool] = {}

    def evaluate(snapshot: MigrationSnapshot) -> None:
        events.append("evaluate")
        seen["unlock_already_done"] = "unlock" in events

    executor.verify_schema_locked(evaluate)

    assert seen["unlock_already_done"] is False, "lock must be held during evaluation"
    assert events == [
        "connect",
        "try_lock",
        "begin",
        "ledger",
        "shape",
        "tables",
        "applied",
        "rollback",
        "evaluate",
        "unlock",
        "close",
    ]
    assert len(opened) == 1, "exactly one physical connection"
    assert opened[0][0] == APP_DSN
    assert opened[0][1].closed == 1


def test_refused_lock_issues_no_snapshot_and_opens_one_connection() -> None:
    executor, events, opened = _executor(_handler(lock_result=False))
    with pytest.raises(MigrationContentionError):
        executor.verify_schema_locked(lambda snapshot: None)
    assert events == ["connect", "try_lock", "close"]
    assert "begin" not in events and "unlock" not in events
    assert len(opened) == 1
    assert opened[0][1].closed == 1


def test_no_retry_occurs_after_a_refused_lock() -> None:
    executor, events, opened = _executor(_handler(lock_result=False))
    with pytest.raises(MigrationContentionError):
        executor.verify_schema_locked(lambda snapshot: None)
    assert events.count("try_lock") == 1
    assert events.count("connect") == 1
    assert len(opened) == 1


def test_unconfirmed_unlock_is_a_cleanup_failure() -> None:
    executor, events, opened = _executor(_handler(unlock_result=False))
    with pytest.raises(MigrationCleanupError):
        executor.verify_schema_locked(lambda snapshot: None)
    assert events[-2:] == ["unlock", "close"]
    assert opened[0][1].closed == 1


def test_unlock_error_is_a_cleanup_failure() -> None:
    executor, events, opened = _executor(_handler(raise_on_unlock=True))
    with pytest.raises(MigrationCleanupError) as excinfo:
        executor.verify_schema_locked(lambda snapshot: None)
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert opened[0][1].closed == 1


def test_close_failure_with_nothing_in_flight_is_a_cleanup_failure() -> None:
    executor, events, opened = _executor(
        _handler(), close_error=OSError("close failed")
    )
    with pytest.raises(MigrationCleanupError):
        executor.verify_schema_locked(lambda snapshot: None)
    assert events[-2:] == ["unlock", "close"]
    assert opened[0][1].closed == 1


def test_snapshot_failure_becomes_a_driver_error() -> None:
    executor, events, opened = _executor(
        _handler(fail_in="SELECT version, checksum")
    )
    with pytest.raises(MigrationDriverError):
        executor.verify_schema_locked(lambda snapshot: None)
    assert opened[0][1].closed == 1


def test_malformed_ledger_shape_is_an_integrity_refusal() -> None:
    executor, events, opened = _executor(_handler(shape=[]))
    with pytest.raises(MigrationIntegrityError):
        executor.verify_schema_locked(lambda snapshot: None)
    assert "applied" not in events, "rows are not interpreted for a malformed ledger"
    assert opened[0][1].closed == 1


def test_evaluate_refusal_is_preserved_when_cleanup_also_fails() -> None:
    refusal = StartupSchemaError(MIGRATIONS_PENDING, "pending")
    executor, events, opened = _executor(
        _handler(), close_error=OSError("close failed")
    )

    def evaluate(snapshot: MigrationSnapshot) -> None:
        raise refusal

    with pytest.raises(StartupSchemaError) as excinfo:
        executor.verify_schema_locked(evaluate)
    assert excinfo.value is refusal
    assert excinfo.value.__notes__, "cleanup failure must be recorded, not masked"
    assert events[-2:] == ["unlock", "close"]


# ---------------------------------------------------------------------------
# Gate-level mapping and success
# ---------------------------------------------------------------------------


def test_gate_allows_an_exactly_matching_state(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory, 1)
    events, opened = _gate(
        directory, _handler(applied=((1, checksums[1]),))
    )
    assert events[0] == "connect" and events[-1] == "close"
    assert len(opened) == 1


def test_gate_maps_contention_to_migration_lock_unavailable(tmp_path: Path) -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        _gate(tmp_path, _handler(lock_result=False))
    assert excinfo.value.code == MIGRATION_LOCK_UNAVAILABLE


def test_gate_maps_unconfirmed_release(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory, 1)
    with pytest.raises(StartupSchemaError) as excinfo:
        _gate(
            directory,
            _handler(applied=((1, checksums[1]),), unlock_result=False),
        )
    assert excinfo.value.code == MIGRATION_LOCK_RELEASE_FAILED


def test_gate_maps_close_failure_to_release_failed(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory, 1)
    with pytest.raises(StartupSchemaError) as excinfo:
        _gate(
            directory,
            _handler(applied=((1, checksums[1]),)),
            close_error=OSError("close failed"),
        )
    assert excinfo.value.code == MIGRATION_LOCK_RELEASE_FAILED


def test_gate_maps_connection_failure_to_database_unavailable(tmp_path: Path) -> None:
    def opener(dsn: str):
        raise DatabaseUnavailableError("unreachable")

    executor = PsycopgMigrationExecutor(APP_DSN, connection_opener=opener)
    with pytest.raises(StartupSchemaError) as excinfo:
        verify_startup_schema(
            _settings(),
            migrations_dir=tmp_path,
            executor_factory=lambda dsn: executor,
        )
    assert excinfo.value.code == DATABASE_UNAVAILABLE


def test_gate_maps_snapshot_failure_to_verification_failed(tmp_path: Path) -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        _gate(tmp_path, _handler(fail_in="SELECT version, checksum"))
    assert excinfo.value.code == SCHEMA_VERIFICATION_FAILED


def test_gate_maps_malformed_ledger_to_history_invalid(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    _write_migrations(directory, 1)
    with pytest.raises(StartupSchemaError) as excinfo:
        _gate(directory, _handler(shape=[]))
    assert excinfo.value.code == MIGRATION_HISTORY_INVALID


def test_gate_refuses_pending_migrations_end_to_end(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory, 1, 2)
    with pytest.raises(StartupSchemaError) as excinfo:
        _gate(directory, _handler(applied=((1, checksums[1]),)))
    assert excinfo.value.code == MIGRATIONS_PENDING


# ---------------------------------------------------------------------------
# Target validation happens before executor construction (audit M2)
# ---------------------------------------------------------------------------

# One row per invalid-target category; the identifiers are the pytest ids.
INVALID_TARGET_CASES: list[tuple[str, dict, str]] = [
    ("missing-application-dsn", {"database_url": None}, DATABASE_CONFIGURATION_INVALID),
    ("blank-application-dsn", {"database_url": ""}, DATABASE_CONFIGURATION_INVALID),
    ("whitespace-application-dsn", {"database_url": "   "}, DATABASE_CONFIGURATION_INVALID),
    ("missing-test-dsn", {"test_database_url": None}, DATABASE_CONFIGURATION_INVALID),
    ("blank-test-dsn", {"test_database_url": ""}, DATABASE_CONFIGURATION_INVALID),
    ("whitespace-test-dsn", {"test_database_url": "   "}, DATABASE_CONFIGURATION_INVALID),
    ("malformed-application-dsn", {"database_url": "postgresql://[::1"}, DATABASE_TARGET_INVALID),
    ("unidentifiable-application-target", {"database_url": "dbname=attendance"}, DATABASE_TARGET_INVALID),
    ("malformed-test-dsn", {"test_database_url": "postgresql://[::1"}, DATABASE_TARGET_INVALID),
    ("unidentifiable-test-target", {"test_database_url": "dbname=attendance_test"}, DATABASE_TARGET_INVALID),
    (
        "identical-normalized-targets",
        {"test_database_url": "postgresql://other:other@127.0.0.1:5432/attendance"},
        TEST_DATABASE_TARGET_REJECTED,
    ),
]


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [case[1:] for case in INVALID_TARGET_CASES],
    ids=[case[0] for case in INVALID_TARGET_CASES],
)
def test_invalid_targets_never_reach_the_executor_or_a_connection(
    tmp_path: Path, monkeypatch, overrides: dict, expected_code: str
) -> None:
    """Validation must refuse before the factory is called and before any connect."""
    factory_calls: list[str] = []

    def factory(dsn: str):
        factory_calls.append(dsn)
        raise AssertionError("executor built for an invalid target")

    def forbidden_connect(*args, **kwargs):
        raise AssertionError("a connection opener ran for an invalid target")

    monkeypatch.setattr("src.db._connect", forbidden_connect)

    with pytest.raises(StartupSchemaError) as excinfo:
        verify_startup_schema(
            _settings(**overrides),
            migrations_dir=tmp_path,
            executor_factory=factory,
        )
    assert excinfo.value.code == expected_code
    assert factory_calls == [], "target validation must precede executor construction"


# ---------------------------------------------------------------------------
# Executor construction is inside the sanitized diagnostic path (audit M3)
# ---------------------------------------------------------------------------


def test_executor_construction_failure_is_sanitized(tmp_path: Path) -> None:
    def factory(dsn: str):
        raise RuntimeError(f"driver exploded on {dsn} with app-secret")  # noqa: TRY003

    with pytest.raises(StartupSchemaError) as excinfo:
        verify_startup_schema(
            _settings(), migrations_dir=tmp_path, executor_factory=factory
        )
    assert excinfo.value.code == SCHEMA_VERIFICATION_FAILED
    assert excinfo.value.message == gate._MESSAGES[SCHEMA_VERIFICATION_FAILED]
    text = str(excinfo.value)
    for fragment in (
        "postgresql",
        "app-secret",
        "test-secret",
        "exploded",
        "RuntimeError",
        "Traceback",
        "localhost",
        "5432",
    ):
        assert fragment not in text
    assert isinstance(excinfo.value.__cause__, RuntimeError), "cause kept internal"


def test_executor_construction_refusal_keeps_its_specific_code(tmp_path: Path) -> None:
    refusal = StartupSchemaError(TEST_DATABASE_TARGET_REJECTED, "refused")

    def factory(dsn: str):
        raise refusal

    with pytest.raises(StartupSchemaError) as excinfo:
        verify_startup_schema(
            _settings(), migrations_dir=tmp_path, executor_factory=factory
        )
    assert excinfo.value is refusal, "a specific refusal must not be generalized"


def test_executor_construction_target_error_keeps_its_specific_code(
    tmp_path: Path,
) -> None:
    from src.db_migrate_executor import MigrationTargetError

    def factory(dsn: str):
        raise MigrationTargetError("unusable target")

    with pytest.raises(StartupSchemaError) as excinfo:
        verify_startup_schema(
            _settings(), migrations_dir=tmp_path, executor_factory=factory
        )
    assert excinfo.value.code == DATABASE_TARGET_INVALID
