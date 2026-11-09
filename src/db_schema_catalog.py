"""G8 — Step 39, S2: the read-only PostgreSQL catalog probe.

S1 (:mod:`src.schema_contract`) declares what the application *expects*. This
module reads what PostgreSQL *actually has*, as immutable facts. It is the
only place in the G8 work that contains SQL, and it makes **no** judgement:
deciding whether a fact is compatible belongs to S3, and turning a refusal into
a startup diagnostic belongs to the G2 gate. Importing this module connects to
nothing and reads nothing.

Scope and contract with the caller
---------------------------------
* :func:`read_public_catalog` takes an **existing** connection and issues four
  fixed ``SELECT`` statements. It never opens, closes, commits, rolls back, or
  locks anything: the caller (G8 inside G2's verified read-only transaction)
  owns the connection, the transaction and the advisory lock.
* The caller's transaction is already ``REPEATABLE READ READ ONLY``, so the
  four statements observe one consistent snapshot. This module must therefore
  never issue ``BEGIN``/``COMMIT``/``ROLLBACK``, which would silently end that
  snapshot and let the facts come from different points in time.
* No policy lives here: no schema names are compared to the contract, no
  prohibited-name pattern is applied, no diagnostic code is defined. The
  probe returns every relation in the probed schema (of every relevant kind)
  so S3 can apply the S1 rules itself.

Why ``pg_catalog`` and never ``information_schema``
--------------------------------------------------
``information_schema`` is **privilege-filtered**: it reports only the columns
the current role holds a privilege on. A least-privilege runtime role (the
project grants ``SELECT, INSERT, UPDATE(full_name), DELETE`` on ``employee``,
and less elsewhere) would then see *fewer columns than exist*, which S3 would
read as schema drift and refuse startup on a perfectly correct database. The
``pg_catalog`` relations used here are world-readable, so the facts describe
the database rather than the role's privileges.

Determinism
-----------
Every statement carries an ``ORDER BY`` **and** each mapping is re-sorted in
Python on the same key, so the result never depends on planner or driver row
order. Callers get tuples of frozen dataclasses, never lists or dicts.

PostgreSQL version note
-----------------------
The column statement reads ``attgenerated`` (PostgreSQL 12+) and
``attidentity`` (10+). Generated columns do not exist before 12, and referencing
the attribute directly means an older server fails loudly (as
``CatalogProbeError``) instead of silently hiding a generated column — a
generated column must never be mistaken for "has a default", because
``INSERT`` cannot write it. The repository pins no server version, so the
12+ prerequisite is recorded as something the separately authorized live
verification (S8) must confirm rather than something assumed here.

Deliberately excluded catalog classes
-------------------------------------
Triggers, rules, policies, functions, types/domains, extensions, schemas,
privileges/ACL, storage parameters and TOAST relations are not read. The
approved G8 policy is about relation names (prohibition) and about columns,
constraints and indexes (compatibility); reading anything else would be
unapproved scope, and an omitted class cannot cause a *false pass* of the
contract, because no contract requirement is expressed in terms of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "PROBED_SCHEMA",
    "CatalogProbeError",
    "CatalogSnapshot",
    "ColumnFact",
    "ConstraintFact",
    "IndexFact",
    "RelationFact",
    "read_public_catalog",
]

#: The one schema this probe reads. Literal, not configurable: G8 verifies the
#: application's schema, and keeping the value out of the SQL text (each
#: statement writes ``'public'``) means no identifier is ever interpolated.
#: ``tests/test_db_schema_catalog.py`` asserts it equals the contract's
#: ``TARGET_SCHEMA`` so the two cannot drift.
PROBED_SCHEMA: Final[str] = "public"

#: Fixed, sanitized message. S2 defines no diagnostic code on purpose: S3 owns
#: the physical codes and the G2 gate decides how this failure is reported.
_CATALOG_UNREADABLE: Final[str] = (
    "the database catalog could not be read for physical schema verification"
)


class CatalogProbeError(RuntimeError):
    """The catalog could not be read **completely**.

    Raised for any failure while reading a statement, so a partial read can
    never be mistaken for a smaller-but-valid schema: there is no code path
    that returns a snapshot after a failure. The message is fixed and contains
    no identifier, value, DSN, host or driver text; the original exception is
    preserved as ``__cause__`` for trusted, server-side diagnosis only.
    """


# ---------------------------------------------------------------------------
# Immutable fact records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelationFact:
    """One relation in :data:`PROBED_SCHEMA` (``pg_class.relkind``)."""

    schema: str
    name: str
    #: Raw ``relkind``: ``r`` table, ``p`` partitioned table, ``v`` view,
    #: ``m`` materialized view, ``S`` sequence, ``f`` foreign table,
    #: ``c`` composite type, ``i``/``I`` (partitioned) index.
    kind: str

    @property
    def is_table(self) -> bool:
        """Whether this relation is an ordinary or partitioned table."""
        return self.kind in ("r", "p")


@dataclass(frozen=True)
class ColumnFact:
    """One column of an ordinary or partitioned table."""

    schema: str
    table: str
    name: str
    #: Canonical ``format_type(atttypid, atttypmod)`` spelling, e.g. ``text``,
    #: ``boolean``, ``timestamp with time zone``.
    pg_type: str
    #: ``attnotnull``.
    nullable: bool
    #: ``pg_get_expr(adbin, adrelid)``, or ``None`` when the column has **no**
    #: default. ``None`` and an expression are always distinguishable, so a
    #: missing default can never be confused with an empty one.
    default_expression: str | None
    #: ``attgenerated``: ``''`` for an ordinary column, ``'s'`` stored.
    #: A generated column has an expression in ``pg_attrdef`` yet accepts no
    #: inserted value, so it must never be treated as "has a default".
    generated: str
    #: ``attidentity``: ``''``, ``'a'`` (always) or ``'d'`` (by default).
    identity: str
    #: ``attnum`` (1-based, gaps from dropped columns preserved).
    ordinal: int

    @property
    def has_default(self) -> bool:
        """Whether a default expression exists (independent of its text)."""
        return self.default_expression is not None

    @property
    def is_generated(self) -> bool:
        """Whether the column is a stored generated column."""
        return self.generated != ""

    @property
    def is_identity(self) -> bool:
        """Whether the column is an identity column."""
        return self.identity != ""


@dataclass(frozen=True)
class ConstraintFact:
    """One table constraint (``pg_constraint``)."""

    schema: str
    table: str
    name: str
    #: ``contype``: ``p`` primary key, ``u`` unique, ``c`` check,
    #: ``f`` foreign key, ``x`` exclusion.
    contype: str
    #: ``pg_get_constraintdef(oid)`` — the server-rendered definition.
    definition: str
    #: Referenced columns **in the constraint's own order** (empty for
    #: constraints that reference no columns, e.g. ``CHECK``).
    columns: tuple[str, ...]

    @property
    def is_primary_key(self) -> bool:
        """Whether this constraint is the table's primary key."""
        return self.contype == "p"


@dataclass(frozen=True)
class IndexFact:
    """One index on an ordinary or partitioned table (``pg_index``)."""

    schema: str
    table: str
    name: str
    is_unique: bool
    is_primary: bool
    #: Indexed columns in index order. Expression positions (``indkey`` 0) and
    #: ``INCLUDE`` columns beyond the key are not distinguishable here — the
    #: definition carries the full truth, and S3 needs column names only for
    #: contracted secondary indexes (none today).
    columns: tuple[str, ...]
    #: ``pg_get_indexdef(indexrelid)``.
    definition: str


@dataclass(frozen=True)
class CatalogSnapshot:
    """Everything the probe read, deterministically ordered and immutable."""

    relations: tuple[RelationFact, ...]
    columns: tuple[ColumnFact, ...]
    constraints: tuple[ConstraintFact, ...]
    indexes: tuple[IndexFact, ...]

    def relations_named(self, name: str) -> tuple[RelationFact, ...]:
        """Every relation called ``name`` in the probed schema."""
        return tuple(relation for relation in self.relations if relation.name == name)

    def columns_for(self, table: str) -> tuple[ColumnFact, ...]:
        """Columns of ``table``, in physical (``attnum``) order."""
        return tuple(column for column in self.columns if column.table == table)

    def constraints_for(self, table: str) -> tuple[ConstraintFact, ...]:
        """Constraints of ``table``, ordered by constraint name."""
        return tuple(
            constraint
            for constraint in self.constraints
            if constraint.table == table
        )

    def primary_key_for(self, table: str) -> ConstraintFact | None:
        """The primary-key constraint of ``table``, or ``None`` when absent."""
        for constraint in self.constraints_for(table):
            if constraint.is_primary_key:
                return constraint
        return None

    def indexes_for(self, table: str) -> tuple[IndexFact, ...]:
        """Indexes of ``table``, ordered by index name."""
        return tuple(index for index in self.indexes if index.table == table)


# ---------------------------------------------------------------------------
# Fixed statements (reviewed text; no identifiers or values interpolated)
# ---------------------------------------------------------------------------

#: Every relation kind a prohibited object could hide in. ``t`` (TOAST) is
#: excluded: those relations are server-managed storage, never authored
#: objects, and PostgreSQL does not expose them in ``public`` normally.
#: Index relations are included so S3 can tell an index from a table by kind
#: rather than by name.
_RELATIONS_SQL: Final[str] = (
    "SELECT n.nspname, c.relname, c.relkind "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' "
    "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f', 'c', 'i', 'I') "
    "ORDER BY n.nspname, c.relname, c.relkind"
)

#: Columns of real tables only. System and dropped columns are excluded, so a
#: dropped column cannot look like an expected one; ``LEFT JOIN`` on
#: ``pg_attrdef`` is what makes "no default" a NULL rather than a missing row.
_COLUMNS_SQL: Final[str] = (
    "SELECT n.nspname, c.relname, a.attname, "
    "pg_catalog.format_type(a.atttypid, a.atttypmod), "
    "a.attnotnull, "
    "pg_catalog.pg_get_expr(d.adbin, d.adrelid), "
    "a.attgenerated, a.attidentity, a.attnum "
    "FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid "
    "LEFT JOIN pg_catalog.pg_attrdef d "
    "ON d.adrelid = c.oid AND d.adnum = a.attnum "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
    "AND a.attnum > 0 AND NOT a.attisdropped "
    "ORDER BY n.nspname, c.relname, a.attnum"
)

#: Every table constraint, with its referenced columns expanded **in the
#: constraint's own order** (``conkey`` ordinality), never inferred from
#: column names.
_CONSTRAINTS_SQL: Final[str] = (
    "SELECT n.nspname, c.relname, con.conname, con.contype, "
    "pg_catalog.pg_get_constraintdef(con.oid), "
    "COALESCE((SELECT pg_catalog.array_agg(a.attname ORDER BY k.ord) "
    "FROM pg_catalog.unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) "
    "JOIN pg_catalog.pg_attribute a "
    "ON a.attrelid = con.conrelid AND a.attnum = k.attnum), "
    "ARRAY[]::text[]) "
    "FROM pg_catalog.pg_constraint con "
    "JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND con.contype IN ('p', 'u', 'c', 'f', 'x') "
    "ORDER BY n.nspname, c.relname, con.conname"
)

#: Every index on a table, with key columns in index order. ``indkey`` is an
#: ``int2vector`` (not an array), so it is subscripted positionally over
#: ``indnatts`` instead of being cast or unnested; positions holding 0 are
#: expressions and are skipped. Definitions come from the server, so no SQL
#: text is ever parsed here.
_INDEXES_SQL: Final[str] = (
    "SELECT n.nspname, c.relname, ic.relname, i.indisunique, i.indisprimary, "
    "pg_catalog.pg_get_indexdef(i.indexrelid), "
    "COALESCE((SELECT pg_catalog.array_agg(a.attname ORDER BY s.ord) "
    "FROM pg_catalog.generate_series(0, i.indnatts - 1) AS s(ord) "
    "JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid "
    "AND a.attnum = i.indkey[s.ord] "
    "WHERE i.indkey[s.ord] > 0), "
    "ARRAY[]::text[]) "
    "FROM pg_catalog.pg_index i "
    "JOIN pg_catalog.pg_class c ON c.oid = i.indrelid "
    "JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
    "ORDER BY n.nspname, c.relname, ic.relname"
)


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def read_public_catalog(connection) -> CatalogSnapshot:
    """Read :data:`PROBED_SCHEMA`'s structural facts from ``connection``.

    ``connection`` is an existing psycopg-style connection (anything exposing
    ``execute(sql)`` that returns a cursor with ``fetchall()``); this function
    calls ``execute`` four times and nothing else. It never opens, closes,
    commits, rolls back or locks: the caller owns that lifecycle, and the
    caller's read-only transaction is what makes the four reads one snapshot.

    Raises
    ------
    CatalogProbeError
        If any statement fails. No partial snapshot is ever returned, so a
        failure can never be mistaken for a smaller valid schema. The driver
        exception is attached as ``__cause__``.
    """
    try:
        relations = _read_relations(connection)
        columns = _read_columns(connection)
        constraints = _read_constraints(connection)
        indexes = _read_indexes(connection)
    except Exception as exc:
        raise CatalogProbeError(_CATALOG_UNREADABLE) from exc

    return CatalogSnapshot(
        relations=relations,
        columns=columns,
        constraints=constraints,
        indexes=indexes,
    )


def _read_relations(connection) -> tuple[RelationFact, ...]:
    facts = [
        RelationFact(schema=str(schema), name=str(name), kind=str(kind))
        for schema, name, kind in connection.execute(_RELATIONS_SQL).fetchall()
    ]
    return tuple(sorted(facts, key=lambda fact: (fact.schema, fact.name, fact.kind)))


def _read_columns(connection) -> tuple[ColumnFact, ...]:
    facts = []
    for row in connection.execute(_COLUMNS_SQL).fetchall():
        (
            schema,
            table,
            name,
            pg_type,
            not_null,
            default_expression,
            generated,
            identity,
            ordinal,
        ) = row
        facts.append(
            ColumnFact(
                schema=str(schema),
                table=str(table),
                name=str(name),
                pg_type=str(pg_type),
                nullable=not bool(not_null),
                default_expression=(
                    None if default_expression is None else str(default_expression)
                ),
                generated=str(generated),
                identity=str(identity),
                ordinal=int(ordinal),
            )
        )
    return tuple(
        sorted(
            facts,
            key=lambda fact: (fact.schema, fact.table, fact.ordinal, fact.name),
        )
    )


def _read_constraints(connection) -> tuple[ConstraintFact, ...]:
    facts = [
        ConstraintFact(
            schema=str(schema),
            table=str(table),
            name=str(name),
            contype=str(contype),
            definition=str(definition),
            columns=tuple(str(column) for column in columns),
        )
        for schema, table, name, contype, definition, columns
        in connection.execute(_CONSTRAINTS_SQL).fetchall()
    ]
    return tuple(
        sorted(facts, key=lambda fact: (fact.schema, fact.table, fact.name))
    )


def _read_indexes(connection) -> tuple[IndexFact, ...]:
    facts = [
        IndexFact(
            schema=str(schema),
            table=str(table),
            name=str(name),
            is_unique=bool(unique),
            is_primary=bool(primary),
            columns=tuple(str(column) for column in columns),
            definition=str(definition),
        )
        for schema, table, name, unique, primary, definition, columns
        in connection.execute(_INDEXES_SQL).fetchall()
    ]
    return tuple(
        sorted(facts, key=lambda fact: (fact.schema, fact.table, fact.name))
    )
