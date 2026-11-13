"""Declarations and captured-facts comparison for G8 S7 (negative provisioning).

This module is **pure data plus pure comparison**. It opens no connection, reads
no environment variable, imports no driver, and writes nothing. It exists so
that the scenario matrix, the expected object states and the comparison rules
can be reviewed and tested without a database.

Scope of the claim (owner decision **Option B**)
------------------------------------------------
A scenario is satisfied only when the captured S2 facts are **exactly equal** to
the declared expectation:

* relations: unqualified name and ``pg_class.relkind``;
* columns: name, canonical type, nullability, default expression, generation and
  identity markers, and ordinal position;
* constraints: ``contype``, ordered constrained columns and
  ``pg_get_constraintdef`` text, after the approved whitespace normalization;
* indexes: name, uniqueness, primary status, ordered key columns and
  ``pg_get_indexdef`` text;
* sequences: name and kind.

This is **not** a claim of complete PostgreSQL object-state verification.
Constraint and index **names** are excluded from the constraint comparison by
design (only the index name is compared, because a generated name is part of the
expected physical state). Ownership, privileges, comments, storage parameters,
tablespaces, ``pg_constraint`` not-null rows, enforcement/validation flags,
inheritance parents and partition lineage are **not** compared. Definitions are
compared as **text** after whitespace normalization, never semantically.

Sources of expectation
----------------------
Every expectation below is declared from the frozen DDL and the shipped
migration history. Nothing is captured, recorded, learned or accepted from a
live database, and there is no record mode: a mismatch fails closed and sends
the campaign to human review.

Properties that PostgreSQL **renders** rather than receives (constraint and
index ``definition`` strings, the name of a primary-key index PostgreSQL
generates, the ``I`` relkind of a partitioned index, and rendered default text
such as ``now()``) are declared in their canonical server spelling and flagged
in :data:`SERVER_RENDERED_PROPERTIES`. They are corroborated, never rewritten,
by the ``N00`` preflight against a live baseline; until that preflight passes,
:data:`EXPECTATIONS_VERIFIED_LIVE` stays ``False``.

Declarations that are **not** fully determined by the frozen DDL carry
:data:`UNRESOLVED_TEXT` instead of an invented value. They are the two
optional/deferred scenarios (``N13`` generated-column expression text, ``N14``
identity sequence name). Anything that tries to compare or provision them
raises :class:`DeclaredStateIncomplete` — the incomplete declaration is never
silently weakened into a wildcard.

Import hygiene
--------------
Only ``src.db_schema_catalog`` (facts, no I/O) and ``src.physical_schema``
(pure rules) are imported. ``src.config``, ``src.db``, ``src.db_migrate``,
``src.startup_schema`` and ``psycopg`` are deliberately absent, so importing
this module reads no ``.env`` and establishes no database surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from src.db_schema_catalog import (
    CatalogSnapshot,
    ColumnFact,
    ConstraintFact,
    IndexFact,
    RelationFact,
)
from src.physical_schema import (
    SCHEMA_COLUMN_DEFAULT_MISMATCH,
    SCHEMA_COLUMN_GENERATION_MISMATCH,
    SCHEMA_COLUMN_IDENTITY_MISMATCH,
    SCHEMA_COLUMN_MISSING,
    SCHEMA_COLUMN_NULLABILITY_MISMATCH,
    SCHEMA_COLUMN_TYPE_MISMATCH,
    SCHEMA_COLUMN_UNEXPECTED,
    SCHEMA_METADATA_INCOMPLETE,
    SCHEMA_OBJECT_PROHIBITED,
    SCHEMA_PRIMARY_KEY_MISMATCH,
    SCHEMA_PRIMARY_KEY_MISSING,
    SCHEMA_TABLE_KIND_MISMATCH,
    SCHEMA_TABLE_KIND_UNCONFIRMED,
    SCHEMA_TABLE_MISSING,
    CompatibilityStatus,
)
from src.schema_contract import TARGET_SCHEMA

# ---------------------------------------------------------------------------
# Declaration vocabulary
# ---------------------------------------------------------------------------

#: Marker for a property the frozen DDL does not determine. Never a wildcard:
#: it makes the declaration incomplete and refuses comparison.
UNRESOLVED_TEXT: Final[str] = "<unresolved>"

#: Token for "the checksum shipped with that migration version". Resolved by
#: the caller from the shipped migration set, so this module never reads the
#: ``migrations`` directory.
SHIPPED_CHECKSUM_TOKEN: Final[str] = "<shipped>"

#: The literal :data:`src.db_migrate` migration checksum used by the G2/G3
#: SQL to forge the ledger with an all-zero history (``repeat('0', 64)``).
ZEROS_CHECKSUM: Final[str] = "0" * 64

#: ``True`` only after the ``N00`` preflight has corroborated the declared,
#: server-rendered properties against a live baseline. Never set here.
EXPECTATIONS_VERIFIED_LIVE: Final[bool] = False

#: Declared properties PostgreSQL renders rather than receives. Each entry is
#: (component, property) and is corroborated by the ``N00`` preflight.
SERVER_RENDERED_PROPERTIES: Final[tuple[tuple[str, str], ...]] = (
    ("constraints", "definition (pg_get_constraintdef text)"),
    ("indexes", "definition (pg_get_indexdef text)"),
    ("indexes", "name of a PostgreSQL-generated primary-key index"),
    ("relations", "relkind I for a partitioned primary-key index"),
    ("columns", "rendered default text (true, false, now())"),
)

#: Relkinds whose columns S2 reads and whose columns are therefore declared.
_COLUMN_BEARING_KINDS: Final[frozenset[str]] = frozenset({"r", "p"})

_MISSING: Final[str] = "missing"
_UNEXPECTED: Final[str] = "unexpected"
_DIFFERS: Final[str] = "differs"

#: The four waypoints a cleanup may observe (DB_DESIGN §3.9, S7).
STATE_EMPTY: Final[str] = "S0"
STATE_LEDGER_ONLY: Final[str] = "S1"
STATE_BASELINE: Final[str] = "S2"
STATE_EXPECTED: Final[str] = "expected"
STATE_UNEXPECTED: Final[str] = "unexpected"

#: The ``S1`` relation manifest: the ledger table and its primary-key index.
S1_RELATION_MANIFEST: Final[tuple[tuple[str, str], ...]] = (
    ("schema_version", "r"),
    ("schema_version_pkey", "i"),
)


# ---------------------------------------------------------------------------
# Expected object state (declaration side)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedColumn:
    """One declared physical column, in declaration order."""

    table: str
    name: str
    ordinal: int
    pg_type: str
    nullable: bool
    default_expression: str | None
    generated: str = ""
    identity: str = ""


@dataclass(frozen=True)
class ExpectedConstraint:
    """One declared table constraint. The name is deliberately absent."""

    table: str
    contype: str
    columns: tuple[str, ...]
    definition: str


@dataclass(frozen=True)
class ExpectedIndex:
    """One declared relation index (``name`` and ``kind`` live here / relations)."""

    table: str
    name: str
    is_unique: bool
    is_primary: bool
    columns: tuple[str, ...]
    definition: str


@dataclass(frozen=True)
class ExpectedSequence:
    """One declared sequence relation."""

    name: str
    kind: str = "S"


@dataclass(frozen=True)
class ExpectedObjectState:
    """Exactly the S2 facts a scenario must produce."""

    relations: tuple[tuple[str, str], ...] = ()
    columns: tuple[ExpectedColumn, ...] = ()
    constraints: tuple[ExpectedConstraint, ...] = ()
    indexes: tuple[ExpectedIndex, ...] = ()
    sequences: tuple[ExpectedSequence, ...] = ()

    @property
    def relation_manifest(self) -> tuple[tuple[str, str], ...]:
        """The declared (name, kind) manifest, for cleanup/manifest checks."""
        return self.relations


@dataclass(frozen=True)
class ExpectedMigrationFacts:
    """The G2 facts a scenario must produce, expressed without any I/O."""

    ledger_present: bool
    applied: tuple[tuple[int, str], ...]
    tables: tuple[str, ...]


@dataclass(frozen=True)
class MigrationFacts:
    """Observed G2 facts, built by the caller from ``read_snapshot()``."""

    ledger_present: bool
    applied: tuple[tuple[int, str], ...]
    tables: tuple[str, ...]


# ---------------------------------------------------------------------------
# Scenario declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """One declared negative-provisioning case and its expectation."""

    scenario_id: str
    layer: str
    title: str
    defect: str
    baseline_mode: str
    statements: tuple[str, ...]
    expected_object_state: ExpectedObjectState
    expected_migration_facts: ExpectedMigrationFacts
    expected_codes: tuple[str, ...]
    expected_status: CompatibilityStatus | None
    expected_gate_code: str | None
    gate_code_path: str | None
    cleanup_manifest: tuple[tuple[str, str], ...]
    allowed_recovery_states: tuple[str, ...]
    min_server_version: int | None = None
    conditional: bool = False
    deferred_unverified: bool = False
    notes: str = ""

    @property
    def database_name(self) -> str:
        """The dedicated database this scenario needs (declared, not created)."""
        return f"visual_attendance_negative_{self.scenario_id.lower()}"

    @property
    def expected_relation_manifest(self) -> tuple[tuple[str, str], ...]:
        """The declared relation manifest of the expected state."""
        return self.expected_object_state.relation_manifest

    @property
    def requires_defect_transaction(self) -> bool:
        """Whether the scenario needs a defect transaction at all."""
        return bool(self.statements)

    @property
    def is_mandatory(self) -> bool:
        """Whether the approved matrix requires this scenario.

        A conditional scenario (``N11``) stays inside the mandatory count until
        evidence withdraws it; only the deferred scenario sits outside it.
        """
        return any(scenario is self for scenario in _MANDATORY)


# ---------------------------------------------------------------------------
# Pure declaration helpers (used only to build the frozen constants below)
# ---------------------------------------------------------------------------


def _state(
    relations: tuple[tuple[str, str], ...],
    columns: tuple[ExpectedColumn, ...],
    constraints: tuple[ExpectedConstraint, ...],
    indexes: tuple[ExpectedIndex, ...],
    sequences: tuple[ExpectedSequence, ...] = (),
) -> ExpectedObjectState:
    """Freeze one state with its components in canonical order.

    The Phase 5 identity tables (:data:`_SECURITY_TABLES`) are invariant across
    every scenario in this matrix: no scenario in the approved negative set
    alters or drops ``app_user`` or ``user_session``. When a declared state
    includes both tables, their columns, primary keys and primary-key indexes
    are merged in centrally, so each scenario declares only its deliberate
    defect and the two declarations cannot drift apart.
    """
    declared_names = {name for name, _kind in relations}
    if set(_SECURITY_TABLES) <= declared_names:
        columns = tuple(columns) + tuple(
            column for column in _security_columns()
            if column.table in declared_names
        )
        constraints = tuple(constraints) + _SECURITY_CONSTRAINTS
        indexes = tuple(indexes) + _SECURITY_INDEXES
    return ExpectedObjectState(
        relations=tuple(sorted(relations)),
        columns=tuple(sorted(columns, key=lambda column: (column.table, column.ordinal))),
        constraints=tuple(
            sorted(constraints, key=lambda item: (item.table, item.contype, item.columns))
        ),
        indexes=tuple(sorted(indexes, key=lambda item: (item.table, item.name))),
        sequences=tuple(sorted(sequences, key=lambda item: item.name)),
    )


def _employee_columns(
    *,
    employee_id_type: str = "text",
    include_full_name: bool = True,
    full_name_nullable: bool = False,
    full_name_generated: bool = False,
    is_active_default: str | None = "true",
    created_at_default: str | None = None,
    identity_employee_id: bool = False,
    extra_columns: tuple[tuple[str, str], ...] = (),
) -> tuple[ExpectedColumn, ...]:
    """The declared ``public.employee`` columns of one variant.

    Every parameter is an explicit property of that variant's frozen DDL; the
    ordinal is the position the DDL gives the column, never an observation.
    """
    columns = [
        ExpectedColumn(
            table="employee",
            name="employee_id",
            ordinal=1,
            pg_type=employee_id_type,
            nullable=False,
            default_expression=None,
            identity="d" if identity_employee_id else "",
        )
    ]
    ordinal = 2
    if include_full_name:
        columns.append(
            ExpectedColumn(
                table="employee",
                name="full_name",
                ordinal=ordinal,
                pg_type="text",
                nullable=full_name_nullable,
                default_expression=UNRESOLVED_TEXT if full_name_generated else None,
                generated="s" if full_name_generated else "",
            )
        )
        ordinal += 1
    columns.append(
        ExpectedColumn(
            table="employee",
            name="is_active",
            ordinal=ordinal,
            pg_type="boolean",
            nullable=False,
            default_expression=is_active_default,
        )
    )
    ordinal += 1
    columns.append(
        ExpectedColumn(
            table="employee",
            name="created_at",
            ordinal=ordinal,
            pg_type="timestamp with time zone",
            nullable=False,
            default_expression=created_at_default,
        )
    )
    ordinal += 1
    for name, pg_type in extra_columns:
        columns.append(
            ExpectedColumn(
                table="employee",
                name=name,
                ordinal=ordinal,
                pg_type=pg_type,
                nullable=True,
                default_expression=None,
            )
        )
        ordinal += 1
    return tuple(columns)


def _ledger_columns(*, checksum_type: str = "text") -> tuple[ExpectedColumn, ...]:
    """The declared ``public.schema_version`` columns (DB_DESIGN §3.1)."""
    return (
        ExpectedColumn("schema_version", "version", 1, "integer", False, None),
        ExpectedColumn("schema_version", "checksum", 2, checksum_type, False, None),
        ExpectedColumn(
            "schema_version", "applied_at", 3, "timestamp with time zone", False, None
        ),
    )


def _primary_key(table: str, columns: tuple[str, ...]) -> ExpectedConstraint:
    """The declared primary-key constraint of one table."""
    return ExpectedConstraint(
        table=table,
        contype="p",
        columns=tuple(columns),
        definition=f"PRIMARY KEY ({', '.join(columns)})",
    )


def _primary_key_index(table: str, columns: tuple[str, ...]) -> ExpectedIndex:
    """The declared primary-key index PostgreSQL creates for that constraint."""
    joined = ", ".join(columns)
    return ExpectedIndex(
        table=table,
        name=f"{table}_pkey",
        is_unique=True,
        is_primary=True,
        columns=tuple(columns),
        definition=(
            f"CREATE UNIQUE INDEX {table}_pkey ON {TARGET_SCHEMA}.{table} "
            f"USING btree ({joined})"
        ),
    )


EMPLOYEE_PRIMARY_KEY: Final[tuple[str, ...]] = ("employee_id",)
LEDGER_PRIMARY_KEY: Final[tuple[str, ...]] = ("version",)
APP_USER_PRIMARY_KEY: Final[tuple[str, ...]] = ("user_id",)
USER_SESSION_PRIMARY_KEY: Final[tuple[str, ...]] = ("session_token_hash",)

#: The P5.2 identity tables. They are never the subject of a scenario in the
#: approved negative matrix, so :func:`_state` merges their facts centrally.
_SECURITY_TABLES: Final[tuple[str, ...]] = ("app_user", "user_session")


def _security_columns() -> tuple[ExpectedColumn, ...]:
    """The declared ``app_user`` and ``user_session`` columns (V002).

    Mirrors ``migrations/V002__security.sql``: no timestamp default anywhere,
    ``is_active`` defaults to true, and ``revoked_at`` is the one nullable
    column. ``session_token_hash`` is ``character(64)`` — the canonical
    ``format_type`` spelling of the ``CHAR(64)`` column.
    """
    return (
        ExpectedColumn("app_user", "user_id", 1, "text", False, None),
        ExpectedColumn("app_user", "username", 2, "text", False, None),
        ExpectedColumn("app_user", "username_key", 3, "text", False, None),
        ExpectedColumn("app_user", "password_hash", 4, "text", False, None),
        ExpectedColumn("app_user", "role", 5, "text", False, None),
        ExpectedColumn("app_user", "is_active", 6, "boolean", False, "true"),
        ExpectedColumn(
            "app_user", "created_at", 7, "timestamp with time zone", False, None
        ),
        ExpectedColumn(
            "app_user", "updated_at", 8, "timestamp with time zone", False, None
        ),
        ExpectedColumn(
            "user_session", "session_token_hash", 1, "character(64)", False, None
        ),
        ExpectedColumn("user_session", "user_id", 2, "text", False, None),
        ExpectedColumn(
            "user_session", "issued_at", 3, "timestamp with time zone", False, None
        ),
        ExpectedColumn(
            "user_session", "expires_at", 4, "timestamp with time zone", False, None
        ),
        ExpectedColumn(
            "user_session",
            "absolute_expires_at",
            5,
            "timestamp with time zone",
            False,
            None,
        ),
        ExpectedColumn(
            "user_session",
            "revoked_at",
            6,
            "timestamp with time zone",
            True,
            None,
        ),
        ExpectedColumn(
            "user_session", "last_seen_at", 7, "timestamp with time zone", False, None
        ),
    )


_SECURITY_CONSTRAINTS: Final[tuple[ExpectedConstraint, ...]] = (
    _primary_key("app_user", APP_USER_PRIMARY_KEY),
    _primary_key("user_session", USER_SESSION_PRIMARY_KEY),
)
_SECURITY_INDEXES: Final[tuple[ExpectedIndex, ...]] = (
    _primary_key_index("app_user", APP_USER_PRIMARY_KEY),
    _primary_key_index("user_session", USER_SESSION_PRIMARY_KEY),
)

_BASELINE_RELATIONS: Final[tuple[tuple[str, str], ...]] = (
    ("employee", "r"),
    ("employee_pkey", "i"),
    ("app_user", "r"),
    ("app_user_pkey", "i"),
    ("app_user_username_key_key", "i"),
    ("user_session", "r"),
    ("user_session_pkey", "i"),
    ("user_session_user_id_idx", "i"),
    ("user_session_expires_at_idx", "i"),
    ("schema_version", "r"),
    ("schema_version_pkey", "i"),
)

#: The state a correct ``migrate()`` baseline produces (steps 1–4 of M10).
BASELINE_STATE: Final[ExpectedObjectState] = _state(
    _BASELINE_RELATIONS,
    _employee_columns() + _ledger_columns(),
    (
        _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
        _primary_key("schema_version", LEDGER_PRIMARY_KEY),
    ),
    (
        _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
        _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
    ),
)

#: The state ``ensure_ledger()`` alone produces (``G04b``): ledger, no employee.
LEDGER_ONLY_STATE: Final[ExpectedObjectState] = _state(
    S1_RELATION_MANIFEST,
    _ledger_columns(),
    (_primary_key("schema_version", LEDGER_PRIMARY_KEY),),
    (_primary_key_index("schema_version", LEDGER_PRIMARY_KEY),),
)

#: The state ``baseline_mode='none'`` produces (``G01``): nothing at all.
EMPTY_STATE: Final[ExpectedObjectState] = ExpectedObjectState()

_BASELINE_TABLES: Final[tuple[str, ...]] = (
    "employee",
    "app_user",
    "user_session",
    "schema_version",
)

#: The G2 facts of a correct baseline: both shipped migrations applied.
BASELINE_MIGRATION_FACTS: Final[ExpectedMigrationFacts] = ExpectedMigrationFacts(
    ledger_present=True,
    applied=((1, SHIPPED_CHECKSUM_TOKEN), (2, SHIPPED_CHECKSUM_TOKEN)),
    tables=_BASELINE_TABLES,
)

_MIGRATE_RECOVERY: Final[tuple[str, ...]] = (
    STATE_EMPTY,
    STATE_LEDGER_ONLY,
    STATE_EXPECTED,
)
_ENSURE_LEDGER_RECOVERY: Final[tuple[str, ...]] = (STATE_EMPTY, STATE_LEDGER_ONLY)
_NO_BASELINE_RECOVERY: Final[tuple[str, ...]] = (STATE_EMPTY,)

_G8_BASELINE_CLEANUP: Final[tuple[tuple[str, str], ...]] = (
    ("employee", "r"),
    # ``user_session`` references ``app_user``, so the session table is
    # dropped first (no CASCADE is ever used).
    ("user_session", "r"),
    ("app_user", "r"),
    ("schema_version", "r"),
)


# ---------------------------------------------------------------------------
# Frozen defect SQL (one statement per execute(); never edited at runtime)
# ---------------------------------------------------------------------------

_DROP_EMPLOYEE: Final[str] = "DROP TABLE public.employee"

_CREATE_EMPLOYEE_BASELINE: Final[str] = """\
CREATE TABLE public.employee (
    employee_id text NOT NULL,
    full_name text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT employee_pkey PRIMARY KEY (employee_id)
)"""

_CREATE_EMPLOYEE_WITHOUT_FULL_NAME: Final[str] = """\
CREATE TABLE public.employee (
    employee_id text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT employee_pkey PRIMARY KEY (employee_id)
)"""

_CREATE_EMPLOYEE_WITH_NICKNAME: Final[str] = """\
CREATE TABLE public.employee (
    employee_id text NOT NULL,
    full_name text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL,
    nickname text,
    CONSTRAINT employee_pkey PRIMARY KEY (employee_id)
)"""

_CREATE_EMPLOYEE_INTEGER_ID: Final[str] = """\
CREATE TABLE public.employee (
    employee_id integer NOT NULL,
    full_name text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT employee_pkey PRIMARY KEY (employee_id)
)"""

_CREATE_EMPLOYEE_NULLABLE_FULL_NAME: Final[str] = """\
CREATE TABLE public.employee (
    employee_id text NOT NULL,
    full_name text,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT employee_pkey PRIMARY KEY (employee_id)
)"""

_CREATE_EMPLOYEE_CREATED_AT_DEFAULT: Final[str] = """\
CREATE TABLE public.employee (
    employee_id text NOT NULL,
    full_name text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT employee_pkey PRIMARY KEY (employee_id)
)"""

_CREATE_EMPLOYEE_NO_IS_ACTIVE_DEFAULT: Final[str] = """\
CREATE TABLE public.employee (
    employee_id text NOT NULL,
    full_name text NOT NULL,
    is_active boolean NOT NULL,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT employee_pkey PRIMARY KEY (employee_id)
)"""

_CREATE_EMPLOYEE_IS_ACTIVE_FALSE: Final[str] = """\
CREATE TABLE public.employee (
    employee_id text NOT NULL,
    full_name text NOT NULL,
    is_active boolean NOT NULL DEFAULT false,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT employee_pkey PRIMARY KEY (employee_id)
)"""

_CREATE_EMPLOYEE_NO_PRIMARY_KEY: Final[str] = """\
CREATE TABLE public.employee (
    employee_id text NOT NULL,
    full_name text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL
)"""

_CREATE_EMPLOYEE_COMPOSITE_PRIMARY_KEY: Final[str] = """\
CREATE TABLE public.employee (
    employee_id text NOT NULL,
    full_name text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT employee_pkey PRIMARY KEY (employee_id, full_name)
)"""

_CREATE_EMPLOYEE_ZERO_COLUMNS: Final[str] = "CREATE TABLE public.employee ()"

_CREATE_EMPLOYEE_PARTITIONED: Final[str] = """\
CREATE TABLE public.employee (
    employee_id text NOT NULL,
    full_name text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT employee_pkey PRIMARY KEY (employee_id)
) PARTITION BY RANGE (employee_id)"""

_CREATE_EMPLOYEE_GENERATED_FULL_NAME: Final[str] = """\
CREATE TABLE public.employee (
    employee_id text NOT NULL,
    full_name text NOT NULL GENERATED ALWAYS AS ('x'::text) STORED,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL,
    CONSTRAINT employee_pkey PRIMARY KEY (employee_id)
)"""

_CREATE_EMPLOYEE_IDENTITY: Final[str] = """\
CREATE TABLE public.employee (
    employee_id integer NOT NULL GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    full_name text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL
)"""


# ---------------------------------------------------------------------------
# G8 scenarios (one control + fifteen negatives)
# ---------------------------------------------------------------------------

_N00 = Scenario(
    scenario_id="N00",
    layer="G8",
    title="control: correct baseline is accepted",
    defect="none (control): the baseline schema must compare equal",
    baseline_mode="migrate",
    statements=(),
    expected_object_state=BASELINE_STATE,
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(),
    expected_status=CompatibilityStatus.COMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
    notes="Establishes the comparison baseline; also the preflight that "
    "corroborates every server-rendered declaration before the campaign.",
)

_N01 = Scenario(
    scenario_id="N01",
    layer="G8",
    title="required table dropped",
    defect="public.employee is absent",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE,),
    expected_object_state=_state(
        (
            ("app_user", "r"),
            ("app_user_pkey", "i"),
            ("app_user_username_key_key", "i"),
            ("user_session", "r"),
            ("user_session_pkey", "i"),
            ("user_session_user_id_idx", "i"),
            ("user_session_expires_at_idx", "i"),
            ("schema_version", "r"),
            ("schema_version_pkey", "i"),
        ),
        _ledger_columns(),
        (_primary_key("schema_version", LEDGER_PRIMARY_KEY),),
        (_primary_key_index("schema_version", LEDGER_PRIMARY_KEY),),
    ),
    expected_migration_facts=ExpectedMigrationFacts(
        ledger_present=True,
        applied=((1, SHIPPED_CHECKSUM_TOKEN), (2, SHIPPED_CHECKSUM_TOKEN)),
        tables=("app_user", "user_session", "schema_version"),
    ),
    expected_codes=(SCHEMA_TABLE_MISSING,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=(
        ("user_session", "r"),
        ("app_user", "r"),
        ("schema_version", "r"),
    ),
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_N02 = Scenario(
    scenario_id="N02",
    layer="G8",
    title="required table replaced by a view",
    defect="public.employee exists as a view",
    baseline_mode="migrate",
    statements=(
        _DROP_EMPLOYEE,
        "CREATE VIEW public.employee AS SELECT 'e'::text AS employee_id, "
        "'n'::text AS full_name, true AS is_active, now() AS created_at",
    ),
    expected_object_state=_state(
        (
            ("employee", "v"),
            ("app_user", "r"),
            ("app_user_pkey", "i"),
            ("app_user_username_key_key", "i"),
            ("user_session", "r"),
            ("user_session_pkey", "i"),
            ("user_session_user_id_idx", "i"),
            ("user_session_expires_at_idx", "i"),
            ("schema_version", "r"),
            ("schema_version_pkey", "i"),
        ),
        _ledger_columns(),
        (_primary_key("schema_version", LEDGER_PRIMARY_KEY),),
        (_primary_key_index("schema_version", LEDGER_PRIMARY_KEY),),
    ),
    expected_migration_facts=ExpectedMigrationFacts(
        ledger_present=True,
        applied=((1, SHIPPED_CHECKSUM_TOKEN), (2, SHIPPED_CHECKSUM_TOKEN)),
        tables=("app_user", "user_session", "schema_version"),
    ),
    expected_codes=(SCHEMA_TABLE_KIND_MISMATCH,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=(
        ("employee", "v"),
        ("user_session", "r"),
        ("app_user", "r"),
        ("schema_version", "r"),
    ),
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_N03 = Scenario(
    scenario_id="N03",
    layer="G8",
    title="required column dropped",
    defect="public.employee.full_name is absent",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE, _CREATE_EMPLOYEE_WITHOUT_FULL_NAME),
    expected_object_state=_state(
        _BASELINE_RELATIONS,
        _employee_columns(include_full_name=False) + _ledger_columns(),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_COLUMN_MISSING,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_N04 = Scenario(
    scenario_id="N04",
    layer="G8",
    title="unexpected column added",
    defect="public.employee has an extra nullable column",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE, _CREATE_EMPLOYEE_WITH_NICKNAME),
    expected_object_state=_state(
        _BASELINE_RELATIONS,
        _employee_columns(extra_columns=(("nickname", "text"),)) + _ledger_columns(),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_COLUMN_UNEXPECTED,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_N05 = Scenario(
    scenario_id="N05",
    layer="G8",
    title="column type changed",
    defect="public.employee.employee_id is integer",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE, _CREATE_EMPLOYEE_INTEGER_ID),
    expected_object_state=_state(
        _BASELINE_RELATIONS,
        _employee_columns(employee_id_type="integer") + _ledger_columns(),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_COLUMN_TYPE_MISMATCH,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_N06 = Scenario(
    scenario_id="N06",
    layer="G8",
    title="nullability loosened",
    defect="public.employee.full_name is nullable",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE, _CREATE_EMPLOYEE_NULLABLE_FULL_NAME),
    expected_object_state=_state(
        _BASELINE_RELATIONS,
        _employee_columns(full_name_nullable=True) + _ledger_columns(),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_COLUMN_NULLABILITY_MISMATCH,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_N07A = Scenario(
    scenario_id="N07a",
    layer="G8",
    title="unexpected default added",
    defect="public.employee.created_at defaults to now()",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE, _CREATE_EMPLOYEE_CREATED_AT_DEFAULT),
    expected_object_state=_state(
        _BASELINE_RELATIONS,
        _employee_columns(created_at_default="now()") + _ledger_columns(),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_COLUMN_DEFAULT_MISMATCH,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_N07B = Scenario(
    scenario_id="N07b",
    layer="G8",
    title="required default removed",
    defect="public.employee.is_active has no default",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE, _CREATE_EMPLOYEE_NO_IS_ACTIVE_DEFAULT),
    expected_object_state=_state(
        _BASELINE_RELATIONS,
        _employee_columns(is_active_default=None) + _ledger_columns(),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_COLUMN_DEFAULT_MISMATCH,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_N07C = Scenario(
    scenario_id="N07c",
    layer="G8",
    title="default changed to the other boolean",
    defect="public.employee.is_active defaults to false",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE, _CREATE_EMPLOYEE_IS_ACTIVE_FALSE),
    expected_object_state=_state(
        _BASELINE_RELATIONS,
        _employee_columns(is_active_default="false") + _ledger_columns(),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_COLUMN_DEFAULT_MISMATCH,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
    conditional=True,
    notes="Optional: the second default-mismatch direction (``false``).",
)

_N08A = Scenario(
    scenario_id="N08a",
    layer="G8",
    title="primary key removed",
    defect="public.employee has no primary key",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE, _CREATE_EMPLOYEE_NO_PRIMARY_KEY),
    expected_object_state=_state(
        (
            ("employee", "r"),
            ("app_user", "r"),
            ("app_user_pkey", "i"),
            ("app_user_username_key_key", "i"),
            ("user_session", "r"),
            ("user_session_pkey", "i"),
            ("user_session_user_id_idx", "i"),
            ("user_session_expires_at_idx", "i"),
            ("schema_version", "r"),
            ("schema_version_pkey", "i"),
        ),
        _employee_columns() + _ledger_columns(),
        (_primary_key("schema_version", LEDGER_PRIMARY_KEY),),
        (_primary_key_index("schema_version", LEDGER_PRIMARY_KEY),),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_PRIMARY_KEY_MISSING,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_N08B = Scenario(
    scenario_id="N08b",
    layer="G8",
    title="primary key widened",
    defect="public.employee primary key covers (employee_id, full_name)",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE, _CREATE_EMPLOYEE_COMPOSITE_PRIMARY_KEY),
    expected_object_state=_state(
        _BASELINE_RELATIONS,
        _employee_columns() + _ledger_columns(),
        (
            _primary_key("employee", ("employee_id", "full_name")),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", ("employee_id", "full_name")),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_PRIMARY_KEY_MISMATCH,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_ATTENDANCE_TABLE_COLUMNS: Final[tuple[ExpectedColumn, ...]] = (
    ExpectedColumn("attendance_event", "event_id", 1, "text", False, None),
    ExpectedColumn(
        "attendance_event", "recorded_at", 2, "timestamp with time zone", False, None
    ),
)

_N09 = Scenario(
    scenario_id="N09",
    layer="G8",
    title="prohibited attendance table",
    defect="public.attendance_event exists",
    baseline_mode="migrate",
    statements=(
        "CREATE TABLE public.attendance_event ("
        "event_id text NOT NULL, "
        "recorded_at timestamp with time zone NOT NULL)",
    ),
    expected_object_state=_state(
        _BASELINE_RELATIONS + (("attendance_event", "r"),),
        _employee_columns() + _ledger_columns() + _ATTENDANCE_TABLE_COLUMNS,
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=ExpectedMigrationFacts(
        ledger_present=True,
        applied=((1, SHIPPED_CHECKSUM_TOKEN), (2, SHIPPED_CHECKSUM_TOKEN)),
        tables=(
            "attendance_event",
            "employee",
            "app_user",
            "user_session",
            "schema_version",
        ),
    ),
    expected_codes=(SCHEMA_OBJECT_PROHIBITED,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP + (("attendance_event", "r"),),
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_N09B = Scenario(
    scenario_id="N09b",
    layer="G8",
    title="prohibited attendance view",
    defect="public.attendance_summary exists as a view",
    baseline_mode="migrate",
    statements=("CREATE VIEW public.attendance_summary AS SELECT 1 AS n",),
    expected_object_state=_state(
        _BASELINE_RELATIONS + (("attendance_summary", "v"),),
        _employee_columns() + _ledger_columns(),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_OBJECT_PROHIBITED,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP + (("attendance_summary", "v"),),
    allowed_recovery_states=_MIGRATE_RECOVERY,
    conditional=True,
    notes="Optional: proves the prohibited-name policy also covers views.",
)

_N10 = Scenario(
    scenario_id="N10",
    layer="G8",
    title="prohibited embedding table",
    defect="public.face_embeddings exists",
    baseline_mode="migrate",
    statements=(
        "CREATE TABLE public.face_embeddings (embedding_id text NOT NULL)",
    ),
    expected_object_state=_state(
        _BASELINE_RELATIONS + (("face_embeddings", "r"),),
        _employee_columns()
        + _ledger_columns()
        + (ExpectedColumn("face_embeddings", "embedding_id", 1, "text", False, None),),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=ExpectedMigrationFacts(
        ledger_present=True,
        applied=((1, SHIPPED_CHECKSUM_TOKEN), (2, SHIPPED_CHECKSUM_TOKEN)),
        tables=(
            "employee",
            "face_embeddings",
            "app_user",
            "user_session",
            "schema_version",
        ),
    ),
    expected_codes=(SCHEMA_OBJECT_PROHIBITED,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP + (("face_embeddings", "r"),),
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_N11 = Scenario(
    scenario_id="N11",
    layer="G8",
    title="table with no columns (conditional)",
    defect="public.employee exists with zero columns",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE, _CREATE_EMPLOYEE_ZERO_COLUMNS),
    expected_object_state=_state(
        (
            ("employee", "r"),
            ("app_user", "r"),
            ("app_user_pkey", "i"),
            ("app_user_username_key_key", "i"),
            ("user_session", "r"),
            ("user_session_pkey", "i"),
            ("user_session_user_id_idx", "i"),
            ("user_session_expires_at_idx", "i"),
            ("schema_version", "r"),
            ("schema_version_pkey", "i"),
        ),
        _ledger_columns(),
        (_primary_key("schema_version", LEDGER_PRIMARY_KEY),),
        (_primary_key_index("schema_version", LEDGER_PRIMARY_KEY),),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_PRIMARY_KEY_MISSING, SCHEMA_METADATA_INCOMPLETE),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
    conditional=True,
    notes="Conditional: withdrawn if the server rejects a zero-column table. "
    "The verdict is INCOMPATIBLE (not merely INDETERMINATE) because the "
    "missing primary key is a definite finding beside the incomplete metadata.",
)

_N12 = Scenario(
    scenario_id="N12",
    layer="G8",
    title="required table partitioned",
    defect="public.employee is a partitioned table",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE, _CREATE_EMPLOYEE_PARTITIONED),
    expected_object_state=_state(
        (
            ("employee", "p"),
            ("employee_pkey", "I"),
            ("app_user", "r"),
            ("app_user_pkey", "i"),
            ("app_user_username_key_key", "i"),
            ("user_session", "r"),
            ("user_session_pkey", "i"),
            ("user_session_user_id_idx", "i"),
            ("user_session_expires_at_idx", "i"),
            ("schema_version", "r"),
            ("schema_version_pkey", "i"),
        ),
        _employee_columns() + _ledger_columns(),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_TABLE_KIND_UNCONFIRMED,),
    expected_status=CompatibilityStatus.INDETERMINATE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=(("employee", "p"), ("schema_version", "r")),
    allowed_recovery_states=_MIGRATE_RECOVERY,
    min_server_version=100000,
    notes="A partitioned table is never accepted and never silently rejected: "
    "it is refused as indeterminate, matching S3.",
)

_N13 = Scenario(
    scenario_id="N13",
    layer="G8",
    title="contracted column generated",
    defect="public.employee.full_name is a stored generated column",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE, _CREATE_EMPLOYEE_GENERATED_FULL_NAME),
    expected_object_state=_state(
        _BASELINE_RELATIONS,
        _employee_columns(full_name_generated=True) + _ledger_columns(),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_COLUMN_GENERATION_MISMATCH,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
    min_server_version=120000,
    conditional=True,
    notes="Optional, declaration incomplete: a generated column stores its "
    "generation expression in pg_attrdef, and the exact rendered text is not "
    "determined by the frozen DDL. The verdict is insensitive to that text "
    "because S3 returns on the generation marker before reading the default.",
)

_N14 = Scenario(
    scenario_id="N14",
    layer="G8",
    title="identity column and its sequence",
    defect="public.employee.employee_id is an identity column with a sequence",
    baseline_mode="migrate",
    statements=(_DROP_EMPLOYEE, _CREATE_EMPLOYEE_IDENTITY),
    expected_object_state=_state(
        _BASELINE_RELATIONS + ((UNRESOLVED_TEXT, "S"),),
        _employee_columns(employee_id_type="integer", identity_employee_id=True)
        + _ledger_columns(),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (ExpectedSequence(UNRESOLVED_TEXT),),
    ),
    expected_migration_facts=BASELINE_MIGRATION_FACTS,
    expected_codes=(SCHEMA_COLUMN_TYPE_MISMATCH, SCHEMA_COLUMN_IDENTITY_MISMATCH),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
    min_server_version=100000,
    deferred_unverified=True,
    notes="Deferred, declaration incomplete: PostgreSQL allocates the identity "
    "sequence name, so the frozen DDL does not determine it. Never mandatory.",
)

_N15 = Scenario(
    scenario_id="N15",
    layer="G8",
    title="test-only marker table",
    defect="public._phase3_test_guard exists",
    baseline_mode="migrate",
    statements=(
        "CREATE TABLE public._phase3_test_guard ("
        "marker text NOT NULL, "
        "created_at timestamp with time zone NOT NULL, "
        "schema_version integer NOT NULL)",
    ),
    expected_object_state=_state(
        _BASELINE_RELATIONS + (("_phase3_test_guard", "r"),),
        _employee_columns()
        + _ledger_columns()
        + (
            ExpectedColumn("_phase3_test_guard", "marker", 1, "text", False, None),
            ExpectedColumn(
                "_phase3_test_guard",
                "created_at",
                2,
                "timestamp with time zone",
                False,
                None,
            ),
            ExpectedColumn(
                "_phase3_test_guard", "schema_version", 3, "integer", False, None
            ),
        ),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=ExpectedMigrationFacts(
        ledger_present=True,
        applied=((1, SHIPPED_CHECKSUM_TOKEN), (2, SHIPPED_CHECKSUM_TOKEN)),
        tables=(
            "_phase3_test_guard",
            "employee",
            "app_user",
            "user_session",
            "schema_version",
        ),
    ),
    expected_codes=(SCHEMA_OBJECT_PROHIBITED,),
    expected_status=CompatibilityStatus.INCOMPATIBLE,
    expected_gate_code=None,
    gate_code_path=None,
    cleanup_manifest=_G8_BASELINE_CLEANUP + (("_phase3_test_guard", "r"),),
    allowed_recovery_states=_MIGRATE_RECOVERY,
)


# ---------------------------------------------------------------------------
# G2 scenarios (gate-refusal states; S3 is never reached)
# ---------------------------------------------------------------------------

_G01 = Scenario(
    scenario_id="G01",
    layer="G2",
    title="ledger missing on an empty database",
    defect="baseline_mode='none'; the database has no ledger",
    baseline_mode="none",
    statements=(),
    expected_object_state=EMPTY_STATE,
    expected_migration_facts=ExpectedMigrationFacts(
        ledger_present=False, applied=(), tables=()
    ),
    expected_codes=(),
    expected_status=None,
    expected_gate_code="MIGRATION_LEDGER_MISSING",
    gate_code_path="policy",
    cleanup_manifest=(),
    allowed_recovery_states=_NO_BASELINE_RECOVERY,
    notes="No defect transaction at all: in_transaction_check is not_applicable.",
)

_G02 = Scenario(
    scenario_id="G02",
    layer="G2",
    title="ledger checksum forged",
    defect="version 1 is recorded with an all-zero checksum",
    baseline_mode="migrate",
    statements=(
        "UPDATE public.schema_version SET checksum = repeat('0', 64) WHERE version = 1",
    ),
    expected_object_state=BASELINE_STATE,
    expected_migration_facts=ExpectedMigrationFacts(
        ledger_present=True,
        applied=((1, ZEROS_CHECKSUM),),
        tables=_BASELINE_TABLES,
    ),
    expected_codes=(),
    expected_status=None,
    expected_gate_code="MIGRATION_CHECKSUM_MISMATCH",
    gate_code_path="policy",
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
    notes="The catalog is untouched: only the recorded history is wrong.",
)

_G03 = Scenario(
    scenario_id="G03",
    layer="G2",
    title="unknown applied version",
    defect="the ledger records version 3, which ships no script",
    baseline_mode="migrate",
    statements=(
        "INSERT INTO public.schema_version (version, checksum, applied_at) "
        "VALUES (3, repeat('0', 64), now())",
    ),
    expected_object_state=BASELINE_STATE,
    expected_migration_facts=ExpectedMigrationFacts(
        ledger_present=True,
        applied=(
            (1, SHIPPED_CHECKSUM_TOKEN),
            (2, SHIPPED_CHECKSUM_TOKEN),
            (3, ZEROS_CHECKSUM),
        ),
        tables=_BASELINE_TABLES,
    ),
    expected_codes=(),
    expected_status=None,
    expected_gate_code="UNKNOWN_MIGRATION",
    gate_code_path="policy",
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_G04 = Scenario(
    scenario_id="G04",
    layer="G2",
    title="history lost while the schema remains",
    defect="employee exists but the ledger records no applied migration",
    baseline_mode="migrate",
    statements=("DELETE FROM public.schema_version",),
    expected_object_state=BASELINE_STATE,
    expected_migration_facts=ExpectedMigrationFacts(
        ledger_present=True,
        applied=(),
        tables=_BASELINE_TABLES,
    ),
    expected_codes=(),
    expected_status=None,
    expected_gate_code="MIGRATION_HISTORY_INVALID",
    gate_code_path="policy",
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
)

_G04B = Scenario(
    scenario_id="G04b",
    layer="G2",
    title="ledger created without the migration",
    defect="baseline_mode='ensure_ledger'; the ledger exists and is empty",
    baseline_mode="ensure_ledger",
    statements=(),
    expected_object_state=LEDGER_ONLY_STATE,
    expected_migration_facts=ExpectedMigrationFacts(
        ledger_present=True, applied=(), tables=("schema_version",)
    ),
    expected_codes=(),
    expected_status=None,
    expected_gate_code="MIGRATIONS_PENDING",
    gate_code_path="policy",
    cleanup_manifest=(("schema_version", "r"),),
    allowed_recovery_states=_ENSURE_LEDGER_RECOVERY,
    notes="No defect transaction: the baseline step itself produces the state.",
)

_G05 = Scenario(
    scenario_id="G05",
    layer="G2",
    title="ledger shape changed",
    defect="public.schema_version.checksum is integer",
    baseline_mode="migrate",
    statements=(
        "ALTER TABLE public.schema_version ALTER COLUMN checksum TYPE integer USING 0",
    ),
    expected_object_state=_state(
        _BASELINE_RELATIONS,
        _employee_columns() + _ledger_columns(checksum_type="integer"),
        (
            _primary_key("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key("schema_version", LEDGER_PRIMARY_KEY),
        ),
        (
            _primary_key_index("employee", EMPLOYEE_PRIMARY_KEY),
            _primary_key_index("schema_version", LEDGER_PRIMARY_KEY),
        ),
    ),
    expected_migration_facts=ExpectedMigrationFacts(
        ledger_present=True,
        applied=((1, SHIPPED_CHECKSUM_TOKEN), (2, SHIPPED_CHECKSUM_TOKEN)),
        tables=_BASELINE_TABLES,
    ),
    expected_codes=(),
    expected_status=None,
    expected_gate_code="MIGRATION_HISTORY_INVALID",
    gate_code_path="ledger_shape",
    cleanup_manifest=_G8_BASELINE_CLEANUP,
    allowed_recovery_states=_MIGRATE_RECOVERY,
    notes="The refusal originates in S4's ledger-shape check inside the "
    "read-only snapshot read, not in the pure policy evaluator; the change is "
    "visible in the catalog as a column type. S8 must observe this code.",
)


# ---------------------------------------------------------------------------
# Ordered matrix
# ---------------------------------------------------------------------------

_MANDATORY: Final[tuple[Scenario, ...]] = (
    _N00,
    _N01,
    _N02,
    _N03,
    _N04,
    _N05,
    _N06,
    _N07A,
    _N07B,
    _N08A,
    _N08B,
    _N09,
    _N10,
    _N11,
    _N12,
    _N15,
    _G01,
    _G02,
    _G03,
    _G04,
    _G04B,
    _G05,
)

_OPTIONAL: Final[tuple[Scenario, ...]] = (_N07C, _N09B, _N13)
_DEFERRED: Final[tuple[Scenario, ...]] = (_N14,)

MANDATORY_SCENARIOS: Final[tuple[Scenario, ...]] = _MANDATORY
OPTIONAL_SCENARIOS: Final[tuple[Scenario, ...]] = _OPTIONAL
DEFERRED_SCENARIOS: Final[tuple[Scenario, ...]] = _DEFERRED
SCENARIOS: Final[tuple[Scenario, ...]] = _MANDATORY + _OPTIONAL + _DEFERRED

MANDATORY_COUNT: Final[int] = 22
OPTIONAL_COUNT: Final[int] = 3
DEFERRED_COUNT: Final[int] = 1
ALL_COUNT: Final[int] = 26

SCENARIOS_BY_ID: Final[MappingProxyType[str, Scenario]] = MappingProxyType(
    {scenario.scenario_id: scenario for scenario in SCENARIOS}
)

G8_SCENARIOS: Final[tuple[Scenario, ...]] = tuple(
    scenario for scenario in SCENARIOS if scenario.layer == "G8"
)
G2_SCENARIOS: Final[tuple[Scenario, ...]] = tuple(
    scenario for scenario in SCENARIOS if scenario.layer == "G2"
)

#: Codes no database state can reproduce, with the reason each is excluded.
#: Every emitted S3 code and every G2 gate code is either declared by a
#: scenario or listed here; the offline suite enforces that coverage.
NOT_PROVISIONABLE: Final[tuple[tuple[str, str], ...]] = (
    (
        "SCHEMA_CATALOG_UNREADABLE",
        "reserved integration code: S3 never emits it and it cannot be "
        "induced by schema drift",
    ),
    (
        "DATABASE_CONFIGURATION_INVALID",
        "depends on the process environment, not on database state; "
        "exercised by the CLI's static stage",
    ),
    (
        "DATABASE_TARGET_INVALID",
        "depends on the DSN text, not on database state",
    ),
    (
        "TEST_DATABASE_TARGET_REJECTED",
        "depends on target distinctness, not on database state",
    ),
    (
        "DATABASE_UNAVAILABLE",
        "depends on reachability, not on database state",
    ),
    (
        "MIGRATION_LOCK_UNAVAILABLE",
        "depends on a concurrent lock holder; not a reproducible schema state",
    ),
    (
        "MIGRATION_LOCK_RELEASE_FAILED",
        "depends on a session failure while releasing; not a schema state",
    ),
    (
        "MIGRATION_SET_EMPTY",
        "depends on the shipped migration set, not on database state",
    ),
    (
        "SCHEMA_VERIFICATION_FAILED",
        "internal fault path, not a database state",
    ),
    (
        "SCHEMA_INCOMPATIBLE",
        "derived from any INCOMPATIBLE S3 verdict; covered by every G8 "
        "scenario through the gate mapping",
    ),
    (
        "SCHEMA_INDETERMINATE",
        "derived from an INDETERMINATE S3 verdict; covered by N12 through "
        "the gate mapping",
    ),
)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


class DeclaredStateIncomplete(RuntimeError):
    """A declaration is not determined by the frozen DDL, so it is not compared."""

    def __init__(self, scenario_id: str, properties: tuple[str, ...]) -> None:
        self.scenario_id = scenario_id
        self.properties = properties
        listed = ", ".join(properties)
        super().__init__(
            f"declaration for {scenario_id or 'state'} is incomplete: {listed}"
        )


@dataclass(frozen=True)
class Mismatch:
    """One sanitized difference: a component, an identifier and a reason."""

    component: str
    object: str
    reason: str


@dataclass(frozen=True)
class ComparisonResult:
    """The outcome of one captured-facts comparison."""

    matched: bool
    mismatches: tuple[Mismatch, ...] = ()

    @property
    def descriptors(self) -> tuple[str, ...]:
        """``component[identifier]`` strings — identifiers only, safe to log."""
        return tuple(
            f"{mismatch.component}[{mismatch.object}]" for mismatch in self.mismatches
        )


def normalize_text(text: str | None) -> str:
    """The approved whitespace/case normalization for compared text.

    Identical to the rule S3 applies to types and defaults
    (``" ".join(text.split()).casefold()``), and pinned to it by the offline
    suite so the two can never drift. ``None`` normalizes to the empty string,
    which keeps "no default" distinct from any non-empty expression.
    """
    return "" if text is None else " ".join(text.split()).casefold()


def _unresolved_properties(state: ExpectedObjectState) -> tuple[str, ...]:
    problems: list[str] = []
    for relation in state.relations:
        if UNRESOLVED_TEXT in relation:
            problems.append("relations[name]")
    for column in state.columns:
        if column.pg_type == UNRESOLVED_TEXT:
            problems.append(f"columns[{column.table}].pg_type")
        if column.default_expression == UNRESOLVED_TEXT:
            problems.append(f"columns[{column.table}].default_expression")
    for constraint in state.constraints:
        if constraint.definition == UNRESOLVED_TEXT:
            problems.append(f"constraints[{constraint.table}].definition")
    for index in state.indexes:
        if index.name == UNRESOLVED_TEXT:
            problems.append(f"indexes[{index.table}].name")
        if index.definition == UNRESOLVED_TEXT:
            problems.append(f"indexes[{index.table}].definition")
    for sequence in state.sequences:
        if sequence.name == UNRESOLVED_TEXT:
            problems.append(f"sequences[{sequence.name}].name")
    return tuple(problems)


def unresolved_properties(state: ExpectedObjectState) -> tuple[str, ...]:
    """The declared properties the frozen DDL leaves undetermined.

    A non-empty result means the declaration is incomplete and is refused for
    comparison rather than weakened into a wildcard.
    """
    return _unresolved_properties(state)


def _require_complete(state: ExpectedObjectState, declared_by: str) -> None:
    problems = _unresolved_properties(state)
    if problems:
        raise DeclaredStateIncomplete(declared_by, problems)


def declared_snapshot(
    state: ExpectedObjectState,
    *,
    schema: str = TARGET_SCHEMA,
    declared_by: str = "",
) -> CatalogSnapshot:
    """Build the S2 snapshot a correct database would yield for ``state``.

    This is a **declaration-time** helper for the offline suite (and for
    reviewing a scenario); it is never a source of runtime observations.
    """
    _require_complete(state, declared_by)
    relations = tuple(
        RelationFact(schema=schema, name=name, kind=kind)
        for name, kind in sorted(state.relations)
    )
    columns = tuple(
        ColumnFact(
            schema=schema,
            table=column.table,
            name=column.name,
            pg_type=column.pg_type,
            nullable=column.nullable,
            default_expression=column.default_expression,
            generated=column.generated,
            identity=column.identity,
            ordinal=column.ordinal,
        )
        for column in sorted(state.columns, key=lambda item: (item.table, item.ordinal))
    )
    constraints = tuple(
        ConstraintFact(
            schema=schema,
            table=constraint.table,
            name=f"{constraint.table}_pkey",
            contype=constraint.contype,
            definition=constraint.definition,
            columns=constraint.columns,
        )
        for constraint in sorted(
            state.constraints, key=lambda item: (item.table, item.contype)
        )
    )
    indexes = tuple(
        IndexFact(
            schema=schema,
            table=index.table,
            name=index.name,
            is_unique=index.is_unique,
            is_primary=index.is_primary,
            columns=index.columns,
            definition=index.definition,
        )
        for index in sorted(state.indexes, key=lambda item: (item.table, item.name))
    )
    return CatalogSnapshot(
        relations=relations,
        columns=columns,
        constraints=constraints,
        indexes=indexes,
    )


def _reason(expected_present: bool, observed_present: bool) -> str:
    if expected_present and not observed_present:
        return _MISSING
    if observed_present and not expected_present:
        return _UNEXPECTED
    return _DIFFERS


def _relation_mismatches(
    observed: CatalogSnapshot,
    expected: ExpectedObjectState,
    schema: str,
) -> list[Mismatch]:
    declared = set(expected.relations)
    found = {(relation.name, relation.kind) for relation in observed.relations
             if relation.schema == schema}
    names_declared = {name for name, _kind in declared}
    names_found = {name for name, _kind in found}
    mismatches: list[Mismatch] = []
    for name in sorted(names_declared | names_found):
        if name in names_declared and name in names_found:
            declared_kinds = {kind for candidate, kind in declared if candidate == name}
            found_kinds = {kind for candidate, kind in found if candidate == name}
            if declared_kinds == found_kinds:
                continue
            mismatches.append(Mismatch("relations", name, _DIFFERS))
            continue
        mismatches.append(
            Mismatch("relations", name, _reason(name in names_declared, name in names_found))
        )
    return mismatches


def _column_mismatches(
    observed: CatalogSnapshot,
    expected: ExpectedObjectState,
    schema: str,
) -> list[Mismatch]:
    declared_tables = {
        name for name, kind in expected.relations if kind in _COLUMN_BEARING_KINDS
    }
    found_tables = {column.table for column in observed.columns if column.schema == schema}
    mismatches: list[Mismatch] = []
    for table in sorted(declared_tables | found_tables):
        declared = tuple(
            (
                column.name,
                normalize_text(column.pg_type),
                column.nullable,
                normalize_text(column.default_expression),
                column.generated,
                column.identity,
                column.ordinal,
            )
            for column in sorted(
                (item for item in expected.columns if item.table == table),
                key=lambda item: item.ordinal,
            )
        )
        found = tuple(
            (
                column.name,
                normalize_text(column.pg_type),
                column.nullable,
                normalize_text(column.default_expression),
                column.generated,
                column.identity,
                column.ordinal,
            )
            for column in sorted(
                (item for item in observed.columns
                 if item.schema == schema and item.table == table),
                key=lambda item: item.ordinal,
            )
        )
        if declared == found:
            continue
        mismatches.append(
            Mismatch("columns", table, _reason(bool(declared), bool(found)))
        )
    return mismatches


def _constraint_mismatches(
    observed: CatalogSnapshot,
    expected: ExpectedObjectState,
    schema: str,
) -> list[Mismatch]:
    declared_tables = {item.table for item in expected.constraints}
    found_tables = {item.table for item in observed.constraints if item.schema == schema}
    mismatches: list[Mismatch] = []
    for table in sorted(declared_tables | found_tables):
        declared = tuple(
            sorted(
                (
                    item.contype,
                    tuple(item.columns),
                    normalize_text(item.definition),
                )
                for item in expected.constraints
                if item.table == table
            )
        )
        found = tuple(
            sorted(
                (
                    item.contype,
                    tuple(item.columns),
                    normalize_text(item.definition),
                )
                for item in observed.constraints
                if item.schema == schema and item.table == table
            )
        )
        if declared == found:
            continue
        mismatches.append(
            Mismatch("constraints", table, _reason(bool(declared), bool(found)))
        )
    return mismatches


def _index_mismatches(
    observed: CatalogSnapshot,
    expected: ExpectedObjectState,
    schema: str,
) -> list[Mismatch]:
    def key(table: str, unique: bool, primary: bool,
            columns: tuple[str, ...], definition: str | None) -> tuple:
        return (table, unique, primary, tuple(columns), normalize_text(definition))

    declared = {
        item.name: key(
            item.table,
            item.is_unique,
            item.is_primary,
            item.columns,
            item.definition,
        )
        for item in expected.indexes
    }
    found = {
        item.name: key(
            item.table,
            item.is_unique,
            item.is_primary,
            item.columns,
            item.definition,
        )
        for item in observed.indexes
        if item.schema == schema
    }
    mismatches: list[Mismatch] = []
    for name in sorted(set(declared) | set(found)):
        if name in declared and name in found and declared[name] == found[name]:
            continue
        mismatches.append(
            Mismatch("indexes", name, _reason(name in declared, name in found))
        )
    return mismatches


def _sequence_mismatches(
    observed: CatalogSnapshot,
    expected: ExpectedObjectState,
    schema: str,
) -> list[Mismatch]:
    declared = {sequence.name for sequence in expected.sequences}
    found = {
        relation.name
        for relation in observed.relations
        if relation.schema == schema and relation.kind == "S"
    }
    mismatches: list[Mismatch] = []
    for name in sorted(declared | found):
        if name in declared and name in found:
            continue
        mismatches.append(
            Mismatch("sequences", name, _reason(name in declared, name in found))
        )
    return mismatches


def compare_object_state(
    observed: CatalogSnapshot,
    expected: ExpectedObjectState,
    *,
    schema: str = TARGET_SCHEMA,
    declared_by: str = "",
) -> ComparisonResult:
    """Compare captured S2 facts with a declared state (Option B, exactly).

    Constraints are compared without their names; text is compared after
    :func:`normalize_text`; every other compared property is exact.
    """
    _require_complete(expected, declared_by)
    mismatches = (
        _relation_mismatches(observed, expected, schema)
        + _column_mismatches(observed, expected, schema)
        + _constraint_mismatches(observed, expected, schema)
        + _index_mismatches(observed, expected, schema)
        + _sequence_mismatches(observed, expected, schema)
    )
    return ComparisonResult(matched=not mismatches, mismatches=tuple(mismatches))


def resolve_expected_applied(
    expected: ExpectedMigrationFacts,
    *,
    shipped_checksum: str | Mapping[int, str],
) -> tuple[tuple[int, str], ...]:
    """Resolve the shipped-checksum token and order the history by version.

    ``shipped_checksum`` is either one checksum (the single-version callers)
    or a per-version mapping, because the shipped set now contains two
    migrations with different checksums and ``V1`` and ``V2`` must resolve to
    their own content digests. A mapping that lacks an entry for a tokenised
    version is a declaration error and raises ``KeyError`` rather than silently
    reusing another version's checksum.

    The recorded history is compared version-ordered because S2's applied-row
    query has no ``ORDER BY``; row order is never treated as meaning.
    """
    per_version = shipped_checksum if isinstance(shipped_checksum, Mapping) else None
    resolved = tuple(
        (
            version,
            (
                per_version[version]
                if per_version is not None
                else shipped_checksum  # type: ignore[assignment]
            )
            if checksum == SHIPPED_CHECKSUM_TOKEN
            else checksum,
        )
        for version, checksum in expected.applied
    )
    return tuple(sorted(resolved))


def compare_migration_facts(
    observed: MigrationFacts,
    expected: ExpectedMigrationFacts,
    *,
    shipped_checksum: str,
) -> ComparisonResult:
    """Compare observed G2 facts with the declared expectation."""
    mismatches: list[Mismatch] = []
    if observed.ledger_present != expected.ledger_present:
        mismatches.append(Mismatch("migration", "schema_version", _DIFFERS))
    if tuple(sorted(observed.applied)) != resolve_expected_applied(
        expected, shipped_checksum=shipped_checksum
    ):
        mismatches.append(Mismatch("migration", "applied_history", _DIFFERS))
    if tuple(sorted(observed.tables)) != tuple(sorted(expected.tables)):
        mismatches.append(Mismatch("migration", "relation_set", _DIFFERS))
    return ComparisonResult(matched=not mismatches, mismatches=tuple(mismatches))


def _is_empty(observed: CatalogSnapshot, schema: str) -> bool:
    return not (
        [item for item in observed.relations if item.schema == schema]
        or [item for item in observed.columns if item.schema == schema]
        or [item for item in observed.constraints if item.schema == schema]
        or [item for item in observed.indexes if item.schema == schema]
    )


def _is_ledger_only(
    observed: CatalogSnapshot,
    migration: MigrationFacts,
    schema: str,
) -> bool:
    relations = {
        (item.name, item.kind)
        for item in observed.relations
        if item.schema == schema
    }
    return (
        relations == set(S1_RELATION_MANIFEST)
        and migration.ledger_present
        and not migration.applied
    )


def classify_state(
    observed: CatalogSnapshot,
    migration: MigrationFacts,
    expected: ExpectedObjectState,
    *,
    shipped_checksum: str,
    schema: str = TARGET_SCHEMA,
    declared_by: str = "",
) -> str:
    """Classify an observed waypoint as S0, S1, expected or unexpected.

    ``expected`` is checked first, so ``G04b`` (whose expected state *is* S1)
    reports ``expected``. The classification never authorizes DDL by itself:
    cleanup additionally requires the manifest check.
    """
    if compare_object_state(
        observed, expected, schema=schema, declared_by=declared_by
    ).matched:
        return STATE_EXPECTED
    if _is_empty(observed, schema) and not migration.ledger_present and not migration.applied:
        return STATE_EMPTY
    if _is_ledger_only(observed, migration, schema):
        return STATE_LEDGER_ONLY
    return STATE_UNEXPECTED


def scenario_by_id(scenario_id: str) -> Scenario:
    """Return the declared scenario or raise ``KeyError``."""
    return SCENARIOS_BY_ID[scenario_id]


__all__ = [
    "ALL_COUNT",
    "BASELINE_MIGRATION_FACTS",
    "BASELINE_STATE",
    "ComparisonResult",
    "DEFERRED_COUNT",
    "DEFERRED_SCENARIOS",
    "DeclaredStateIncomplete",
    "EMPLOYEE_PRIMARY_KEY",
    "EMPTY_STATE",
    "EXPECTATIONS_VERIFIED_LIVE",
    "ExpectedColumn",
    "ExpectedConstraint",
    "ExpectedIndex",
    "ExpectedMigrationFacts",
    "ExpectedObjectState",
    "ExpectedSequence",
    "G2_SCENARIOS",
    "G8_SCENARIOS",
    "LEDGER_ONLY_STATE",
    "LEDGER_PRIMARY_KEY",
    "MANDATORY_COUNT",
    "MANDATORY_SCENARIOS",
    "MigrationFacts",
    "Mismatch",
    "NOT_PROVISIONABLE",
    "OPTIONAL_COUNT",
    "OPTIONAL_SCENARIOS",
    "S1_RELATION_MANIFEST",
    "SCENARIOS",
    "SCENARIOS_BY_ID",
    "SERVER_RENDERED_PROPERTIES",
    "SHIPPED_CHECKSUM_TOKEN",
    "STATE_EMPTY",
    "STATE_EXPECTED",
    "STATE_LEDGER_ONLY",
    "STATE_UNEXPECTED",
    "Scenario",
    "UNRESOLVED_TEXT",
    "ZEROS_CHECKSUM",
    "classify_state",
    "compare_migration_facts",
    "compare_object_state",
    "declared_snapshot",
    "normalize_text",
    "resolve_expected_applied",
    "scenario_by_id",
    "unresolved_properties",
]
