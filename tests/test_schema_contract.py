"""Offline tests for the G8 declarative schema contract (Step 39, S1).

Everything here is pure data and a fresh-subprocess import check: no
PostgreSQL connection, no driver import, no SQL execution, no database access,
and no file mutation. The migration files are read only through the existing
loader, so the contract's pinned digest and version are proven against the
*actual* shipped set rather than against a value recomputed at test time.

Deliberate anti-tautology rule: ``PINNED_DIGEST`` and ``PINNED_VERSION`` below
are hard-coded literals, not imports or recomputations. The contract module's
constants must equal them, so re-pinning the contract requires editing both
files — a human step that cannot happen accidentally.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import src.schema_contract as contract
from src.db_migrate import LEDGER_TABLE as MIGRATION_LEDGER_TABLE
from src.db_migrate import load_migrations
from src.schema_contract import (
    CONTRACT_MIGRATION_VERSION,
    EMPLOYEE_TABLE,
    LEDGER_TABLE,
    MIGRATION_SET_DIGEST,
    PROHIBITED_TABLE_NAMES,
    PROHIBITED_TABLE_PATTERNS,
    REQUIRED_TABLES,
    REQUIRED_TABLES_BY_NAME,
    TARGET_SCHEMA,
    ColumnContract,
    ColumnDefault,
    ProhibitedNamePattern,
    TableContract,
    is_prohibited_table_name,
    migration_set_digest,
    prohibition_for,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Hard-coded: SHA-256 of the single canonical line
#: ``V1:9e6ac6b5d1e0fd016a99c007ec5b91070dd480853d54d37cdab5b069dd2deac9``
#: (the shipped ``migrations/V001__employee.sql`` checksum as the existing
#: loader derives it). Never computed from the repository here.
PINNED_DIGEST = "dcf2ed2ca62bc3a8a8cb741adf7446cfc84a43c83d329b5dc5e1c1059748f63c"

#: Hard-coded: ``migrations/`` ships exactly one migration, ``V001``.
PINNED_VERSION = 1


def _shipped_entries() -> list[tuple[int, str]]:
    """The real ``(version, checksum)`` pairs, via the existing loader only."""
    return [(script.version, script.checksum) for script in load_migrations()]


# ---------------------------------------------------------------------------
# Contract structure
# ---------------------------------------------------------------------------


def test_contract_targets_the_public_schema() -> None:
    assert TARGET_SCHEMA == "public"


def test_contract_declares_exactly_the_two_application_tables() -> None:
    assert tuple(table.name for table in REQUIRED_TABLES) == (
        "employee",
        "schema_version",
    )
    assert set(REQUIRED_TABLES_BY_NAME) == {"employee", "schema_version"}
    assert REQUIRED_TABLES_BY_NAME["employee"] is EMPLOYEE_TABLE
    assert REQUIRED_TABLES_BY_NAME["schema_version"] is LEDGER_TABLE


def test_employee_columns_match_the_migration_exactly() -> None:
    """Every field, not just names: the migration is the authoritative DDL."""
    assert EMPLOYEE_TABLE.columns == (
        ColumnContract("employee_id", "text", False, ColumnDefault.NONE),
        ColumnContract("full_name", "text", False, ColumnDefault.NONE),
        ColumnContract("is_active", "boolean", False, ColumnDefault.TRUE),
        ColumnContract(
            "created_at", "timestamp with time zone", False, ColumnDefault.NONE
        ),
    )
    assert EMPLOYEE_TABLE.primary_key == ("employee_id",)


def test_created_at_has_no_database_default() -> None:
    """Approved D2: the timestamp is application-authored, so a default is a bug."""
    created_at = EMPLOYEE_TABLE.column("created_at")
    assert created_at.default is ColumnDefault.NONE
    assert created_at.default.requires_default is False
    assert created_at.default.expected_normalized_default is None


def test_is_active_requires_a_true_default() -> None:
    is_active = EMPLOYEE_TABLE.column("is_active")
    assert is_active.default is ColumnDefault.TRUE
    assert is_active.default.requires_default is True
    assert is_active.default.expected_normalized_default == "true"


def test_ledger_columns_match_the_documented_shape() -> None:
    """The same shape ``_EXPECTED_LEDGER_SHAPE`` pins inside the executor."""
    assert LEDGER_TABLE.columns == (
        ColumnContract("version", "integer", False, ColumnDefault.NONE),
        ColumnContract("checksum", "text", False, ColumnDefault.NONE),
        ColumnContract(
            "applied_at", "timestamp with time zone", False, ColumnDefault.NONE
        ),
    )
    assert LEDGER_TABLE.primary_key == ("version",)


def test_contract_ledger_name_matches_the_migration_ledger() -> None:
    """Cross-check without coupling: the contract names the runner's real ledger."""
    assert LEDGER_TABLE.name == MIGRATION_LEDGER_TABLE


def test_no_secondary_index_is_required() -> None:
    """Documented decision: the primary-key index serves every repository query.

    An inventarised requirement would refuse a harmless index removal or
    addition, so ``secondary_indexes`` stays empty until repository evidence
    demands one.
    """
    for table in REQUIRED_TABLES:
        assert table.secondary_indexes == ()


def test_contract_migration_version_is_pinned() -> None:
    assert CONTRACT_MIGRATION_VERSION == PINNED_VERSION


def test_all_declared_types_are_canonical_catalog_spellings() -> None:
    """``format_type`` spellings only — never ``information_schema`` aliases."""
    allowed = {"text", "boolean", "integer", "timestamp with time zone"}
    for table in REQUIRED_TABLES:
        for column in table.columns:
            assert column.pg_type in allowed, column
            assert column.pg_type == column.pg_type.lower()
            assert "varchar" not in column.pg_type


def test_contract_objects_are_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        EMPLOYEE_TABLE.name = "renamed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        EMPLOYEE_TABLE.columns[0].pg_type = "integer"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        EMPLOYEE_TABLE.columns = ()  # type: ignore[misc]
    with pytest.raises(TypeError):
        REQUIRED_TABLES[0] = LEDGER_TABLE  # type: ignore[index]
    with pytest.raises(TypeError):
        REQUIRED_TABLES_BY_NAME["other"] = EMPLOYEE_TABLE  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        PROHIBITED_TABLE_PATTERNS[0].category = "other"  # type: ignore[misc]


def test_column_lookup_helpers() -> None:
    assert EMPLOYEE_TABLE.column_names == (
        "employee_id",
        "full_name",
        "is_active",
        "created_at",
    )
    assert EMPLOYEE_TABLE.column("full_name").pg_type == "text"
    with pytest.raises(KeyError):
        EMPLOYEE_TABLE.column("embedding")


# ---------------------------------------------------------------------------
# Migration-set consistency (the pin that stops silent contract drift)
# ---------------------------------------------------------------------------


def test_pinned_digest_matches_the_shipped_migration_set() -> None:
    assert migration_set_digest(_shipped_entries()) == MIGRATION_SET_DIGEST


def test_pinned_digest_constant_is_the_hard_coded_literal() -> None:
    """Anti-tautology: the module must equal the literal in this file."""
    assert MIGRATION_SET_DIGEST == PINNED_DIGEST
    assert len(MIGRATION_SET_DIGEST) == 64
    assert set(MIGRATION_SET_DIGEST) <= set("0123456789abcdef")


def test_contract_version_matches_the_highest_shipped_version() -> None:
    versions = [script.version for script in load_migrations()]
    assert versions, "the repository must ship at least one migration"
    assert max(versions) == CONTRACT_MIGRATION_VERSION
    assert CONTRACT_MIGRATION_VERSION == PINNED_VERSION


def test_modified_migration_is_detected() -> None:
    entries = _shipped_entries()
    tampered = [(version, "0" * 64) for version, _ in entries]
    assert migration_set_digest(tampered) != MIGRATION_SET_DIGEST


def test_added_migration_is_detected() -> None:
    entries = _shipped_entries()
    extended = entries + [(CONTRACT_MIGRATION_VERSION + 1, "a" * 64)]
    assert migration_set_digest(extended) != MIGRATION_SET_DIGEST


def test_removed_migration_is_detected() -> None:
    entries = _shipped_entries()
    assert len(entries) >= 2 or migration_set_digest([]) != MIGRATION_SET_DIGEST
    assert migration_set_digest(entries[1:]) != MIGRATION_SET_DIGEST


def test_swapped_version_content_mapping_is_detected() -> None:
    """Renaming files so content moves between versions is a real change."""
    original = [(1, "a" * 64), (2, "b" * 64)]
    swapped = [(1, "b" * 64), (2, "a" * 64)]
    assert migration_set_digest(original) != migration_set_digest(swapped)


def test_digest_ignores_discovery_order() -> None:
    """Version is the ordering key, so input order cannot change the digest."""
    entries = [(2, "b" * 64), (1, "a" * 64)]
    assert migration_set_digest(entries) == migration_set_digest(list(reversed(entries)))
    assert migration_set_digest(entries) == migration_set_digest(
        [(1, "a" * 64), (2, "b" * 64)]
    )


def test_digest_is_pure_and_accepts_synthetic_entries() -> None:
    """No discovery, no I/O: the helper works on arbitrary caller input."""
    digest = migration_set_digest([(7, "f" * 64)])
    assert digest == migration_set_digest([(7, "f" * 64)])
    assert len(digest) == 64
    assert migration_set_digest([]) == migration_set_digest(iter([]))


# ---------------------------------------------------------------------------
# Prohibited-object policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table_name",
    [
        "attendance",
        "attendance_event",
        "attendance_events",
        "employee_attendance",
        "ATTENDANCE_EVENT",
    ],
)
def test_attendance_table_names_are_prohibited(table_name: str) -> None:
    rule = prohibition_for(table_name)
    assert rule is not None
    assert rule.category == "attendance"


@pytest.mark.parametrize(
    "table_name",
    ["embedding", "embeddings", "face_embedding", "employee_embeddings"],
)
def test_embedding_table_names_are_prohibited(table_name: str) -> None:
    rule = prohibition_for(table_name)
    assert rule is not None
    assert rule.category in {"embedding", "face"}


@pytest.mark.parametrize("table_name", ["biometric", "biometrics", "biometric_sample"])
def test_biometric_table_names_are_prohibited(table_name: str) -> None:
    rule = prohibition_for(table_name)
    assert rule is not None
    assert rule.category == "biometric"


@pytest.mark.parametrize(
    "table_name", ["face", "faces", "face_template", "facial_sample", "Face_Template"]
)
def test_face_table_names_are_prohibited(table_name: str) -> None:
    rule = prohibition_for(table_name)
    assert rule is not None
    assert rule.category == "face"


@pytest.mark.parametrize(
    "table_name", ["enrollment", "enrollments", "employee_enrollment"]
)
def test_enrollment_table_names_are_prohibited(table_name: str) -> None:
    rule = prohibition_for(table_name)
    assert rule is not None
    assert rule.category == "enrollment"


def test_test_marker_table_is_prohibited_with_its_own_category() -> None:
    """Exact name only: the marker means "wrong database", not "drifted schema"."""
    assert PROHIBITED_TABLE_NAMES == ("_phase3_test_guard",)
    rule = prohibition_for("_phase3_test_guard")
    assert rule is not None
    assert rule.category == "test_marker"
    # A near-miss is not the marker and matches no D1 pattern.
    assert prohibition_for("_phase3_test_guard_backup") is None


@pytest.mark.parametrize(
    "table_name",
    [
        "employee",
        "schema_version",
        "audit_log",
        "app_user",
        "department",
        "payroll_run",
        "employee_directory",
        "surface_metrics",  # contains "face" as a substring, not as a token
        "faceted_search",
        "trace_metrics",
    ],
)
def test_legitimate_unrelated_table_names_are_allowed(table_name: str) -> None:
    """Approved decision: unrelated objects in ``public`` are allowed."""
    assert prohibition_for(table_name) is None
    assert is_prohibited_table_name(table_name) is False


def test_matching_is_token_bounded_not_substring() -> None:
    assert is_prohibited_table_name("surface_metrics") is False
    assert is_prohibited_table_name("surface_face_metrics") is True
    assert is_prohibited_table_name("scores_attendance") is True
    assert is_prohibited_table_name("attendance_scores") is True


def test_every_declared_pattern_matches_its_documented_example() -> None:
    """A typo'd pattern that matches nothing must fail loudly, not silently pass."""
    examples = {
        "attendance": "attendance_event",
        "embedding": "face_embedding",
        "biometric": "biometric_template",
        "face": "face_template",
        "enrollment": "employee_enrollment",
    }
    assert {rule.category for rule in PROHIBITED_TABLE_PATTERNS} == set(examples)
    for rule in PROHIBITED_TABLE_PATTERNS:
        assert rule.rationale.strip(), rule
        assert rule.matches(examples[rule.category]), rule.category


def test_prohibited_rule_metadata_is_documented() -> None:
    for rule in PROHIBITED_TABLE_PATTERNS:
        assert isinstance(rule, ProhibitedNamePattern)
        assert rule.category and rule.category == rule.category.lower()
        assert rule.regex
        assert rule.rationale


# ---------------------------------------------------------------------------
# Safety: no driver, no I/O, no SQL, no side effects
# ---------------------------------------------------------------------------

#: Every module the contract is allowed to import (pure stdlib only).
_ALLOWED_IMPORT_ROOTS = {
    "__future__",
    "collections",
    "dataclasses",
    "enum",
    "hashlib",
    "re",
    "types",
    "typing",
}


def test_module_imports_only_pure_standard_library_modules() -> None:
    source = Path(contract.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            if node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= _ALLOWED_IMPORT_ROOTS, imported - _ALLOWED_IMPORT_ROOTS


def test_module_performs_no_io_or_dynamic_execution() -> None:
    source = Path(contract.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & {"open", "exec", "eval", "compile", "input", "__import__"}


def test_import_hygiene_fresh_process() -> None:
    """Importing the contract must load no driver, socket, or database module."""
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "import src.schema_contract;"
        "banned = {'psycopg', 'socket', 'cv2', 'fastapi', 'uvicorn',"
        "'src.db', 'src.db_migrate', 'src.attendance', 'src.attendance_store',"
        "'src.startup_schema'};"
        "loaded = banned & set(sys.modules);"
        "assert not loaded, f'unexpected imports: {loaded}';"
        "from src.schema_contract import REQUIRED_TABLES as t;"
        "assert [x.name for x in t] == ['employee', 'schema_version'], t;"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")


def test_contract_holds_no_employee_or_biometric_values() -> None:
    """Metadata only: no column the contract declares could carry personal data.

    This is a policy assertion over the declared names/types, not a data read:
    the contract's columns are identifiers, display names, flags and
    timestamps, and no binary/JSON/vector type is declared anywhere.
    """
    declared_types = {
        column.pg_type
        for table in REQUIRED_TABLES
        for column in table.columns
    }
    assert declared_types <= {"text", "boolean", "integer", "timestamp with time zone"}
    assert not declared_types & {"bytea", "jsonb", "json", "vector", "real[]"}


def test_tables_are_only_the_approved_two() -> None:
    """Guard against someone adding an attendance/embedding table to the contract."""
    for table in REQUIRED_TABLES:
        assert not is_prohibited_table_name(table.name), table.name
    with pytest.raises(TypeError):
        # The exported tuple cannot grow at run time either.
        REQUIRED_TABLES_BY_NAME["attendance_event"] = TableContract(  # type: ignore[index]
            name="attendance_event", columns=(), primary_key=()
        )
