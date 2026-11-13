"""Offline tests for the G8 S3 physical-schema evaluator (``src.physical_schema``).

Every case builds an immutable :class:`CatalogSnapshot` by hand — no connection,
no driver, no SQL, no filesystem fixture — so the whole compatibility policy is
executable without a database. Assertions are on the public interface
(``evaluate_physical_schema`` / ``SchemaEvaluation`` / ``Finding``), not on
private helpers.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src import physical_schema as ps
from src.db_schema_catalog import (
    CatalogSnapshot,
    ColumnFact,
    ConstraintFact,
    IndexFact,
    RelationFact,
)
from src.schema_contract import (
    EMPLOYEE_TABLE,
    LEDGER_TABLE,
    ColumnContract,
    ColumnDefault,
    TableContract,
    prohibition_for,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = Path(ps.__file__)


# ---------------------------------------------------------------------------
# Snapshot builders (fresh immutable facts per call)
# ---------------------------------------------------------------------------


def _column(
    table: str,
    name: str,
    pg_type: str,
    ordinal: int,
    *,
    nullable: bool = False,
    default_expression: str | None = None,
    generated: str = "",
    identity: str = "",
    schema: str = "public",
) -> ColumnFact:
    return ColumnFact(
        schema=schema,
        table=table,
        name=name,
        pg_type=pg_type,
        nullable=nullable,
        default_expression=default_expression,
        generated=generated,
        identity=identity,
        ordinal=ordinal,
    )


def _base_columns() -> tuple[ColumnFact, ...]:
    """The physically correct column facts for every contracted table."""
    return (
        _column("employee", "employee_id", "text", 1),
        _column("employee", "full_name", "text", 2),
        _column("employee", "is_active", "boolean", 3, default_expression="true"),
        _column("employee", "created_at", "timestamp with time zone", 4),
        _column("app_user", "user_id", "text", 1),
        _column("app_user", "username", "text", 2),
        _column("app_user", "username_key", "text", 3),
        _column("app_user", "password_hash", "text", 4),
        _column("app_user", "role", "text", 5),
        _column("app_user", "is_active", "boolean", 6, default_expression="true"),
        _column("app_user", "created_at", "timestamp with time zone", 7),
        _column("app_user", "updated_at", "timestamp with time zone", 8),
        _column("user_session", "session_token_hash", "character(64)", 1),
        _column("user_session", "user_id", "text", 2),
        _column("user_session", "issued_at", "timestamp with time zone", 3),
        _column("user_session", "expires_at", "timestamp with time zone", 4),
        _column("user_session", "absolute_expires_at", "timestamp with time zone", 5),
        _column(
            "user_session", "revoked_at", "timestamp with time zone", 6, nullable=True
        ),
        _column("user_session", "last_seen_at", "timestamp with time zone", 7),
        _column("schema_version", "version", "integer", 1),
        _column("schema_version", "checksum", "text", 2),
        _column("schema_version", "applied_at", "timestamp with time zone", 3),
    )


def _customized(**changes: dict) -> tuple[ColumnFact, ...]:
    """Base columns with per-column changes, applied to ``employee`` only.

    Names such as ``created_at`` and ``is_active`` exist in more than one
    contracted table now, so changes are scoped to the employee table to keep
    these fixtures expressing exactly one deliberate fault per case (the
    identity/session tables are covered by their own tests below and by
    ``tests/test_migration_v002.py``).
    """
    return tuple(
        dataclasses.replace(column, **changes.get(column.name, {}))
        if column.table in {"employee", "schema_version"}
        else column
        for column in _base_columns()
    )


def _relations(*, employee_kind: str = "r") -> tuple[RelationFact, ...]:
    return (
        RelationFact("public", "employee", employee_kind),
        RelationFact("public", "employee_pkey", "i"),
        RelationFact("public", "app_user", "r"),
        RelationFact("public", "app_user_pkey", "i"),
        RelationFact("public", "app_user_username_key_key", "i"),
        RelationFact("public", "user_session", "r"),
        RelationFact("public", "user_session_pkey", "i"),
        RelationFact("public", "user_session_user_id_idx", "i"),
        RelationFact("public", "user_session_expires_at_idx", "i"),
        RelationFact("public", "schema_version", "r"),
        RelationFact("public", "schema_version_pkey", "i"),
    )


def _pk(table: str, columns: tuple[str, ...], name: str | None = None) -> ConstraintFact:
    return ConstraintFact(
        schema="public",
        table=table,
        name=name or f"{table}_pkey",
        contype="p",
        definition=f"PRIMARY KEY ({', '.join(columns)})",
        columns=columns,
    )


def _indexes() -> tuple[IndexFact, ...]:
    return (
        IndexFact(
            schema="public",
            table="employee",
            name="employee_pkey",
            is_unique=True,
            is_primary=True,
            columns=("employee_id",),
            definition="CREATE UNIQUE INDEX employee_pkey ON public.employee USING btree (employee_id)",
        ),
        IndexFact(
            schema="public",
            table="app_user",
            name="app_user_pkey",
            is_unique=True,
            is_primary=True,
            columns=("user_id",),
            definition="CREATE UNIQUE INDEX app_user_pkey ON public.app_user USING btree (user_id)",
        ),
        IndexFact(
            schema="public",
            table="user_session",
            name="user_session_pkey",
            is_unique=True,
            is_primary=True,
            columns=("session_token_hash",),
            definition="CREATE UNIQUE INDEX user_session_pkey ON public.user_session USING btree (session_token_hash)",
        ),
        IndexFact(
            schema="public",
            table="schema_version",
            name="schema_version_pkey",
            is_unique=True,
            is_primary=True,
            columns=("version",),
            definition="CREATE UNIQUE INDEX schema_version_pkey ON public.schema_version USING btree (version)",
        ),
    )


def _snapshot(
    *,
    relations: tuple[RelationFact, ...] | None = None,
    columns: tuple[ColumnFact, ...] | None = None,
    constraints: tuple[ConstraintFact, ...] | None = None,
    indexes: tuple[IndexFact, ...] | None = None,
) -> CatalogSnapshot:
    return CatalogSnapshot(
        relations=_relations() if relations is None else relations,
        columns=_base_columns() if columns is None else columns,
        constraints=(
            (
                _pk("employee", ("employee_id",)),
                _pk("app_user", ("user_id",)),
                _pk("user_session", ("session_token_hash",)),
                _pk("schema_version", ("version",)),
            )
            if constraints is None
            else constraints
        ),
        indexes=_indexes() if indexes is None else indexes,
    )


def _evaluate(snapshot: CatalogSnapshot, **kwargs) -> ps.SchemaEvaluation:
    return ps.evaluate_physical_schema(snapshot, **kwargs)


def _codes(result: ps.SchemaEvaluation) -> list[str]:
    return [finding.code for finding in result.findings]


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------


def test_healthy_snapshot_is_compatible() -> None:
    result = _evaluate(_snapshot())
    assert result.compatible is True
    assert result.status is ps.CompatibilityStatus.COMPATIBLE
    assert result.indeterminate is False
    assert result.findings == ()
    assert result.codes == ()


def test_reordered_physical_columns_are_compatible() -> None:
    order = {
        "employee_id": 4,
        "full_name": 3,
        "is_active": 2,
        "created_at": 1,
        "version": 3,
        "checksum": 2,
        "applied_at": 1,
    }
    columns = tuple(
        dataclasses.replace(column, ordinal=order[column.name])
        if column.table in {"employee", "schema_version"}
        else column
        for column in _base_columns()
    )
    result = _evaluate(_snapshot(columns=columns))
    assert result.compatible is True, result.findings


def test_unrelated_public_table_is_allowed() -> None:
    relations = _relations() + (RelationFact("public", "surface_metrics", "r"),)
    columns = _base_columns() + (_column("surface_metrics", "measured_at", "timestamp with time zone", 1),)
    result = _evaluate(_snapshot(relations=relations, columns=columns))
    assert result.compatible is True, result.findings


def test_unrelated_public_view_index_and_sequence_are_allowed() -> None:
    relations = _relations() + (
        RelationFact("public", "employee_directory", "v"),
        RelationFact("public", "employee_name_idx", "i"),
        RelationFact("public", "employee_seq", "S"),
    )
    result = _evaluate(_snapshot(relations=relations))
    assert result.compatible is True, result.findings


def test_additional_constraints_and_indexes_are_allowed() -> None:
    constraints = _snapshot().constraints + (
        ConstraintFact(
            schema="public",
            table="employee",
            name="employee_full_name_check",
            contype="c",
            definition="CHECK ((length(full_name) > 0))",
            columns=(),
        ),
    )
    indexes = _indexes() + (
        IndexFact(
            schema="public",
            table="employee",
            name="employee_full_name_idx",
            is_unique=False,
            is_primary=False,
            columns=("full_name",),
            definition="CREATE INDEX employee_full_name_idx ON public.employee USING btree (full_name)",
        ),
    )
    result = _evaluate(_snapshot(constraints=constraints, indexes=indexes))
    assert result.compatible is True, result.findings


def test_normalized_true_default_is_accepted() -> None:
    columns = _customized(is_active={"default_expression": "  TRUE "})
    result = _evaluate(_snapshot(columns=columns))
    assert result.compatible is True, result.findings


def test_type_spelling_is_compared_case_and_whitespace_insensitively() -> None:
    columns = _customized(
        employee_id={"pg_type": "TEXT"},
        created_at={"pg_type": "timestamp  with time zone"},
    )
    result = _evaluate(_snapshot(columns=columns))
    assert result.compatible is True, result.findings


def test_other_schemas_are_ignored() -> None:
    relations = _relations() + (RelationFact("other", "attendance_events", "r"),)
    columns = _base_columns() + (_column("attendance_events", "id", "text", 1, schema="other"),)
    result = _evaluate(_snapshot(relations=relations, columns=columns))
    assert result.compatible is True, result.findings


def test_findings_are_immutable_and_hashable() -> None:
    result = _evaluate(_snapshot(relations=()))
    finding = result.findings[0]
    assert isinstance(result.findings, tuple)
    assert all(isinstance(item, ps.Finding) for item in result.findings)
    assert isinstance(result.status, ps.CompatibilityStatus)
    hash(finding)
    hash(result)
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.code = "SCHEMA_TABLE_MISSING"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = ps.CompatibilityStatus.COMPATIBLE  # type: ignore[misc]


def test_public_status_constants_match_enum_values() -> None:
    assert ps.COMPATIBLE == "compatible"
    assert ps.INCOMPATIBLE == "incompatible"
    assert ps.INDETERMINATE == "indeterminate"
    assert {status.value for status in ps.CompatibilityStatus} == {
        ps.COMPATIBLE,
        ps.INCOMPATIBLE,
        ps.INDETERMINATE,
    }


# ---------------------------------------------------------------------------
# Required tables
# ---------------------------------------------------------------------------


def test_empty_snapshot_is_never_compatible() -> None:
    result = _evaluate(CatalogSnapshot(relations=(), columns=(), constraints=(), indexes=()))
    assert result.compatible is False
    assert result.status is ps.CompatibilityStatus.INCOMPATIBLE
    assert _codes(result) == [
        ps.SCHEMA_TABLE_MISSING,
        ps.SCHEMA_TABLE_MISSING,
        ps.SCHEMA_TABLE_MISSING,
        ps.SCHEMA_TABLE_MISSING,
    ]
    assert [finding.object for finding in result.findings] == [
        "app_user",
        "employee",
        "schema_version",
        "user_session",
    ]


def test_empty_snapshot_does_not_invent_column_findings() -> None:
    result = _evaluate(CatalogSnapshot(relations=(), columns=(), constraints=(), indexes=()))
    assert set(_codes(result)) == {ps.SCHEMA_TABLE_MISSING}


def test_missing_employee_table_reports_only_the_table() -> None:
    relations = tuple(relation for relation in _relations() if relation.name != "employee")
    # Column and constraint facts deliberately stay in the snapshot: a missing
    # table makes them unknowable, so they must not be reported again.
    result = _evaluate(_snapshot(relations=relations))
    assert _codes(result) == [ps.SCHEMA_TABLE_MISSING]
    assert result.findings[0].object == "employee"
    assert result.status is ps.CompatibilityStatus.INCOMPATIBLE


def test_missing_ledger_table_still_evaluates_employee() -> None:
    relations = tuple(
        relation for relation in _relations() if relation.name != "schema_version"
    )
    columns = _customized(employee_id={"pg_type": "integer"})
    result = _evaluate(_snapshot(relations=relations, columns=columns))
    assert _codes(result) == [ps.SCHEMA_TABLE_MISSING, ps.SCHEMA_COLUMN_TYPE_MISMATCH]
    assert result.findings[0].object == "schema_version"
    assert result.findings[1].object == "employee"


@pytest.mark.parametrize("kind", ["v", "m", "S", "f", "c", "i", "I"])
def test_non_table_relation_kinds_cannot_satisfy_a_table(kind: str) -> None:
    result = _evaluate(_snapshot(relations=_relations(employee_kind=kind)))
    assert _codes(result) == [ps.SCHEMA_TABLE_KIND_MISMATCH]
    assert result.findings[0].object == "employee"
    assert result.status is ps.CompatibilityStatus.INCOMPATIBLE
    # The raw relkind letter is not echoed; findings stay human-readable.
    assert f"'{kind}'" not in result.findings[0].detail
    assert "relkind" not in result.findings[0].detail


def test_partitioned_table_is_indeterminate_not_accepted() -> None:
    result = _evaluate(_snapshot(relations=_relations(employee_kind="p")))
    assert _codes(result) == [ps.SCHEMA_TABLE_KIND_UNCONFIRMED]
    assert result.status is ps.CompatibilityStatus.INDETERMINATE
    assert result.compatible is False
    assert result.indeterminate is True


def test_partitioned_table_still_reports_definite_column_drift() -> None:
    columns = _customized(employee_id={"pg_type": "integer"})
    result = _evaluate(
        _snapshot(relations=_relations(employee_kind="p"), columns=columns)
    )
    assert _codes(result) == [
        ps.SCHEMA_TABLE_KIND_UNCONFIRMED,
        ps.SCHEMA_COLUMN_TYPE_MISMATCH,
    ]
    assert result.status is ps.CompatibilityStatus.INCOMPATIBLE


def test_duplicate_relation_facts_are_incomplete_metadata() -> None:
    relations = _relations() + (RelationFact("public", "employee", "r"),)
    result = _evaluate(_snapshot(relations=relations))
    assert _codes(result) == [ps.SCHEMA_METADATA_INCOMPLETE]
    assert result.status is ps.CompatibilityStatus.INDETERMINATE


def test_table_without_column_facts_is_incomplete_metadata() -> None:
    columns = tuple(column for column in _base_columns() if column.table != "employee")
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [ps.SCHEMA_METADATA_INCOMPLETE]
    assert result.findings[0].object == "employee"
    assert result.status is ps.CompatibilityStatus.INDETERMINATE
    assert result.compatible is False


def test_duplicate_column_facts_are_incomplete_metadata() -> None:
    columns = _base_columns() + (_column("employee", "full_name", "text", 2),)
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [ps.SCHEMA_METADATA_INCOMPLETE]
    assert result.status is ps.CompatibilityStatus.INDETERMINATE


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "table"),
    [
        ("employee_id", "employee"),
        ("full_name", "employee"),
        ("is_active", "employee"),
        ("created_at", "employee"),
        ("version", "schema_version"),
        ("checksum", "schema_version"),
        ("applied_at", "schema_version"),
    ],
)
def test_missing_required_column(name: str, table: str) -> None:
    columns = tuple(
        column
        for column in _base_columns()
        if not (column.table == table and column.name == name)
    )
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [ps.SCHEMA_COLUMN_MISSING]
    assert result.findings[0].column == name
    assert result.findings[0].object == table
    assert result.status is ps.CompatibilityStatus.INCOMPATIBLE


def test_unexpected_employee_column() -> None:
    columns = _base_columns() + (_column("employee", "nickname", "text", 5),)
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [ps.SCHEMA_COLUMN_UNEXPECTED]
    assert result.findings[0].column == "nickname"


def test_unexpected_ledger_column() -> None:
    columns = _base_columns() + (_column("schema_version", "applied_by", "text", 4),)
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [ps.SCHEMA_COLUMN_UNEXPECTED]
    assert result.findings[0].object == "schema_version"


@pytest.mark.parametrize(
    ("name", "observed"),
    [
        ("employee_id", "character varying(255)"),
        ("full_name", "character varying"),
        ("is_active", "integer"),
        ("created_at", "timestamp without time zone"),
        ("version", "bigint"),
        ("checksum", "bytea"),
        ("applied_at", "date"),
    ],
)
def test_column_type_mismatch(name: str, observed: str) -> None:
    columns = _customized(**{name: {"pg_type": observed}})
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [ps.SCHEMA_COLUMN_TYPE_MISMATCH]
    assert result.findings[0].column == name
    assert observed in result.findings[0].detail


def test_type_aliases_are_not_accepted() -> None:
    """``format_type`` never emits aliases, so accepting them would be unsafe."""
    columns = _customized(created_at={"pg_type": "timestamptz"})
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [ps.SCHEMA_COLUMN_TYPE_MISMATCH]


@pytest.mark.parametrize(
    "name",
    ["employee_id", "full_name", "is_active", "created_at", "version", "checksum", "applied_at"],
)
def test_nullable_column_violates_not_null(name: str) -> None:
    columns = _customized(**{name: {"nullable": True}})
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [ps.SCHEMA_COLUMN_NULLABILITY_MISMATCH]
    assert result.findings[0].column == name


def test_unexpected_default_on_created_at() -> None:
    columns = _customized(created_at={"default_expression": "now()"})
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [ps.SCHEMA_COLUMN_DEFAULT_MISMATCH]
    assert "default" in result.findings[0].detail
    assert "now()" not in result.findings[0].detail


def test_missing_true_default_on_is_active() -> None:
    columns = _customized(is_active={"default_expression": None})
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [ps.SCHEMA_COLUMN_DEFAULT_MISMATCH]
    assert "no default" in result.findings[0].detail


@pytest.mark.parametrize("observed", ["false", "'f'::boolean", "1 = 1", "(true)"])
def test_non_true_default_on_is_active_is_refused(observed: str) -> None:
    columns = _customized(is_active={"default_expression": observed})
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [ps.SCHEMA_COLUMN_DEFAULT_MISMATCH]


def test_generated_column_is_not_a_defaulted_column() -> None:
    columns = _customized(is_active={"generated": "s", "default_expression": "true"})
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [ps.SCHEMA_COLUMN_GENERATION_MISMATCH]
    assert ps.SCHEMA_COLUMN_DEFAULT_MISMATCH not in _codes(result)


def test_identity_column_is_not_a_defaulted_column() -> None:
    columns = _customized(
        employee_id={"identity": "a", "default_expression": "nextval('employee_id_seq')"}
    )
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [ps.SCHEMA_COLUMN_IDENTITY_MISMATCH]
    assert ps.SCHEMA_COLUMN_DEFAULT_MISMATCH not in _codes(result)


def test_generated_column_still_reports_type_and_nullability() -> None:
    columns = _customized(
        is_active={"generated": "s", "pg_type": "integer", "nullable": True}
    )
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [
        ps.SCHEMA_COLUMN_TYPE_MISMATCH,
        ps.SCHEMA_COLUMN_NULLABILITY_MISMATCH,
        ps.SCHEMA_COLUMN_GENERATION_MISMATCH,
    ]


def test_multiple_column_errors_are_all_reported() -> None:
    columns = _customized(
        is_active={"pg_type": "integer", "nullable": True, "default_expression": None}
    )
    result = _evaluate(_snapshot(columns=columns))
    assert _codes(result) == [
        ps.SCHEMA_COLUMN_TYPE_MISMATCH,
        ps.SCHEMA_COLUMN_NULLABILITY_MISMATCH,
        ps.SCHEMA_COLUMN_DEFAULT_MISMATCH,
    ]


def test_default_expression_text_is_never_echoed() -> None:
    columns = _customized(
        created_at={"default_expression": "secret_function('hunter2')"}
    )
    result = _evaluate(_snapshot(columns=columns))
    detail = result.findings[0].detail
    assert "hunter2" not in detail
    assert "secret_function" not in detail


# ---------------------------------------------------------------------------
# Primary keys
# ---------------------------------------------------------------------------


def test_missing_primary_key() -> None:
    constraints = (
        _pk("app_user", ("user_id",)),
        _pk("user_session", ("session_token_hash",)),
        _pk("schema_version", ("version",)),
    )
    result = _evaluate(_snapshot(constraints=constraints))
    assert _codes(result) == [ps.SCHEMA_PRIMARY_KEY_MISSING]
    assert result.findings[0].object == "employee"


def test_wrong_primary_key_column() -> None:
    constraints = (
        _pk("employee", ("full_name",)),
        _pk("app_user", ("user_id",)),
        _pk("user_session", ("session_token_hash",)),
        _pk("schema_version", ("version",)),
    )
    result = _evaluate(_snapshot(constraints=constraints))
    assert _codes(result) == [ps.SCHEMA_PRIMARY_KEY_MISMATCH]
    assert "full_name" in result.findings[0].detail
    assert "employee_id" in result.findings[0].detail


def test_wrong_ledger_primary_key_column() -> None:
    constraints = (
        _pk("employee", ("employee_id",)),
        _pk("app_user", ("user_id",)),
        _pk("user_session", ("session_token_hash",)),
        _pk("schema_version", ("checksum",)),
    )
    result = _evaluate(_snapshot(constraints=constraints))
    assert _codes(result) == [ps.SCHEMA_PRIMARY_KEY_MISMATCH]
    assert result.findings[0].object == "schema_version"


def test_conflicting_primary_key_metadata() -> None:
    constraints = (
        _pk("employee", ("employee_id",)),
        _pk("employee", ("employee_id",), name="employee_pkey_duplicate"),
        _pk("app_user", ("user_id",)),
        _pk("user_session", ("session_token_hash",)),
        _pk("schema_version", ("version",)),
    )
    result = _evaluate(_snapshot(constraints=constraints))
    assert _codes(result) == [ps.SCHEMA_METADATA_INCOMPLETE]
    assert result.status is ps.CompatibilityStatus.INDETERMINATE


def test_primary_key_without_columns_is_incomplete() -> None:
    constraints = (
        ConstraintFact(
            schema="public",
            table="employee",
            name="employee_pkey",
            contype="p",
            definition="PRIMARY KEY (employee_id)",
            columns=(),
        ),
        _pk("app_user", ("user_id",)),
        _pk("user_session", ("session_token_hash",)),
        _pk("schema_version", ("version",)),
    )
    result = _evaluate(_snapshot(constraints=constraints))
    assert _codes(result) == [ps.SCHEMA_METADATA_INCOMPLETE]


def test_unique_index_does_not_satisfy_the_primary_key() -> None:
    constraints = (
        _pk("app_user", ("user_id",)),
        _pk("user_session", ("session_token_hash",)),
        _pk("schema_version", ("version",)),
    )
    indexes = _indexes()  # includes employee_pkey with is_primary=True
    result = _evaluate(_snapshot(constraints=constraints, indexes=indexes))
    assert _codes(result) == [ps.SCHEMA_PRIMARY_KEY_MISSING]


PAIR_TABLE = TableContract(
    name="pair",
    columns=(
        ColumnContract("left_id", "text", False, ColumnDefault.NONE),
        ColumnContract("right_id", "text", False, ColumnDefault.NONE),
    ),
    primary_key=("left_id", "right_id"),
)


def _pair_snapshot(pk_columns: tuple[str, ...]) -> CatalogSnapshot:
    return CatalogSnapshot(
        relations=(RelationFact("public", "pair", "r"),),
        columns=(
            _column("pair", "left_id", "text", 1),
            _column("pair", "right_id", "text", 2),
        ),
        constraints=(_pk("pair", pk_columns),),
        indexes=(),
    )


def test_two_column_primary_key_order_is_enforced() -> None:
    result = _evaluate(_pair_snapshot(("left_id", "right_id")), contract=(PAIR_TABLE,))
    assert result.compatible is True, result.findings


def test_two_column_primary_key_wrong_order_is_a_mismatch() -> None:
    result = _evaluate(_pair_snapshot(("right_id", "left_id")), contract=(PAIR_TABLE,))
    assert _codes(result) == [ps.SCHEMA_PRIMARY_KEY_MISMATCH]
    assert result.status is ps.CompatibilityStatus.INCOMPATIBLE


# ---------------------------------------------------------------------------
# Prohibited objects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("attendance_events", "attendance"),
        ("ATTENDANCE_EVENT", "attendance"),
        ("my_attendance_log", "attendance"),
        ("face_embedding", "embedding"),
        ("face_embeddings", "embedding"),
        ("biometric_samples", "biometric"),
        ("facial_images", "face"),
        ("face_templates", "face"),
        ("enrollment_artifacts", "enrollment"),
        ("_phase3_test_guard", "test_marker"),
    ],
)
def test_prohibited_relation_names(name: str, category: str) -> None:
    relations = _relations() + (RelationFact("public", name, "r"),)
    result = _evaluate(_snapshot(relations=relations))
    assert _codes(result) == [ps.SCHEMA_OBJECT_PROHIBITED]
    assert result.findings[0].object == name
    assert result.findings[0].category == category
    assert result.status is ps.CompatibilityStatus.INCOMPATIBLE


def test_prohibited_category_matches_the_s1_rule() -> None:
    for name in (
        "attendance_events",
        "face_embedding",
        "biometric_samples",
        "facial_images",
        "enrollment_artifacts",
        "_phase3_test_guard",
    ):
        rule = prohibition_for(name)
        assert rule is not None
        result = _evaluate(
            _snapshot(relations=_relations() + (RelationFact("public", name, "r"),))
        )
        assert result.findings[0].category == rule.category


def test_prohibited_relation_of_any_kind_is_refused() -> None:
    relations = _relations() + (RelationFact("public", "attendance_events", "v"),)
    result = _evaluate(_snapshot(relations=relations))
    assert _codes(result) == [ps.SCHEMA_OBJECT_PROHIBITED]


def test_test_marker_is_classified_separately() -> None:
    relations = _relations() + (RelationFact("public", "_phase3_test_guard", "r"),)
    result = _evaluate(_snapshot(relations=relations))
    assert result.findings[0].category == "test_marker"
    assert result.findings[0].code == ps.SCHEMA_OBJECT_PROHIBITED


@pytest.mark.parametrize(
    "name",
    ["_phase3_test_guards", "phase3_test_guard", "_phase3_test_guard_old", "_phase3_guard"],
)
def test_test_marker_near_misses_are_allowed(name: str) -> None:
    relations = _relations() + (RelationFact("public", name, "r"),)
    result = _evaluate(_snapshot(relations=relations))
    assert result.compatible is True, result.findings


@pytest.mark.parametrize(
    "name",
    ["surface_metrics", "workface", "facelift_log", "embeddingless_notes", "scores", "attendancefree"],
)
def test_substring_false_positives_are_allowed(name: str) -> None:
    relations = _relations() + (RelationFact("public", name, "r"),)
    assert prohibition_for(name) is None
    result = _evaluate(_snapshot(relations=relations))
    assert result.compatible is True, result.findings


def test_test_marker_ordered_before_other_prohibited_objects() -> None:
    relations = _relations() + (
        RelationFact("public", "zzz_attendance", "r"),
        RelationFact("public", "_phase3_test_guard", "r"),
        RelationFact("public", "aaa_biometric", "r"),
    )
    result = _evaluate(_snapshot(relations=relations))
    # The marker leads the prohibited rank; the rest are alphabetical by
    # object name, exactly as documented (never input order).
    assert [finding.category for finding in result.findings] == [
        "test_marker",
        "biometric",
        "attendance",
    ]
    assert [finding.object for finding in result.findings] == [
        "_phase3_test_guard",
        "aaa_biometric",
        "zzz_attendance",
    ]


def test_duplicate_relation_facts_do_not_duplicate_prohibited_findings() -> None:
    relations = _relations() + (
        RelationFact("public", "attendance_events", "r"),
        RelationFact("public", "attendance_events", "r"),
    )
    result = _evaluate(_snapshot(relations=relations))
    assert _codes(result) == [ps.SCHEMA_OBJECT_PROHIBITED]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def _shuffled(snapshot: CatalogSnapshot) -> CatalogSnapshot:
    return CatalogSnapshot(
        relations=tuple(reversed(snapshot.relations)),
        columns=tuple(reversed(snapshot.columns)),
        constraints=tuple(reversed(snapshot.constraints)),
        indexes=tuple(reversed(snapshot.indexes)),
    )


def _broken_snapshot() -> CatalogSnapshot:
    """One snapshot with definite findings across most precedence ranks."""
    relations = _relations() + (
        RelationFact("public", "attendance_events", "r"),
        RelationFact("public", "_phase3_test_guard", "r"),
    )
    columns = _customized(
        employee_id={"pg_type": "integer"},
        is_active={"nullable": True, "default_expression": None},
        created_at={"default_expression": "now()"},
    ) + (_column("employee", "nickname", "text", 5),)
    constraints = (_pk("schema_version", ("checksum",)),)
    return _snapshot(relations=relations, columns=columns, constraints=constraints)


def test_shuffled_snapshot_produces_identical_findings() -> None:
    snapshot = _broken_snapshot()
    first = _evaluate(snapshot)
    second = _evaluate(_shuffled(snapshot))
    assert first.status is second.status
    assert first.findings == second.findings


@pytest.mark.parametrize("attribute", ["relations", "columns", "constraints", "indexes"])
def test_each_fact_tuple_can_be_reordered_independently(attribute: str) -> None:
    snapshot = _broken_snapshot()
    baseline = _evaluate(snapshot)
    reordered = dataclasses.replace(
        snapshot, **{attribute: tuple(reversed(getattr(snapshot, attribute)))}
    )
    assert _evaluate(reordered).findings == baseline.findings


def test_findings_follow_documented_precedence() -> None:
    result = _evaluate(_broken_snapshot())
    ranks = [ps.DIAGNOSTIC_CODES.index(code) for code in _codes(result)]
    assert ranks == sorted(ranks)


def test_relation_kind_mismatch_suppresses_downstream_findings() -> None:
    columns = _customized(employee_id={"pg_type": "integer"})
    ledger_key = (
        _pk("app_user", ("user_id",)),
        _pk("user_session", ("session_token_hash",)),
        _pk("schema_version", ("version",)),
    )
    result = _evaluate(
        _snapshot(
            relations=_relations(employee_kind="v"),
            columns=columns,
            constraints=ledger_key,
        )
    )
    # The employee relation is a view, so its column and key facts are
    # unknowable and must not be reported as drift.
    assert _codes(result) == [ps.SCHEMA_TABLE_KIND_MISMATCH]
    # With the same facts but an ordinary table, both problems are reported.
    ordinary = _evaluate(_snapshot(columns=columns, constraints=ledger_key))
    assert set(_codes(ordinary)) == {
        ps.SCHEMA_COLUMN_TYPE_MISMATCH,
        ps.SCHEMA_PRIMARY_KEY_MISSING,
    }


def test_repeated_evaluation_is_stable() -> None:
    snapshot = _broken_snapshot()
    assert _evaluate(snapshot) == _evaluate(snapshot)


def test_multiple_error_categories_are_all_reported() -> None:
    result = _evaluate(_broken_snapshot())
    codes = set(_codes(result))
    assert ps.SCHEMA_OBJECT_PROHIBITED in codes
    assert ps.SCHEMA_COLUMN_UNEXPECTED in codes
    assert ps.SCHEMA_COLUMN_TYPE_MISMATCH in codes
    assert ps.SCHEMA_COLUMN_NULLABILITY_MISMATCH in codes
    assert ps.SCHEMA_COLUMN_DEFAULT_MISMATCH in codes
    assert ps.SCHEMA_PRIMARY_KEY_MISSING in codes
    assert ps.SCHEMA_PRIMARY_KEY_MISMATCH in codes
    assert result.status is ps.CompatibilityStatus.INCOMPATIBLE


# ---------------------------------------------------------------------------
# Verdict semantics
# ---------------------------------------------------------------------------


def test_indeterminate_only_when_nothing_definite_is_wrong() -> None:
    result = _evaluate(_snapshot(relations=_relations(employee_kind="p")))
    assert result.status is ps.CompatibilityStatus.INDETERMINATE
    assert result.compatible is False
    assert result.indeterminate is True
    assert result.codes


def test_definite_finding_wins_over_indeterminate() -> None:
    columns = _customized(full_name={"pg_type": "integer"})
    result = _evaluate(
        _snapshot(relations=_relations(employee_kind="p"), columns=columns)
    )
    assert set(_codes(result)) == {
        ps.SCHEMA_TABLE_KIND_UNCONFIRMED,
        ps.SCHEMA_COLUMN_TYPE_MISMATCH,
    }
    assert result.status is ps.CompatibilityStatus.INCOMPATIBLE
    assert result.indeterminate is False


def test_emitted_codes_are_documented_and_reserved_code_is_never_used() -> None:
    snapshots = [
        _snapshot(),
        _snapshot(relations=()),
        _broken_snapshot(),
        _snapshot(columns=_customized(is_active={"generated": "s"})),
    ]
    emitted: set[str] = set()
    for snapshot in snapshots:
        emitted.update(_codes(_evaluate(snapshot)))
    assert emitted <= set(ps.DIAGNOSTIC_CODES)
    assert ps.SCHEMA_CATALOG_UNREADABLE not in emitted
    assert ps.SCHEMA_CATALOG_UNREADABLE not in ps.DIAGNOSTIC_CODES
    assert ps.RESERVED_DIAGNOSTIC_CODES == (ps.SCHEMA_CATALOG_UNREADABLE,)


def test_diagnostic_code_constants_are_stable_and_unique() -> None:
    codes = [
        ps.SCHEMA_OBJECT_PROHIBITED,
        ps.SCHEMA_TABLE_MISSING,
        ps.SCHEMA_TABLE_KIND_MISMATCH,
        ps.SCHEMA_TABLE_KIND_UNCONFIRMED,
        ps.SCHEMA_COLUMN_MISSING,
        ps.SCHEMA_COLUMN_UNEXPECTED,
        ps.SCHEMA_COLUMN_TYPE_MISMATCH,
        ps.SCHEMA_COLUMN_NULLABILITY_MISMATCH,
        ps.SCHEMA_COLUMN_DEFAULT_MISMATCH,
        ps.SCHEMA_COLUMN_GENERATION_MISMATCH,
        ps.SCHEMA_COLUMN_IDENTITY_MISMATCH,
        ps.SCHEMA_PRIMARY_KEY_MISSING,
        ps.SCHEMA_PRIMARY_KEY_MISMATCH,
        ps.SCHEMA_METADATA_INCOMPLETE,
    ]
    assert tuple(codes) == ps.DIAGNOSTIC_CODES
    assert len(set(codes)) == len(codes)
    for code in codes:
        assert code.startswith("SCHEMA_")
        assert code.isupper()


# ---------------------------------------------------------------------------
# Purity and safety
# ---------------------------------------------------------------------------


def test_arguments_are_not_mutated() -> None:
    snapshot = _broken_snapshot()
    before = copy.deepcopy(snapshot)
    contract_before = copy.deepcopy(ps.REQUIRED_TABLES)
    _evaluate(snapshot)
    assert snapshot == before
    assert ps.REQUIRED_TABLES == contract_before


def test_s1_contract_is_used_not_copied() -> None:
    """The evaluator's defaults are the S1 objects themselves."""
    import inspect

    signature = inspect.signature(ps.evaluate_physical_schema)
    assert signature.parameters["contract"].default is ps.REQUIRED_TABLES
    assert signature.parameters["schema"].default == "public"


def test_employee_and_ledger_contracts_match_the_documented_rules() -> None:
    result = _evaluate(_snapshot())
    assert result.compatible is True
    # A snapshot obeying each documented column rule exactly is the only one
    # accepted: removing the true default is refused.
    columns = _customized(is_active={"default_expression": None})
    assert _evaluate(_snapshot(columns=columns)).compatible is False
    assert EMPLOYEE_TABLE.primary_key == ("employee_id",)
    assert LEDGER_TABLE.primary_key == ("version",)


def test_module_has_no_io_imports_or_sql() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    allowed = {
        "__future__",
        "dataclasses",
        "enum",
        "typing",
        "src.db_schema_catalog",
        "src.schema_contract",
    }
    assert imported == allowed, imported

    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ("psycopg", "socket", "urllib", "requests", "logging", "os.environ"):
        assert forbidden not in source
    assert "SELECT" not in source
    assert "connect(" not in source
    assert ".execute(" not in source


def test_module_calls_no_dangerous_builtins() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & {"open", "exec", "eval", "compile", "input", "__import__"}


def test_fresh_subprocess_import_pulls_no_driver_or_io_layers() -> None:
    code = (
        "import json, sys\n"
        "import src.physical_schema\n"
        "names = ('psycopg', 'psycopg2', 'socket', 'src.db', 'src.db_migrate',\n"
        "         'src.startup_schema', 'src.api', 'tier2_support')\n"
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


def test_no_module_level_mutable_state() -> None:
    for name in ps.__all__:
        value = getattr(ps, name)
        assert not isinstance(value, (list, dict, set, bytearray)), name


def test_finding_labels_and_strings_are_safe() -> None:
    result = _evaluate(_broken_snapshot())
    for finding in result.findings:
        assert finding.label.startswith("public.")
        assert "\n" not in finding.detail
        assert "postgres://" not in finding.detail
        assert "postgresql://" not in finding.detail
        assert "password" not in finding.detail.lower()
        assert "{" not in finding.detail
        assert finding.schema == "public"


# ---------------------------------------------------------------------------
# Input validation (programming errors, never database verdicts)
# ---------------------------------------------------------------------------


def test_non_snapshot_input_is_rejected() -> None:
    with pytest.raises(ps.PhysicalSchemaInputError):
        ps.evaluate_physical_schema(object())  # type: ignore[arg-type]
    assert issubclass(ps.PhysicalSchemaInputError, ValueError)


def test_snapshot_with_wrong_fact_types_is_rejected() -> None:
    with pytest.raises(ps.PhysicalSchemaInputError):
        ps.evaluate_physical_schema(
            CatalogSnapshot(relations=("employee",), columns=(), constraints=(), indexes=())  # type: ignore[arg-type]
        )


def test_empty_contract_is_rejected() -> None:
    with pytest.raises(ps.PhysicalSchemaInputError):
        ps.evaluate_physical_schema(_snapshot(), contract=())


def test_contract_with_wrong_entry_type_is_rejected() -> None:
    with pytest.raises(ps.PhysicalSchemaInputError):
        ps.evaluate_physical_schema(_snapshot(), contract=(object(),))  # type: ignore[arg-type]


def test_contract_without_columns_is_rejected() -> None:
    table = TableContract(name="empty", columns=(), primary_key=("a",))
    with pytest.raises(ps.PhysicalSchemaInputError):
        ps.evaluate_physical_schema(_snapshot(), contract=(table,))


def test_contract_without_primary_key_is_rejected() -> None:
    table = TableContract(
        name="nokey",
        columns=(ColumnContract("a", "text", False, ColumnDefault.NONE),),
        primary_key=(),
    )
    with pytest.raises(ps.PhysicalSchemaInputError):
        ps.evaluate_physical_schema(_snapshot(), contract=(table,))


def test_contract_with_undeclared_primary_key_column_is_rejected() -> None:
    table = TableContract(
        name="nokey",
        columns=(ColumnContract("a", "text", False, ColumnDefault.NONE),),
        primary_key=("b",),
    )
    with pytest.raises(ps.PhysicalSchemaInputError):
        ps.evaluate_physical_schema(_snapshot(), contract=(table,))


def test_contract_with_duplicate_table_is_rejected() -> None:
    with pytest.raises(ps.PhysicalSchemaInputError):
        ps.evaluate_physical_schema(_snapshot(), contract=(EMPLOYEE_TABLE, EMPLOYEE_TABLE))


def test_contract_with_duplicate_column_is_rejected() -> None:
    table = TableContract(
        name="dup",
        columns=(
            ColumnContract("a", "text", False, ColumnDefault.NONE),
            ColumnContract("a", "text", False, ColumnDefault.NONE),
        ),
        primary_key=("a",),
    )
    with pytest.raises(ps.PhysicalSchemaInputError):
        ps.evaluate_physical_schema(_snapshot(), contract=(table,))


def test_contract_declaring_secondary_indexes_is_rejected() -> None:
    """S3 must never silently ignore an index requirement it cannot enforce."""
    table = TableContract(
        name="indexed",
        columns=(ColumnContract("a", "text", False, ColumnDefault.NONE),),
        primary_key=("a",),
        secondary_indexes=(("a",),),
    )
    with pytest.raises(ps.PhysicalSchemaInputError):
        ps.evaluate_physical_schema(_snapshot(), contract=(table,))


@pytest.mark.parametrize("schema", ["", "   ", None, 5])
def test_invalid_schema_argument_is_rejected(schema: object) -> None:
    with pytest.raises(ps.PhysicalSchemaInputError):
        ps.evaluate_physical_schema(_snapshot(), schema=schema)  # type: ignore[arg-type]
