#!/usr/bin/env python
"""Provision one declared G8 S7 negative-schema scenario, offline-first.

This tool applies **exactly one** declared defect to **one** dedicated
negative database, proves the database reached the declared state, and only
then commits. It never invents, records, learns or repairs an expectation: the
expectations live in :mod:`tools.negative_schema_scenarios` and a mismatch
sends the campaign to human review instead of being accepted.

Why a dedicated database per scenario
-------------------------------------
Every case here is *deliberately broken*. Applying two defects to one database
would make each verdict unattributable, and cleaning up a broken schema in
place is the destructive operation this design avoids. Each scenario therefore
names its own database (:attr:`Scenario.database_name`); the tool never
creates, drops, provisions or cleans a database, and ``print-databases`` only
prints the names an operator has to create.

The M10 lifecycle (one defect connection)
-----------------------------------------
For a ``baseline_mode='migrate'`` scenario the order is fixed:

1. open one dedicated connection and set ``autocommit = False``;
2. apply :data:`SESSION_GUARDS` — three ``SET LOCAL`` statements — **before**
   any scenario SQL, so a slow or blocked defect cannot outlive the approval;
3. execute the scenario's statements one by one, in declaration order, never
   joined and never reordered;
4. probe the physical catalog **on that same connection**, inside that same
   transaction, so the facts describe the defect and nothing else;
5. read the G2 facts on that same connection, with S4's ledger-shape check
   enabled (this is what refuses ``G05``);
6. compare the complete captured state with the frozen declarations and with
   the declared ``(codes, status)`` / gate code;
7. ``COMMIT`` only when **everything** matched; otherwise ``ROLLBACK`` exactly
   once;
8. close the connection;
9. open a **separate** connection, read only, and re-verify the committed
   state. A commit that cannot be re-observed is never reported as ready.

``baseline_mode='none'`` (``G01``) opens no defect transaction at all: it proves
the database is empty with one read-only pass. ``baseline_mode='ensure_ledger'``
(``G04b``) lets S4's ``ensure_ledger()`` create and commit the ledger on its own
connection, then re-verifies read-only.

Failure handling never escalates to success
-------------------------------------------
An uncertain commit, a failed rollback, a failed close, an unreadable catalog
and an unreadable post-commit state are all fail-closed: none of them can be
reported as ready, and a cleanup failure never hides the original finding.

Exit codes (stable, approved contract)
--------------------------------------
===== ====================================================================
0     the declared operation executed **and** re-verified after commit
2     usage/configuration/target refusal: a static target-identity check, or
      a live identity/version/schema check before any scenario SQL
3     unexpected or unsafe state (statement/guard failure, an unauthorized
      cleanup, a failed report write, or a committed state that could not be
      verified)
4     database unavailable, or the in-transaction catalog/ledger read failed
5     complete expected state did **not** match: rolled back, nothing
      committed
6     committed, but the separate read-only verification did not match
7     commit outcome unconfirmed (connection loss or failure during commit);
      never treated as ready
===== ====================================================================

Cleanup (Milestone D)
---------------------
Cleanup is a **separate explicit command** and is never triggered by a run,
never runs after an ambiguous failure and never runs after an uncertain
commit. It is authorized only by the frozen ``cleanup_manifest``: every
relation it drops must be declared there with its exact name and relation kind,
every relation observed in the schema must be declared there (indexes are
implicit — they disappear with their table), and the observed state must
classify into the scenario's declared ``allowed_recovery_states``. Any extra
object, kind mismatch, unsupported kind or unlisted state refuses the cleanup
before a single ``DROP`` is issued. Statements are emitted in manifest order,
never ``CASCADE``, never ``IF EXISTS``, never a wildcard, and never
``DROP SCHEMA``/``DROP DATABASE``/``TRUNCATE``; PostgreSQL's default RESTRICT
therefore turns an unanticipated dependency into a refusal instead of a
cascade.

Reports (Milestone D)
---------------------
``--report-dir`` writes one JSON document (``report_schema_version``,
timestamp, declared expectation, both observations, commit outcome, recovery
classification, cleanup decision) and one human-readable ``.txt`` beside it.
Both contain stable codes and identifiers only: no DSN, credential,
environment value, driver exception text, SQL text, employee data or biometric
data can reach them. A report that cannot be written is a refusal (exit 3)
when the run itself succeeded, and never masks the run's own failure.

Target policy (Milestone D)
---------------------------
``validate_target`` proves the DSN statically (unambiguous identity, the
exact declared database, not a forbidden application/test database, not the
schema-less or host-less form). A live run additionally re-proves the identity
**on the connection itself** (``current_database()``, ``current_schema()``,
``server_version_num``) before any scenario SQL, so safety never rests on the
database name in the DSN alone.

Offline commands
----------------
``list`` and ``print-databases`` are pure: they read no environment variable,
load no configuration, import no driver and open no connection. The live layer
(``src.config``, ``src.db``, ``src.db_migrate``, ``src.db_migrate_executor``,
``src.startup_schema`` and ``psycopg``) is imported **inside** the functions
that need it, so importing this module cannot read a ``.env`` file or establish
a database surface. Running anything against a database additionally requires
the explicit ``--allow-live`` opt-in and the ``ATTENDANCE_NEGATIVE_SCHEMA_DSN``
process variable; the DSN is never printed, logged or accepted as an argument.

Reuse of S1–S6
--------------
S1's contract, S2's probe, S3's evaluator, S4's executor statements, S5's gate
policy and S6's startup gate are **used**, never modified. The only private
member this tool touches is S4's same-connection snapshot statement helper,
because the public ``read_snapshot()`` / ``verify_schema_locked()`` each own
their connection and cannot share the defect transaction. That reuse is
deliberate and is pinned by a test; the S4 interface itself is unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Final

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db_schema_catalog import (  # noqa: E402  (after the sys.path bootstrap)
    CatalogProbeError,
    CatalogSnapshot,
    read_public_catalog,
)
from src.physical_schema import (  # noqa: E402
    CompatibilityStatus,
    evaluate_physical_schema,
)
from src.schema_contract import TARGET_SCHEMA  # noqa: E402
from tools.negative_schema_scenarios import (  # noqa: E402
    DEFERRED_SCENARIOS,
    EMPTY_STATE,
    MANDATORY_SCENARIOS,
    OPTIONAL_SCENARIOS,
    SCENARIOS,
    STATE_EMPTY,
    STATE_LEDGER_ONLY,
    ComparisonResult,
    DeclaredStateIncomplete,
    ExpectedMigrationFacts,
    MigrationFacts,
    Scenario,
    classify_state,
    compare_migration_facts,
    compare_object_state,
    scenario_by_id,
    unresolved_properties,
)

__all__ = [
    "DSN_ENV_VAR",
    "EXIT_EXPECTED_MISMATCH",
    "EXIT_OUTCOME_UNCONFIRMED",
    "EXIT_POST_COMMIT_MISMATCH",
    "EXIT_READY",
    "EXIT_UNAVAILABLE",
    "EXIT_UNSAFE_STATE",
    "EXIT_USAGE",
    "CleanupPlan",
    "Outcome",
    "ProvisioningEnvironment",
    "REPORT_SCHEMA_VERSION",
    "Recovery",
    "SESSION_GUARDS",
    "ScenarioResult",
    "ServerIdentity",
    "TargetDecision",
    "VerificationReport",
    "build_report",
    "classify_recovery",
    "main",
    "plan_cleanup",
    "run_cleanup",
    "run_scenario",
    "validate_target",
    "write_report",
]

# ---------------------------------------------------------------------------
# Exit codes (approved S7 contract; never renumbered)
# ---------------------------------------------------------------------------

EXIT_READY: Final[int] = 0
EXIT_USAGE: Final[int] = 2
EXIT_UNSAFE_STATE: Final[int] = 3
EXIT_UNAVAILABLE: Final[int] = 4
EXIT_EXPECTED_MISMATCH: Final[int] = 5
EXIT_POST_COMMIT_MISMATCH: Final[int] = 6
EXIT_OUTCOME_UNCONFIRMED: Final[int] = 7

#: The only variable this tool reads, and only on an explicitly authorized
#: live run. It is never accepted on the command line, so it can never appear
#: in a process listing.
DSN_ENV_VAR: Final[str] = "ATTENDANCE_NEGATIVE_SCHEMA_DSN"

#: The three transaction-local guards applied before any scenario SQL. They are
#: ``SET LOCAL`` on purpose: the values die with the transaction and can never
#: leak into the pooled session that opened the connection. Recorded here as
#: one frozen tuple so the exact texts are reviewable in a single place.
SESSION_GUARDS: Final[tuple[str, ...]] = (
    "SET LOCAL statement_timeout = '30s'",
    "SET LOCAL lock_timeout = '10s'",
    "SET LOCAL idle_in_transaction_session_timeout = '0'",
)

#: Mirrors S4's snapshot statements; the verification pass is read-only and is
#: always ended with ``ROLLBACK`` (nothing is ever written on that connection).
_VERIFY_BEGIN: Final[str] = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
_VERIFY_END: Final[str] = "ROLLBACK"

_GATE_LEDGER_SHAPE: Final[str] = "ledger_shape"
_GATE_CODE_MIGRATION_HISTORY_INVALID: Final[str] = "MIGRATION_HISTORY_INVALID"

#: The PostgreSQL floor every scenario needs: S2 reads ``attgenerated`` and
#: ``attidentity``, which only exist from PostgreSQL 12 on. A scenario may
#: declare a higher floor (``min_server_version``); the stricter one wins.
MIN_SERVER_VERSION: Final[int] = 120000

#: The only DDL verbs cleanup may emit, per declared relation kind. There is
#: deliberately no entry for indexes (they vanish with their table), for
#: schemas, or for databases, and no ``CASCADE``/``IF EXISTS`` form exists.
_DROP_PREFIX: Final[dict[str, str]] = {
    "r": "DROP TABLE",
    "p": "DROP TABLE",
    "v": "DROP VIEW",
    "m": "DROP MATERIALIZED VIEW",
    "S": "DROP SEQUENCE",
}

#: Index relation kinds: implicit in the manifest (their table owns them).
_INDEX_KINDS: Final[frozenset[str]] = frozenset({"i", "I"})

#: The live identity probe. It runs on the connection that will carry the
#: scenario, so safety is proven by the server, not inferred from the DSN text.
_IDENTITY_SQL: Final[str] = (
    "SELECT current_database(), current_schema(), "
    "current_setting('server_version_num')"
)

REPORT_SCHEMA_VERSION: Final[int] = 1
TOOL_VERSION: Final[str] = "s7-milestone-d-1"

#: Placeholder DSN for the offline tests' default facts reader. It is never
#: connected to: the reader only carries S4's fixed statements during tests.
_PLACEHOLDER_DSN: Final[str] = "postgresql://unused/offline-test"

_PREFIX: Final[str] = "provision-negative"


# ---------------------------------------------------------------------------
# Outcomes and results
# ---------------------------------------------------------------------------


class Outcome(Enum):
    """The terminal classification of one scenario run."""

    READY = "ready"
    USAGE = "usage"
    UNSAFE_STATE = "unsafe_state"
    UNAVAILABLE = "unavailable"
    EXPECTED_MISMATCH = "expected_mismatch"
    POST_COMMIT_MISMATCH = "post_commit_mismatch"
    OUTCOME_UNCONFIRMED = "outcome_unconfirmed"

    @property
    def exit_code(self) -> int:
        """The approved exit code for this outcome."""
        return _EXIT_CODES[self]


_EXIT_CODES: Final[dict[Outcome, int]] = {
    Outcome.READY: EXIT_READY,
    Outcome.USAGE: EXIT_USAGE,
    Outcome.UNSAFE_STATE: EXIT_UNSAFE_STATE,
    Outcome.UNAVAILABLE: EXIT_UNAVAILABLE,
    Outcome.EXPECTED_MISMATCH: EXIT_EXPECTED_MISMATCH,
    Outcome.POST_COMMIT_MISMATCH: EXIT_POST_COMMIT_MISMATCH,
    Outcome.OUTCOME_UNCONFIRMED: EXIT_OUTCOME_UNCONFIRMED,
}


class Recovery(Enum):
    """What the database is left in, stated without any optimism.

    Every value names evidence, never an assumption: ``uncertain_commit`` and
    ``partial_recovery`` are terminal classifications, not prompts to guess.
    """

    EXPECTED_COMMITTED = "expected_committed"
    EXPECTED_ROLLED_BACK = "expected_rolled_back"
    UNCERTAIN_COMMIT = "uncertain_commit"
    POST_COMMIT_MISMATCH = "post_commit_mismatch"
    UNEXPECTED_OBJECT_STATE = "unexpected_object_state"
    PARTIAL_RECOVERY = "partial_recovery"
    UNSAFE_CLEANUP_STATE = "unsafe_cleanup_state"


def classify_recovery(outcome: Outcome, committed: bool | None) -> Recovery:
    """Map one run outcome to its recovery classification.

    ``committed`` is consulted only as evidence: an outcome that admits it does
    not know the commit result can never be classified as recovered.
    """
    if outcome is Outcome.READY:
        return Recovery.EXPECTED_COMMITTED if committed else Recovery.EXPECTED_ROLLED_BACK
    if outcome is Outcome.EXPECTED_MISMATCH:
        return Recovery.EXPECTED_ROLLED_BACK
    if outcome is Outcome.OUTCOME_UNCONFIRMED:
        return Recovery.UNCERTAIN_COMMIT
    if outcome is Outcome.POST_COMMIT_MISMATCH:
        return Recovery.POST_COMMIT_MISMATCH
    return Recovery.UNEXPECTED_OBJECT_STATE


@dataclass(frozen=True)
class ServerIdentity:
    """The identity the **server** reports for the connection in use."""

    database: str
    schema: str
    version_num: int


@dataclass(frozen=True)
class TargetDecision:
    """The result of the static target-identity check (no connection)."""

    ok: bool
    reason: str | None = None
    database: str | None = None


def read_server_identity(connection: Any) -> ServerIdentity:
    """Read the connected database, schema and server version number.

    This is the live proof that the connection reached the declared database;
    the DSN's database name alone is never treated as authorization.
    """
    row = connection.execute(_IDENTITY_SQL).fetchone()
    if row is None or len(row) < 3:  # pragma: no cover - defensive
        raise RuntimeError("the server identity row is unavailable")
    return ServerIdentity(
        database=str(row[0]),
        schema=str(row[1]),
        version_num=int(row[2]),
    )


def validate_target(
    dsn: str,
    scenario: Scenario,
    *,
    identity_parser: Callable[[str], tuple[str, str, int] | None] | None = None,
    forbidden_databases: tuple[str, ...] = (),
) -> TargetDecision:
    """Prove the target statically, before any connection is opened.

    Fail closed on every uncertainty: an unparseable, ambiguous, forbidden or
    differently-named database is a refusal, never a warning. The check is
    deliberately *not* a security boundary (the live identity probe is), but a
    wrong target is refused before a single statement runs.
    """
    if identity_parser is None:
        from src.db_target import (  # lazy: imports the DSN parser layer
            normalized_target_identity as identity_parser,
        )

    if not isinstance(dsn, str) or not dsn.strip():
        return TargetDecision(ok=False, reason="target_missing")

    try:
        identity = identity_parser(dsn)
    except Exception:
        return TargetDecision(ok=False, reason="target_malformed")

    if identity is None:
        return TargetDecision(ok=False, reason="target_ambiguous")

    database = identity[0]
    forbidden = {name.casefold() for name in forbidden_databases}
    if database.casefold() in forbidden:
        return TargetDecision(ok=False, reason="target_forbidden_database", database=database)
    if database != scenario.database_name:
        return TargetDecision(
            ok=False, reason="target_database_mismatch", database=database
        )
    return TargetDecision(ok=True, database=database)


@dataclass(frozen=True)
class CleanupPlan:
    """The authorized (or refused) cleanup for one scenario.

    ``statements`` is derived only from the frozen ``cleanup_manifest``
    intersected with what is actually observed, so a vanished object is never
    re-created by the plan and an unexpected object is never swept up by it.
    """

    scenario_id: str
    state: str
    authorized: bool
    refusal: str | None
    statements: tuple[str, ...]
    dropped: tuple[tuple[str, str], ...]
    already_absent: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario_id,
            "state": self.state,
            "authorized": self.authorized,
            "refusal": self.refusal,
            "statements": len(self.statements),
            "dropped": [f"{kind}:{name}" for name, kind in self.dropped],
            "already_absent": [f"{kind}:{name}" for name, kind in self.already_absent],
        }


@dataclass(frozen=True)
class VerificationReport:
    """One evaluated observation of a scenario's database, sanitized.

    ``objects`` is always present (it is what every scenario declares);
    ``facts`` is absent only when the ledger shape itself was refused, because
    then no history is readable and the refusal *is* the declared evidence.
    """

    scope: str
    objects: ComparisonResult
    facts: ComparisonResult | None
    codes: tuple[str, ...]
    status: str | None
    gate_code: str | None
    ledger_shape_refused: bool
    matched: bool

    @property
    def descriptors(self) -> tuple[str, ...]:
        """``component[object]`` mismatch descriptors — identifiers only."""
        collected = list(self.objects.descriptors)
        if self.facts is not None:
            collected.extend(self.facts.descriptors)
        return tuple(collected)

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serializable view with no SQL, DSN or driver text."""
        return {
            "scope": self.scope,
            "matched": self.matched,
            "objects": _comparison_dict(self.objects),
            "facts": None if self.facts is None else _comparison_dict(self.facts),
            "codes": list(self.codes),
            "status": self.status,
            "gate_code": self.gate_code,
            "ledger_shape_refused": self.ledger_shape_refused,
        }


def _comparison_dict(result: ComparisonResult) -> dict[str, Any]:
    return {
        "matched": result.matched,
        "mismatches": [
            {
                "component": mismatch.component,
                "object": mismatch.object,
                "reason": mismatch.reason,
            }
            for mismatch in result.mismatches
        ],
    }


@dataclass(frozen=True)
class ScenarioResult:
    """The structured, sanitized result of running one scenario."""

    scenario_id: str
    outcome: Outcome
    committed: bool | None
    recovery: Recovery
    statements_executed: int = 0
    guards_applied: bool = False
    in_transaction: VerificationReport | None = None
    post_commit: VerificationReport | None = None
    diagnostics: tuple[str, ...] = ()
    cleanup_errors: tuple[str, ...] = ()
    cleanup: CleanupPlan | None = None

    @property
    def exit_code(self) -> int:
        """The approved exit code for this result."""
        return self.outcome.exit_code

    @property
    def verified(self) -> bool:
        """Whether the committed state was proven and re-proven."""
        return self.outcome is Outcome.READY

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serializable view; never contains SQL, DSN or driver text."""
        return {
            "scenario": self.scenario_id,
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "committed": self.committed,
            "verified": self.verified,
            "recovery": self.recovery.value,
            "statements_executed": self.statements_executed,
            "guards_applied": self.guards_applied,
            "in_transaction": (
                None if self.in_transaction is None else self.in_transaction.as_dict()
            ),
            "post_commit": (
                None if self.post_commit is None else self.post_commit.as_dict()
            ),
            "diagnostics": list(self.diagnostics),
            "cleanup_errors": list(self.cleanup_errors),
            "cleanup": None if self.cleanup is None else self.cleanup.as_dict(),
        }


# ---------------------------------------------------------------------------
# Environment (all seams the offline tests inject)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvisioningEnvironment:
    """Everything one run needs, with the live pieces injected.

    ``open_connection`` must return a psycopg-shaped connection (``execute``,
    ``commit``, ``rollback``, ``close`` and a settable ``autocommit``). The
    remaining callables default to the production implementations; tests
    replace them with fakes, which is why no test needs a database.
    """

    open_connection: Callable[[], Any]
    catalog_probe: Callable[[Any], CatalogSnapshot] = read_public_catalog
    facts_reader: Callable[[Any], MigrationFacts] | None = None
    ensure_ledger: Callable[[], None] | None = None
    shipped_checksum: str = ""
    migrations_dir: Path | None = None
    dsn: str = field(default=_PLACEHOLDER_DSN, repr=False)
    #: Set by every live entry point. When it is ``None`` the identity probe is
    #: disabled, which exists only as an offline test seam: ``_live_environment``
    #: always supplies it, so a real run can never skip the proof.
    target_database: str | None = None
    forbidden_databases: tuple[str, ...] = ()
    server_identity_reader: Callable[[Any], ServerIdentity] | None = None

    @property
    def enforces_identity(self) -> bool:
        """Whether this environment must prove the live server identity."""
        return self.target_database is not None

    def server_identity(self, connection: Any) -> ServerIdentity:
        """Read the server identity with the injected (or production) reader."""
        reader = self.server_identity_reader or read_server_identity
        return reader(connection)

    def read_facts(self, connection: Any) -> MigrationFacts:
        """Read the G2 facts **on the given connection** (no new connection)."""
        if self.facts_reader is not None:
            return self.facts_reader(connection)
        return _read_facts_on(
            connection,
            dsn=self.dsn,
            opener=lambda _dsn: self.open_connection(),
        )


def _read_facts_on(
    connection: Any,
    *,
    dsn: str,
    opener: Callable[[str], Any],
) -> MigrationFacts:
    """Reuse S4's fixed, same-connection snapshot statements.

    ``read_snapshot()`` and ``verify_schema_locked()`` each own a connection, so
    neither can read inside the defect transaction. S4's statement helper is
    the only reader that runs on a caller-owned connection; it executes the
    same four fixed statements as the public operations, including the ledger
    shape check, and opens nothing. The executor object is a vehicle for those
    statements: ``opener`` is never called here.
    """
    from src.db_migrate_executor import (  # lazy: imports config + driver funnel
        PsycopgMigrationExecutor,
    )

    reader = PsycopgMigrationExecutor(dsn, connection_opener=opener)
    snapshot = reader._read_snapshot_statements(  # noqa: SLF001 - pinned by test
        connection, validate_ledger_shape=True
    )
    return MigrationFacts(
        ledger_present=snapshot.ledger_present,
        applied=tuple(snapshot.applied),
        tables=tuple(sorted(snapshot.tables)),
    )


def _migration_error_types() -> tuple[type[Exception], type[Exception]]:
    """The two S4 error classes this tool must distinguish (lazy import)."""
    from src.db_migrate import MigrationError, MigrationIntegrityError

    return MigrationError, MigrationIntegrityError


def _check_identity(
    scenario: Scenario,
    environment: ProvisioningEnvironment,
    connection: Any,
) -> str | None:
    """Prove the connection reached the declared database and a recent enough
    server in the intended schema. Returns a sanitized refusal token or None.

    Never raises: an unreadable identity is ``target_identity_unverified``, so
    a failure to prove the target can never be mistaken for permission.
    """
    if not environment.enforces_identity:
        return None
    try:
        identity = environment.server_identity(connection)
    except Exception:
        return "target_identity_unverified"

    if identity.database != environment.target_database:
        return "target_database_mismatch"
    if identity.schema != TARGET_SCHEMA:
        return "target_schema_mismatch"
    floor = max(MIN_SERVER_VERSION, scenario.min_server_version or 0)
    if identity.version_num < floor:
        return "server_version_below_floor"
    return None


# ---------------------------------------------------------------------------
# Evaluation (frozen declarations + the real S3/S5 policy)
# ---------------------------------------------------------------------------


def _evaluate(
    scenario: Scenario,
    catalog: CatalogSnapshot,
    facts: MigrationFacts | None,
    *,
    ledger_shape_refused: bool,
    environment: ProvisioningEnvironment,
    scope: str,
) -> VerificationReport:
    """Compare one captured state with the frozen declaration.

    Never learns anything: the expectation comes from the scenario, the
    observation from the database, and the answer is a comparison.
    """
    objects = compare_object_state(
        catalog,
        scenario.expected_object_state,
        declared_by=scenario.scenario_id,
    )

    if ledger_shape_refused:
        # The refusal *is* the declared evidence: with the ledger's shape
        # broken there is no readable history, so the declared path
        # ("ledger_shape") and the declared gate code must both say so.
        matched = (
            objects.matched
            and scenario.gate_code_path == _GATE_LEDGER_SHAPE
            and scenario.expected_gate_code == _GATE_CODE_MIGRATION_HISTORY_INVALID
        )
        return VerificationReport(
            scope=scope,
            objects=objects,
            facts=None,
            codes=(),
            status=None,
            gate_code=_GATE_CODE_MIGRATION_HISTORY_INVALID,
            ledger_shape_refused=True,
            matched=matched,
        )

    if facts is None:  # pragma: no cover - defensive: the reader always answers
        raise DeclaredStateIncomplete(scenario.scenario_id, ("migration_facts",))

    fact_comparison = compare_migration_facts(
        facts,
        scenario.expected_migration_facts,
        shipped_checksum=environment.shipped_checksum,
    )

    if scenario.layer == "G8":
        evaluation = evaluate_physical_schema(catalog)
        expected_status = (
            None if scenario.expected_status is None else scenario.expected_status.value
        )
        matched = (
            objects.matched
            and fact_comparison.matched
            and evaluation.codes == scenario.expected_codes
            and evaluation.status.value == expected_status
        )
        return VerificationReport(
            scope=scope,
            objects=objects,
            facts=fact_comparison,
            codes=tuple(evaluation.codes),
            status=evaluation.status.value,
            gate_code=None,
            ledger_shape_refused=False,
            matched=matched,
        )

    gate_code = _gate_code(scenario, facts, environment.migrations_dir)
    matched = (
        objects.matched
        and fact_comparison.matched
        and gate_code == scenario.expected_gate_code
    )
    return VerificationReport(
        scope=scope,
        objects=objects,
        facts=fact_comparison,
        codes=(),
        status=None,
        gate_code=gate_code,
        ledger_shape_refused=False,
        matched=matched,
    )


def _gate_code(
    scenario: Scenario,
    facts: MigrationFacts,
    migrations_dir: Path | None,
) -> str | None:
    """Reproduce a G2 gate verdict with the real S5 policy.

    ``evaluate_migration_state`` is the startup gate's own decision function;
    calling it here is what makes "the declared gate code" an observation
    rather than a restatement of the declaration.
    """
    from src.db_migrate import MigrationSnapshot  # lazy: imports config
    from src.startup_schema import (  # lazy: imports config + S4
        StartupSchemaError,
        evaluate_migration_state,
    )

    snapshot = MigrationSnapshot(
        tables=frozenset(facts.tables),
        ledger_present=facts.ledger_present,
        applied=tuple(facts.applied),
    )
    try:
        evaluate_migration_state(snapshot, migrations_dir)
    except StartupSchemaError as exc:
        return exc.code
    return None


# ---------------------------------------------------------------------------
# Connection hygiene (every path closes exactly once)
# ---------------------------------------------------------------------------


def _close(connection: Any) -> tuple[str, ...]:
    try:
        connection.close()
    except Exception:
        return ("close_failed",)
    return ()


def _rollback_and_close(connection: Any) -> tuple[str, ...]:
    """Roll back **exactly once**, then close; never hides either failure."""
    problems: list[str] = []
    try:
        connection.rollback()
    except Exception:
        problems.append("rollback_failed")
    problems.extend(_close(connection))
    return tuple(problems)


def _result(
    scenario: Scenario,
    outcome: Outcome,
    *,
    committed: bool | None,
    recovery: Recovery | None = None,
    executed: int = 0,
    guards: bool = False,
    in_transaction: VerificationReport | None = None,
    post_commit: VerificationReport | None = None,
    diagnostics: tuple[str, ...] = (),
    cleanup: tuple[str, ...] = (),
    plan: CleanupPlan | None = None,
) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=scenario.scenario_id,
        outcome=outcome,
        committed=committed,
        recovery=(
            classify_recovery(outcome, committed) if recovery is None else recovery
        ),
        statements_executed=executed,
        guards_applied=guards,
        in_transaction=in_transaction,
        post_commit=post_commit,
        diagnostics=diagnostics,
        cleanup_errors=cleanup,
        cleanup=plan,
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def run_scenario(
    scenario: Scenario,
    environment: ProvisioningEnvironment,
) -> ScenarioResult:
    """Run one declared scenario through its M10 lifecycle.

    Selection refusal happens before any connection: a deferred scenario or an
    incomplete declaration is a usage error, never a "best effort" run.
    """
    if scenario.deferred_unverified:
        return _result(
            scenario,
            Outcome.USAGE,
            committed=False,
            diagnostics=("scenario_deferred",),
        )
    problems = unresolved_properties(scenario.expected_object_state)
    if problems:
        return _result(
            scenario,
            Outcome.USAGE,
            committed=False,
            diagnostics=("declaration_incomplete",) + tuple(problems),
        )
    if scenario.baseline_mode == "none":
        return _run_without_baseline(scenario, environment)
    if scenario.baseline_mode == "ensure_ledger":
        return _run_with_ensure_ledger(scenario, environment)
    if scenario.baseline_mode == "migrate":
        return _run_defect_transaction(scenario, environment)
    return _result(
        scenario,
        Outcome.USAGE,
        committed=False,
        diagnostics=("unknown_baseline_mode",),
    )


def _run_defect_transaction(
    scenario: Scenario,
    environment: ProvisioningEnvironment,
) -> ScenarioResult:
    """Steps 1–9 of M10: defect, probe, compare, commit-or-rollback, close."""
    base, integrity = _migration_error_types()

    try:
        connection = environment.open_connection()
    except Exception:
        return _result(
            scenario, Outcome.UNAVAILABLE, committed=False,
            diagnostics=("connection_unavailable",),
        )

    try:
        connection.autocommit = False
    except Exception:
        cleanup = _close(connection)
        return _result(
            scenario, Outcome.UNAVAILABLE, committed=False,
            diagnostics=("connection_unavailable",) + cleanup,
            cleanup=cleanup,
        )

    try:
        for guard in SESSION_GUARDS:
            connection.execute(guard)
    except Exception:
        cleanup = _rollback_and_close(connection)
        return _result(
            scenario, Outcome.UNSAFE_STATE, committed=False,
            diagnostics=("guard_failed",), cleanup=cleanup,
        )

    # The target is re-proven on this connection, before any scenario SQL. A
    # wrong database, schema or an old server is refused, never "repaired".
    refusal = _check_identity(scenario, environment, connection)
    if refusal is not None:
        cleanup = _rollback_and_close(connection)
        return _result(
            scenario, Outcome.USAGE, committed=False, guards=True,
            diagnostics=(refusal,), cleanup=cleanup,
        )

    executed = 0
    try:
        for statement in scenario.statements:
            connection.execute(statement)
            executed += 1
    except Exception:
        cleanup = _rollback_and_close(connection)
        return _result(
            scenario, Outcome.UNSAFE_STATE, committed=False,
            executed=executed, guards=True,
            diagnostics=("defect_statement_failed",), cleanup=cleanup,
        )

    try:
        catalog = environment.catalog_probe(connection)
    except CatalogProbeError:
        cleanup = _rollback_and_close(connection)
        return _result(
            scenario, Outcome.UNAVAILABLE, committed=False,
            executed=executed, guards=True,
            diagnostics=("catalog_unreadable",), cleanup=cleanup,
        )
    except Exception:
        cleanup = _rollback_and_close(connection)
        return _result(
            scenario, Outcome.UNAVAILABLE, committed=False,
            executed=executed, guards=True,
            diagnostics=("catalog_unreadable",), cleanup=cleanup,
        )

    ledger_shape_refused = False
    facts: MigrationFacts | None = None
    try:
        facts = environment.read_facts(connection)
    except integrity:
        ledger_shape_refused = True
    except base:
        cleanup = _rollback_and_close(connection)
        return _result(
            scenario, Outcome.UNAVAILABLE, committed=False,
            executed=executed, guards=True,
            diagnostics=("migration_state_unreadable",), cleanup=cleanup,
        )
    except Exception:
        cleanup = _rollback_and_close(connection)
        return _result(
            scenario, Outcome.UNAVAILABLE, committed=False,
            executed=executed, guards=True,
            diagnostics=("migration_state_unreadable",), cleanup=cleanup,
        )

    try:
        report = _evaluate(
            scenario,
            catalog,
            facts,
            ledger_shape_refused=ledger_shape_refused,
            environment=environment,
            scope="in_transaction",
        )
    except DeclaredStateIncomplete:
        cleanup = _rollback_and_close(connection)
        return _result(
            scenario, Outcome.USAGE, committed=False,
            executed=executed, guards=True,
            diagnostics=("declaration_incomplete",), cleanup=cleanup,
        )

    if not report.matched:
        cleanup = _rollback_and_close(connection)
        outcome = Outcome.UNSAFE_STATE if cleanup else Outcome.EXPECTED_MISMATCH
        return _result(
            scenario, outcome, committed=False,
            executed=executed, guards=True, in_transaction=report,
            diagnostics=("expected_state_mismatch",), cleanup=cleanup,
        )

    # Commit only on a complete match, and exactly once.
    try:
        connection.commit()
    except Exception:
        cleanup = _close(connection)
        return _result(
            scenario, Outcome.OUTCOME_UNCONFIRMED, committed=None,
            executed=executed, guards=True, in_transaction=report,
            diagnostics=("commit_unconfirmed",), cleanup=cleanup,
        )

    cleanup = _close(connection)
    if cleanup:
        return _result(
            scenario, Outcome.UNSAFE_STATE, committed=True,
            executed=executed, guards=True, in_transaction=report,
            diagnostics=("committed_but_close_failed",), cleanup=cleanup,
        )

    post_commit, failure = _read_only_verification(
        scenario, environment, scope="post_commit"
    )
    if failure is not None:
        return _result(
            scenario, Outcome.UNSAFE_STATE, committed=True,
            executed=executed, guards=True, in_transaction=report,
            diagnostics=("post_commit_unreadable", failure),
        )
    assert post_commit is not None  # _read_only_verification pairs these
    if not post_commit.matched:
        return _result(
            scenario, Outcome.POST_COMMIT_MISMATCH, committed=True,
            executed=executed, guards=True, in_transaction=report,
            post_commit=post_commit,
            diagnostics=("post_commit_mismatch",),
        )
    return _result(
        scenario, Outcome.READY, committed=True,
        executed=executed, guards=True, in_transaction=report,
        post_commit=post_commit,
    )


def _run_without_baseline(
    scenario: Scenario,
    environment: ProvisioningEnvironment,
) -> ScenarioResult:
    """``G01``: no defect transaction, prove the baseline is empty."""
    report, failure = _read_only_verification(
        scenario, environment, scope="read_only"
    )
    if failure is not None:
        return _result(
            scenario, Outcome.UNAVAILABLE, committed=False,
            diagnostics=(failure,),
        )
    assert report is not None
    outcome = Outcome.READY if report.matched else Outcome.EXPECTED_MISMATCH
    return _result(
        scenario, outcome, committed=False, in_transaction=report,
        diagnostics=() if report.matched else ("expected_state_mismatch",),
    )


def _run_with_ensure_ledger(
    scenario: Scenario,
    environment: ProvisioningEnvironment,
) -> ScenarioResult:
    """``G04b``: S4 creates and commits the ledger, then we re-verify read-only."""
    if environment.ensure_ledger is None:
        return _result(
            scenario, Outcome.USAGE, committed=False,
            diagnostics=("ensure_ledger_unavailable",),
        )
    try:
        environment.ensure_ledger()
    except Exception:
        # The ledger creation commits on its own connection: a failure may
        # still have committed, so the outcome is unconfirmed, never ready.
        return _result(
            scenario, Outcome.OUTCOME_UNCONFIRMED, committed=None,
            diagnostics=("ensure_ledger_failed",),
        )

    report, failure = _read_only_verification(
        scenario, environment, scope="post_commit"
    )
    if failure is not None:
        return _result(
            scenario, Outcome.UNSAFE_STATE, committed=True,
            diagnostics=("post_commit_unreadable", failure),
        )
    assert report is not None
    if not report.matched:
        return _result(
            scenario, Outcome.POST_COMMIT_MISMATCH, committed=True,
            post_commit=report, diagnostics=("post_commit_mismatch",),
        )
    return _result(
        scenario, Outcome.READY, committed=True, post_commit=report,
    )


def _evaluate_empty(
    catalog: CatalogSnapshot,
    facts: MigrationFacts | None,
    *,
    environment: ProvisioningEnvironment,
    scope: str,
) -> VerificationReport:
    """Evaluate the end state a completed cleanup must leave behind.

    The declared expectation is the empty state: no relation, column,
    constraint or index in the target schema, and no ledger. It is built from
    the frozen ``EMPTY_STATE`` constant, never observed.
    """
    objects = compare_object_state(catalog, EMPTY_STATE, declared_by="cleanup")
    if facts is None:  # a refused ledger shape cannot prove emptiness
        return VerificationReport(
            scope=scope,
            objects=objects,
            facts=None,
            codes=(),
            status=None,
            gate_code=None,
            ledger_shape_refused=True,
            matched=False,
        )
    fact_comparison = compare_migration_facts(
        facts,
        ExpectedMigrationFacts(ledger_present=False, applied=(), tables=()),
        shipped_checksum=environment.shipped_checksum,
    )
    return VerificationReport(
        scope=scope,
        objects=objects,
        facts=fact_comparison,
        codes=(),
        status=None,
        gate_code=None,
        ledger_shape_refused=False,
        matched=objects.matched and fact_comparison.matched,
    )


def plan_cleanup(
    scenario: Scenario,
    catalog: CatalogSnapshot,
    facts: MigrationFacts,
    *,
    shipped_checksum: str,
) -> CleanupPlan:
    """Authorize cleanup strictly from the frozen ``cleanup_manifest``.

    Rules, in order:

    1. only declared relation kinds with a known ``DROP`` verb may be cleaned;
    2. the observed state must classify into the scenario's declared
       ``allowed_recovery_states`` (otherwise the cleanup is unsafe);
    3. every observed relation (indexes excluded: their table owns them) must
       be declared in the manifest with exactly the observed kind.

    Statements are emitted in manifest order, only for relations actually
    present, and never in a ``CASCADE``/``IF EXISTS``/wildcard form.
    """
    state = classify_state(
        catalog,
        facts,
        scenario.expected_object_state,
        shipped_checksum=shipped_checksum,
        declared_by=scenario.scenario_id,
    )
    manifest = dict(scenario.cleanup_manifest)
    unsupported = [
        (name, kind)
        for name, kind in scenario.cleanup_manifest
        if kind not in _DROP_PREFIX
    ]
    observed = tuple(
        sorted(
            (relation.name, relation.kind)
            for relation in catalog.relations
            if relation.schema == TARGET_SCHEMA and relation.kind not in _INDEX_KINDS
        )
    )
    observed_names = {name for name, _kind in observed}

    # Object-identity checks come first so an unexpected object is reported as
    # such (a state check would refuse it too, but would hide *why*). The state
    # check then still runs, because a manifest-shaped but different catalog
    # (e.g. wrong column types) must never be cleaned either.
    refusal: str | None = None
    if unsupported:
        refusal = "cleanup_manifest_unsupported_kind"
    else:
        for name, kind in observed:
            if name not in manifest:
                refusal = "cleanup_unexpected_object"
                break
            if manifest[name] != kind:
                refusal = "cleanup_kind_mismatch"
                break
        if refusal is None and state not in scenario.allowed_recovery_states:
            refusal = "cleanup_state_not_allowed"

    statements = (
        ()
        if refusal is not None
        else tuple(
            f"{_DROP_PREFIX[kind]} {TARGET_SCHEMA}.{name}"
            for name, kind in scenario.cleanup_manifest
            if name in observed_names
        )
    )
    return CleanupPlan(
        scenario_id=scenario.scenario_id,
        state=state,
        authorized=refusal is None,
        refusal=refusal,
        statements=statements,
        dropped=tuple(
            (name, kind)
            for name, kind in scenario.cleanup_manifest
            if name in observed_names
        ),
        already_absent=tuple(
            (name, kind)
            for name, kind in scenario.cleanup_manifest
            if name not in observed_names
        ),
    )


def _read_only_observation(
    scenario: Scenario,
    environment: ProvisioningEnvironment,
) -> tuple[CatalogSnapshot | None, MigrationFacts | None, bool, str | None]:
    """One read-only observation on a **separate** connection.

    Returns ``(catalog, facts, ledger_shape_refused, failure_token)``. Nothing
    is ever written: the transaction is ``READ ONLY`` and ended with
    ``ROLLBACK``; no ``commit`` is called on this connection. A ledger whose
    shape is refused yields ``facts=None`` and ``ledger_shape_refused=True``,
    which is evidence, not an unreadable database.
    """
    _base, integrity = _migration_error_types()
    try:
        connection = environment.open_connection()
    except Exception:
        return None, None, False, "connection_unavailable"

    try:
        connection.autocommit = True
        connection.execute(_VERIFY_BEGIN)
        refusal = _check_identity(scenario, environment, connection)
        if refusal is not None:
            return None, None, False, refusal
        catalog = environment.catalog_probe(connection)
        ledger_shape_refused = False
        facts: MigrationFacts | None = None
        try:
            facts = environment.read_facts(connection)
        except integrity:
            ledger_shape_refused = True
        connection.execute(_VERIFY_END)
        return catalog, facts, ledger_shape_refused, None
    except CatalogProbeError:
        return None, None, False, "catalog_unreadable"
    except Exception:
        return None, None, False, "verification_failed"
    finally:
        _close(connection)


def _read_only_verification(
    scenario: Scenario,
    environment: ProvisioningEnvironment,
    *,
    scope: str,
    checker: Callable[
        [CatalogSnapshot, MigrationFacts | None, bool], VerificationReport
    ]
    | None = None,
) -> tuple[VerificationReport | None, str | None]:
    """Observe read-only, then evaluate with the declared (or supplied) rule.

    Returns ``(report, None)`` or ``(None, sanitized_failure_token)``.
    """
    catalog, facts, ledger_shape_refused, failure = _read_only_observation(
        scenario, environment
    )
    if failure is not None or catalog is None:
        return None, failure or "verification_failed"
    if checker is not None:
        return checker(catalog, facts, ledger_shape_refused), None
    return (
        _evaluate(
            scenario,
            catalog,
            facts,
            ledger_shape_refused=ledger_shape_refused,
            environment=environment,
            scope=scope,
        ),
        None,
    )


def run_cleanup(
    scenario: Scenario,
    environment: ProvisioningEnvironment,
) -> ScenarioResult:
    """Clean one scenario's database back to the declared empty state.

    Cleanup is explicit, never automatic: it has its own command, it runs only
    when the frozen manifest authorizes it, and it never runs after an
    ambiguous failure or an uncertain commit by itself. The drop statements are
    executed on a dedicated transaction (guards, identity, then in-transaction
    emptiness proof), committed only on a full match, and re-proven read-only on
    a separate connection.
    """
    if scenario.deferred_unverified:
        return _result(
            scenario, Outcome.USAGE, committed=False,
            diagnostics=("scenario_deferred",),
        )
    problems = unresolved_properties(scenario.expected_object_state)
    if problems:
        return _result(
            scenario, Outcome.USAGE, committed=False,
            diagnostics=("declaration_incomplete",) + tuple(problems),
        )
    if not scenario.cleanup_manifest:
        return _result(
            scenario, Outcome.USAGE, committed=False,
            diagnostics=("cleanup_not_declared",),
        )

    catalog, facts, ledger_shape_refused, failure = _read_only_observation(
        scenario, environment
    )
    if failure is not None or catalog is None:
        return _result(
            scenario, Outcome.UNAVAILABLE, committed=False,
            diagnostics=(failure or "verification_failed",),
        )
    if facts is None or ledger_shape_refused:
        # The history (and therefore the recovery state) cannot be classified.
        return _result(
            scenario, Outcome.UNSAFE_STATE, committed=False,
            recovery=Recovery.UNSAFE_CLEANUP_STATE,
            diagnostics=("cleanup_state_unverifiable",),
        )

    plan = plan_cleanup(
        scenario, catalog, facts, shipped_checksum=environment.shipped_checksum
    )
    if not plan.authorized:
        return _result(
            scenario, Outcome.UNSAFE_STATE, committed=False,
            recovery=Recovery.UNSAFE_CLEANUP_STATE,
            diagnostics=(plan.refusal or "cleanup_refused",), plan=plan,
        )

    if not plan.statements:
        # Nothing is left to drop: the database is already at the empty state.
        return _result(
            scenario, Outcome.READY, committed=False,
            recovery=Recovery.PARTIAL_RECOVERY
            if plan.state == STATE_LEDGER_ONLY
            else Recovery.EXPECTED_COMMITTED,
            plan=plan,
        )

    try:
        connection = environment.open_connection()
    except Exception:
        return _result(
            scenario, Outcome.UNAVAILABLE, committed=False,
            diagnostics=("connection_unavailable",), plan=plan,
        )
    try:
        connection.autocommit = False
    except Exception:
        cleanup = _close(connection)
        return _result(
            scenario, Outcome.UNAVAILABLE, committed=False,
            diagnostics=("connection_unavailable",) + cleanup, cleanup=cleanup,
            plan=plan,
        )

    try:
        for guard in SESSION_GUARDS:
            connection.execute(guard)
    except Exception:
        cleanup = _rollback_and_close(connection)
        return _result(
            scenario, Outcome.UNSAFE_STATE, committed=False,
            diagnostics=("guard_failed",), cleanup=cleanup, plan=plan,
        )

    refusal = _check_identity(scenario, environment, connection)
    if refusal is not None:
        cleanup = _rollback_and_close(connection)
        return _result(
            scenario, Outcome.USAGE, committed=False, guards=True,
            diagnostics=(refusal,), cleanup=cleanup, plan=plan,
        )

    executed = 0
    try:
        for statement in plan.statements:
            connection.execute(statement)
            executed += 1
    except Exception:
        cleanup = _rollback_and_close(connection)
        return _result(
            scenario, Outcome.UNSAFE_STATE, committed=False,
            executed=executed, guards=True, plan=plan,
            diagnostics=("cleanup_statement_failed",), cleanup=cleanup,
        )

    try:
        catalog_after = environment.catalog_probe(connection)
        facts_after = environment.read_facts(connection)
    except Exception:
        cleanup = _rollback_and_close(connection)
        return _result(
            scenario, Outcome.UNAVAILABLE, committed=False,
            executed=executed, guards=True, plan=plan,
            diagnostics=("cleanup_verification_unreadable",), cleanup=cleanup,
        )

    report = _evaluate_empty(
        catalog_after, facts_after, environment=environment, scope="cleanup"
    )
    if not report.matched:
        cleanup = _rollback_and_close(connection)
        outcome = Outcome.UNSAFE_STATE if cleanup else Outcome.EXPECTED_MISMATCH
        return _result(
            scenario, outcome, committed=False,
            executed=executed, guards=True, in_transaction=report, plan=plan,
            diagnostics=("cleanup_state_mismatch",), cleanup=cleanup,
        )

    try:
        connection.commit()
    except Exception:
        cleanup = _close(connection)
        return _result(
            scenario, Outcome.OUTCOME_UNCONFIRMED, committed=None,
            executed=executed, guards=True, in_transaction=report, plan=plan,
            diagnostics=("commit_unconfirmed",), cleanup=cleanup,
        )

    cleanup = _close(connection)
    if cleanup:
        return _result(
            scenario, Outcome.UNSAFE_STATE, committed=True,
            executed=executed, guards=True, in_transaction=report, plan=plan,
            diagnostics=("committed_but_close_failed",), cleanup=cleanup,
        )

    post_commit, failure = _read_only_verification(
        scenario,
        environment,
        scope="post_commit",
        checker=lambda cat, fac, refused: _evaluate_empty(
            cat, fac, environment=environment, scope="post_commit"
        ),
    )
    if failure is not None:
        return _result(
            scenario, Outcome.UNSAFE_STATE, committed=True,
            executed=executed, guards=True, in_transaction=report, plan=plan,
            diagnostics=("post_commit_unreadable", failure),
        )
    assert post_commit is not None
    if not post_commit.matched:
        return _result(
            scenario, Outcome.POST_COMMIT_MISMATCH, committed=True,
            executed=executed, guards=True, in_transaction=report,
            post_commit=post_commit, plan=plan,
            diagnostics=("post_commit_mismatch",),
        )
    return _result(
        scenario, Outcome.READY, committed=True,
        recovery=Recovery.PARTIAL_RECOVERY
        if plan.state == STATE_LEDGER_ONLY
        else Recovery.EXPECTED_COMMITTED,
        executed=executed, guards=True, in_transaction=report,
        post_commit=post_commit, plan=plan,
    )


# ---------------------------------------------------------------------------
# Scenario selection and offline reporting
# ---------------------------------------------------------------------------


def _matrix_label(scenario: Scenario) -> str:
    if scenario in DEFERRED_SCENARIOS:
        return "deferred"
    if scenario in OPTIONAL_SCENARIOS:
        return "optional"
    return "mandatory"


def _verdict(scenario: Scenario) -> str:
    if scenario.layer == "G2":
        return f"gate:{scenario.expected_gate_code}"
    status = "" if scenario.expected_status is None else scenario.expected_status.value
    if not scenario.expected_codes:
        return status
    return f"{','.join(scenario.expected_codes)}/{status}"


def _select(matrix: str) -> tuple[Scenario, ...]:
    if matrix == "mandatory":
        return MANDATORY_SCENARIOS
    if matrix == "optional":
        return OPTIONAL_SCENARIOS
    if matrix == "deferred":
        return DEFERRED_SCENARIOS
    return SCENARIOS


def _scenario_row(scenario: Scenario) -> dict[str, Any]:
    return {
        "id": scenario.scenario_id,
        "layer": scenario.layer,
        "matrix": _matrix_label(scenario),
        "baseline_mode": scenario.baseline_mode,
        "database": scenario.database_name,
        "statements": len(scenario.statements),
        "expected_codes": list(scenario.expected_codes),
        "expected_status": (
            None if scenario.expected_status is None else scenario.expected_status.value
        ),
        "expected_gate_code": scenario.expected_gate_code,
        "verdict": _verdict(scenario),
    }


def _print_list(matrix: str, *, as_json: bool) -> None:
    selected = _select(matrix)
    if as_json:
        print(json.dumps([_scenario_row(s) for s in selected], indent=2))
        return
    for scenario in selected:
        print(
            f"{scenario.scenario_id:<6}{scenario.layer:<4}{_matrix_label(scenario):<10}"
            f"{scenario.baseline_mode:<14}{scenario.database_name:<40}{_verdict(scenario)}"
        )


def _print_databases(matrix: str) -> None:
    for scenario in _select(matrix):
        print(scenario.database_name)


def _result_lines(result: ScenarioResult) -> list[str]:
    """Human lines: stable codes and identifiers only, never SQL or a DSN."""
    lines = [
        f"{_PREFIX}: scenario {result.scenario_id}",
        f"{_PREFIX}: outcome {result.outcome.value} (exit {result.exit_code})",
        f"{_PREFIX}: committed {_tri_state(result.committed)}",
        f"{_PREFIX}: recovery {result.recovery.value}",
        f"{_PREFIX}: statements {result.statements_executed}",
        f"{_PREFIX}: guards {'applied' if result.guards_applied else 'not applied'}",
    ]
    for scope, report in (
        ("in-transaction", result.in_transaction),
        ("post-commit", result.post_commit),
    ):
        if report is None:
            continue
        verdict = "matched" if report.matched else "mismatch"
        detail = report.status or report.gate_code or ""
        lines.append(
            f"{_PREFIX}: {scope} {verdict} {','.join(report.codes)} {detail}".rstrip()
        )
        for descriptor in report.descriptors:
            lines.append(f"{_PREFIX}: mismatch {descriptor}")
    if result.cleanup is not None:
        decision = "authorized" if result.cleanup.authorized else "refused"
        lines.append(
            f"{_PREFIX}: cleanup {decision} state={result.cleanup.state} "
            f"dropped={len(result.cleanup.dropped)} "
            f"statements={len(result.cleanup.statements)}"
        )
        if result.cleanup.refusal is not None:
            lines.append(f"{_PREFIX}: cleanup refusal {result.cleanup.refusal}")
    for token in result.diagnostics:
        lines.append(f"{_PREFIX}: diagnostic {token}")
    for token in result.cleanup_errors:
        lines.append(f"{_PREFIX}: cleanup-error {token}")
    return lines


def _print_result(result: ScenarioResult, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.as_dict(), indent=2))
        return
    for line in _result_lines(result):
        print(line)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_report(
    result: ScenarioResult,
    scenario: Scenario,
    *,
    timestamp: datetime,
) -> dict[str, Any]:
    """Build the versioned report document (no secret can reach it)."""
    expected_status = (
        None if scenario.expected_status is None else scenario.expected_status.value
    )
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "tool": _PREFIX,
        "tool_version": TOOL_VERSION,
        "timestamp": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "scenario": {
            "id": scenario.scenario_id,
            "layer": scenario.layer,
            "database": scenario.database_name,
            "expected_codes": list(scenario.expected_codes),
            "expected_status": expected_status,
            "expected_gate_code": scenario.expected_gate_code,
            "expected_relations": [
                f"{kind}:{name}" for name, kind in scenario.expected_relation_manifest
            ],
        },
        "observed": (
            None if result.in_transaction is None else result.in_transaction.as_dict()
        ),
        "post_commit": (
            None if result.post_commit is None else result.post_commit.as_dict()
        ),
        "commit": {
            "committed": result.committed,
            "statements_executed": result.statements_executed,
            "guards_applied": result.guards_applied,
        },
        "recovery": result.recovery.value,
        "cleanup": None if result.cleanup is None else result.cleanup.as_dict(),
        "diagnostics": list(result.diagnostics),
        "cleanup_errors": list(result.cleanup_errors),
        "exit_code": result.exit_code,
        "verified": result.verified,
    }


def write_report(
    directory: Path,
    result: ScenarioResult,
    scenario: Scenario,
    *,
    timestamp: datetime,
) -> tuple[Path, Path]:
    """Write the JSON and human reports; any write failure propagates as OSError."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    json_path = directory / f"{scenario.scenario_id}-{stamp}.json"
    text_path = directory / f"{scenario.scenario_id}-{stamp}.txt"
    document = build_report(result, scenario, timestamp=timestamp)
    json_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    text_path.write_text("\n".join(_result_lines(result)) + "\n", encoding="utf-8")
    return json_path, text_path


def _tri_state(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


# ---------------------------------------------------------------------------
# Live wiring (lazy imports; reached only with --allow-live)
# ---------------------------------------------------------------------------


def _forbidden_databases() -> tuple[str, ...]:
    """The application and dedicated test database names, when configured.

    Read only on an explicitly authorized live path, and only to refuse: an
    unconfigured or unreadable DSN simply contributes no name, so this check
    can never widen what is allowed.
    """
    from src.config import SETTINGS  # lazy: reads configuration
    from src.db_target import normalized_target_identity  # lazy

    names: list[str] = []
    for candidate in (SETTINGS.database_url, SETTINGS.test_database_url):
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            identity = normalized_target_identity(candidate)
        except Exception:
            continue
        if identity is not None:
            names.append(identity[0])
    return tuple(names)


def _live_environment(
    dsn: str,
    migrations_dir: Path | None,
    scenario: Scenario,
) -> ProvisioningEnvironment:
    """Build the production environment; imports the live layer on purpose."""
    from src.db import open_connection  # lazy: imports config + driver
    from src.db_migrate import load_migrations  # lazy: imports config
    from src.db_migrate_executor import PsycopgMigrationExecutor  # lazy

    scripts = load_migrations(migrations_dir)
    checksum = scripts[0].checksum if scripts else ""

    def _open() -> Any:
        return open_connection(dsn)

    def _ensure() -> None:
        executor = PsycopgMigrationExecutor(dsn, connection_opener=lambda _dsn: _open())
        executor.ensure_ledger()

    return ProvisioningEnvironment(
        open_connection=_open,
        ensure_ledger=_ensure,
        shipped_checksum=checksum,
        migrations_dir=migrations_dir,
        dsn=dsn,
        target_database=scenario.database_name,
        forbidden_databases=_forbidden_databases(),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provision_negative_schema_cases.py",
        description=(
            "Provision one declared G8 S7 negative-schema scenario in its own "
            "dedicated database. Offline commands never touch a database."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list", help="print the declared scenario matrix (offline)"
    )
    list_parser.add_argument(
        "--matrix",
        choices=("all", "mandatory", "optional", "deferred"),
        default="all",
    )
    list_parser.add_argument("--json", action="store_true")

    databases_parser = subparsers.add_parser(
        "print-databases", help="print the dedicated database name per scenario (offline)"
    )
    databases_parser.add_argument(
        "--matrix",
        choices=("all", "mandatory", "optional", "deferred"),
        default="all",
    )

    run_parser = subparsers.add_parser(
        "run", help="apply one scenario to its dedicated database (live, opt-in)"
    )
    run_parser.add_argument("--scenario", required=True)
    run_parser.add_argument("--json", action="store_true")
    run_parser.add_argument(
        "--allow-live",
        action="store_true",
        help=(
            "required to open a connection; the DSN is read from "
            f"{DSN_ENV_VAR} and is never printed"
        ),
    )
    run_parser.add_argument("--migrations-dir", default=None)
    run_parser.add_argument(
        "--report-dir",
        default=None,
        help="write a JSON and a human-readable report into this directory",
    )

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help=(
            "drop only the objects declared in cleanup_manifest, back to the "
            "empty state (live, opt-in)"
        ),
    )
    cleanup_parser.add_argument("--scenario", required=True)
    cleanup_parser.add_argument("--json", action="store_true")
    cleanup_parser.add_argument(
        "--allow-live",
        action="store_true",
        help=(
            "required to open a connection; the DSN is read from "
            f"{DSN_ENV_VAR} and is never printed"
        ),
    )
    cleanup_parser.add_argument("--migrations-dir", default=None)
    cleanup_parser.add_argument("--report-dir", default=None)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: ProvisioningEnvironment | None = None,
) -> int:
    """Run the CLI; return the approved exit code.

    ``environment`` is the offline seam: when it is supplied, ``run`` never
    reads the environment variable, never imports the driver and never opens
    a live connection.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed the usage line
        return int(exc.code or 0)

    if args.command == "list":
        _print_list(args.matrix, as_json=args.json)
        return EXIT_READY
    if args.command == "print-databases":
        _print_databases(args.matrix)
        return EXIT_READY

    try:
        scenario = scenario_by_id(args.scenario)
    except KeyError:
        print(f"{_PREFIX}: unknown scenario {args.scenario}", file=sys.stderr)
        return EXIT_USAGE

    resolved = environment
    if resolved is None:
        if not args.allow_live:
            print(
                f"{_PREFIX}: live execution requires --allow-live "
                f"(and {DSN_ENV_VAR})",
                file=sys.stderr,
            )
            return EXIT_USAGE
        dsn = os.environ.get(DSN_ENV_VAR, "").strip()
        if not dsn:
            print(f"{_PREFIX}: {DSN_ENV_VAR} is not set", file=sys.stderr)
            return EXIT_USAGE
        decision = validate_target(
            dsn, scenario, forbidden_databases=_forbidden_databases()
        )
        if not decision.ok:
            print(f"{_PREFIX}: {decision.reason}", file=sys.stderr)
            return EXIT_USAGE
        migrations_dir = (
            Path(args.migrations_dir) if args.migrations_dir is not None else None
        )
        resolved = _live_environment(dsn, migrations_dir, scenario)

    if args.command == "cleanup":
        result = run_cleanup(scenario, resolved)
    else:
        result = run_scenario(scenario, resolved)

    _print_result(result, as_json=args.json)

    if args.report_dir is not None:
        try:
            write_report(Path(args.report_dir), result, scenario, timestamp=_utc_now())
        except OSError:
            # Never claim success without the requested artifact, and never
            # replace the run's own failure with the report failure.
            print(f"{_PREFIX}: report_write_failed", file=sys.stderr)
            return result.exit_code or EXIT_UNSAFE_STATE
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess tests
    raise SystemExit(main())
