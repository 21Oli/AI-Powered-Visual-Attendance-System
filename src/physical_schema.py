"""G8 — Step 39, S3: the pure physical schema evaluator.

S1 (:mod:`src.schema_contract`) says what the application *expects*. S2
(:mod:`src.db_schema_catalog`) reads what PostgreSQL *has*. This module is the
only place that compares the two, and it is **pure Python**: it opens no
connection, issues no SQL, reads no file or environment variable, writes no
log, and holds no mutable state. Every input arrives as an argument and every
output is an immutable value.

Why a separate, pure stage
--------------------------
Keeping the comparison out of the probe and out of the gate means the whole
compatibility policy is executable offline over hand-built snapshots: every
rule below is covered by tests that never touch a database. It also makes the
judgement reusable by the executor hook (S4) and the startup gate (S5) without
either of them owning policy.

Interface
---------
:func:`evaluate_physical_schema` takes an immutable
:class:`~src.db_schema_catalog.CatalogSnapshot` and, by default, the S1
contract (:data:`~src.schema_contract.REQUIRED_TABLES`). It returns a frozen
:class:`SchemaEvaluation` whose ``status`` is one of three values and whose
``findings`` tuple is fully ordered.

Three outcomes, not two
-----------------------
* ``COMPATIBLE`` — every contracted fact is present and matches.
* ``INCOMPATIBLE`` — at least one fact is definitely wrong. These findings are
  *reasons*: the schema is known not to satisfy the contract.
* ``INDETERMINATE`` — nothing is definitely wrong, but the available facts are
  not sufficient to accept (or do not cover a question the contract leaves
  open). Startup must refuse an indeterminate result exactly like an
  incompatible one; the difference is that an indeterminate result is a defect
  in the *metadata or the contract*, not necessarily in the database.

:func:`evaluate_physical_schema` deliberately never raises for a merely
incompatible or incomplete schema: incompatibility is data, not an exception.
Only a structurally unusable *call* (a non-snapshot value, an empty or
self-contradictory contract) raises :class:`PhysicalSchemaInputError`, because
that is a programming error rather than a fact about a database.

Compatibility means nothing is compatible
-----------------------------------------
``COMPATIBLE`` is returned only when ``findings`` is empty. There is no
"warning", no "skipped" and no partial acceptance: an empty catalog therefore
produces two ``SCHEMA_TABLE_MISSING`` findings and is never compatible, and a
table whose column facts are absent is reported as
``SCHEMA_METADATA_INCOMPLETE`` instead of being treated as a table with no
columns. Missing metadata is never read as a match.

Rules
-----
For each contracted table, in contract order (``employee``, then
``schema_version``):

1. **Presence.** No relation of that name in the probed schema →
   ``SCHEMA_TABLE_MISSING``. Presence is never inferred from columns or
   constraints.
2. **Kind.** A view, materialized view, sequence, foreign table, composite
   type or index does not satisfy a required table →
   ``SCHEMA_TABLE_KIND_MISMATCH``. An ordinary table (``r``) is accepted. A
   **partitioned** table (``p``) is reported as
   ``SCHEMA_TABLE_KIND_UNCONFIRMED``: the S1 contract describes the ordinary
   table ``V001`` creates and says nothing about partitioned acceptance, so S3
   neither accepts it silently nor calls it drift. That is an owner decision
   (see ``DB_DESIGN.md`` §3.9), and until it is made the result is
   indeterminate, which the gate must refuse.
3. **Columns.** Each contracted column must exist (``SCHEMA_COLUMN_MISSING``),
   have the contracted canonical type
   (``SCHEMA_COLUMN_TYPE_MISMATCH``), nullability
   (``SCHEMA_COLUMN_NULLABILITY_MISMATCH``), default semantics
   (``SCHEMA_COLUMN_DEFAULT_MISMATCH``) and be an ordinary column — a stored
   generated column (``SCHEMA_COLUMN_GENERATION_MISMATCH``) or an identity
   column (``SCHEMA_COLUMN_IDENTITY_MISMATCH``) is never equivalent to one
   with a default. Columns not named by the contract are
   ``SCHEMA_COLUMN_UNEXPECTED``.
4. **Primary key.** The contracted columns, in the contracted order, inside a
   ``pg_constraint`` primary-key constraint → ``SCHEMA_PRIMARY_KEY_MISSING`` /
   ``SCHEMA_PRIMARY_KEY_MISMATCH``. A primary key is never inferred from
   column names, a unique index or ``IndexFact.is_primary``.

Prohibited objects are checked over **every** relation kind in the probed
schema, using :func:`src.schema_contract.prohibition_for` — the single S1
implementation, so the token-boundary rules are never re-implemented or
broadened here. A relation named after the Tier-2 marker
(``_phase3_test_guard``) carries the distinct ``category`` ``test_marker`` so
later integration can report a wrong *target* rather than schema drift.

Unrelated objects are allowed: any non-prohibited table, view, index, sequence
or constraint that the contract does not mention produces no finding at all, so
a DBA addition is not drift and the public schema is not turned into an
allowlist.

Type and default comparison (documented conservative choices)
------------------------------------------------------------
* Types are compared after collapsing internal whitespace and case, against
  the canonical ``format_type`` spelling the contract stores. This cannot make
  two different PostgreSQL types equal; it only tolerates formatting. Aliases
  such as ``timestamptz`` or ``int4`` are **not** accepted, because
  ``format_type`` never emits them — accepting them would mean maintaining a
  second alias table that could silently equate two spellings of different
  types.
* A required default is accepted only when the default expression, after the
  same whitespace/case normalization, is exactly ``true``. PostgreSQL renders
  every spelling of a boolean-true default (``TRUE``, ``'t'::boolean``) as
  ``true``, so any other text — including ``false``, ``now()`` or an arbitrary
  expression — is a mismatch. Nothing weaker would be safe.
* Raw default *expressions* are never copied into a finding, only the fact
  that the default is absent, unexpected, or not ``true``: an expression is
  free-form SQL and could carry a literal. Constraint and index definitions are
  likewise never echoed; findings name columns only.

Deterministic ordering
----------------------
Findings are sorted by a fixed precedence rank
(:data:`_PRECEDENCE`), then by the S1 rule category (so the test marker always
precedes ordinary prohibited objects), then by schema, object, column, code and
detail. Nothing depends on snapshot order or on Python's sort stability: tests
feed shuffled snapshots and assert byte-identical results. Descriptive fields
are never the primary key, so two findings that differ only in detail still
have a total order.

Scope: what this module is not
------------------------------
It does not evaluate secondary indexes (the S1 contract declares none, and S2
cannot distinguish ``INCLUDE`` columns from key columns, so comparing
``IndexFact.columns`` would be unsafe); it does not check PostgreSQL versions
(that needs a connection, and belongs to the integration stages); it does not
read triggers, rules, policies, functions, types, extensions, ACLs or storage
parameters (S2 does not collect them, and none expresses a G8 requirement); it
does not repair, migrate, or write anything; and it does not enforce anything
at startup — that is S5, which does not exist yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from src.db_schema_catalog import (
    CatalogSnapshot,
    ColumnFact,
    ConstraintFact,
    IndexFact,
    RelationFact,
)
from src.schema_contract import (
    REQUIRED_TABLES,
    TARGET_SCHEMA,
    ColumnContract,
    ColumnDefault,
    TableContract,
    prohibition_for,
)

__all__ = [
    "COMPATIBLE",
    "INCOMPATIBLE",
    "INDETERMINATE",
    "DIAGNOSTIC_CODES",
    "RESERVED_DIAGNOSTIC_CODES",
    "SCHEMA_CATALOG_UNREADABLE",
    "SCHEMA_COLUMN_DEFAULT_MISMATCH",
    "SCHEMA_COLUMN_GENERATION_MISMATCH",
    "SCHEMA_COLUMN_IDENTITY_MISMATCH",
    "SCHEMA_COLUMN_MISSING",
    "SCHEMA_COLUMN_NULLABILITY_MISMATCH",
    "SCHEMA_COLUMN_TYPE_MISMATCH",
    "SCHEMA_COLUMN_UNEXPECTED",
    "SCHEMA_METADATA_INCOMPLETE",
    "SCHEMA_OBJECT_PROHIBITED",
    "SCHEMA_PRIMARY_KEY_MISMATCH",
    "SCHEMA_PRIMARY_KEY_MISSING",
    "SCHEMA_TABLE_KIND_MISMATCH",
    "SCHEMA_TABLE_KIND_UNCONFIRMED",
    "SCHEMA_TABLE_MISSING",
    "CompatibilityStatus",
    "Finding",
    "PhysicalSchemaInputError",
    "SchemaEvaluation",
    "evaluate_physical_schema",
]

# ---------------------------------------------------------------------------
# Diagnostic vocabulary (stable machine surface)
# ---------------------------------------------------------------------------

#: A relation in the probed schema carries a prohibited name (S1 policy). The
#: finding's ``category`` says which rule matched; ``test_marker`` is the
#: wrong-database marker and is deliberately distinguishable.
SCHEMA_OBJECT_PROHIBITED: Final[str] = "SCHEMA_OBJECT_PROHIBITED"
#: A contracted table has no relation of that name in the probed schema.
SCHEMA_TABLE_MISSING: Final[str] = "SCHEMA_TABLE_MISSING"
#: A contracted table exists as something that is not an ordinary table.
SCHEMA_TABLE_KIND_MISMATCH: Final[str] = "SCHEMA_TABLE_KIND_MISMATCH"
#: A contracted table exists as a *partitioned* table, which the contract does
#: not describe either way (owner decision pending).
SCHEMA_TABLE_KIND_UNCONFIRMED: Final[str] = "SCHEMA_TABLE_KIND_UNCONFIRMED"
#: A contracted column is absent.
SCHEMA_COLUMN_MISSING: Final[str] = "SCHEMA_COLUMN_MISSING"
#: A physical column is not named by the contract.
SCHEMA_COLUMN_UNEXPECTED: Final[str] = "SCHEMA_COLUMN_UNEXPECTED"
#: A contracted column has a different canonical type.
SCHEMA_COLUMN_TYPE_MISMATCH: Final[str] = "SCHEMA_COLUMN_TYPE_MISMATCH"
#: A contracted column's nullability differs from the contract.
SCHEMA_COLUMN_NULLABILITY_MISMATCH: Final[str] = "SCHEMA_COLUMN_NULLABILITY_MISMATCH"
#: A contracted column's default differs (absent, unexpected, or not ``true``).
SCHEMA_COLUMN_DEFAULT_MISMATCH: Final[str] = "SCHEMA_COLUMN_DEFAULT_MISMATCH"
#: A contracted column is a stored generated column.
SCHEMA_COLUMN_GENERATION_MISMATCH: Final[str] = "SCHEMA_COLUMN_GENERATION_MISMATCH"
#: A contracted column is an identity column.
SCHEMA_COLUMN_IDENTITY_MISMATCH: Final[str] = "SCHEMA_COLUMN_IDENTITY_MISMATCH"
#: A contracted table has no primary-key constraint.
SCHEMA_PRIMARY_KEY_MISSING: Final[str] = "SCHEMA_PRIMARY_KEY_MISSING"
#: A contracted table's primary-key columns or their order differ.
SCHEMA_PRIMARY_KEY_MISMATCH: Final[str] = "SCHEMA_PRIMARY_KEY_MISMATCH"
#: The snapshot's facts for a contracted table are absent, duplicated or
#: self-contradictory, so compatibility cannot be decided from them.
SCHEMA_METADATA_INCOMPLETE: Final[str] = "SCHEMA_METADATA_INCOMPLETE"

#: **Reserved.** A catalog read failure is S2's ``CatalogProbeError``; naming it
#: here documents the code the integration stages (S4/S5) must use to report
#: that failure. S3 never emits it, because S3 performs no catalog read.
SCHEMA_CATALOG_UNREADABLE: Final[str] = "SCHEMA_CATALOG_UNREADABLE"

#: Every code this module can emit, in precedence order (documentation and test
#: surface). The reserved code is deliberately absent.
DIAGNOSTIC_CODES: Final[tuple[str, ...]] = (
    SCHEMA_OBJECT_PROHIBITED,
    SCHEMA_TABLE_MISSING,
    SCHEMA_TABLE_KIND_MISMATCH,
    SCHEMA_TABLE_KIND_UNCONFIRMED,
    SCHEMA_COLUMN_MISSING,
    SCHEMA_COLUMN_UNEXPECTED,
    SCHEMA_COLUMN_TYPE_MISMATCH,
    SCHEMA_COLUMN_NULLABILITY_MISMATCH,
    SCHEMA_COLUMN_DEFAULT_MISMATCH,
    SCHEMA_COLUMN_GENERATION_MISMATCH,
    SCHEMA_COLUMN_IDENTITY_MISMATCH,
    SCHEMA_PRIMARY_KEY_MISSING,
    SCHEMA_PRIMARY_KEY_MISMATCH,
    SCHEMA_METADATA_INCOMPLETE,
)

#: Codes that are never emitted by S3 (integration-stage codes only).
RESERVED_DIAGNOSTIC_CODES: Final[tuple[str, ...]] = (SCHEMA_CATALOG_UNREADABLE,)

#: Fixed precedence rank per code. Prohibited objects first (the test marker
#: leads inside that rank via the finding's ``category``), then table identity,
#: then columns, then the primary key, then metadata incompleteness last: an
#: incomplete snapshot is a statement about what could not be checked, so it
#: never displaces a definite finding it accompanies.
_PRECEDENCE: Final[tuple[str, ...]] = DIAGNOSTIC_CODES
_RANK: Final[dict[str, int]] = {
    code: position for position, code in enumerate(_PRECEDENCE)
}

#: Findings with these codes mean "cannot decide", not "wrong". A definite
#: finding anywhere makes the whole result ``INCOMPATIBLE``; otherwise any of
#: these makes it ``INDETERMINATE``.
_INDETERMINATE_CODES: Final[frozenset[str]] = frozenset(
    {SCHEMA_TABLE_KIND_UNCONFIRMED, SCHEMA_METADATA_INCOMPLETE}
)

#: Human labels for ``pg_class.relkind`` values a required table could
#: wrongly appear as. The raw catalog letter is not echoed in findings.
_KIND_LABELS: Final[dict[str, str]] = {
    "v": "a view",
    "m": "a materialized view",
    "S": "a sequence",
    "f": "a foreign table",
    "c": "a composite type",
    "i": "an index",
    "I": "a partitioned index",
}

#: The only relation kind an S1 table contract is written against (the
#: ordinary table ``V001`` creates).
_ORDINARY_TABLE_KIND: Final[str] = "r"
#: Partitioned tables: a relation G8 must not silently accept *or* reject.
_PARTITIONED_TABLE_KIND: Final[str] = "p"

#: The one default expression a contracted default accepts, after
#: normalization (PostgreSQL renders every boolean-true spelling this way).
_TRUE_DEFAULT: Final[str] = "true"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class CompatibilityStatus(Enum):
    """Overall verdict of one evaluation.

    ``COMPATIBLE`` is the only value that permits anything; it means the
    findings tuple is empty. The other two are both refusals, distinguished so
    an operator can tell database drift (``INCOMPATIBLE``) from metadata or
    contract incompleteness (``INDETERMINATE``).
    """

    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    INDETERMINATE = "indeterminate"


#: ``SchemaEvaluation.status`` values as plain strings, for callers that
#: serialize the verdict rather than import the enum.
COMPATIBLE: Final[str] = CompatibilityStatus.COMPATIBLE.value
INCOMPATIBLE: Final[str] = CompatibilityStatus.INCOMPATIBLE.value
INDETERMINATE: Final[str] = CompatibilityStatus.INDETERMINATE.value


class PhysicalSchemaInputError(ValueError):
    """The evaluator was called with something it cannot evaluate.

    This is a **programming** error — a value that is not a
    :class:`~src.db_schema_catalog.CatalogSnapshot`, or a contract that is
    empty, duplicated, self-contradictory, or declares a requirement S3 does
    not implement. It is never raised because a *database* is incompatible:
    that is reported as an ``INCOMPATIBLE`` evaluation.
    """


@dataclass(frozen=True)
class Finding:
    """One structured compatibility finding.

    ``code`` is a stable :data:`DIAGNOSTIC_CODES` value. ``schema`` and
    ``object`` name the relation (never a qualified SQL string), ``column`` is
    set only for column-scoped findings, and ``detail`` is a fixed,
    value-free explanation: it contains identifiers and contract type
    spellings but never a default expression, constraint definition, index
    definition, SQL text, DSN, credential or driver message.
    """

    code: str
    schema: str
    object: str
    detail: str
    column: str | None = None
    #: The S1 prohibited-name rule category for ``SCHEMA_OBJECT_PROHIBITED``
    #: (``attendance``, ``embedding``, ``biometric``, ``face``, ``enrollment``
    #: or ``test_marker``); ``None`` for every other code.
    category: str | None = None

    @property
    def label(self) -> str:
        """``schema.object[.column]`` — identifiers only, safe to log."""
        if self.column is None:
            return f"{self.schema}.{self.object}"
        return f"{self.schema}.{self.object}.{self.column}"

    def __str__(self) -> str:
        return f"{self.code}: {self.label}: {self.detail}"


@dataclass(frozen=True)
class SchemaEvaluation:
    """The complete, ordered result of one evaluation.

    ``findings`` is a tuple in documented precedence order (see
    :data:`_PRECEDENCE`). Use :attr:`compatible` for the gate's only question;
    :attr:`indeterminate` distinguishes "cannot decide" from "wrong".
    """

    status: CompatibilityStatus
    findings: tuple[Finding, ...]

    @property
    def compatible(self) -> bool:
        """Whether the schema satisfies the contract (``findings`` is empty)."""
        return self.status is CompatibilityStatus.COMPATIBLE

    @property
    def indeterminate(self) -> bool:
        """Whether the result is refused for incomplete facts, not for drift."""
        return self.status is CompatibilityStatus.INDETERMINATE

    @property
    def codes(self) -> tuple[str, ...]:
        """Emitted diagnostic codes in order, one per finding."""
        return tuple(finding.code for finding in self.findings)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def evaluate_physical_schema(
    snapshot: CatalogSnapshot,
    contract: tuple[TableContract, ...] = REQUIRED_TABLES,
    schema: str = TARGET_SCHEMA,
) -> SchemaEvaluation:
    """Compare ``snapshot`` against ``contract`` and return an ordered verdict.

    ``snapshot`` is what S2 read; ``contract`` defaults to the S1 contract and
    exists as a parameter so tests (and a future contract revision) can drive
    the same rules without monkeypatching module state. ``schema`` defaults to
    the contract's schema; facts for any other schema are ignored, so a shared
    database's other schemas cannot influence the result.

    The function is pure: it mutates neither argument and calls nothing that
    has I/O. It does **not** raise for an incompatible or incomplete database —
    that is a returned ``INCOMPATIBLE``/``INDETERMINATE`` evaluation. It raises
    :class:`PhysicalSchemaInputError` only for an unusable call, and doing so
    cannot be mistaken for a verdict.
    """
    _validate_call(snapshot, contract, schema)

    relations = tuple(
        relation for relation in snapshot.relations if relation.schema == schema
    )
    columns = tuple(
        column for column in snapshot.columns if column.schema == schema
    )
    constraints = tuple(
        constraint
        for constraint in snapshot.constraints
        if constraint.schema == schema
    )

    findings: list[Finding] = []
    findings.extend(_prohibited_findings(relations, schema))
    for table in contract:
        findings.extend(
            _table_findings(table, schema, relations, columns, constraints)
        )

    ordered = _order(findings)
    return SchemaEvaluation(status=_verdict(ordered), findings=ordered)


# ---------------------------------------------------------------------------
# Prohibited objects (S1 policy, applied to every relation kind)
# ---------------------------------------------------------------------------


def _prohibited_findings(
    relations: tuple[RelationFact, ...], schema: str
) -> list[Finding]:
    """One finding per prohibited *name* among ``relations``.

    Every relation kind is checked, not only ordinary tables: a view over
    attendance data is exactly the artefact the S1 policy forbids, and skipping
    it would turn a prohibited object into a silent pass. The rules themselves
    come only from :func:`src.schema_contract.prohibition_for`.
    """
    findings: list[Finding] = []
    for name in sorted({relation.name for relation in relations}):
        rule = prohibition_for(name)
        if rule is None:
            continue
        findings.append(
            Finding(
                code=SCHEMA_OBJECT_PROHIBITED,
                schema=schema,
                object=name,
                category=rule.category,
                detail=f"relation name is prohibited ({rule.category}): {rule.rationale}",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Per-table rules
# ---------------------------------------------------------------------------


def _table_findings(
    table: TableContract,
    schema: str,
    relations: tuple[RelationFact, ...],
    columns: tuple[ColumnFact, ...],
    constraints: tuple[ConstraintFact, ...],
) -> list[Finding]:
    """Every finding for one contracted table, in rule order."""
    findings: list[Finding] = []

    named = tuple(relation for relation in relations if relation.name == table.name)
    if len(named) > 1:
        # Two relations cannot share a name in one PostgreSQL schema, so this
        # is contradictory metadata rather than a physical fact; nothing about
        # the table can be decided from it.
        return [
            _metadata_incomplete(
                schema,
                table.name,
                "the catalog reports more than one relation with this name",
            )
        ]
    if not named:
        return [
            Finding(
                code=SCHEMA_TABLE_MISSING,
                schema=schema,
                object=table.name,
                detail="required table is absent from the probed schema",
            )
        ]

    relation = named[0]
    if relation.kind == _PARTITIONED_TABLE_KIND:
        findings.append(
            Finding(
                code=SCHEMA_TABLE_KIND_UNCONFIRMED,
                schema=schema,
                object=table.name,
                detail=(
                    "relation is a partitioned table; the contract does not "
                    "state whether a partitioned table satisfies it"
                ),
            )
        )
    elif relation.kind != _ORDINARY_TABLE_KIND:
        label = _KIND_LABELS.get(relation.kind, "a different relation kind")
        return [
            Finding(
                code=SCHEMA_TABLE_KIND_MISMATCH,
                schema=schema,
                object=table.name,
                detail=f"required table exists as {label}, not an ordinary table",
            )
        ]

    findings.extend(_column_findings(table, schema, columns))
    findings.extend(_primary_key_findings(table, schema, constraints))
    return findings


def _column_findings(
    table: TableContract, schema: str, columns: tuple[ColumnFact, ...]
) -> list[Finding]:
    """Column presence, type, nullability, default and generation findings."""
    facts = tuple(column for column in columns if column.table == table.name)
    if not facts:
        # A real table always has columns, so an empty set means the snapshot
        # is incomplete: treating it as "every column is missing" would
        # misdescribe the database and treating it as a match would be a false
        # pass.
        return [
            _metadata_incomplete(
                schema,
                table.name,
                "the catalog reports no column facts for this table",
            )
        ]
    if len({fact.name for fact in facts}) != len(facts):
        return [
            _metadata_incomplete(
                schema,
                table.name,
                "the catalog reports more than one column with the same name",
            )
        ]

    by_name = {fact.name: fact for fact in facts}
    findings: list[Finding] = []

    for contract_column in table.columns:
        fact = by_name.get(contract_column.name)
        if fact is None:
            findings.append(
                Finding(
                    code=SCHEMA_COLUMN_MISSING,
                    schema=schema,
                    object=table.name,
                    column=contract_column.name,
                    detail="required column is absent",
                )
            )
            continue
        findings.extend(
            _column_comparison(contract_column, fact, schema, table.name)
        )

    contracted = {column.name for column in table.columns}
    for fact in sorted(facts, key=lambda column: column.name):
        if fact.name not in contracted:
            findings.append(
                Finding(
                    code=SCHEMA_COLUMN_UNEXPECTED,
                    schema=schema,
                    object=table.name,
                    column=fact.name,
                    detail="column is not part of the contract",
                )
            )
    return findings


def _column_comparison(
    contract_column: ColumnContract,
    fact: ColumnFact,
    schema: str,
    table: str,
) -> list[Finding]:
    """Every independently checkable fact about one present column."""
    findings: list[Finding] = []

    if _normalize_type(fact.pg_type) != _normalize_type(contract_column.pg_type):
        findings.append(
            Finding(
                code=SCHEMA_COLUMN_TYPE_MISMATCH,
                schema=schema,
                object=table,
                column=contract_column.name,
                detail=(
                    f"column type is {fact.pg_type!r}; the contract requires "
                    f"{contract_column.pg_type!r}"
                ),
            )
        )

    if fact.nullable != contract_column.nullable:
        findings.append(
            Finding(
                code=SCHEMA_COLUMN_NULLABILITY_MISMATCH,
                schema=schema,
                object=table,
                column=contract_column.name,
                detail=(
                    "column is nullable; the contract requires NOT NULL"
                    if contract_column.nullable is False
                    else "column is NOT NULL; the contract requires a nullable column"
                ),
            )
        )

    # Generation and identity are reported before the default, and *instead*
    # of it: both put an expression in pg_attrdef, and calling that "a default"
    # would misdescribe a column INSERT cannot write.
    if fact.is_generated:
        findings.append(
            Finding(
                code=SCHEMA_COLUMN_GENERATION_MISMATCH,
                schema=schema,
                object=table,
                column=contract_column.name,
                detail=(
                    "column is a stored generated column; the contract requires "
                    "an ordinary column"
                ),
            )
        )
        return findings
    if fact.is_identity:
        findings.append(
            Finding(
                code=SCHEMA_COLUMN_IDENTITY_MISMATCH,
                schema=schema,
                object=table,
                column=contract_column.name,
                detail=(
                    "column is an identity column; the contract requires an "
                    "ordinary column"
                ),
            )
        )
        return findings

    expected_default = contract_column.default.expected_normalized_default
    if expected_default is None:
        if fact.has_default:
            findings.append(
                Finding(
                    code=SCHEMA_COLUMN_DEFAULT_MISMATCH,
                    schema=schema,
                    object=table,
                    column=contract_column.name,
                    detail="column has a default; the contract requires none",
                )
            )
    elif not fact.has_default:
        findings.append(
            Finding(
                code=SCHEMA_COLUMN_DEFAULT_MISMATCH,
                schema=schema,
                object=table,
                column=contract_column.name,
                detail=(
                    "column has no default; the contract requires the boolean "
                    "literal true"
                ),
            )
        )
    elif _normalize_default(fact.default_expression) != expected_default:
        findings.append(
            Finding(
                code=SCHEMA_COLUMN_DEFAULT_MISMATCH,
                schema=schema,
                object=table,
                column=contract_column.name,
                detail=(
                    "column default is not the boolean literal true"
                    if expected_default == _TRUE_DEFAULT
                    else "column default differs from the contract"
                ),
            )
        )
    return findings


def _primary_key_findings(
    table: TableContract,
    schema: str,
    constraints: tuple[ConstraintFact, ...],
) -> list[Finding]:
    """Primary-key presence, uniqueness and ordered column findings."""
    keys = tuple(
        constraint
        for constraint in constraints
        if constraint.table == table.name and constraint.is_primary_key
    )
    if not keys:
        return [
            Finding(
                code=SCHEMA_PRIMARY_KEY_MISSING,
                schema=schema,
                object=table.name,
                detail="table has no primary-key constraint",
            )
        ]
    if len(keys) > 1:
        return [
            _metadata_incomplete(
                schema,
                table.name,
                "the catalog reports more than one primary-key constraint",
            )
        ]

    observed = keys[0].columns
    if not observed:
        return [
            _metadata_incomplete(
                schema,
                table.name,
                "the primary-key constraint reports no columns",
            )
        ]
    if observed != table.primary_key:
        return [
            Finding(
                code=SCHEMA_PRIMARY_KEY_MISMATCH,
                schema=schema,
                object=table.name,
                detail=(
                    f"primary key is ({', '.join(observed)}); the contract "
                    f"requires ({', '.join(table.primary_key)})"
                ),
            )
        ]
    return []


def _metadata_incomplete(schema: str, table: str, detail: str) -> Finding:
    """A finding that says the facts, not the database, are insufficient."""
    return Finding(
        code=SCHEMA_METADATA_INCOMPLETE,
        schema=schema,
        object=table,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Verdict and ordering
# ---------------------------------------------------------------------------


def _verdict(findings: tuple[Finding, ...]) -> CompatibilityStatus:
    """``COMPATIBLE`` only when nothing was found; definite findings win."""
    if not findings:
        return CompatibilityStatus.COMPATIBLE
    if any(finding.code not in _INDETERMINATE_CODES for finding in findings):
        return CompatibilityStatus.INCOMPATIBLE
    return CompatibilityStatus.INDETERMINATE


def _order(findings: list[Finding]) -> tuple[Finding, ...]:
    """Total, input-order-independent ordering of findings."""
    return tuple(sorted(findings, key=_sort_key))


def _sort_key(finding: Finding) -> tuple[int, int, str, str, str, str, str]:
    """Precedence rank, category rank, then identifiers, code and detail."""
    return (
        _RANK[finding.code],
        0 if finding.category == "test_marker" else 1,
        finding.schema,
        finding.object,
        finding.column or "",
        finding.code,
        finding.detail,
    )


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize_type(pg_type: str) -> str:
    """Case- and whitespace-insensitive type spelling (never an alias)."""
    return " ".join(pg_type.split()).casefold()


def _normalize_default(expression: str | None) -> str:
    """Case- and whitespace-insensitive default text (``None`` → empty)."""
    return "" if expression is None else " ".join(expression.split()).casefold()


# ---------------------------------------------------------------------------
# Call validation (programming errors, never database verdicts)
# ---------------------------------------------------------------------------


def _validate_call(
    snapshot: object,
    contract: object,
    schema: object,
) -> None:
    """Reject calls that cannot produce a meaningful verdict.

    Anything wrong with the *snapshot's own consistency* (duplicate names,
    missing column facts) is data and becomes an
    ``SCHEMA_METADATA_INCOMPLETE`` finding; only wrong *types and structures*
    belong here.
    """
    _validate_snapshot(snapshot)
    if not isinstance(schema, str) or not schema.strip():
        raise PhysicalSchemaInputError("schema must be a non-empty string")
    _validate_contract(contract)


def _validate_snapshot(snapshot: object) -> None:
    if not isinstance(snapshot, CatalogSnapshot):
        raise PhysicalSchemaInputError("snapshot must be a CatalogSnapshot")
    for fact in snapshot.relations:
        if not isinstance(fact, RelationFact):
            raise PhysicalSchemaInputError(
                "snapshot.relations must contain RelationFact values"
            )
    for column in snapshot.columns:
        if not isinstance(column, ColumnFact):
            raise PhysicalSchemaInputError(
                "snapshot.columns must contain ColumnFact values"
            )
    for constraint in snapshot.constraints:
        if not isinstance(constraint, ConstraintFact):
            raise PhysicalSchemaInputError(
                "snapshot.constraints must contain ConstraintFact values"
            )
    for index in snapshot.indexes:
        if not isinstance(index, IndexFact):
            raise PhysicalSchemaInputError(
                "snapshot.indexes must contain IndexFact values"
            )


def _validate_contract(contract: object) -> None:
    """Require a non-empty, unambiguous contract S3 can actually enforce."""
    if isinstance(contract, (str, bytes)) or not isinstance(contract, (tuple, list)):
        raise PhysicalSchemaInputError("contract must be a sequence of TableContract")
    if not contract:
        raise PhysicalSchemaInputError("contract must declare at least one table")

    names: set[str] = set()
    for table in contract:
        if not isinstance(table, TableContract):
            raise PhysicalSchemaInputError("contract must contain TableContract values")
        if not table.name:
            raise PhysicalSchemaInputError("a contract table must have a name")
        if table.name in names:
            raise PhysicalSchemaInputError(
                f"contract declares {table.name!r} more than once"
            )
        names.add(table.name)
        if table.secondary_indexes:
            # Deliberate: S2 cannot distinguish INCLUDE columns from key
            # columns, so an index requirement could not be enforced exactly.
            raise PhysicalSchemaInputError(
                "S3 does not evaluate secondary indexes; the contract must "
                "declare none"
            )
        if not table.columns:
            raise PhysicalSchemaInputError(
                f"contract table {table.name!r} must declare at least one column"
            )
        if not table.primary_key:
            raise PhysicalSchemaInputError(
                f"contract table {table.name!r} must declare a primary key"
            )
        column_names: set[str] = set()
        for column in table.columns:
            if not isinstance(column, ColumnContract):
                raise PhysicalSchemaInputError(
                    "a contract table must contain ColumnContract values"
                )
            if not column.name or not column.pg_type:
                raise PhysicalSchemaInputError(
                    "a contract column must have a name and a type"
                )
            if not isinstance(column.default, ColumnDefault):
                raise PhysicalSchemaInputError(
                    "a contract column must declare a ColumnDefault"
                )
            if column.name in column_names:
                raise PhysicalSchemaInputError(
                    f"contract table {table.name!r} declares column "
                    f"{column.name!r} more than once"
                )
            column_names.add(column.name)
        missing = [name for name in table.primary_key if name not in column_names]
        if missing:
            raise PhysicalSchemaInputError(
                f"contract table {table.name!r} has a primary key over "
                f"undeclared columns: {', '.join(missing)}"
            )
