"""Offline tests for the G8 S2 catalog probe (Step 39).

No PostgreSQL connection, no driver import, no SQL execution against a
database and no filesystem access: every probe call runs against a recording
fake connection that returns canned rows for the module's fixed statements.

What is asserted is the *contract of the probe*: exactly four fixed
``pg_catalog`` SELECTs, no ``information_schema``, no parameters or
interpolation, no connection/transaction lifecycle in the module, deterministic
ordering independent of row order, immutable records, and a failure that can
never be mistaken for a smaller valid schema.
"""

from __future__ import annotations

import ast
import inspect
import re
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import src.db_schema_catalog as catalog
from src.db_schema_catalog import (
    PROBED_SCHEMA,
    CatalogProbeError,
    CatalogSnapshot,
    ColumnFact,
    ConstraintFact,
    IndexFact,
    RelationFact,
    read_public_catalog,
)
from src.schema_contract import TARGET_SCHEMA

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_FIXED_STATEMENTS = (
    catalog._RELATIONS_SQL,
    catalog._COLUMNS_SQL,
    catalog._CONSTRAINTS_SQL,
    catalog._INDEXES_SQL,
)

#: Statements that would write, change state, or end the caller's read-only
#: snapshot. None of these may appear anywhere in the probe's SQL.
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|"
    r"BEGIN|COMMIT|ROLLBACK|SAVEPOINT|LOCK|SET|COPY|VACUUM|ANALYZE|CALL|DO)\b"
)


# ---------------------------------------------------------------------------
# Recording fake connection
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def fetchall(self) -> list:
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeCatalogConnection:
    """Serves canned rows per fixed statement; records everything else."""

    def __init__(self, rows=None, *, fail_on=()) -> None:
        self.rows = dict(rows or {})
        self.fail_on = set(fail_on)
        self.statements: list[tuple[str, object]] = []
        self.closed = 0
        self.commits = 0
        self.rollbacks = 0
        self.autocommit = None
        self.read_only = None

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if sql in self.fail_on:
            raise RuntimeError(
                "connection lost: postgresql://app_user:app-secret@dbhost:5432/attendance"
            )
        return FakeCursor(self.rows.get(sql, []))

    def close(self) -> None:
        self.closed += 1

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _connection(
    *,
    relations=(),
    columns=(),
    constraints=(),
    indexes=(),
    fail_on=(),
) -> FakeCatalogConnection:
    return FakeCatalogConnection(
        {
            catalog._RELATIONS_SQL: relations,
            catalog._COLUMNS_SQL: columns,
            catalog._CONSTRAINTS_SQL: constraints,
            catalog._INDEXES_SQL: indexes,
        },
        fail_on=fail_on,
    )


#: Row shapes exactly as psycopg would return them.
EMPLOYEE_COLUMNS = [
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
]
EMPLOYEE_PK = [
    (
        "public",
        "employee",
        "employee_pkey",
        "p",
        "PRIMARY KEY (employee_id)",
        ["employee_id"],
    )
]
EMPLOYEE_PKEY_INDEX = [
    (
        "public",
        "employee",
        "employee_pkey",
        True,
        True,
        "CREATE UNIQUE INDEX employee_pkey ON public.employee USING btree (employee_id)",
        ["employee_id"],
    )
]


# ---------------------------------------------------------------------------
# Statement safety
# ---------------------------------------------------------------------------


def test_probe_issues_exactly_four_statements_in_order() -> None:
    connection = _connection()
    read_public_catalog(connection)
    assert [sql for sql, _ in connection.statements] == list(_FIXED_STATEMENTS)


@pytest.mark.parametrize("sql", _FIXED_STATEMENTS)
def test_every_statement_is_a_fixed_pg_catalog_select(sql: str) -> None:
    assert sql.startswith("SELECT ")
    assert "pg_catalog." in sql
    assert "information_schema" not in sql
    assert "n.nspname = 'public'" in sql
    assert "ORDER BY" in sql, "server-side ordering is part of the contract"
    # No interpolation or bound parameters: nothing to inject, nothing to bind.
    assert "%" not in sql
    assert "{" not in sql and "}" not in sql


@pytest.mark.parametrize("sql", _FIXED_STATEMENTS)
def test_no_statement_can_write_or_change_the_transaction(sql: str) -> None:
    assert not _FORBIDDEN_KEYWORDS.search(sql), _FORBIDDEN_KEYWORDS.search(sql)


def test_statements_reference_the_expected_catalog_functions() -> None:
    """A silently gutted query must fail here rather than pass vacuously."""
    assert "format_type(a.atttypid, a.atttypmod)" in catalog._COLUMNS_SQL
    assert "pg_get_expr(d.adbin, d.adrelid)" in catalog._COLUMNS_SQL
    assert "a.attnotnull" in catalog._COLUMNS_SQL
    assert "a.attgenerated" in catalog._COLUMNS_SQL
    assert "a.attidentity" in catalog._COLUMNS_SQL
    assert "NOT a.attisdropped" in catalog._COLUMNS_SQL
    assert "pg_constraint" in catalog._CONSTRAINTS_SQL
    assert "pg_get_constraintdef(con.oid)" in catalog._CONSTRAINTS_SQL
    assert "WITH ORDINALITY" in catalog._CONSTRAINTS_SQL
    assert "pg_index" in catalog._INDEXES_SQL
    assert "i.indisprimary" in catalog._INDEXES_SQL
    assert "i.indisunique" in catalog._INDEXES_SQL
    assert "pg_get_indexdef(i.indexrelid)" in catalog._INDEXES_SQL
    assert "c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f', 'c', 'i', 'I')" in (
        catalog._RELATIONS_SQL
    )


def test_probe_passes_no_parameters() -> None:
    connection = _connection()
    read_public_catalog(connection)
    assert [params for _, params in connection.statements] == [None] * 4


def test_probe_never_touches_the_connection_lifecycle() -> None:
    """The caller owns connect/commit/rollback/close and the advisory lock."""
    connection = _connection()
    read_public_catalog(connection)
    assert connection.closed == 0
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert connection.autocommit is None
    assert connection.read_only is None


def test_probe_takes_a_supplied_connection_and_nothing_else() -> None:
    parameters = list(inspect.signature(read_public_catalog).parameters.values())
    assert [parameter.name for parameter in parameters] == ["connection"]
    assert parameters[0].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Fact mapping
# ---------------------------------------------------------------------------


def test_relation_kinds_are_preserved() -> None:
    rows = [
        ("public", "employee", "r"),
        ("public", "events_partitioned", "p"),
        ("public", "employee_view", "v"),
        ("public", "employee_matview", "m"),
        ("public", "employee_seq", "S"),
        ("public", "remote_employee", "f"),
        ("public", "employee_type", "c"),
        ("public", "employee_pkey", "i"),
        ("public", "employee_partitioned_pkey", "I"),
    ]
    snapshot = read_public_catalog(_connection(relations=rows))
    # Sorted by name, not by insertion order; kinds are preserved verbatim.
    assert {fact.name: fact.kind for fact in snapshot.relations} == {
        "employee": "r",
        "events_partitioned": "p",
        "employee_view": "v",
        "employee_matview": "m",
        "employee_seq": "S",
        "remote_employee": "f",
        "employee_type": "c",
        "employee_pkey": "i",
        "employee_partitioned_pkey": "I",
    }
    assert all(isinstance(fact, RelationFact) for fact in snapshot.relations)
    assert all(fact.schema == "public" for fact in snapshot.relations)
    assert [fact.name for fact in snapshot.relations] == sorted(
        fact.name for fact in snapshot.relations
    )


def test_relation_is_table_only_for_ordinary_and_partitioned_tables() -> None:
    snapshot = read_public_catalog(
        _connection(
            relations=[
                ("public", "employee", "r"),
                ("public", "events", "p"),
                ("public", "employee_view", "v"),
                ("public", "employee_pkey", "i"),
            ]
        )
    )
    assert {fact.name: fact.is_table for fact in snapshot.relations} == {
        "employee": True,
        "events": True,
        "employee_view": False,
        "employee_pkey": False,
    }


def test_column_mapping_preserves_type_nullability_and_ordinal() -> None:
    snapshot = read_public_catalog(_connection(columns=EMPLOYEE_COLUMNS))
    assert tuple(snapshot.columns) == (
        ColumnFact("public", "employee", "employee_id", "text", False, None, "", "", 1),
        ColumnFact("public", "employee", "full_name", "text", False, None, "", "", 2),
        ColumnFact("public", "employee", "is_active", "boolean", False, "true", "", "", 3),
        ColumnFact(
            "public",
            "employee",
            "created_at",
            "timestamp with time zone",
            False,
            None,
            "",
            "",
            4,
        ),
    )
    # attnotnull True means NOT NULL, so the fact's ``nullable`` is False.
    assert all(column.nullable is False for column in snapshot.columns)


def test_a_nullable_column_is_reported_as_nullable() -> None:
    rows = [("public", "employee", "nickname", "text", False, None, "", "", 5)]
    snapshot = read_public_catalog(_connection(columns=rows))
    assert snapshot.columns[0].nullable is True


def test_missing_default_is_none_and_an_expression_is_kept_verbatim() -> None:
    rows = [
        ("public", "employee", "created_at", "timestamp with time zone", True, None, "", "", 4),
        ("public", "employee", "is_active", "boolean", True, "true", "", "", 3),
        ("public", "employee", "stamp", "timestamp with time zone", True, "now()", "", "", 5),
    ]
    snapshot = read_public_catalog(_connection(columns=rows))
    by_name = {column.name: column for column in snapshot.columns}
    assert by_name["created_at"].default_expression is None
    assert by_name["created_at"].has_default is False
    assert by_name["is_active"].default_expression == "true"
    assert by_name["is_active"].has_default is True
    assert by_name["stamp"].default_expression == "now()"


def test_generated_and_identity_flags_are_reported_separately_from_defaults() -> None:
    rows = [
        ("public", "employee", "computed", "text", True, "(lower(full_name))", "s", "", 5),
        ("public", "employee", "counter", "integer", True, None, "", "a", 6),
    ]
    snapshot = read_public_catalog(_connection(columns=rows))
    computed, counter = snapshot.columns
    assert computed.is_generated is True
    assert computed.generated == "s"
    assert computed.is_identity is False
    # A generated column has an expression but must never read as "defaulted".
    assert computed.has_default is True
    assert counter.is_identity is True
    assert counter.identity == "a"
    assert counter.is_generated is False


def test_primary_key_columns_keep_the_constraint_order() -> None:
    rows = [
        ("public", "composite", "composite_pkey", "p", "PRIMARY KEY (b, a)", ["b", "a"])
    ]
    snapshot = read_public_catalog(_connection(constraints=rows))
    primary_key = snapshot.primary_key_for("composite")
    assert primary_key is not None
    assert primary_key.columns == ("b", "a"), "order is the constraint's, not sorted"
    assert primary_key.is_primary_key is True
    assert primary_key.contype == "p"


def test_check_constraint_has_no_columns_but_keeps_its_definition() -> None:
    rows = [
        (
            "public",
            "employee",
            "employee_full_name_check",
            "c",
            "CHECK ((length(full_name) > 0))",
            [],
        )
    ]
    snapshot = read_public_catalog(_connection(constraints=rows))
    constraint = snapshot.constraints[0]
    assert constraint.columns == ()
    assert constraint.is_primary_key is False
    assert "length" in constraint.definition


def test_index_metadata_is_preserved() -> None:
    rows = [
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
            "employee",
            "employee_name_idx",
            False,
            False,
            "CREATE INDEX employee_name_idx ON public.employee USING btree (full_name)",
            ["full_name"],
        ),
    ]
    snapshot = read_public_catalog(_connection(indexes=rows))
    assert tuple(snapshot.indexes) == (
        IndexFact(
            "public",
            "employee",
            "employee_name_idx",
            False,
            False,
            ("full_name",),
            "CREATE INDEX employee_name_idx ON public.employee USING btree (full_name)",
        ),
        IndexFact(
            "public",
            "employee",
            "employee_pkey",
            True,
            True,
            ("employee_id",),
            "CREATE UNIQUE INDEX employee_pkey ON public.employee USING btree (employee_id)",
        ),
    )


def test_expression_index_has_no_column_names_but_keeps_its_definition() -> None:
    rows = [
        (
            "public",
            "employee",
            "employee_lower_name_idx",
            False,
            False,
            "CREATE INDEX employee_lower_name_idx ON public.employee USING btree (lower(full_name))",
            [],
        )
    ]
    snapshot = read_public_catalog(_connection(indexes=rows))
    index = snapshot.indexes[0]
    assert index.columns == ()
    assert "lower(full_name)" in index.definition


# ---------------------------------------------------------------------------
# Determinism and immutability
# ---------------------------------------------------------------------------


def test_relations_are_sorted_regardless_of_row_order() -> None:
    rows = [
        ("public", "zeta", "r"),
        ("public", "alpha", "v"),
        ("public", "alpha", "r"),
    ]
    forward = read_public_catalog(_connection(relations=rows))
    backward = read_public_catalog(_connection(relations=list(reversed(rows))))
    assert forward == backward
    assert [(fact.name, fact.kind) for fact in forward.relations] == [
        ("alpha", "r"),
        ("alpha", "v"),
        ("zeta", "r"),
    ]


def test_columns_are_sorted_by_physical_ordinal_regardless_of_row_order() -> None:
    rows = list(reversed(EMPLOYEE_COLUMNS))
    snapshot = read_public_catalog(_connection(columns=rows))
    assert [column.name for column in snapshot.columns] == [
        "employee_id",
        "full_name",
        "is_active",
        "created_at",
    ]
    assert [column.ordinal for column in snapshot.columns] == [1, 2, 3, 4]


def test_constraints_and_indexes_are_sorted_by_name() -> None:
    constraints = [
        ("public", "employee", "z_last", "u", "UNIQUE (full_name)", ["full_name"]),
        ("public", "employee", "a_first", "p", "PRIMARY KEY (employee_id)", ["employee_id"]),
    ]
    indexes = [
        ("public", "employee", "z_idx", False, False, "CREATE INDEX z_idx", ["full_name"]),
        ("public", "employee", "employee_pkey", True, True, "CREATE UNIQUE INDEX", ["employee_id"]),
    ]
    snapshot = read_public_catalog(
        _connection(constraints=constraints, indexes=indexes)
    )
    assert [fact.name for fact in snapshot.constraints] == ["a_first", "z_last"]
    assert [fact.name for fact in snapshot.indexes] == ["employee_pkey", "z_idx"]


def test_probing_twice_yields_equal_snapshots() -> None:
    first = read_public_catalog(
        _connection(
            relations=[("public", "employee", "r")],
            columns=EMPLOYEE_COLUMNS,
            constraints=EMPLOYEE_PK,
            indexes=EMPLOYEE_PKEY_INDEX,
        )
    )
    second = read_public_catalog(
        _connection(
            relations=[("public", "employee", "r")],
            columns=EMPLOYEE_COLUMNS,
            constraints=EMPLOYEE_PK,
            indexes=EMPLOYEE_PKEY_INDEX,
        )
    )
    assert first == second


def test_facts_and_snapshot_are_immutable() -> None:
    snapshot = read_public_catalog(
        _connection(
            relations=[("public", "employee", "r")],
            columns=EMPLOYEE_COLUMNS,
            constraints=EMPLOYEE_PK,
            indexes=EMPLOYEE_PKEY_INDEX,
        )
    )
    with pytest.raises(FrozenInstanceError):
        snapshot.relations[0].name = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.columns[0].nullable = True  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.constraints[0].contype = "u"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.indexes[0].is_unique = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.relations = ()  # type: ignore[misc]


def test_collections_are_tuples_not_lists_or_dicts() -> None:
    snapshot = read_public_catalog(_connection(columns=EMPLOYEE_COLUMNS))
    for collection in (
        snapshot.relations,
        snapshot.columns,
        snapshot.constraints,
        snapshot.indexes,
    ):
        assert isinstance(collection, tuple)
    assert isinstance(snapshot.columns[0].pg_type, str)
    assert isinstance(snapshot.constraints, tuple)
    assert isinstance(snapshot.columns_for("employee"), tuple)


# ---------------------------------------------------------------------------
# Failure handling: never a partial or empty-as-valid snapshot
# ---------------------------------------------------------------------------


def test_query_failure_raises_catalog_probe_error_and_returns_nothing() -> None:
    connection = _connection(columns=EMPLOYEE_COLUMNS, fail_on=(catalog._COLUMNS_SQL,))
    with pytest.raises(CatalogProbeError) as excinfo:
        read_public_catalog(connection)
    assert excinfo.value.args == (catalog._CATALOG_UNREADABLE,)


def test_failure_on_a_later_statement_cannot_yield_a_partial_snapshot() -> None:
    connection = _connection(
        relations=[("public", "employee", "r")],
        columns=EMPLOYEE_COLUMNS,
        fail_on=(catalog._INDEXES_SQL,),
    )
    with pytest.raises(CatalogProbeError):
        read_public_catalog(connection)


def test_error_message_is_fixed_and_sanitized() -> None:
    connection = _connection(fail_on=(catalog._RELATIONS_SQL,))
    with pytest.raises(CatalogProbeError) as excinfo:
        read_public_catalog(connection)
    text = str(excinfo.value)
    for fragment in (
        "postgresql",
        "app_user",
        "app-secret",
        "dbhost",
        "5432",
        "connection lost",
        "RuntimeError",
        "Traceback",
        "public",
        "employee",
    ):
        assert fragment not in text


def test_error_preserves_the_driver_cause_for_trusted_diagnostics() -> None:
    connection = _connection(fail_on=(catalog._RELATIONS_SQL,))
    with pytest.raises(CatalogProbeError) as excinfo:
        read_public_catalog(connection)
    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_empty_catalog_is_a_valid_empty_snapshot() -> None:
    """Zero rows is genuinely empty (G2 refuses a missing ledger before S3)."""
    snapshot = read_public_catalog(_connection())
    assert snapshot == CatalogSnapshot((), (), (), ())
    assert snapshot.primary_key_for("employee") is None


# ---------------------------------------------------------------------------
# Snapshot helpers used by S3
# ---------------------------------------------------------------------------


def test_snapshot_lookup_helpers() -> None:
    snapshot = read_public_catalog(
        _connection(
            relations=[("public", "employee", "r")],
            columns=EMPLOYEE_COLUMNS,
            constraints=EMPLOYEE_PK,
            indexes=EMPLOYEE_PKEY_INDEX,
        )
    )
    assert [fact.name for fact in snapshot.relations_named("employee")] == ["employee"]
    assert snapshot.relations_named("attendance_event") == ()
    assert [column.name for column in snapshot.columns_for("employee")] == [
        "employee_id",
        "full_name",
        "is_active",
        "created_at",
    ]
    assert snapshot.columns_for("schema_version") == ()
    assert snapshot.primary_key_for("employee") is not None
    assert snapshot.primary_key_for("schema_version") is None
    assert [index.name for index in snapshot.indexes_for("employee")] == [
        "employee_pkey"
    ]


# ---------------------------------------------------------------------------
# Module hygiene and boundaries
# ---------------------------------------------------------------------------

_ALLOWED_IMPORT_ROOTS = {"__future__", "dataclasses", "typing"}


def test_module_imports_only_pure_standard_library_modules() -> None:
    """No driver, no database layer, no test support, no policy module."""
    tree = ast.parse(Path(catalog.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            if node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= _ALLOWED_IMPORT_ROOTS, imported - _ALLOWED_IMPORT_ROOTS


def test_module_does_not_import_test_support_or_policy() -> None:
    """No project import at all: the probe is standalone policy-free data access."""
    tree = ast.parse(Path(catalog.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name.split(".")[0] in {"src", "tests"}}
    # Deliberately not asserting on ``sys.modules``: other Tier-2 modules may
    # already have imported the test harness in a full-suite run, which says
    # nothing about this module's imports (the AST check above is the proof).
    assert not hasattr(catalog, "is_prohibited_table_name")
    assert not hasattr(catalog, "REQUIRED_TABLES")


def test_probe_defines_no_diagnostic_codes() -> None:
    """S2 must not invent S3's codes: the only public constant is the schema."""
    constants = {
        name
        for name in dir(catalog)
        if name.isupper() and not name.startswith("_")
    }
    assert constants == {"PROBED_SCHEMA"}
    assert PROBED_SCHEMA == "public" == TARGET_SCHEMA


def test_import_hygiene_fresh_process() -> None:
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "import src.db_schema_catalog;"
        "banned = {'psycopg', 'socket', 'cv2', 'fastapi', 'uvicorn',"
        "'src.db', 'src.db_migrate', 'src.startup_schema', 'src.api',"
        "'tier2_support', 'tests.tier2_support'};"
        "loaded = banned & set(sys.modules);"
        "assert not loaded, f'unexpected imports: {loaded}';"
        "from src.db_schema_catalog import PROBED_SCHEMA as s;"
        "assert s == 'public', s;"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")
