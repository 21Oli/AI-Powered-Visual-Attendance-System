"""G8 — Step 39, S1: the declarative physical schema contract.

G2 (:mod:`src.startup_schema`) proves that the database's *migration history*
matches the shipped migration files, and inspects no physical object of its
own. G8 closes that gap and is enforced at startup alongside G2: a database
whose ledger says ``V001`` while ``employee`` has the wrong types, a missing
primary key, or a ``created_at`` default is refused at startup rather than
served.

**S1 — this module — is the contract only**: an explicit, immutable,
declarative description of the physical schema the application requires. S1
does not inspect a database, run a query, open a connection, or read a file at
run time; the catalog probe (S2), the pure evaluation and its diagnostic codes
(S3), the executor hook (S4) and the startup integration (S5) are separately
reviewed stages that have since been implemented.

Why an explicit contract instead of deriving expectations from the
migration SQL
--------------
Deriving the expected schema by parsing ``migrations/*.sql`` would need a
real SQL parser, would silently accept DDL shapes the parser does not
understand, and would turn a review artefact into derived state. The
contract is therefore written by hand — but it is **pinned to the shipped
migration set** (see :data:`MIGRATION_SET_DIGEST` and
:data:`CONTRACT_MIGRATION_VERSION`), so it cannot silently drift from the
migrations: an added, removed, renamed, reordered or edited migration fails
``tests/test_schema_contract.py`` offline until a human updates this module
on purpose.

What this module may and may not do
-----------------------------------
* It defines frozen dataclasses and constants only: no filesystem access, no
  SQL, no PostgreSQL driver, no ``src.db``/``src.db_migrate`` import, no
  environment read, no logging. Importing it is side-effect free (enforced by
  tests that parse this file and by a fresh-subprocess import check).
* It holds **no** attendance, embedding, biometric, enrollment or employee
  *data* — only schema metadata (names, PostgreSQL type spellings,
  nullability, default semantics) already published in ``DB_DESIGN.md`` §2
  and ``migrations/V001__employee.sql``.
* The digest helper (:func:`migration_set_digest`) is pure arithmetic over
  caller-supplied ``(version, checksum)`` pairs; it never discovers files
  itself, and the expected digest is a **pinned literal**, never computed at
  import or test time.

Type spellings
--------------
``pg_type`` strings are the canonical spellings PostgreSQL's
``format_type(atttypid, atttypmod)`` produces for the declared columns, not
``information_schema.data_type`` values: ``text``, ``boolean`` and
``timestamp with time zone``. Using the catalog spelling keeps aliases
(``int4``, ``timestamptz``, ``varchar``) from producing false mismatches once
S3 compares live facts.

Column order
------------
The ``columns`` tuples follow the order in ``migrations/V001__employee.sql``
for reviewability only. **Ordinal position is not part of the verified
contract**: the repository always names its columns (``SELECT employee_id,
full_name, is_active, created_at``), so a physically reordered table is
compatible and S3 must not refuse it.

Updating the contract when a migration is added
-----------------------------------------------
1. Add the migration file (``migrations/V<N>__<name>.sql``); do not edit an
   already-applied migration.
2. Update :data:`REQUIRED_TABLES` so it describes the resulting schema.
3. Recompute the pinned digest from the shipped set::

       ./.venv/Scripts/python.exe -c "from src.db_migrate import load_migrations; \
       from src.schema_contract import migration_set_digest; \
       print(migration_set_digest((s.version, s.checksum) for s in load_migrations()))"

4. Set :data:`CONTRACT_MIGRATION_VERSION` to the new highest version and paste
   the digest printed above into :data:`MIGRATION_SET_DIGEST`.
5. Run ``./.venv/Scripts/python.exe -m pytest tests/test_schema_contract.py -q``.

Both the version and the digest must be updated together; a contract update
without a matching digest (or vice versa) fails offline.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final

__all__ = [
    "CONTRACT_MIGRATION_VERSION",
    "EMPLOYEE_TABLE",
    "LEDGER_TABLE",
    "MIGRATION_SET_DIGEST",
    "PROHIBITED_TABLE_NAMES",
    "PROHIBITED_TABLE_PATTERNS",
    "REQUIRED_TABLES",
    "REQUIRED_TABLES_BY_NAME",
    "TARGET_SCHEMA",
    "ColumnContract",
    "ColumnDefault",
    "ProhibitedNamePattern",
    "TableContract",
    "is_prohibited_table_name",
    "migration_set_digest",
    "prohibition_for",
]

# ---------------------------------------------------------------------------
# Schema location
# ---------------------------------------------------------------------------

#: The one schema these contracts describe. Every expectation is matched
#: against ``public`` only; whatever a shared database holds in other schemas
#: is none of G8's business (approved owner decision: unrelated objects are
#: allowed).
TARGET_SCHEMA: Final[str] = "public"


# ---------------------------------------------------------------------------
# Column- and table-level expectations
# ---------------------------------------------------------------------------


class ColumnDefault(Enum):
    """Required *default* semantics for one column.

    Only the two shapes the application actually depends on are modelled:
    no default at all, or a default that must evaluate to boolean true.
    Values are the normalized ``pg_get_expr(adbin, adrelid)`` texts S3 will
    compare against, so the enum carries the expectation rather than a driver
    detail. ``ColumnDefault.TRUE`` deliberately accepts only the normalized
    ``true`` form: PostgreSQL renders every spelling of a boolean-true
    default (``TRUE``, ``'t'::boolean``) as ``true``.
    """

    NONE = "none"
    TRUE = "true"

    @property
    def requires_default(self) -> bool:
        """Whether the column must carry a default at all."""
        return self is not ColumnDefault.NONE

    @property
    def expected_normalized_default(self) -> str | None:
        """The normalized default text required, or ``None`` for no default."""
        return None if self is ColumnDefault.NONE else self.value


@dataclass(frozen=True)
class ColumnContract:
    """One required column: name, canonical type, nullability, default."""

    name: str
    #: Canonical ``format_type(...)`` spelling (see the module docstring).
    pg_type: str
    nullable: bool
    default: ColumnDefault


@dataclass(frozen=True)
class TableContract:
    """One required table in :data:`TARGET_SCHEMA`.

    ``secondary_indexes`` lists ordered column tuples for indexes the
    application genuinely needs. It is empty for both application tables on
    purpose: the only index required is the primary-key index (the repository's
    keyset listing is ``WHERE employee_id > %s ORDER BY employee_id``, which
    the ``employee`` primary-key B-tree already serves), and an index asserted
    without repository evidence would turn a harmless DBA addition or removal
    into a false refusal.
    """

    name: str
    columns: tuple[ColumnContract, ...]
    primary_key: tuple[str, ...]
    secondary_indexes: tuple[tuple[str, ...], ...] = ()

    def column(self, name: str) -> ColumnContract:
        """Return the contract for column ``name`` (``KeyError`` if absent)."""
        for column in self.columns:
            if column.name == name:
                return column
        raise KeyError(name)

    @property
    def column_names(self) -> tuple[str, ...]:
        """The required column names, in contract (migration) order."""
        return tuple(column.name for column in self.columns)


#: ``public.employee`` — approved employee-metadata contract (DB_DESIGN §2,
#: ``migrations/V001__employee.sql``, ``src/employee_repository.py``).
#:
#: ``created_at`` must have **no** default (approved D2): the value is
#: application-authored and inserted verbatim, so a database default would
#: silently substitute a timestamp the application did not author.
#: ``is_active`` must default to true as the documented defensive DB-level
#: match to the model default.
EMPLOYEE_TABLE: Final[TableContract] = TableContract(
    name="employee",
    columns=(
        ColumnContract(
            name="employee_id",
            pg_type="text",
            nullable=False,
            default=ColumnDefault.NONE,
        ),
        ColumnContract(
            name="full_name",
            pg_type="text",
            nullable=False,
            default=ColumnDefault.NONE,
        ),
        ColumnContract(
            name="is_active",
            pg_type="boolean",
            nullable=False,
            default=ColumnDefault.TRUE,
        ),
        ColumnContract(
            name="created_at",
            pg_type="timestamp with time zone",
            nullable=False,
            default=ColumnDefault.NONE,
        ),
    ),
    primary_key=("employee_id",),
)

#: ``public.schema_version`` — the migration ledger's documented shape
#: (DB_DESIGN §3.1, ``_EXPECTED_LEDGER_SHAPE`` in
#: ``src/db_migrate_executor.py``). G2 already validates this shape; G8
#: re-states it so the physical contract is complete and version-independent,
#: and so a wrong ledger type is reported as structural rather than merely as
#: bad history.
LEDGER_TABLE: Final[TableContract] = TableContract(
    name="schema_version",
    columns=(
        ColumnContract(
            name="version",
            pg_type="integer",
            nullable=False,
            default=ColumnDefault.NONE,
        ),
        ColumnContract(
            name="checksum",
            pg_type="text",
            nullable=False,
            default=ColumnDefault.NONE,
        ),
        ColumnContract(
            name="applied_at",
            pg_type="timestamp with time zone",
            nullable=False,
            default=ColumnDefault.NONE,
        ),
    ),
    primary_key=("version",),
)

#: Every table the application itself requires, in review order.
REQUIRED_TABLES: Final[tuple[TableContract, ...]] = (EMPLOYEE_TABLE, LEDGER_TABLE)

#: Lookup by unqualified table name. ``MappingProxyType`` keeps the mapping
#: immutable without a custom class.
REQUIRED_TABLES_BY_NAME: Final[Mapping[str, TableContract]] = MappingProxyType(
    {table.name: table for table in REQUIRED_TABLES}
)


# ---------------------------------------------------------------------------
# Prohibited objects (approved D1 enforcement + the test-only marker)
# ---------------------------------------------------------------------------

#: Exact, fully documented table names that must never exist in the
#: application target. This is a literal set, not a pattern: the Tier-2
#: harness's marker table belongs only in a positively verified disposable
#: test database (``src/test_database_guard.py``), so finding it in the
#: application database means the configured target is the wrong database.
PROHIBITED_TABLE_NAMES: Final[tuple[str, ...]] = ("_phase3_test_guard",)

#: Token boundaries: a pattern matches a whole underscore/dash-delimited
#: *token* of the table name, never an arbitrary substring. Without this,
#: ``surface_metrics`` would look like ``face`` and ``attendance_scores``
#: would be indistinguishable from an unrelated ``scores`` table. Table names
#: are the only thing matched; no column, schema, or SQL text is inspected.
_TOKEN_START: Final[str] = r"(?:^|_)"
_TOKEN_END: Final[str] = r"(?:_|$)"


@dataclass(frozen=True)
class ProhibitedNamePattern:
    """One documented prohibited-name rule (category, regex, rationale)."""

    category: str
    regex: str
    rationale: str

    def matches(self, table_name: str) -> bool:
        """Whether ``table_name`` contains this rule's token.

        Matching is case-insensitive and anchored to token boundaries via the
        pattern text, so ``ATTENDANCE_EVENT`` and ``attendance_event`` are the
        same rule hit. ``re`` caches compiled patterns, so matching per name
        costs no extra compilation in practice.
        """
        return re.search(self.regex, table_name, re.IGNORECASE) is not None


#: The approved prohibited-object policy (owner decision D1 + D-11):
#: PostgreSQL stores employee metadata only. Attendance lives in the JSONL
#: journal, embeddings live in local files, and biometric/enrollment artefacts
#: are never rows. Each category is a separate rule so a refusal can name what
#: was found without echoing a value.
PROHIBITED_TABLE_PATTERNS: Final[tuple[ProhibitedNamePattern, ...]] = (
    ProhibitedNamePattern(
        category="attendance",
        regex=f"{_TOKEN_START}attendance{_TOKEN_END}",
        rationale=(
            "attendance events belong to the authoritative JSONL journal "
            "(DB_DESIGN D1); no PostgreSQL attendance table may exist"
        ),
    ),
    ProhibitedNamePattern(
        category="embedding",
        regex=f"{_TOKEN_START}embeddings?{_TOKEN_END}",
        rationale=(
            "face embeddings are local-file artefacts and never database rows"
        ),
    ),
    ProhibitedNamePattern(
        category="biometric",
        regex=f"{_TOKEN_START}biometrics?{_TOKEN_END}",
        rationale="biometric samples and templates must never enter PostgreSQL",
    ),
    ProhibitedNamePattern(
        category="face",
        regex=f"{_TOKEN_START}(?:face|facial)s?{_TOKEN_END}",
        rationale=(
            "face images, detections and recognition artefacts stay outside "
            "PostgreSQL (this also catches biometric templates named after a face)"
        ),
    ),
    ProhibitedNamePattern(
        category="enrollment",
        regex=f"{_TOKEN_START}enrollments?{_TOKEN_END}",
        rationale=(
            "enrollment artefacts live on local disk; an enrollment table "
            "would imply biometric data in the database"
        ),
    ),
)


def prohibition_for(table_name: str) -> ProhibitedNamePattern | None:
    """Return the rule that prohibits ``table_name``, or ``None``.

    ``table_name`` is an **unqualified** relation name as PostgreSQL's
    ``pg_class.relname`` reports it (``attendance_event``, not
    ``public.attendance_event``). The exact test-marker name is checked first
    and reported under its own ``test_marker`` category, because its presence
    means the target is a test database rather than that the schema drifted.
    """
    if table_name in PROHIBITED_TABLE_NAMES:
        return ProhibitedNamePattern(
            category="test_marker",
            regex=re.escape(table_name),
            rationale=(
                "the Tier-2 guard marker belongs only in a positively verified "
                "disposable test database; finding it here means the configured "
                "target is the wrong database"
            ),
        )
    for rule in PROHIBITED_TABLE_PATTERNS:
        if rule.matches(table_name):
            return rule
    return None


def is_prohibited_table_name(table_name: str) -> bool:
    """Whether the prohibited-object policy covers ``table_name``."""
    return prohibition_for(table_name) is not None


# ---------------------------------------------------------------------------
# Pinning the contract to the shipped migration set
# ---------------------------------------------------------------------------

#: Highest shipped migration version this contract describes. Must equal the
#: highest ``V<N>`` in ``migrations/`` — a new migration without a contract
#: update fails offline.
CONTRACT_MIGRATION_VERSION: Final[int] = 1

#: SHA-256 of the canonical migration-set listing, pinned as a literal.
#:
#: Algorithm (deliberately tiny and independent of file discovery): take each
#: shipped script's ``(version, checksum)`` exactly as
#: :func:`src.db_migrate.load_migrations` derives them — the checksum is that
#: module's canonical-text SHA-256, so line-ending conversion cannot fake a
#: change and no second checksum implementation exists — sort the pairs by
#: version, render one line per script as ``V<version>:<checksum>``, join the
#: lines with ``"\n"`` (no trailing newline), and SHA-256 the UTF-8 bytes.
#:
#: For the current single-script set that payload is one line::
#:
#:     V1:9e6ac6b5d1e0fd016a99c007ec5b91070dd480853d54d37cdab5b069dd2deac9
#:
#: Because entries are sorted by version, discovery order cannot affect the
#: digest; because each entry carries both version and content checksum,
#: swapping content between versions (a rename/reorder) does change it.
MIGRATION_SET_DIGEST: Final[str] = (
    "dcf2ed2ca62bc3a8a8cb741adf7446cfc84a43c83d329b5dc5e1c1059748f63c"
)


def migration_set_digest(entries: Iterable[tuple[int, str]]) -> str:
    """Digest of a shipped migration set given its ``(version, checksum)`` pairs.

    Pure and side-effect free: it discovers nothing, reads no file, and
    imports no migration machinery. Callers (tests, and later the G8 gate)
    supply the pairs derived by :func:`src.db_migrate.load_migrations`, whose
    checksums are already canonicalized and line-ending insensitive.

    The empty set has a well-defined digest (SHA-256 of the empty string), but
    G2 refuses an empty shipped set with ``MIGRATION_SET_EMPTY`` before G8 is
    ever reached, so an empty set can never be treated as compatible here.
    """
    ordered = sorted((int(version), str(checksum)) for version, checksum in entries)
    payload = "\n".join(
        f"V{version}:{checksum}" for version, checksum in ordered
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
