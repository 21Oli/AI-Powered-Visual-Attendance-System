"""Offline tests for the expanded Tier-2 catalog inventory (gap closure).

Fake-driven only: a recording connection stands in for psycopg, so the
category coverage, single-snapshot transaction discipline, and cleanup are
observable without a database. No PostgreSQL server is contacted.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tier2_support import CatalogInventory, catalog_inventory

DSN = "postgresql://tier2_user:super-secret@localhost:5432/visual_attendance_tier2_test"

#: Fixed so two fake-backed inventory reads are byte-for-byte comparable.
_GUARD_CREATED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

FORBIDDEN_FRAGMENTS = (
    "COMMIT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "CREATE ",
    "DROP ",
    "ALTER ",
    "TRUNCATE",
)


class _Result:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class _FakeConnection:
    def __init__(self, catalogue, *, fail_on: str | None = None) -> None:
        self.catalogue = catalogue
        self.fail_on = fail_on
        self.autocommit = False
        self.read_only = False
        self.statements: list[str] = []
        self.closed = 0
        self.commits = 0

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if self.fail_on is not None and self.fail_on in sql:
            raise RuntimeError("catalogue read failed")
        return _Result(self.catalogue(sql))

    def close(self) -> None:
        self.closed += 1


def _catalogue(sql: str):
    if "BEGIN ISOLATION" in sql or sql.strip() == "ROLLBACK":
        return []
    if sql.startswith("SELECT c.relname, c.relkind"):
        # Sorted as the SQL's ORDER BY relname, relkind would return them.
        return [
            ("employee", "r"),
            ("employee_state", "c"),
            ("idx_employee_full_name", "i"),
        ]
    if "format_type(" in sql:
        return [("employee", "employee_id", "text", True)]
    if "pg_constraint" in sql:
        return [("employee_pkey", "p", "employee")]
    if "pg_indexes" in sql:
        return [("employee_pkey", "CREATE UNIQUE INDEX employee_pkey ...")]
    if "pg_proc" in sql:
        return [("employee_count", "")]
    if "pg_trigger" in sql:
        return [("employee", "employee_touch")]
    if "pg_policy" in sql:
        return [("employee", "employee_visible")]
    if "pg_rewrite" in sql:
        return [("v_active_employee", "v_active_employee_audit")]
    if "FROM pg_type" in sql:
        return [("public", "employee_state", "e")]
    if "FROM pg_namespace" in sql:
        return [("public",)]
    if "FROM pg_extension" in sql:
        return [("plpgsql",)]
    if "_phase3_test_guard" in sql and "count(*)" in sql:
        return [(1,)]
    if "_phase3_test_guard" in sql:
        return [("VISUAL_ATTENDANCE_PHASE3_TEST_ONLY", _GUARD_CREATED_AT, 1)]
    if "count(*)" in sql and "employee" in sql:
        return [(3,)]
    if "count(*)" in sql and "schema_version" in sql:
        return [(1,)]
    if "schema_version" in sql:
        return [(1, "abc")]
    raise AssertionError(f"unexpected SQL: {sql}")


def _opener(monkeypatch, *, fail_on: str | None = None):
    connections: list[_FakeConnection] = []
    opened: list[str] = []

    def open_connection(dsn: str) -> _FakeConnection:
        opened.append(dsn)
        connection = _FakeConnection(_catalogue, fail_on=fail_on)
        connections.append(connection)
        return connection

    monkeypatch.setattr("src.db.open_connection", open_connection)
    return opened, connections


def test_inventory_covers_every_required_category(monkeypatch) -> None:
    _, connections = _opener(monkeypatch)

    inventory = catalog_inventory(DSN)

    assert inventory.relations == (
        ("employee", "r"),
        ("employee_state", "c"),
        ("idx_employee_full_name", "i"),
    )
    assert inventory.columns == (("employee", "employee_id", "text", True),)
    assert inventory.constraints == (("employee_pkey", "p", "employee"),)
    assert inventory.indexes == (("employee_pkey", "CREATE UNIQUE INDEX employee_pkey ..."),)
    assert inventory.functions == (("employee_count", ""),)
    assert inventory.triggers == (("employee", "employee_touch"),)
    assert inventory.policies == (("employee", "employee_visible"),)
    assert inventory.rules == (("v_active_employee", "v_active_employee_audit"),)
    assert inventory.types == (("public", "employee_state", "e"),)
    assert inventory.schema_names == ("public",)
    assert inventory.extensions == ("plpgsql",)
    assert inventory.ledger_rows == ((1, "abc"),)
    assert inventory.ledger_count == 1
    assert inventory.employee_count == 3
    assert inventory.guard_count == 1
    assert len(inventory.guard_rows) == 1
    assert connections[0].closed == 1


def test_inventory_covers_the_approved_catalog_sources(monkeypatch) -> None:
    _, connections = _opener(monkeypatch)

    catalog_inventory(DSN)

    joined = "\n".join(connections[0].statements)
    markers = [
        "'r', 'p', 'S', 'v', 'm', 'c', 'f', 'i', 'I'",  # every relation kind
        "pg_indexes",  # index definitions
        "pg_proc",  # functions
        "pg_trigger",
        "NOT t.tgisinternal",  # non-internal triggers only
        "pg_policy",  # policies
        "pg_rewrite",
        "'_RETURN'",  # auto view rules excluded
        "FROM pg_type",
        "typtype IN ('e', 'd', 'c')",  # enum, domain, composite
        "c.relkind = 'c'",  # table row types excluded
        "FROM pg_namespace",
        "nspname !~ '^pg_'",  # system schemas excluded
        "information_schema",
        "FROM pg_extension",
    ]
    for marker in markers:
        assert marker in joined, marker


def test_inventory_reads_everything_from_one_read_only_snapshot(monkeypatch) -> None:
    _, connections = _opener(monkeypatch)

    catalog_inventory(DSN)

    connection = connections[0]
    assert connection.autocommit is True
    assert connection.read_only is True
    assert connection.statements[0] == (
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
    )
    assert connection.statements[-1] == "ROLLBACK"
    assert connection.commits == 0
    for statement in connection.statements:
        for fragment in FORBIDDEN_FRAGMENTS:
            assert fragment not in statement, (fragment, statement)


def test_inventory_closes_the_connection_on_failure(monkeypatch) -> None:
    _, connections = _opener(monkeypatch, fail_on="pg_proc")

    with pytest.raises(RuntimeError):
        catalog_inventory(DSN)

    assert connections[0].closed == 1


def test_inventory_uses_only_the_explicit_target(monkeypatch) -> None:
    opened, _ = _opener(monkeypatch)

    catalog_inventory(DSN)

    assert opened == [DSN]


def test_inventory_is_stable_across_runs(monkeypatch) -> None:
    _opener(monkeypatch)

    first = catalog_inventory(DSN)
    second = catalog_inventory(DSN)

    assert first == second
    assert isinstance(first, CatalogInventory)


def test_counts_match_the_rows_read_in_the_same_snapshot(monkeypatch) -> None:
    _opener(monkeypatch)

    inventory = catalog_inventory(DSN)

    assert inventory.ledger_count == len(inventory.ledger_rows)
    assert inventory.guard_count == len(inventory.guard_rows)
