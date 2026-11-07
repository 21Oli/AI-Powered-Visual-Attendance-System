"""Shared Tier-2 support for the migration-executor harness (Step 1.5b-6).

Small, explicit helpers so the live test module contains no duplicated
pre-flight logic:

* opt-in detection and the three pre-flight conditions;
* the existing guard, invoked unchanged (its ordered checks decide);
* :func:`bind_target_to_dsn` — proof that the DSN handed to an executor is
  the *verified* test target and not merely "some DSN that was configured";
* :func:`catalog_inventory` — a read-only fingerprint of every catalog
  category a migration attempt could disturb (relations of every relevant
  kind including indexes, function, non-internal triggers, policies, rules,
  user-defined types, schemas, extensions, plus the ledger/employee/guard
  rows), used as the artifact-preservation proof.

The fingerprint runs inside **one** ``REPEATABLE READ READ ONLY``
transaction on an explicitly targeted connection, so a category cannot be
read from a different point in time than the rows. System objects and
session activity are excluded by construction: the public schema only for
relations, ``NOT tgisinternal`` for triggers, no auto-created ``_RETURN``
rules, no table row types or array element types, and ``pg_*`` /
``information_schema`` excluded from the schema list.

Nothing here writes, and nothing here reads an employee row's contents.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import pytest

from src.config import AppSettings, load_settings
from src.db_target import normalized_target_identity
from src.test_database_guard import (
    GuardBlocked,
    VerifiedTestTarget,
    verify_test_database,
)

#: Explicit opt-in switch. Anything other than ``"1"`` leaves Tier-2 off.
TIER2_OPT_IN_ENV: Final[str] = "ATTENDANCE_TIER2_OPT_IN"

BLOCK_MESSAGES: Final[dict[str, str]] = {
    "OPT_IN_MISSING": f"{TIER2_OPT_IN_ENV}=1 is not set; Tier-2 verification is opt-in",
    "TEST_DSN_MISSING": "no explicit ATTENDANCE_TEST_DATABASE_URL is configured",
    "APP_DSN_MISSING": (
        "ATTENDANCE_DATABASE_URL must also be configured so the guard can "
        "prove the test target is distinct from it"
    ),
    "NOT_VERIFIED": "no positively verified test target was supplied",
    "TARGET_MISMATCH": (
        "the DSN handed to the executor is not the verified test target"
    ),
}


class Tier2NotConfigured(Exception):
    """A pre-flight or binding condition is missing; nothing was opened."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(
            f"{reason}: {message or BLOCK_MESSAGES.get(reason, 'blocked')}"
        )


def tier2_opt_in_enabled(environ: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return str(source.get(TIER2_OPT_IN_ENV, "")).strip() == "1"


def is_configured(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def tier2_block_reason(
    settings: AppSettings, environ: Mapping[str, str] | None = None
) -> str | None:
    """First missing pre-flight code, or ``None`` when everything is present."""
    if not tier2_opt_in_enabled(environ):
        return "OPT_IN_MISSING"
    if not is_configured(settings.test_database_url):
        return "TEST_DSN_MISSING"
    if not is_configured(settings.database_url):
        return "APP_DSN_MISSING"
    return None


def verified_target_or_skip(*, settings: AppSettings | None = None) -> VerifiedTestTarget:
    """Pre-flight, then the unchanged guard; any block becomes a typed skip."""
    resolved = settings if settings is not None else load_settings()
    reason = tier2_block_reason(resolved)
    if reason is not None:
        pytest.skip(f"tier2 blocked: {reason}: {BLOCK_MESSAGES[reason]}")
    try:
        return verify_test_database(resolved.database_url, resolved.test_database_url)
    except GuardBlocked as exc:
        pytest.skip(f"tier2 blocked: {exc}")


def bind_target_to_dsn(target: object, test_dsn: object) -> str:
    """Return ``test_dsn`` only when it denotes the verified test target.

    The executor is always constructed from this return value, so a stale or
    mistyped DSN cannot silently retarget a live test: identity is compared
    on the guard's server-confirmed dbname/host/port, with local host aliases
    collapsed by the same normalization the CLI uses.
    """
    if not isinstance(target, VerifiedTestTarget):
        raise Tier2NotConfigured("NOT_VERIFIED")
    if not is_configured(test_dsn):
        raise Tier2NotConfigured("TEST_DSN_MISSING")

    expected = normalized_target_identity(
        f"host={target.host} port={target.port} dbname={target.dbname}"
    )
    actual = normalized_target_identity(str(test_dsn))
    if expected is None or actual is None or expected != actual:
        raise Tier2NotConfigured("TARGET_MISMATCH")
    return str(test_dsn)


# ---------------------------------------------------------------------------
# Read-only catalog fingerprint (one snapshot, every §R4 category)
# ---------------------------------------------------------------------------

#: Every relation kind a migration could create in the public schema:
#: ordinary and partitioned tables, sequences, views, materialized views,
#: composite relations, foreign tables, and (partitioned) indexes.
_RELATIONS_SQL: Final[str] = (
    "SELECT c.relname, c.relkind FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' "
    "AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'c', 'f', 'i', 'I') "
    "ORDER BY c.relname, c.relkind"
)
_COLUMNS_SQL: Final[str] = (
    "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), "
    "a.attnotnull FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 "
    "AND NOT a.attisdropped "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
    "ORDER BY c.relname, a.attnum"
)
_CONSTRAINTS_SQL: Final[str] = (
    "SELECT conname, contype, conrelid::regclass::text FROM pg_constraint "
    "WHERE connamespace = 'public'::regnamespace "
    "ORDER BY conrelid::regclass::text, conname"
)
_INDEXES_SQL: Final[str] = (
    "SELECT indexname, indexdef FROM pg_indexes "
    "WHERE schemaname = 'public' ORDER BY indexname"
)
#: Functions in the public schema, disambiguated by identity arguments so an
#: overloaded name contributes one row per signature.
_FUNCTIONS_SQL: Final[str] = (
    "SELECT p.proname, pg_get_function_identity_arguments(p.oid) "
    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
    "WHERE n.nspname = 'public' ORDER BY p.proname, 2"
)
_TRIGGERS_SQL: Final[str] = (
    "SELECT c.relname, t.tgname FROM pg_trigger t "
    "JOIN pg_class c ON c.oid = t.tgrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND NOT t.tgisinternal "
    "ORDER BY c.relname, t.tgname"
)
_POLICIES_SQL: Final[str] = (
    "SELECT c.relname, p.polname FROM pg_policy p "
    "JOIN pg_class c ON c.oid = p.polrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' ORDER BY c.relname, p.polname"
)
#: User rules only: PostgreSQL auto-creates a ``_RETURN`` rule per view, so
#: excluding it keeps the fingerprint about authored objects.
_RULES_SQL: Final[str] = (
    "SELECT c.relname, r.rulename FROM pg_rewrite r "
    "JOIN pg_class c ON c.oid = r.ev_class "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND r.rulename <> '_RETURN' "
    "ORDER BY c.relname, r.rulename"
)
#: Enum, domain, and composite types only. Table row types (``typtype = 'c'``
#: whose ``typrelid`` is a real relation) and array element types are
#: excluded, so the fingerprint tracks authored types.
_TYPES_SQL: Final[str] = (
    "SELECT n.nspname, t.typname, t.typtype FROM pg_type t "
    "JOIN pg_namespace n ON n.oid = t.typnamespace "
    "WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd', 'c') "
    "AND (t.typrelid = 0 OR EXISTS ("
    "SELECT 1 FROM pg_class c WHERE c.oid = t.typrelid AND c.relkind = 'c')) "
    "ORDER BY t.typname"
)
#: Non-system schemas (``pg_catalog``, ``pg_toast``, temp schemas and
#: ``information_schema`` are excluded as server-managed objects).
_SCHEMAS_SQL: Final[str] = (
    "SELECT nspname FROM pg_namespace "
    "WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' "
    "ORDER BY nspname"
)
_EXTENSIONS_SQL: Final[str] = "SELECT extname FROM pg_extension ORDER BY extname"

_LEDGER_SQL: Final[str] = (
    "SELECT version, checksum FROM schema_version ORDER BY version"
)
_LEDGER_COUNT_SQL: Final[str] = "SELECT count(*) FROM schema_version"
_EMPLOYEE_COUNT_SQL: Final[str] = "SELECT count(*) FROM employee"
_GUARD_ROWS_SQL: Final[str] = "SELECT * FROM _phase3_test_guard ORDER BY 1"
_GUARD_COUNT_SQL: Final[str] = "SELECT count(*) FROM _phase3_test_guard"

#: One consistent view: every category below is read from this transaction.
_BEGIN_SNAPSHOT_SQL: Final[str] = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
_ROLLBACK_SQL: Final[str] = "ROLLBACK"


@dataclass(frozen=True)
class CatalogInventory:
    """Everything a migration attempt could disturb, read-only."""

    relations: tuple[tuple[str, str], ...]
    columns: tuple[tuple[str, str, str, bool], ...]
    constraints: tuple[tuple[str, str, str], ...]
    indexes: tuple[tuple[str, str], ...]
    functions: tuple[tuple[str, str], ...]
    triggers: tuple[tuple[str, str], ...]
    policies: tuple[tuple[str, str], ...]
    rules: tuple[tuple[str, str], ...]
    types: tuple[tuple[str, str, str], ...]
    schema_names: tuple[str, ...]
    extensions: tuple[str, ...]
    ledger_rows: tuple[tuple[int, str], ...]
    ledger_count: int
    employee_count: int
    guard_rows: tuple[tuple, ...]
    guard_count: int


def catalog_inventory(test_dsn: str) -> CatalogInventory:
    """Fingerprint the schema and the rows this harness must never touch.

    One explicitly targeted connection, one ``REPEATABLE READ READ ONLY``
    transaction, ``ROLLBACK`` and close on every path. Employee rows are
    counted, never read: no full name, status, or timestamp of a real
    employee crosses this boundary.
    """
    from src.db import open_connection  # lazy: keeps collection import light

    connection = open_connection(test_dsn)
    try:
        connection.autocommit = True
        connection.read_only = True
        connection.execute(_BEGIN_SNAPSHOT_SQL)

        relations = _text_pairs(connection, _RELATIONS_SQL)
        columns = tuple(
            (str(row[0]), str(row[1]), str(row[2]), bool(row[3]))
            for row in connection.execute(_COLUMNS_SQL).fetchall()
        )
        constraints = _text_triples(connection, _CONSTRAINTS_SQL)
        indexes = _text_pairs(connection, _INDEXES_SQL)
        functions = _text_pairs(connection, _FUNCTIONS_SQL)
        triggers = _text_pairs(connection, _TRIGGERS_SQL)
        policies = _text_pairs(connection, _POLICIES_SQL)
        rules = _text_pairs(connection, _RULES_SQL)
        types = _text_triples(connection, _TYPES_SQL)
        schema_names = tuple(
            str(row[0]) for row in connection.execute(_SCHEMAS_SQL).fetchall()
        )
        extensions = tuple(
            str(row[0]) for row in connection.execute(_EXTENSIONS_SQL).fetchall()
        )
        ledger_rows = tuple(
            (int(row[0]), str(row[1]))
            for row in connection.execute(_LEDGER_SQL).fetchall()
        )
        ledger_count = _single_count(connection, _LEDGER_COUNT_SQL)
        employee_count = _single_count(connection, _EMPLOYEE_COUNT_SQL)
        guard_rows = tuple(
            tuple(row) for row in connection.execute(_GUARD_ROWS_SQL).fetchall()
        )
        guard_count = _single_count(connection, _GUARD_COUNT_SQL)

        connection.execute(_ROLLBACK_SQL)
    finally:
        connection.close()

    return CatalogInventory(
        relations=relations,
        columns=columns,
        constraints=constraints,
        indexes=indexes,
        functions=functions,
        triggers=triggers,
        policies=policies,
        rules=rules,
        types=types,
        schema_names=schema_names,
        extensions=extensions,
        ledger_rows=ledger_rows,
        ledger_count=ledger_count,
        employee_count=employee_count,
        guard_rows=guard_rows,
        guard_count=guard_count,
    )


def _text_pairs(connection, sql: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(row[0]), str(row[1])) for row in connection.execute(sql).fetchall()
    )


def _text_triples(connection, sql: str) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(sql).fetchall()
    )


def _single_count(connection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    return int(row[0])
