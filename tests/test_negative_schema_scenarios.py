"""Offline S7 declaration and comparison tests (Milestone B, G8 S7).

No database, no driver, no network. The module under test is pure data plus
pure comparison; the **real** S3 evaluator and the **real** G2 policy evaluator
are used for the cross-checks, so the declared expectations are validated
against the shipped rules rather than against a restatement of them.

Covered: the frozen matrix (counts, order, ids, database names), declared-state
completeness, the Option B comparator and its mutation matrix, manifest and
cleanup consistency, S3 cross-check, G2 cross-check, normalization equivalence,
sanitization, and import hygiene.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from src.config import AppSettings
from src.db_migrate import (
    MigrationIntegrityError,
    MigrationSnapshot,
    load_migrations,
)
from src.db_migrate_executor import PsycopgMigrationExecutor
from src.db_schema_catalog import CatalogSnapshot, ColumnFact, IndexFact, RelationFact
from src.physical_schema import (
    DIAGNOSTIC_CODES,
    SCHEMA_COLUMN_GENERATION_MISMATCH,
    CompatibilityStatus,
    evaluate_physical_schema,
)
from src.schema_contract import TARGET_SCHEMA, ColumnDefault, REQUIRED_TABLES
from src.startup_schema import (
    DIAGNOSTIC_CODES as G2_DIAGNOSTIC_CODES,
    StartupSchemaError,
    evaluate_migration_state,
    verify_startup_schema,
)
from tools import negative_schema_scenarios as s7

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "tools" / "negative_schema_scenarios.py"

SHIPPED_CHECKSUMS = {script.version: script.checksum for script in load_migrations()}
SHIPPED_CHECKSUM = SHIPPED_CHECKSUMS[1]
SHIPPED_CHECKSUM_V2 = SHIPPED_CHECKSUMS[2]

APP_DSN = "postgresql://app_user:super-secret@localhost:5432/attendance"
TEST_DSN = "postgresql://app_user:super-secret@localhost:5432/attendance_test"

MANDATORY_IDS = (
    "N00",
    "N01",
    "N02",
    "N03",
    "N04",
    "N05",
    "N06",
    "N07a",
    "N07b",
    "N08a",
    "N08b",
    "N09",
    "N10",
    "N11",
    "N12",
    "N15",
    "G01",
    "G02",
    "G03",
    "G04",
    "G04b",
    "G05",
)
OPTIONAL_IDS = ("N07c", "N09b", "N13")
DEFERRED_IDS = ("N14",)

#: Scenarios whose declaration the frozen DDL determines completely.
COMPLETE_SCENARIOS = tuple(
    scenario
    for scenario in s7.SCENARIOS
    if not s7.unresolved_properties(scenario.expected_object_state)
)

FORBIDDEN_MODULES = (
    "src.config",
    "src.db",
    "src.db_migrate",
    "src.db_migrate_executor",
    "src.startup_schema",
    "psycopg",
    "psycopg2",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _facts(expected: s7.ExpectedMigrationFacts) -> s7.MigrationFacts:
    """The observed G2 facts a correct database would report for ``expected``."""
    return s7.MigrationFacts(
        ledger_present=expected.ledger_present,
        applied=s7.resolve_expected_applied(expected, shipped_checksum=SHIPPED_CHECKSUMS),
        tables=expected.tables,
    )


def _mutate(state: s7.ExpectedObjectState, mutation: str) -> CatalogSnapshot:
    """Return an observed snapshot of ``state`` with one property changed."""
    base = s7.declared_snapshot(state)
    relations = list(base.relations)
    columns = list(base.columns)
    constraints = list(base.constraints)
    indexes = list(base.indexes)

    def retable(item):
        return dataclasses.replace(item, table="employee_renamed")

    if mutation == "drop_relation":
        relations = [item for item in relations if item.name != "employee"]
        columns = [item for item in columns if item.table != "employee"]
        constraints = [item for item in constraints if item.table != "employee"]
        indexes = [item for item in indexes if item.table != "employee"]
    elif mutation == "rename_relation":
        relations = [
            dataclasses.replace(item, name="employee_renamed")
            if item.name == "employee"
            else item
            for item in relations
        ]
        columns = [retable(item) if item.table == "employee" else item for item in columns]
        constraints = [
            retable(item) if item.table == "employee" else item for item in constraints
        ]
        indexes = [retable(item) if item.table == "employee" else item for item in indexes]
    elif mutation == "change_kind":
        relations = [
            dataclasses.replace(item, kind="p") if item.name == "employee" else item
            for item in relations
        ]
    elif mutation == "drop_column":
        columns = [
            item
            for item in columns
            if not (item.table == "employee" and item.name == "full_name")
        ]
    elif mutation == "add_column":
        columns.append(
            ColumnFact(
                schema=TARGET_SCHEMA,
                table="employee",
                name="nickname",
                pg_type="text",
                nullable=True,
                default_expression=None,
                generated="",
                identity="",
                ordinal=5,
            )
        )
    elif mutation == "change_type":
        columns = [
            dataclasses.replace(item, pg_type="integer")
            if (item.table == "employee" and item.name == "employee_id")
            else item
            for item in columns
        ]
    elif mutation == "flip_nullability":
        columns = [
            dataclasses.replace(item, nullable=True)
            if (item.table == "employee" and item.name == "full_name")
            else item
            for item in columns
        ]
    elif mutation == "add_default":
        columns = [
            dataclasses.replace(item, default_expression="now()")
            if (item.table == "employee" and item.name == "full_name")
            else item
            for item in columns
        ]
    elif mutation == "remove_default":
        columns = [
            dataclasses.replace(item, default_expression=None)
            if (item.table == "employee" and item.name == "is_active")
            else item
            for item in columns
        ]
    elif mutation == "change_generated":
        columns = [
            dataclasses.replace(item, generated="s")
            if (item.table == "employee" and item.name == "is_active")
            else item
            for item in columns
        ]
    elif mutation == "change_identity":
        columns = [
            dataclasses.replace(item, identity="d")
            if (item.table == "employee" and item.name == "is_active")
            else item
            for item in columns
        ]
    elif mutation == "drop_constraint":
        constraints = [
            item
            for item in constraints
            if not (item.table == "employee" and item.contype == "p")
        ]
    elif mutation == "change_constraint_definition":
        constraints = [
            dataclasses.replace(item, definition="PRIMARY KEY (full_name)")
            if (item.table == "employee" and item.contype == "p")
            else item
            for item in constraints
        ]
    elif mutation == "change_constraint_name":
        constraints = [
            dataclasses.replace(item, name="renamed_primary_key")
            if (item.table == "employee" and item.contype == "p")
            else item
            for item in constraints
        ]
    elif mutation == "change_index_columns":
        indexes = [
            dataclasses.replace(item, columns=("full_name",))
            if item.name == "employee_pkey"
            else item
            for item in indexes
        ]
    elif mutation == "change_index_uniqueness":
        indexes = [
            dataclasses.replace(item, is_unique=False)
            if item.name == "employee_pkey"
            else item
            for item in indexes
        ]
    elif mutation == "add_index":
        indexes.append(
            IndexFact(
                schema=TARGET_SCHEMA,
                table="employee",
                name="employee_full_name_idx",
                is_unique=False,
                is_primary=False,
                columns=("full_name",),
                definition=(
                    "CREATE INDEX employee_full_name_idx ON "
                    "public.employee USING btree (full_name)"
                ),
            )
        )
    elif mutation == "add_sequence":
        relations.append(
            RelationFact(
                schema=TARGET_SCHEMA, name="employee_id_seq", kind="S"
            )
        )
    else:  # pragma: no cover - guards the parametrization itself
        raise AssertionError(f"unknown mutation {mutation!r}")

    return CatalogSnapshot(
        relations=tuple(relations),
        columns=tuple(columns),
        constraints=tuple(constraints),
        indexes=tuple(indexes),
    )


# ---------------------------------------------------------------------------
# Matrix: counts, order, identities
# ---------------------------------------------------------------------------


def test_mandatory_matrix_counts_and_order():
    assert tuple(s.scenario_id for s in s7.MANDATORY_SCENARIOS) == MANDATORY_IDS
    assert len(s7.MANDATORY_SCENARIOS) == 22
    assert s7.MANDATORY_COUNT == 22
    assert len([s for s in s7.MANDATORY_SCENARIOS if s.layer == "G8"]) == 16
    assert len([s for s in s7.MANDATORY_SCENARIOS if s.layer == "G2"]) == 6


def test_optional_and_deferred_matrix_counts_and_order():
    assert tuple(s.scenario_id for s in s7.OPTIONAL_SCENARIOS) == OPTIONAL_IDS
    assert tuple(s.scenario_id for s in s7.DEFERRED_SCENARIOS) == DEFERRED_IDS
    assert len(s7.OPTIONAL_SCENARIOS) == 3
    assert len(s7.DEFERRED_SCENARIOS) == 1
    assert s7.OPTIONAL_COUNT == 3
    assert s7.DEFERRED_COUNT == 1


def test_all_scenarios_are_the_approved_26_in_approved_order():
    assert len(s7.SCENARIOS) == 26
    assert s7.ALL_COUNT == 26
    assert tuple(s.scenario_id for s in s7.SCENARIOS) == (
        MANDATORY_IDS + OPTIONAL_IDS + DEFERRED_IDS
    )


def test_every_scenario_is_reachable_by_id_exactly_once():
    assert len(s7.SCENARIOS_BY_ID) == 26
    for scenario in s7.SCENARIOS:
        assert s7.scenario_by_id(scenario.scenario_id) is scenario


def test_database_names_are_unique_declared_and_dedicated():
    names = [scenario.database_name for scenario in s7.SCENARIOS]
    assert len(set(names)) == len(names)
    for scenario in s7.SCENARIOS:
        assert scenario.database_name == (
            f"visual_attendance_negative_{scenario.scenario_id.lower()}"
        )
        assert scenario.database_name.islower()
        assert scenario.database_name.replace("_", "").isalnum()


def test_conditional_and_deferred_flags_are_exact():
    assert s7.scenario_by_id("N11").conditional is True
    assert s7.scenario_by_id("N12").conditional is False
    assert s7.scenario_by_id("N14").deferred_unverified is True
    assert all(not s.deferred_unverified for s in s7.MANDATORY_SCENARIOS)
    assert s7.scenario_by_id("N11").is_mandatory is True
    assert s7.scenario_by_id("N07c").is_mandatory is False
    assert s7.scenario_by_id("N13").is_mandatory is False
    assert s7.scenario_by_id("N14").is_mandatory is False
    assert all(s.is_mandatory for s in s7.MANDATORY_SCENARIOS)


def test_baseline_modes_match_the_degenerate_cases():
    assert s7.scenario_by_id("G01").baseline_mode == "none"
    assert s7.scenario_by_id("G04b").baseline_mode == "ensure_ledger"
    for scenario in ("N00", "N01", "G02", "G05"):
        assert s7.scenario_by_id(scenario).baseline_mode == "migrate"


def test_defect_transaction_is_declared_only_where_one_is_needed():
    for scenario_id in ("N00", "G01", "G04b"):
        assert s7.scenario_by_id(scenario_id).requires_defect_transaction is False
    for scenario_id in ("N01", "N11", "G02", "G05"):
        assert s7.scenario_by_id(scenario_id).requires_defect_transaction is True


def test_declared_codes_are_emitted_codes_and_statuses_are_consistent():
    ranks = {code: position for position, code in enumerate(DIAGNOSTIC_CODES)}
    for scenario in s7.G8_SCENARIOS:
        if scenario.scenario_id != "N00":
            assert scenario.expected_codes
        assert set(scenario.expected_codes) <= set(DIAGNOSTIC_CODES)
        assert scenario.expected_status is not None
        assert scenario.expected_gate_code is None
        assert scenario.gate_code_path is None
        ordered = [ranks[code] for code in scenario.expected_codes]
        assert ordered == sorted(ordered), scenario.scenario_id
        definite = [
            code
            for code in scenario.expected_codes
            if code
            not in ("SCHEMA_TABLE_KIND_UNCONFIRMED", "SCHEMA_METADATA_INCOMPLETE")
        ]
        expected_status = (
            CompatibilityStatus.INCOMPATIBLE
            if definite
            else CompatibilityStatus.INDETERMINATE
        )
        if scenario.scenario_id != "N00":
            assert scenario.expected_status is expected_status, scenario.scenario_id


def test_g2_scenarios_declare_a_gate_code_and_no_s3_verdict():
    for scenario in s7.G2_SCENARIOS:
        assert scenario.expected_codes == ()
        assert scenario.expected_status is None
        assert scenario.expected_gate_code
        assert scenario.gate_code_path in ("policy", "ledger_shape")


def test_every_emitted_code_is_covered_or_documented():
    declared = {code for scenario in s7.SCENARIOS for code in scenario.expected_codes}
    documented = {code for code, _reason in s7.NOT_PROVISIONABLE}
    assert set(DIAGNOSTIC_CODES) <= declared | documented
    gate_declared = {
        scenario.expected_gate_code
        for scenario in s7.SCENARIOS
        if scenario.expected_gate_code
    }
    assert set(G2_DIAGNOSTIC_CODES) <= gate_declared | documented
    assert all(reason for _code, reason in s7.NOT_PROVISIONABLE)
    assert len({code for code, _reason in s7.NOT_PROVISIONABLE}) == len(
        s7.NOT_PROVISIONABLE
    )


# ---------------------------------------------------------------------------
# Declared states: agreement with S1 and internal consistency
# ---------------------------------------------------------------------------


def test_declared_baseline_agrees_with_the_s1_contract():
    for table in REQUIRED_TABLES:
        declared = {
            column.name: column
            for column in s7.BASELINE_STATE.columns
            if column.table == table.name
        }
        assert set(declared) == {column.name for column in table.columns}
        for column in table.columns:
            entry = declared[column.name]
            assert entry.pg_type == column.pg_type, (table.name, column.name)
            assert entry.nullable == column.nullable, (table.name, column.name)
            assert entry.generated == ""
            assert entry.identity == ""
            assert (entry.default_expression is not None) == (
                column.default is ColumnDefault.TRUE
            ), (table.name, column.name)
        keys = [
            constraint
            for constraint in s7.BASELINE_STATE.constraints
            if constraint.table == table.name
        ]
        assert len(keys) == 1
        assert keys[0].contype == "p"
        assert keys[0].columns == table.primary_key


@pytest.mark.parametrize(
    "scenario", s7.SCENARIOS, ids=[s.scenario_id for s in s7.SCENARIOS]
)
def test_expected_state_is_internally_consistent(scenario):
    state = scenario.expected_object_state
    assert state.relations == tuple(sorted(state.relations))
    names = [name for name, _kind in state.relations]
    assert len(set(names)) == len(names)
    kinds = dict(state.relations)

    for name, kind in scenario.cleanup_manifest:
        assert name in kinds, (scenario.scenario_id, name)
        assert kinds[name] == kind, (scenario.scenario_id, name)
    assert set(scenario.cleanup_manifest) <= set(state.relations)

    for column in state.columns:
        assert column.table in kinds
        assert kinds[column.table] in ("r", "p")
    for constraint in state.constraints:
        assert constraint.table in kinds
        assert constraint.contype and constraint.columns and constraint.definition
    for index in state.indexes:
        assert index.table in kinds
        assert index.name in kinds
        assert kinds[index.name] in ("i", "I")
        assert index.columns
    for sequence in state.sequences:
        assert (sequence.name, sequence.kind) in state.relations

    for constraint in state.constraints:
        if constraint.contype != "p":
            continue
        primary = [
            index
            for index in state.indexes
            if index.table == constraint.table and index.is_primary
        ]
        assert len(primary) == 1, (scenario.scenario_id, constraint.table)
        assert primary[0].is_unique is True
        assert primary[0].columns == constraint.columns


@pytest.mark.parametrize(
    "scenario", s7.SCENARIOS, ids=[s.scenario_id for s in s7.SCENARIOS]
)
def test_statement_hygiene_is_safe_and_single(scenario):
    for statement in scenario.statements:
        assert statement.strip()
        assert ";" not in statement
        lowered = statement.lower()
        for forbidden in (
            "cascade",
            "drop schema",
            "drop database",
            "truncate",
            "drop table public.schema_version",
        ):
            assert forbidden not in lowered, (scenario.scenario_id, forbidden)


def test_cleanup_manifest_of_an_empty_state_is_empty():
    assert s7.scenario_by_id("G01").cleanup_manifest == ()
    assert s7.scenario_by_id("G01").expected_object_state == s7.EMPTY_STATE


def test_recovery_states_are_declared_per_baseline_mode():
    assert s7.scenario_by_id("G01").allowed_recovery_states == ("S0",)
    assert s7.scenario_by_id("G04b").allowed_recovery_states == ("S0", "S1")
    assert s7.scenario_by_id("N00").allowed_recovery_states == (
        "S0",
        "S1",
        "expected",
    )


# ---------------------------------------------------------------------------
# S3 cross-check (the real evaluator)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    [scenario for scenario in COMPLETE_SCENARIOS if scenario.layer == "G8"],
    ids=[
        scenario.scenario_id
        for scenario in COMPLETE_SCENARIOS
        if scenario.layer == "G8"
    ],
)
def test_s3_cross_check_matches_every_declared_g8_verdict(scenario):
    snapshot = s7.declared_snapshot(
        scenario.expected_object_state, declared_by=scenario.scenario_id
    )
    evaluation = evaluate_physical_schema(snapshot)
    assert evaluation.codes == scenario.expected_codes, scenario.scenario_id
    assert evaluation.status is scenario.expected_status, scenario.scenario_id


def test_s3_verdict_for_generated_column_is_insensitive_to_expression_text():
    """``N13``'s unresolved property cannot influence the declared verdict."""
    base = s7.declared_snapshot(s7.BASELINE_STATE)
    for text in ("'x'::text", "('x'::text)"):
        columns = tuple(
            dataclasses.replace(item, generated="s", default_expression=text)
            if (item.table == "employee" and item.name == "full_name")
            else item
            for item in base.columns
        )
        evaluation = evaluate_physical_schema(
            dataclasses.replace(base, columns=columns)
        )
        assert evaluation.codes == (SCHEMA_COLUMN_GENERATION_MISMATCH,)
        assert evaluation.status is CompatibilityStatus.INCOMPATIBLE


def test_unresolved_declarations_refuse_comparison_and_snapshot():
    for scenario_id in ("N13", "N14"):
        scenario = s7.scenario_by_id(scenario_id)
        assert s7.unresolved_properties(scenario.expected_object_state)
        with pytest.raises(s7.DeclaredStateIncomplete) as excinfo:
            s7.declared_snapshot(
                scenario.expected_object_state, declared_by=scenario_id
            )
        assert excinfo.value.scenario_id == scenario_id
        assert excinfo.value.properties
        with pytest.raises(s7.DeclaredStateIncomplete):
            s7.compare_object_state(
                s7.declared_snapshot(s7.BASELINE_STATE),
                scenario.expected_object_state,
                declared_by=scenario_id,
            )


def test_complete_declarations_report_no_unresolved_property():
    for scenario in COMPLETE_SCENARIOS:
        assert s7.unresolved_properties(scenario.expected_object_state) == ()
    assert len(COMPLETE_SCENARIOS) == 24


# ---------------------------------------------------------------------------
# Comparator: positive cases and the mutation matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario", COMPLETE_SCENARIOS, ids=[s.scenario_id for s in COMPLETE_SCENARIOS]
)
def test_unmutated_declared_state_compares_equal(scenario):
    observed = s7.declared_snapshot(
        scenario.expected_object_state, declared_by=scenario.scenario_id
    )
    result = s7.compare_object_state(
        observed, scenario.expected_object_state, declared_by=scenario.scenario_id
    )
    assert result.matched is True
    assert result.mismatches == ()
    assert result.descriptors == ()

    migration = s7.compare_migration_facts(
        _facts(scenario.expected_migration_facts),
        scenario.expected_migration_facts,
        shipped_checksum=SHIPPED_CHECKSUMS,
    )
    assert migration.matched is True


DETECTED_MUTATIONS = (
    "drop_relation",
    "rename_relation",
    "change_kind",
    "drop_column",
    "add_column",
    "change_type",
    "flip_nullability",
    "add_default",
    "remove_default",
    "change_generated",
    "change_identity",
    "drop_constraint",
    "change_constraint_definition",
    "change_index_columns",
    "change_index_uniqueness",
    "add_index",
    "add_sequence",
)

MUTATION_COMPONENTS = {
    "drop_relation": "relations",
    "rename_relation": "relations",
    "change_kind": "relations",
    "drop_column": "columns",
    "add_column": "columns",
    "change_type": "columns",
    "flip_nullability": "columns",
    "add_default": "columns",
    "remove_default": "columns",
    "change_generated": "columns",
    "change_identity": "columns",
    "drop_constraint": "constraints",
    "change_constraint_definition": "constraints",
    "change_index_columns": "indexes",
    "change_index_uniqueness": "indexes",
    "add_index": "indexes",
    "add_sequence": "relations",
}


@pytest.mark.parametrize("mutation", DETECTED_MUTATIONS)
def test_comparator_rejects_every_mutation(mutation):
    expected = s7.BASELINE_STATE
    observed = _mutate(expected, mutation)
    result = s7.compare_object_state(observed, expected, declared_by="N00")
    assert result.matched is False, mutation
    assert result.mismatches
    assert MUTATION_COMPONENTS[mutation] in {
        mismatch.component for mismatch in result.mismatches
    }, mutation


def test_comparator_detects_a_constraint_definition_change_explicitly():
    result = s7.compare_object_state(
        _mutate(s7.BASELINE_STATE, "change_constraint_definition"),
        s7.BASELINE_STATE,
    )
    assert result.matched is False
    assert [(m.component, m.object) for m in result.mismatches] == [
        ("constraints", "employee")
    ]


def test_comparator_ignores_constraint_names_by_design():
    result = s7.compare_object_state(
        _mutate(s7.BASELINE_STATE, "change_constraint_name"),
        s7.BASELINE_STATE,
    )
    assert result.matched is True
    assert result.mismatches == ()


def test_comparator_normalizes_text_before_comparing():
    base = s7.declared_snapshot(s7.BASELINE_STATE)
    columns = tuple(
        dataclasses.replace(item, pg_type="TIMESTAMP   WITH TIME ZONE")
        if (item.table == "employee" and item.name == "created_at")
        else item
        for item in base.columns
    )
    constraints = tuple(
        dataclasses.replace(item, definition="  primary   key (employee_id) ")
        if (item.table == "employee" and item.contype == "p")
        else item
        for item in base.constraints
    )
    observed = dataclasses.replace(
        base, columns=columns, constraints=constraints
    )
    assert s7.compare_object_state(observed, s7.BASELINE_STATE).matched is True


def test_comparator_ignores_fact_order_and_other_schemas():
    base = s7.declared_snapshot(s7.BASELINE_STATE)
    observed = CatalogSnapshot(
        relations=tuple(reversed(base.relations)),
        columns=tuple(reversed(base.columns)),
        constraints=tuple(reversed(base.constraints)),
        indexes=tuple(reversed(base.indexes)),
    )
    assert s7.compare_object_state(observed, s7.BASELINE_STATE).matched is True

    foreign = RelationFact(schema="other", name="employee", kind="v")
    observed = CatalogSnapshot(
        relations=base.relations + (foreign,),
        columns=base.columns,
        constraints=base.constraints,
        indexes=base.indexes,
    )
    assert s7.compare_object_state(observed, s7.BASELINE_STATE).matched is True


def test_comparator_descriptors_are_sanitized():
    result = s7.compare_object_state(
        _mutate(s7.BASELINE_STATE, "change_index_columns"), s7.BASELINE_STATE
    )
    assert result.descriptors == ("indexes[employee_pkey]",)
    rendered = repr(result) + " ".join(result.descriptors)
    for secret in ("CREATE", "USING btree", "PRIMARY KEY", "public.employee"):
        assert secret not in rendered
    for mismatch in result.mismatches:
        assert set(dataclasses.asdict(mismatch)) == {"component", "object", "reason"}


def test_normalization_is_identical_to_the_s3_rule():
    from src import physical_schema as s3

    samples = (
        "timestamp with time zone",
        "  TIMESTAMP   WITH TIME ZONE ",
        "true",
        None,
        "now()",
        "'x'::text",
        "",
    )
    for sample in samples:
        assert s7.normalize_text(sample) == s3._normalize_default(sample)
    for sample in ("text", " INTEGER ", "boolean"):
        assert s7.normalize_text(sample) == s3._normalize_type(sample)


def test_mismatch_reasons_are_the_documented_vocabulary():
    reasons = set()
    for mutation in DETECTED_MUTATIONS:
        result = s7.compare_object_state(
            _mutate(s7.BASELINE_STATE, mutation), s7.BASELINE_STATE
        )
        reasons.update(mismatch.reason for mismatch in result.mismatches)
    assert reasons <= {"missing", "unexpected", "differs"}


# ---------------------------------------------------------------------------
# G2 cross-check (the real policy evaluator)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario", s7.G2_SCENARIOS, ids=[s.scenario_id for s in s7.G2_SCENARIOS]
)
def test_g2_cross_check_matches_every_declared_gate_code(scenario):
    facts = scenario.expected_migration_facts
    snapshot = MigrationSnapshot(
        tables=tuple(facts.tables),
        ledger_present=facts.ledger_present,
        applied=s7.resolve_expected_applied(facts, shipped_checksum=SHIPPED_CHECKSUMS),
    )
    if scenario.gate_code_path == "ledger_shape":
        # The refusal comes from S4's ledger-shape check inside the read-only
        # snapshot read, so the pure policy evaluator must accept the state;
        # the shape change is visible in the declared catalog state instead.
        evaluate_migration_state(snapshot)
        checksum = [
            column
            for column in scenario.expected_object_state.columns
            if column.table == "schema_version" and column.name == "checksum"
        ]
        assert len(checksum) == 1
        assert checksum[0].pg_type == "integer"
        return
    with pytest.raises(StartupSchemaError) as excinfo:
        evaluate_migration_state(snapshot)
    assert excinfo.value.code == scenario.expected_gate_code, scenario.scenario_id


def test_g2_fact_comparison_rejects_each_wrong_history_dimension():
    expected = s7.scenario_by_id("G03").expected_migration_facts
    good = _facts(expected)
    assert s7.compare_migration_facts(
        good, expected, shipped_checksum=SHIPPED_CHECKSUMS
    ).matched

    wrong_checksum = s7.MigrationFacts(
        ledger_present=True,
        applied=((1, SHIPPED_CHECKSUM), (2, SHIPPED_CHECKSUM)),
        tables=expected.tables,
    )
    assert not s7.compare_migration_facts(
        wrong_checksum, expected, shipped_checksum=SHIPPED_CHECKSUMS
    ).matched

    wrong_ledger = dataclasses.replace(good, ledger_present=False)
    assert not s7.compare_migration_facts(
        wrong_ledger, expected, shipped_checksum=SHIPPED_CHECKSUMS
    ).matched

    wrong_tables = dataclasses.replace(good, tables=("employee",))
    assert not s7.compare_migration_facts(
        wrong_tables, expected, shipped_checksum=SHIPPED_CHECKSUMS
    ).matched


def test_g2_history_comparison_is_row_order_independent():
    expected = s7.scenario_by_id("G03").expected_migration_facts
    resolved = s7.resolve_expected_applied(expected, shipped_checksum=SHIPPED_CHECKSUMS)
    reordered = s7.MigrationFacts(
        ledger_present=True, applied=tuple(reversed(resolved)), tables=expected.tables
    )
    assert s7.compare_migration_facts(
        reordered, expected, shipped_checksum=SHIPPED_CHECKSUMS
    ).matched


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------


def test_classification_names_the_expected_waypoint():
    scenario = s7.scenario_by_id("N00")
    observed = s7.declared_snapshot(s7.BASELINE_STATE)
    assert (
        s7.classify_state(
            observed,
            _facts(scenario.expected_migration_facts),
            scenario.expected_object_state,
            shipped_checksum=SHIPPED_CHECKSUMS,
            declared_by="N00",
        )
        == "expected"
    )


def test_classification_detects_S0_S1_and_unexpected():
    empty = s7.declared_snapshot(s7.EMPTY_STATE)
    assert (
        s7.classify_state(
            empty,
            s7.MigrationFacts(False, (), ()),
            s7.BASELINE_STATE,
            shipped_checksum=SHIPPED_CHECKSUMS,
        )
        == "S0"
    )

    ledger_only = s7.declared_snapshot(s7.LEDGER_ONLY_STATE)
    assert (
        s7.classify_state(
            ledger_only,
            s7.MigrationFacts(True, (), ("schema_version",)),
            s7.BASELINE_STATE,
            shipped_checksum=SHIPPED_CHECKSUMS,
        )
        == "S1"
    )

    damaged = _mutate(s7.BASELINE_STATE, "add_index")
    assert (
        s7.classify_state(
            damaged,
            s7.MigrationFacts(False, (), ()),
            s7.BASELINE_STATE,
            shipped_checksum=SHIPPED_CHECKSUMS,
        )
        == "unexpected"
    )


def test_g04b_own_state_classifies_as_expected_not_S1():
    scenario = s7.scenario_by_id("G04b")
    assert (
        s7.classify_state(
            s7.declared_snapshot(scenario.expected_object_state),
            _facts(scenario.expected_migration_facts),
            scenario.expected_object_state,
            shipped_checksum=SHIPPED_CHECKSUMS,
            declared_by="G04b",
        )
        == "expected"
    )


# ---------------------------------------------------------------------------
# Purity and import hygiene
# ---------------------------------------------------------------------------


def test_declaration_registry_is_documented_and_not_live_verified():
    assert s7.EXPECTATIONS_VERIFIED_LIVE is False
    assert len(s7.SERVER_RENDERED_PROPERTIES) >= 4
    for component, prop in s7.SERVER_RENDERED_PROPERTIES:
        assert isinstance(component, str) and component
        assert isinstance(prop, str) and prop
    assert s7.UNRESOLVED_TEXT not in {
        value
        for scenario in COMPLETE_SCENARIOS
        for value, _ in scenario.expected_object_state.relations
    }


def test_module_source_has_no_forbidden_import_or_io():
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    attributes: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Attribute):
            attributes.add(node.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)

    for name in imported:
        assert name not in FORBIDDEN_MODULES, name
        assert not name.startswith("psycopg"), name
    assert "os" not in imported
    assert "environ" not in attributes
    assert not {"connect", "cursor", "commit", "rollback"} & attributes
    assert not {"open", "eval", "exec", "__import__"} & called


def test_fresh_import_pulls_no_config_no_driver_and_no_gate_module():
    program = (
        "import sys\n"
        "import tools.negative_schema_scenarios as module\n"
        f"forbidden = {FORBIDDEN_MODULES!r}\n"
        "leaked = [name for name in forbidden if name in sys.modules]\n"
        "print('OK' if not leaked else 'LEAK:' + ','.join(leaked))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "OK", completed.stdout + completed.stderr


# ---------------------------------------------------------------------------
# Milestone B review: independent validation of the G05 ledger-shape refusal
# ---------------------------------------------------------------------------

SHIPPED_LEDGER_SHAPE = (
    ("version", "integer", "NO"),
    ("checksum", "text", "NO"),
    ("applied_at", "timestamp with time zone", "NO"),
)
G05_LEDGER_SHAPE = (
    ("version", "integer", "NO"),
    ("checksum", "integer", "NO"),
    ("applied_at", "timestamp with time zone", "NO"),
)


class _Rows:
    """Minimal psycopg-shaped result for the ledger-shape path."""

    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)


class _SnapshotConnection:
    """Recording fake connection for ``read_snapshot`` (no driver, no socket)."""

    def __init__(self, shape_rows) -> None:
        self.shape_rows = list(shape_rows)
        self.autocommit = False
        self.statements: list[str] = []
        self.rollbacks = 0
        self.closed = 0

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if "pg_try_advisory_lock" in sql:
            return _Rows([(True,)])
        if "pg_advisory_unlock" in sql:
            return _Rows([(True,)])
        if "to_regclass" in sql:
            return _Rows([("public.schema_version",)])
        if "information_schema.columns" in sql:
            return _Rows(self.shape_rows)
        if "SELECT c.relname" in sql:
            return _Rows([("employee",), ("schema_version",)])
        if "SELECT version, checksum" in sql:
            return _Rows([(1, SHIPPED_CHECKSUM)])
        return _Rows([])

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


def test_g05_shape_is_refused_by_the_real_locked_verification():
    """The declared G05 shape is rejected by S4's own shape check."""
    connection = _SnapshotConnection(G05_LEDGER_SHAPE)
    executor = PsycopgMigrationExecutor(
        APP_DSN, connection_opener=lambda dsn: connection
    )
    with pytest.raises(MigrationIntegrityError):
        executor.verify_schema_locked(lambda snapshot: None)
    assert connection.closed == 1
    assert any("information_schema.columns" in sql for sql in connection.statements)
    # The refusal precedes the table and applied-row reads.
    assert not any("SELECT c.relname" in sql for sql in connection.statements)
    assert not any("SELECT version, checksum" in sql for sql in connection.statements)


def test_g05_shape_rejection_is_specific_to_the_declared_change():
    """The same fake accepts the shipped shape, so the refusal is not vacuous."""
    connection = _SnapshotConnection(SHIPPED_LEDGER_SHAPE)
    executor = PsycopgMigrationExecutor(
        APP_DSN, connection_opener=lambda dsn: connection
    )
    snapshot = executor.verify_schema_locked(lambda snapshot: None)
    assert snapshot.ledger_present is True
    assert snapshot.applied == ((1, SHIPPED_CHECKSUM),)
    assert snapshot.tables == frozenset({"employee", "schema_version"})
    assert any("information_schema.columns" in sql for sql in connection.statements)


class _ShapeRefusingExecutor:
    """Executor seam whose locked verification refuses a malformed ledger."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def verify_schema_locked(self, evaluate, *, catalog_probe=None, physical_evaluator=None):
        raise self.error


def test_g05_shape_refusal_maps_to_the_declared_gate_code():
    settings = AppSettings(database_url=APP_DSN, test_database_url=TEST_DSN)
    error = MigrationIntegrityError("malformed ledger")
    with pytest.raises(StartupSchemaError) as excinfo:
        verify_startup_schema(
            settings, executor_factory=lambda dsn: _ShapeRefusingExecutor(error)
        )
    assert excinfo.value.code == "MIGRATION_HISTORY_INVALID"


# ---------------------------------------------------------------------------
# Milestone B review: declaration integrity questions
# ---------------------------------------------------------------------------


def test_incomplete_declarations_never_enter_the_mandatory_matrix():
    incomplete = {
        scenario.scenario_id
        for scenario in s7.SCENARIOS
        if s7.unresolved_properties(scenario.expected_object_state)
    }
    assert incomplete == {"N13", "N14"}
    mandatory_ids = {scenario.scenario_id for scenario in s7.MANDATORY_SCENARIOS}
    assert incomplete.isdisjoint(mandatory_ids)
    for scenario_id in incomplete:
        scenario = s7.scenario_by_id(scenario_id)
        assert scenario.conditional or scenario.deferred_unverified
        assert scenario.is_mandatory is False


def test_incomplete_declaration_fails_closed_in_every_entry_point():
    scenario = s7.scenario_by_id("N14")
    observed = s7.declared_snapshot(s7.BASELINE_STATE)
    facts = s7.MigrationFacts(
        ledger_present=True,
        applied=((1, SHIPPED_CHECKSUM), (2, SHIPPED_CHECKSUM_V2)),
        tables=("employee", "app_user", "user_session", "schema_version"),
    )
    with pytest.raises(s7.DeclaredStateIncomplete):
        s7.declared_snapshot(
            scenario.expected_object_state, declared_by="N14"
        )
    with pytest.raises(s7.DeclaredStateIncomplete):
        s7.compare_object_state(
            observed, scenario.expected_object_state, declared_by="N14"
        )
    with pytest.raises(s7.DeclaredStateIncomplete):
        s7.classify_state(
            observed,
            facts,
            scenario.expected_object_state,
            shipped_checksum=SHIPPED_CHECKSUMS,
            declared_by="N14",
        )


def test_declarations_are_frozen_and_have_no_record_mode():
    scenario = s7.scenario_by_id("N00")
    with pytest.raises(dataclasses.FrozenInstanceError):
        scenario.expected_codes = ("SCHEMA_TABLE_MISSING",)  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        scenario.expected_object_state.columns[0].nullable = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        s7.SCENARIOS[0] = scenario  # type: ignore[index]

    public = {name for name in dir(s7) if not name.startswith("_")}
    assert not {
        "record",
        "record_mode",
        "accept",
        "accept_observed",
        "snapshot_expected",
        "learn",
    } & public
    assert all(isinstance(item, s7.Scenario) for item in s7.SCENARIOS)
    parameters = list(inspect.signature(s7.compare_object_state).parameters)
    assert parameters[:2] == ["observed", "expected"]
    assert list(inspect.signature(s7.declared_snapshot).parameters)[:1] == ["state"]
