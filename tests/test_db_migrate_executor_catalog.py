"""Offline S4 tests: combined G2 + G8 verification on one connection.

Every database interaction is a recording psycopg-shaped fake; no driver, no
socket, no DSN is contacted. The **real** S2 probe
(:func:`src.db_schema_catalog.read_public_catalog`) and the **real** S3
evaluator (:func:`src.physical_schema.evaluate_physical_schema`) run against
those fakes, so these tests exercise the actual integration rather than a
stand-in, while remaining entirely offline.

Covered: the unchanged default G2 lifecycle and return type, the combined
one-connection/one-lock/one-transaction order, same-snapshot and same-connection
guarantees, G2 precedence over the catalog probe, all three S3 outcomes,
sanitized catalog failures, the cleanup matrix, and the unchanged startup path.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.db_migrate import (
    MigrationCleanupError,
    MigrationContentionError,
    MigrationExecutor,
    MigrationSnapshot,
    load_migrations,
)
from src.db_migrate_executor import (
    LockedSchemaVerification,
    MigrationDriverError,
    PsycopgMigrationExecutor,
)
from src.db_schema_catalog import (
    CatalogProbeError,
    CatalogSnapshot,
    read_public_catalog,
)
from src.physical_schema import (
    SCHEMA_COLUMN_TYPE_MISMATCH,
    SCHEMA_OBJECT_PROHIBITED,
    SCHEMA_TABLE_KIND_UNCONFIRMED,
    CompatibilityStatus,
    SchemaEvaluation,
    evaluate_physical_schema,
)
from src.startup_schema import (
    MIGRATIONS_PENDING,
    StartupSchemaError,
    evaluate_migration_state,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DSN = "postgresql://app_user:super-secret@localhost:5432/attendance"

EXPECTED_SHAPE = [
    ("version", "integer", "NO"),
    ("checksum", "text", "NO"),
    ("applied_at", "timestamp with time zone", "NO"),
]

MIGRATION_SQL = "CREATE TABLE employee (id INTEGER PRIMARY KEY);\n"


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
    """Stable name for every statement this operation can issue."""
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
    if "SELECT version, checksum" in sql:
        return "applied"
    if "SELECT c.relname FROM pg_class" in sql:
        return "tables"
    if "format_type" in sql:
        return "catalog_columns"
    if "pg_constraint" in sql:
        return "catalog_constraints"
    if "pg_index" in sql:
        return "catalog_indexes"
    if "c.relkind" in sql:
        return "catalog_relations"
    if sql.strip().upper() == "ROLLBACK":
        return "rollback"
    return sql


class RecordingConnection:
    def __init__(
        self,
        handler,
        events: list[str],
        *,
        close_error: Exception | None = None,
        rollback_error: Exception | None = None,
    ) -> None:
        self.handler = handler
        self.events = events
        self.autocommit = False
        self.statements: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self.close_error = close_error
        self.rollback_error = rollback_error

    def execute(self, sql, params=None):
        self.events.append(_label(sql))
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
        self.events.append("close")
        if self.close_error is not None:
            raise self.close_error


def _healthy_rows():
    """Row sets the real S2 statements map to a compatible snapshot."""
    relations = [
        ("public", "employee", "r"),
        ("public", "employee_pkey", "i"),
        ("public", "schema_version", "r"),
        ("public", "schema_version_pkey", "i"),
    ]
    columns = [
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
    constraints = [
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
            "schema_version",
            "schema_version_pkey",
            "p",
            "PRIMARY KEY (version)",
            ["version"],
        ),
    ]
    indexes = [
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
    return relations, columns, constraints, indexes


def _handler(
    *,
    relations=None,
    columns=None,
    constraints=None,
    indexes=None,
    applied: tuple[tuple[int, str], ...] = ((1, "checksum-1"),),
    ledger_present: bool = True,
    tables: tuple[str, ...] = ("employee", "schema_version"),
    lock_result: bool = True,
    unlock_result: bool = True,
    raise_on_unlock: bool = False,
    fail_on: str | None = None,
    fail_exc: Exception | None = None,
):
    base_relations, base_columns, base_constraints, base_indexes = _healthy_rows()
    rows = {
        "catalog_relations": base_relations if relations is None else relations,
        "catalog_columns": base_columns if columns is None else columns,
        "catalog_constraints": base_constraints if constraints is None else constraints,
        "catalog_indexes": base_indexes if indexes is None else indexes,
    }

    def handle(sql: str, params=None):
        label = _label(sql)
        if fail_on is not None and fail_on == label:
            raise fail_exc if fail_exc is not None else RuntimeError("driver exploded")
        if label == "try_lock":
            return [(lock_result,)]
        if label == "unlock":
            if raise_on_unlock:
                raise RuntimeError("driver exploded on unlock")
            return [(unlock_result,)]
        if label == "ledger":
            return [("public.schema_version",)] if ledger_present else [(None,)]
        if label == "shape":
            return list(EXPECTED_SHAPE)
        if label == "tables":
            return [(name,) for name in tables]
        if label == "applied":
            return list(applied)
        return list(rows.get(label, []))

    return handle


def _executor(handler, **connection_kwargs):
    events: list[str] = []
    opened: list[RecordingConnection] = []

    def opener(dsn: str) -> RecordingConnection:
        connection = RecordingConnection(handler, events, **connection_kwargs)
        opened.append(connection)
        events.append("connect")
        return connection

    executor = PsycopgMigrationExecutor(DSN, connection_opener=opener)
    return executor, events, opened


def _counting_probe(calls: list):
    """The real S2 probe, recording that it ran (and on which connection)."""

    def probe(connection):
        calls.append(connection)
        return read_public_catalog(connection)

    return probe


def _write_migrations(directory: Path, *versions: int) -> dict[int, str]:
    directory.mkdir(parents=True, exist_ok=True)
    for version in versions or (1,):
        (directory / f"V{version:03d}__step_{version}.sql").write_text(
            MIGRATION_SQL, encoding="utf-8"
        )
    return {script.version: script.checksum for script in load_migrations(directory)}


def _combined(handler, *, probe_calls: list, evaluate=lambda snapshot: None, **kwargs):
    executor, events, opened = _executor(handler)
    result = executor.verify_schema_locked(
        evaluate, catalog_probe=_counting_probe(probe_calls), **kwargs
    )
    return result, events, opened


# ---------------------------------------------------------------------------
# Backward compatibility: the default call is still exactly G2
# ---------------------------------------------------------------------------


def test_default_call_keeps_the_g2_lifecycle_and_return_type() -> None:
    executor, events, opened = _executor(_handler())

    def evaluate(snapshot: MigrationSnapshot) -> None:
        events.append("evaluate")

    result = executor.verify_schema_locked(evaluate)

    assert isinstance(result, MigrationSnapshot)
    assert not isinstance(result, LockedSchemaVerification)
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
    assert len(opened) == 1 and opened[0].closed == 1


def test_default_call_never_issues_catalog_statements() -> None:
    executor, events, _ = _executor(_handler())
    executor.verify_schema_locked(lambda snapshot: None)
    assert not any(label.startswith("catalog_") for label in events)


def test_signature_defaults_preserve_g2() -> None:
    signature = inspect.signature(PsycopgMigrationExecutor.verify_schema_locked)
    assert signature.parameters["catalog_probe"].default is None
    assert (
        signature.parameters["catalog_probe"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )
    assert signature.parameters["physical_evaluator"].default is evaluate_physical_schema


def test_executor_abc_is_unchanged() -> None:
    assert MigrationExecutor.__abstractmethods__ == frozenset(
        {
            "acquire_advisory_lock",
            "release_advisory_lock",
            "ensure_ledger",
            "list_tables",
            "fetch_applied",
            "apply_migration",
        }
    )
    assert not hasattr(MigrationExecutor, "verify_schema_locked")
    assert not hasattr(MigrationExecutor, "read_snapshot")


# ---------------------------------------------------------------------------
# Combined lifecycle
# ---------------------------------------------------------------------------


def test_combined_lifecycle_reads_catalog_inside_the_same_transaction() -> None:
    calls: list = []
    result, events, opened = _combined(_handler(), probe_calls=calls)
    assert events == [
        "connect",
        "try_lock",
        "begin",
        "ledger",
        "shape",
        "tables",
        "applied",
        "catalog_relations",
        "catalog_columns",
        "catalog_constraints",
        "catalog_indexes",
        "rollback",
        "unlock",
        "close",
    ]
    assert len(calls) == 1
    assert len(opened) == 1 and opened[0].closed == 1
    assert isinstance(result.physical, SchemaEvaluation)


def test_every_catalog_read_happens_between_begin_and_rollback() -> None:
    _, events, _ = _combined(_handler(), probe_calls=[])
    begin = events.index("begin")
    rollback = events.index("rollback")
    catalog_positions = [
        index for index, label in enumerate(events) if label.startswith("catalog_")
    ]
    assert catalog_positions, "the catalog must actually be read"
    assert all(begin < position < rollback for position in catalog_positions)


def test_catalog_probe_uses_the_migration_connection() -> None:
    calls: list = []
    _, _, opened = _combined(_handler(), probe_calls=calls)
    assert calls[0] is opened[0]


def test_catalog_read_happens_before_the_lock_is_released() -> None:
    events: list[str] = []
    seen: dict[str, bool] = {}

    def probe(connection):
        seen["unlock_already_done"] = "unlock" in events
        seen["rollback_already_done"] = "rollback" in events
        return read_public_catalog(connection)

    executor, events, _ = _executor(_handler())
    executor.verify_schema_locked(lambda snapshot: None, catalog_probe=probe)
    assert seen == {
        "unlock_already_done": False,
        "rollback_already_done": False,
    }


def test_exactly_one_connection_lock_and_transaction() -> None:
    _, events, opened = _combined(_handler(), probe_calls=[])
    assert events.count("connect") == 1
    assert events.count("try_lock") == 1
    assert events.count("begin") == 1
    assert events.count("rollback") == 1
    assert events.count("unlock") == 1
    assert events.count("close") == 1
    assert len(opened) == 1


def test_refused_lock_probes_nothing() -> None:
    calls: list = []
    executor, events, opened = _executor(_handler(lock_result=False))
    with pytest.raises(MigrationContentionError):
        executor.verify_schema_locked(
            lambda snapshot: None, catalog_probe=_counting_probe(calls)
        )
    assert calls == []
    assert events == ["connect", "try_lock", "close"]
    assert len(opened) == 1 and opened[0].closed == 1


# ---------------------------------------------------------------------------
# G2 precedence
# ---------------------------------------------------------------------------


def test_g2_refusal_happens_before_any_catalog_read() -> None:
    calls: list = []
    refusal = StartupSchemaError(MIGRATIONS_PENDING, "pending migrations")
    executor, events, opened = _executor(_handler())

    def evaluate(snapshot: MigrationSnapshot) -> None:
        events.append("evaluate")
        raise refusal

    with pytest.raises(StartupSchemaError) as excinfo:
        executor.verify_schema_locked(
            evaluate, catalog_probe=_counting_probe(calls)
        )
    assert excinfo.value is refusal
    assert calls == []
    assert not any(label.startswith("catalog_") for label in events)
    assert events == [
        "connect",
        "try_lock",
        "begin",
        "ledger",
        "shape",
        "tables",
        "applied",
        "evaluate",
        "unlock",
        "close",
    ]
    assert opened[0].rollbacks >= 1, "the open transaction must be rolled back"
    assert opened[0].closed == 1


def test_real_g2_gate_allows_valid_history_then_reads_the_catalog(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory)
    calls: list = []
    executor, _, opened = _executor(_handler(applied=((1, checksums[1]),)))

    result = executor.verify_schema_locked(
        lambda snapshot: evaluate_migration_state(snapshot, directory),
        catalog_probe=_counting_probe(calls),
    )

    assert isinstance(result, LockedSchemaVerification)
    assert result.physical.status is CompatibilityStatus.COMPATIBLE
    assert result.migration.applied == ((1, checksums[1]),)
    assert len(calls) == 1
    assert opened[0].closed == 1


def test_real_g2_gate_refusal_prevents_the_catalog_probe(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    # A genuine pending case: V002 is shipped but only V001 is applied.
    checksums = _write_migrations(directory, 1, 2)
    calls: list = []
    executor, events, _ = _executor(_handler(applied=((1, checksums[1]),)))

    with pytest.raises(StartupSchemaError) as excinfo:
        executor.verify_schema_locked(
            lambda snapshot: evaluate_migration_state(snapshot, directory),
            catalog_probe=_counting_probe(calls),
        )
    assert excinfo.value.code == MIGRATIONS_PENDING
    assert calls == []
    assert not any(label.startswith("catalog_") for label in events)


def test_physical_drift_does_not_replace_a_valid_g2_result(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory)
    _, columns, _, _ = _healthy_rows()
    drifted = [
        tuple(row[:3]) + ("integer",) + tuple(row[4:]) for row in columns
    ]
    calls: list = []
    executor, _, _ = _executor(_handler(applied=((1, checksums[1]),), columns=drifted))

    result = executor.verify_schema_locked(
        lambda snapshot: evaluate_migration_state(snapshot, directory),
        catalog_probe=_counting_probe(calls),
    )

    assert result.migration.applied == ((1, checksums[1]),)
    assert result.physical.status is CompatibilityStatus.INCOMPATIBLE
    assert SCHEMA_COLUMN_TYPE_MISMATCH in result.physical.codes


# ---------------------------------------------------------------------------
# Physical-schema outcomes are preserved, never re-interpreted
# ---------------------------------------------------------------------------


def test_compatible_result_is_returned_as_compatible() -> None:
    result, _, _ = _combined(_handler(), probe_calls=[])
    assert isinstance(result, LockedSchemaVerification)
    assert result.physical.compatible is True
    assert result.physical.findings == ()


def test_incompatible_result_preserves_findings() -> None:
    _, columns, _, _ = _healthy_rows()
    drifted = [tuple(row[:3]) + ("integer",) + tuple(row[4:]) for row in columns]
    result, _, _ = _combined(_handler(columns=drifted), probe_calls=[])
    assert result.physical.status is CompatibilityStatus.INCOMPATIBLE
    assert result.physical.compatible is False
    assert result.physical.findings
    assert all(finding.code in result.physical.codes for finding in result.physical.findings)


def test_partitioned_table_stays_indeterminate_not_compatible() -> None:
    relations, _, _, _ = _healthy_rows()
    partitioned = [("public", "employee", "p")] + list(relations[1:])
    result, _, _ = _combined(_handler(relations=partitioned), probe_calls=[])
    assert result.physical.status is CompatibilityStatus.INDETERMINATE
    assert result.physical.compatible is False
    assert result.physical.indeterminate is True
    assert result.physical.codes == (SCHEMA_TABLE_KIND_UNCONFIRMED,)


def test_prohibited_view_remains_prohibited() -> None:
    relations, _, _, _ = _healthy_rows()
    result, _, _ = _combined(
        _handler(relations=relations + [("public", "attendance_events", "v")]),
        probe_calls=[],
    )
    assert result.physical.compatible is False
    assert SCHEMA_OBJECT_PROHIBITED in result.physical.codes
    prohibited = [
        finding
        for finding in result.physical.findings
        if finding.code == SCHEMA_OBJECT_PROHIBITED
    ]
    assert prohibited[0].object == "attendance_events"
    assert prohibited[0].category == "attendance"


def test_test_marker_stays_distinguishable() -> None:
    relations, _, _, _ = _healthy_rows()
    result, _, _ = _combined(
        _handler(relations=relations + [("public", "_phase3_test_guard", "r")]),
        probe_calls=[],
    )
    assert result.physical.compatible is False
    assert SCHEMA_OBJECT_PROHIBITED in result.physical.codes
    assert result.physical.findings[0].category == "test_marker"


def test_unrelated_public_objects_stay_allowed() -> None:
    relations, columns, _, _ = _healthy_rows()
    result, _, _ = _combined(
        _handler(
            relations=relations + [("public", "surface_metrics", "r")],
            columns=columns
            + [
                (
                    "public",
                    "surface_metrics",
                    "measured_at",
                    "timestamp with time zone",
                    True,
                    None,
                    "",
                    "",
                    1,
                )
            ],
        ),
        probe_calls=[],
    )
    assert result.physical.compatible is True, result.physical.findings


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_catalog_read_failure_is_a_sanitized_driver_error() -> None:
    executor, events, opened = _executor(_handler(fail_on="catalog_indexes"))
    with pytest.raises(MigrationDriverError) as excinfo:
        executor.verify_schema_locked(
            lambda snapshot: None, catalog_probe=read_public_catalog
        )
    error = excinfo.value
    assert "driver exploded" not in str(error)
    assert "catalog could not be read" in str(error)
    assert isinstance(error.__cause__, CatalogProbeError)
    assert opened[0].closed == 1
    assert events[-1] == "close"
    assert "unlock" in events, "the lock must still be released"


def test_catalog_read_failure_is_not_a_catalog_snapshot() -> None:
    executor, _, _ = _executor(_handler(fail_on="catalog_columns"))
    with pytest.raises(MigrationDriverError):
        executor.verify_schema_locked(
            lambda snapshot: None, catalog_probe=read_public_catalog
        )


def test_evaluator_failure_propagates_but_cleanup_still_runs() -> None:
    failure = RuntimeError("evaluator bug")
    executor, events, opened = _executor(_handler())

    def broken_evaluator(snapshot: CatalogSnapshot):
        raise failure

    with pytest.raises(RuntimeError) as excinfo:
        executor.verify_schema_locked(
            lambda snapshot: None,
            catalog_probe=read_public_catalog,
            physical_evaluator=broken_evaluator,
        )
    assert excinfo.value is failure
    assert events[-2:] == ["unlock", "close"]
    assert opened[0].closed == 1


def test_rollback_failure_is_a_driver_error_not_a_success() -> None:
    executor, events, opened = _executor(_handler(fail_on="rollback"))
    with pytest.raises(MigrationDriverError) as excinfo:
        executor.verify_schema_locked(
            lambda snapshot: None, catalog_probe=read_public_catalog
        )
    assert "transaction could not be ended" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert events[-2:] == ["unlock", "close"]
    assert opened[0].closed == 1


def test_unconfirmed_unlock_after_success_is_a_cleanup_failure() -> None:
    executor, events, opened = _executor(_handler(unlock_result=False))
    with pytest.raises(MigrationCleanupError):
        executor.verify_schema_locked(
            lambda snapshot: None, catalog_probe=read_public_catalog
        )
    assert events[-2:] == ["unlock", "close"]
    assert opened[0].closed == 1


def test_unlock_exception_after_success_is_a_cleanup_failure() -> None:
    executor, _, opened = _executor(_handler(raise_on_unlock=True))
    with pytest.raises(MigrationCleanupError) as excinfo:
        executor.verify_schema_locked(
            lambda snapshot: None, catalog_probe=read_public_catalog
        )
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert opened[0].closed == 1


def test_close_failure_after_success_is_a_cleanup_failure() -> None:
    executor, events, _ = _executor(_handler(), close_error=OSError("close failed"))
    with pytest.raises(MigrationCleanupError) as excinfo:
        executor.verify_schema_locked(
            lambda snapshot: None, catalog_probe=read_public_catalog
        )
    assert "could not be closed" in str(excinfo.value)
    assert events[-2:] == ["unlock", "close"]


def test_primary_catalog_failure_is_not_masked_by_cleanup() -> None:
    executor, events, _ = _executor(
        _handler(fail_on="catalog_relations"), close_error=OSError("close failed")
    )
    with pytest.raises(MigrationDriverError) as excinfo:
        executor.verify_schema_locked(
            lambda snapshot: None, catalog_probe=read_public_catalog
        )
    assert "catalog could not be read" in str(excinfo.value)
    assert excinfo.value.__notes__
    assert isinstance(excinfo.value.__cause__, CatalogProbeError)
    assert events[-2:] == ["unlock", "close"]


def test_g2_refusal_is_preserved_when_cleanup_also_fails() -> None:
    refusal = StartupSchemaError(MIGRATIONS_PENDING, "pending")
    executor, events, opened = _executor(_handler(), close_error=OSError("boom"))

    def evaluate(snapshot: MigrationSnapshot) -> None:
        raise refusal

    with pytest.raises(StartupSchemaError) as excinfo:
        executor.verify_schema_locked(
            evaluate, catalog_probe=read_public_catalog
        )
    assert excinfo.value is refusal
    assert excinfo.value.__notes__
    assert opened[0].closed == 1


def test_non_callable_seams_are_rejected_before_connecting() -> None:
    executor, events, opened = _executor(_handler())
    with pytest.raises(TypeError):
        executor.verify_schema_locked(lambda snapshot: None, catalog_probe=object())
    with pytest.raises(TypeError):
        executor.verify_schema_locked(
            lambda snapshot: None,
            catalog_probe=read_public_catalog,
            physical_evaluator=object(),
        )
    assert opened == []
    assert events == []


# ---------------------------------------------------------------------------
# Result shape and safety
# ---------------------------------------------------------------------------


def test_combined_result_is_immutable_and_carries_no_connection_detail() -> None:
    result, _, _ = _combined(_handler(), probe_calls=[])
    assert isinstance(result, LockedSchemaVerification)
    assert isinstance(result.migration, MigrationSnapshot)
    assert isinstance(result.physical, SchemaEvaluation)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.migration = None  # type: ignore[misc]
    text = repr(result)
    assert "super-secret" not in text
    assert "postgresql://" not in text


def test_combined_verification_issues_no_writing_statement() -> None:
    executor, _, opened = _executor(_handler())
    executor.verify_schema_locked(
        lambda snapshot: None, catalog_probe=read_public_catalog
    )
    connection = opened[0]
    assert connection.commits == 0
    forbidden = re.compile(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TRUNCATE)\b")
    for sql, _ in connection.statements:
        upper = sql.strip().upper()
        assert upper.startswith(("SELECT", "BEGIN", "ROLLBACK")), upper
        # Word boundaries: catalog identifiers such as ``attisdropped`` must
        # not be mistaken for a DDL/DML keyword.
        assert forbidden.search(upper) is None, upper


def test_startup_path_activates_physical_verification_through_this_operation() -> None:
    """S5 activates the gate here, in one place, with the production seams.

    S4 recorded that startup did *not* yet enforce the verdict; S5 deliberately
    reverses that, so this test now pins the active wiring instead. The gate
    must hand the real probe and evaluator to this combined operation (a bare
    ``MigrationSnapshot`` is not a successful verification), and the API/CLI
    layers must stay thin rather than call S2 or S3 themselves.
    """
    gate = (PROJECT_ROOT / "src" / "startup_schema.py").read_text(encoding="utf-8")
    for symbol in (
        "catalog_probe=read_public_catalog",
        "physical_evaluator=evaluate_physical_schema",
        "LockedSchemaVerification",
        "if evaluation.compatible:",
        "CompatibilityStatus.INDETERMINATE",
    ):
        assert symbol in gate, f"src/startup_schema.py must reference {symbol}"
    for relative in ("src/api.py", "tools/run_api.py"):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        for symbol in (
            "catalog_probe",
            "read_public_catalog",
            "evaluate_physical_schema",
            "LockedSchemaVerification",
        ):
            assert symbol not in source, f"{relative} must not reference {symbol}"


def test_executor_import_still_pulls_no_driver() -> None:
    code = (
        "import json, sys\n"
        "import src.db_migrate_executor\n"
        "names = ('psycopg', 'psycopg2', 'socket')\n"
        "print(json.dumps([name for name in names if name in sys.modules]))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == []
