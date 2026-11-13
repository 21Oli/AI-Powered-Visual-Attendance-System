"""Offline G8 S5 tests: the startup gate enforces the physical schema.

Everything is fake-driven: recording psycopg-shaped connections, the guard and
Uvicorn are stubs, and the runner is loaded as a module. The **real** S2 probe
and **real** S3 evaluator run against those fakes through the gate, so the
integration is exercised without a database.

Covered: the full startup decision matrix (only ``COMPATIBLE`` allows startup),
G2 precedence, catalog-unreadable and evaluator-failure refusals, the single
connection/lock/transaction lifecycle, guard release, exit code 4 with sanitized
findings, `--print-bind` independence, and import hygiene.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn

import src.api as api_module
import src.api_guard as guard_module
import src.startup_schema as gate
from src.api_guard import StartupGuardError
from src.config import AppSettings
from src.db_migrate import MigrationSnapshot
from src.db_migrate_executor import MigrationDriverError, PsycopgMigrationExecutor
from src.db_schema_catalog import CatalogProbeError, read_public_catalog
from src.physical_schema import (
    SCHEMA_COLUMN_TYPE_MISMATCH,
    SCHEMA_OBJECT_PROHIBITED,
    SCHEMA_TABLE_KIND_UNCONFIRMED,
    Finding,
    evaluate_physical_schema,
)
from src.startup_schema import (
    MIGRATIONS_PENDING,
    MIGRATION_LOCK_RELEASE_FAILED,
    SCHEMA_CATALOG_UNREADABLE,
    SCHEMA_INCOMPATIBLE,
    SCHEMA_INDETERMINATE,
    SCHEMA_VERIFICATION_FAILED,
    StartupSchemaError,
    verify_startup_schema,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = PROJECT_ROOT / "tools" / "run_api.py"

APP_DSN = "postgresql://app_user:app-secret@localhost:5432/attendance"
TEST_DSN = "postgresql://tester:test-secret@127.0.0.1:5432/attendance_test"

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
    def __init__(self, handler, events: list[str]) -> None:
        self.handler = handler
        self.events = events
        self.autocommit = False
        self.statements: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def execute(self, sql, params=None):
        self.events.append(_label(sql))
        self.statements.append((sql, params))
        return Result(self.handler(sql, params))

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1
        self.events.append("close")


def _healthy_rows():
    relations = [
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
    applied: tuple[tuple[int, str], ...] = (),
    relations=None,
    columns=None,
    constraints=None,
    indexes=None,
    lock_result: bool = True,
    unlock_result: bool = True,
    fail_on: str | None = None,
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
            raise RuntimeError("driver exploded: connection lost")
        if label == "try_lock":
            return [(lock_result,)]
        if label == "unlock":
            return [(unlock_result,)]
        if label == "ledger":
            return [("public.schema_version",)]
        if label == "shape":
            return list(EXPECTED_SHAPE)
        if label == "tables":
            return [
                ("employee",),
                ("app_user",),
                ("user_session",),
                ("schema_version",),
            ]
        if label == "applied":
            return list(applied)
        return list(rows.get(label, []))

    return handle


def _settings() -> AppSettings:
    return AppSettings(database_url=APP_DSN, test_database_url=TEST_DSN)


def _write_migrations(directory: Path, *versions: int) -> dict[int, str]:
    directory.mkdir(parents=True, exist_ok=True)
    for version in versions:
        (directory / f"V{version:03d}__step_{version}.sql").write_text(
            MIGRATION_SQL, encoding="utf-8"
        )
    from src.db_migrate import load_migrations

    return {script.version: script.checksum for script in load_migrations(directory)}


def _run_gate(
    tmp_path: Path,
    *,
    versions: tuple[int, ...] = (1, 2),
    applied_versions: tuple[int, ...] = (1, 2),
    handler_kwargs: dict | None = None,
):
    directory = tmp_path / "migrations"
    checksums = _write_migrations(directory, *versions)
    applied = tuple((version, checksums[version]) for version in applied_versions)
    events: list[str] = []
    opened: list[RecordingConnection] = []
    handler = _handler(applied=applied, **(handler_kwargs or {}))

    def factory(dsn: str):
        def opener(inner_dsn: str) -> RecordingConnection:
            connection = RecordingConnection(handler, events)
            opened.append(connection)
            events.append("connect")
            return connection

        return PsycopgMigrationExecutor(dsn, connection_opener=opener)

    result = verify_startup_schema(
        _settings(), migrations_dir=directory, executor_factory=factory
    )
    return result, events, opened


# ---------------------------------------------------------------------------
# Decision matrix
# ---------------------------------------------------------------------------


def test_compatible_physical_schema_allows_startup(tmp_path: Path) -> None:
    result, events, opened = _run_gate(tmp_path)
    assert result is None
    assert "catalog_relations" in events and "catalog_indexes" in events
    assert len(opened) == 1 and opened[0].closed == 1


def test_incompatible_physical_schema_refuses(tmp_path: Path) -> None:
    """A type drift on the employee table is refused with its finding code.

    Only the employee table is mutated: the identity tables share column
    names (``created_at``, ``user_id``), so a blanket mutation would produce
    findings that do not start with ``public.employee``.
    """
    _, columns, _, _ = _healthy_rows()
    drifted = [
        tuple(row[:3]) + ("integer",) + tuple(row[4:])
        if row[1] == "employee"
        else row
        for row in columns
    ]
    with pytest.raises(StartupSchemaError) as excinfo:
        _run_gate(tmp_path, handler_kwargs={"columns": drifted})
    error = excinfo.value
    assert error.code == SCHEMA_INCOMPATIBLE
    assert SCHEMA_COLUMN_TYPE_MISMATCH in error.finding_codes
    assert error.findings[0].label.startswith("public.employee")


def test_indeterminate_physical_schema_refuses(tmp_path: Path) -> None:
    relations, _, _, _ = _healthy_rows()
    partitioned = [("public", "employee", "p")] + list(relations[1:])
    with pytest.raises(StartupSchemaError) as excinfo:
        _run_gate(tmp_path, handler_kwargs={"relations": partitioned})
    error = excinfo.value
    assert error.code == SCHEMA_INDETERMINATE
    assert error.finding_codes == (SCHEMA_TABLE_KIND_UNCONFIRMED,)


def test_prohibited_view_refuses_with_its_finding_code(tmp_path: Path) -> None:
    relations, _, _, _ = _healthy_rows()
    with pytest.raises(StartupSchemaError) as excinfo:
        _run_gate(
            tmp_path,
            handler_kwargs={
                "relations": relations + [("public", "attendance_events", "v")]
            },
        )
    error = excinfo.value
    assert error.code == SCHEMA_INCOMPATIBLE
    assert error.finding_codes == (SCHEMA_OBJECT_PROHIBITED,)
    assert error.findings[0].category == "attendance"


def test_unrelated_public_table_is_allowed(tmp_path: Path) -> None:
    relations, columns, _, _ = _healthy_rows()
    result, _, _ = _run_gate(
        tmp_path,
        handler_kwargs={
            "relations": relations + [("public", "surface_metrics", "r")],
            "columns": columns
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
        },
    )
    assert result is None


def test_g2_history_refusal_prevents_the_catalog_read(tmp_path: Path) -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        _run_gate(tmp_path, versions=(1, 2), applied_versions=(1,))
    assert excinfo.value.code == MIGRATIONS_PENDING
    assert not excinfo.value.findings


def test_catalog_unreadable_refuses_with_the_reserved_code(tmp_path: Path) -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        _run_gate(tmp_path, handler_kwargs={"fail_on": "catalog_indexes"})
    error = excinfo.value
    assert error.code == SCHEMA_CATALOG_UNREADABLE
    assert error.findings == ()
    # The catalog failure remains diagnosable on the trusted side only.
    assert isinstance(error.__cause__, MigrationDriverError)
    assert isinstance(error.__cause__.__cause__, CatalogProbeError)
    assert "driver exploded" not in str(error)


def test_evaluator_failure_refuses_as_verification_failure(
    tmp_path: Path, monkeypatch
) -> None:
    def broken(catalog):
        raise RuntimeError("evaluator exploded")

    monkeypatch.setattr(gate, "evaluate_physical_schema", broken)
    with pytest.raises(StartupSchemaError) as excinfo:
        _run_gate(tmp_path)
    assert excinfo.value.code == SCHEMA_VERIFICATION_FAILED


def test_cleanup_failure_refuses(tmp_path: Path) -> None:
    with pytest.raises(StartupSchemaError) as excinfo:
        _run_gate(tmp_path, handler_kwargs={"unlock_result": False})
    assert excinfo.value.code == MIGRATION_LOCK_RELEASE_FAILED


def test_g2_failures_keep_their_own_codes(tmp_path: Path) -> None:
    """A G2 refusal is never relabelled as a physical refusal."""
    with pytest.raises(StartupSchemaError) as excinfo:
        _run_gate(tmp_path, versions=(1, 2), applied_versions=(1,))
    assert excinfo.value.code == MIGRATIONS_PENDING
    assert excinfo.value.code not in {
        SCHEMA_INCOMPATIBLE,
        SCHEMA_INDETERMINATE,
        SCHEMA_CATALOG_UNREADABLE,
    }


def test_unexpected_result_shape_is_a_verification_failure(tmp_path: Path) -> None:
    class StubExecutor:
        def __init__(self) -> None:
            self.seen: dict = {}

        def verify_schema_locked(self, evaluate, **kwargs):
            self.seen = kwargs
            return MigrationSnapshot(tables=frozenset(), ledger_present=True, applied=())

    stub = StubExecutor()
    with pytest.raises(StartupSchemaError) as excinfo:
        verify_startup_schema(
            _settings(), executor_factory=lambda dsn: stub
        )
    assert excinfo.value.code == SCHEMA_VERIFICATION_FAILED
    # The gate asks the executor for the combined operation with the
    # production probe and evaluator — never a hand-rolled second pass.
    assert stub.seen["catalog_probe"] is read_public_catalog
    assert stub.seen["physical_evaluator"] is evaluate_physical_schema


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_one_connection_one_lock_one_transaction(tmp_path: Path) -> None:
    _, events, opened = _run_gate(tmp_path)
    assert events.count("connect") == 1
    assert events.count("try_lock") == 1
    assert events.count("begin") == 1
    assert events.count("rollback") == 1
    assert events.count("unlock") == 1
    assert events.count("close") == 1
    assert len(opened) == 1


def test_catalog_is_read_inside_the_transaction_and_lock(tmp_path: Path) -> None:
    _, events, _ = _run_gate(tmp_path)
    begin = events.index("begin")
    rollback = events.index("rollback")
    unlock = events.index("unlock")
    catalog = [i for i, label in enumerate(events) if label.startswith("catalog_")]
    assert catalog
    assert all(begin < position < rollback for position in catalog)
    assert all(position < unlock for position in catalog)
    assert rollback < unlock < events.index("close")


def test_g2_reads_precede_the_catalog_reads(tmp_path: Path) -> None:
    _, events, _ = _run_gate(tmp_path)
    assert events.index("applied") < events.index("catalog_relations")


# ---------------------------------------------------------------------------
# Guard and Uvicorn wiring
# ---------------------------------------------------------------------------


class _RecordingGuard:
    def __init__(self, events: list[str], exit_error: Exception | None = None):
        self.lock_path = Path("guard.lock")
        self._events = events
        self._exit_error = exit_error

    def __enter__(self):
        self._events.append("guard_enter")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._events.append("guard_exit")
        if self._exit_error is not None:
            raise self._exit_error
        return False


def _install_server_fakes(monkeypatch, *, guard_exit_error=None) -> list[str]:
    events: list[str] = []
    monkeypatch.setattr(
        guard_module,
        "acquire_startup_guard",
        lambda settings: _RecordingGuard(events, guard_exit_error),
    )

    class RecordingServer:
        def __init__(self, config) -> None:
            events.append("uvicorn_init")

        def run(self) -> None:
            events.append("uvicorn_run")

    monkeypatch.setattr(uvicorn, "Server", RecordingServer)
    return events


def test_physical_refusal_prevents_uvicorn_and_releases_the_guard(
    monkeypatch,
) -> None:
    events = _install_server_fakes(monkeypatch)

    def refusing(settings) -> None:
        events.append("verify")
        raise StartupSchemaError(SCHEMA_INCOMPATIBLE, "physical schema mismatch")

    with pytest.raises(StartupSchemaError):
        api_module.run_server(_settings(), startup_verifier=refusing)
    assert events == ["guard_enter", "verify", "guard_exit"]
    assert "uvicorn_init" not in events and "uvicorn_run" not in events


def test_indeterminate_refusal_prevents_uvicorn(monkeypatch) -> None:
    events = _install_server_fakes(monkeypatch)

    def refusing(settings) -> None:
        raise StartupSchemaError(SCHEMA_INDETERMINATE, "physical schema unconfirmed")

    with pytest.raises(StartupSchemaError):
        api_module.run_server(_settings(), startup_verifier=refusing)
    assert events == ["guard_enter", "guard_exit"]


def test_guard_release_failure_never_serves(monkeypatch) -> None:
    events = _install_server_fakes(
        monkeypatch, guard_exit_error=StartupGuardError("guard release failed")
    )

    def refusing(settings) -> None:
        raise StartupSchemaError(SCHEMA_INCOMPATIBLE, "physical schema mismatch")

    with pytest.raises(StartupGuardError):
        api_module.run_server(_settings(), startup_verifier=refusing)
    assert "uvicorn_init" not in events and "uvicorn_run" not in events


def test_production_default_verifier_is_the_physical_gate(
    monkeypatch,
) -> None:
    events = _install_server_fakes(monkeypatch)
    calls: list[AppSettings] = []

    def recorded(settings: AppSettings) -> None:
        calls.append(settings)
        events.append("verify")

    monkeypatch.setattr("src.startup_schema.verify_startup_schema", recorded)
    settings = _settings()
    api_module.run_server(settings)
    assert calls == [settings]
    assert events == [
        "guard_enter",
        "verify",
        "uvicorn_init",
        "uvicorn_run",
        "guard_exit",
    ]


# ---------------------------------------------------------------------------
# Runner exit code and sanitized findings
# ---------------------------------------------------------------------------


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_api_under_test", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_physical_refusal_exits_4_with_safe_findings(monkeypatch, capsys) -> None:
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setenv("API_PORT", "8000")
    finding = Finding(
        code=SCHEMA_COLUMN_TYPE_MISMATCH,
        schema="public",
        object="employee",
        column="employee_id",
        detail="column type is 'integer'; the contract requires 'text'",
    )

    def refusing(settings) -> None:
        raise StartupSchemaError(
            SCHEMA_INCOMPATIBLE,
            gate._MESSAGES[SCHEMA_INCOMPATIBLE],
            findings=(finding,),
        )

    monkeypatch.setattr("src.api.run_server", refusing)
    rc = _load_runner().main([])
    captured = capsys.readouterr()

    assert rc == 4
    assert captured.out == ""
    assert "run-api: SCHEMA_INCOMPATIBLE:" in captured.err
    assert "run-api: schema finding: SCHEMA_COLUMN_TYPE_MISMATCH public.employee.employee_id" in captured.err
    for fragment in (
        "postgresql://",
        "app-secret",
        "test-secret",
        "Traceback",
        "psycopg",
        "driver exploded",
        "SELECT",
    ):
        assert fragment not in captured.err


def test_print_bind_never_verifies_the_schema() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ATTENDANCE_")
    }
    env["API_HOST"] = "127.0.0.1"
    env["API_PORT"] = "8022"
    completed = subprocess.run(
        [sys.executable, "tools/run_api.py", "--print-bind"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    # Without DSNs a verification would refuse with exit 4; a bind print proves
    # `--print-bind` performs none.
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "bind: 127.0.0.1:8022"
    assert completed.stderr == ""


def test_startup_module_import_pulls_no_driver() -> None:
    code = (
        "import json, sys\n"
        "import src.startup_schema\n"
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


def test_every_refusal_code_is_documented() -> None:
    for code in (
        SCHEMA_INCOMPATIBLE,
        SCHEMA_INDETERMINATE,
        SCHEMA_CATALOG_UNREADABLE,
    ):
        assert code in gate.DIAGNOSTIC_CODES
        assert code in gate._MESSAGES
        assert "refusing to start the API" in gate._MESSAGES[code]
    # The G2 vocabulary is unchanged.
    assert len([code for code in gate.DIAGNOSTIC_CODES if code.startswith("SCHEMA_")]) == 4
