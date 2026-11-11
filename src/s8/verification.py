"""S8 — offline implementation of the live schema/migration verification.

Implements `S8-CONTRACT 1.2` (``docs/S8_CONTRACT_V1_2.md``): a dedicated,
**no-lock**, strictly read-only verification of one owner-designated PostgreSQL
target against the approved database contract.

What it does
------------

1. Refuses without the explicit live-authorization flag (no DSN parsed, no
   connection attempted).
2. Positively proves the designated target **is** the configured application
   database: the application DSN must be configured and must normalize to the
   same ``(dbname, host, port)`` identity as the designated target. An explicit
   target may restate that identity; it can never select another database.
3. Opens at most one connection, to the exact designated DSN.
4. Establishes read-only **before any SQL verification read**: no-SQL metadata,
   ``autocommit``, session ``read_only`` requested and read back, exactly one
   ``BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY``, then the server-confirmed
   transaction properties (``transaction_read_only`` / ``transaction_isolation``).
5. Captures, in that one transaction: the connected identity fallback, the server
   version, the S2 catalog snapshot, the ledger evidence and the applied rows.
6. Rolls back, closes, and only then evaluates the immutable snapshots with the
   **existing** G2 and G8 authorities (``evaluate_migration_state``,
   ``evaluate_physical_schema``) plus the pinned digest/version assertions.
7. Renders a sanitized report.

What it never does
------------------

No advisory lock (and no substitute coordination lock), no
``verify_schema_locked``, no ledger creation, no migration, no write of any
kind, no ``information_schema`` read, no second connection, no retry. S1–S6 are
imported, never modified.

Ledger evidence (contract §8.5)
-------------------------------

Presence is decided from the **catalog** (``pg_catalog``, world-readable, so it
describes the database rather than the role's privileges), not from
``to_regclass``: an existing ledger the role cannot read must not be reported as
a missing ledger, and a wrong-kind relation must reach the S3 evaluator instead
of being filtered away.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from src.db_migrate import LEDGER_TABLE, MigrationSnapshot, load_migrations
from src.db_schema_catalog import (
    CatalogProbeError,
    CatalogSnapshot,
    read_public_catalog,
)
from src.db_target import normalized_target_identity
from src.physical_schema import (
    SCHEMA_OBJECT_PROHIBITED,
    CompatibilityStatus,
    SchemaEvaluation,
    evaluate_physical_schema,
)
from src.schema_contract import (
    CONTRACT_MIGRATION_VERSION,
    MIGRATION_SET_DIGEST,
    migration_set_digest,
)
from src.startup_schema import StartupSchemaError, evaluate_migration_state

__all__ = [
    "BLOCKED_AUTHORIZATION",
    "BLOCKED_DATABASE",
    "BLOCKED_READ_ONLY",
    "BLOCKED_TARGET",
    "CONCURRENCY_LIMITATION",
    "CONTRACT_VERSION",
    "FAIL_HISTORY",
    "FAIL_SCHEMA",
    "MIN_SERVER_VERSION_NUM",
    "PASS",
    "S8Request",
    "S8Report",
    "TOOL_ID",
    "verify_schema",
]

# ---------------------------------------------------------------------------
# Identity, vocabulary
# ---------------------------------------------------------------------------

TOOL_ID: Final[str] = "s8-schema-verification"
CONTRACT_VERSION: Final[str] = "S8-CONTRACT 1.2"

#: The seven approved outcome strings (byte-for-byte; no additions).
PASS: Final[str] = "PASS — VERIFIED"
FAIL_SCHEMA: Final[str] = "FAIL — SCHEMA MISMATCH"
FAIL_HISTORY: Final[str] = "FAIL — MIGRATION HISTORY INVALID"
BLOCKED_TARGET: Final[str] = "BLOCKED — TARGET NOT VERIFIED"
BLOCKED_AUTHORIZATION: Final[str] = "BLOCKED — AUTHORIZATION REQUIRED"
BLOCKED_DATABASE: Final[str] = "BLOCKED — DATABASE UNAVAILABLE"
BLOCKED_READ_ONLY: Final[str] = "BLOCKED — READ-ONLY GUARANTEE NOT ESTABLISHED"

#: Fixed limitation sentence — always present in the report, including on PASS.
CONCURRENCY_LIMITATION: Final[str] = (
    "REPEATABLE READ gives one consistent snapshot. It does not serialize this "
    "verification against a concurrent migration. A migration committed after "
    "this snapshot began may not be reflected in this report."
)

#: PostgreSQL 12 — the floor S2 requires (``attgenerated`` is 12+).
MIN_SERVER_VERSION_NUM: Final[int] = 120000

_READ_ONLY_READ_FAILED: Final[str] = (
    "the read-only transaction properties could not be confirmed"
)

_BEGIN_SQL: Final[str] = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
_ROLLBACK_SQL: Final[str] = "ROLLBACK"
_LEDGER_PRESENT_SQL: Final[str] = f"SELECT to_regclass('public.{LEDGER_TABLE}')"
_APPLIED_SQL: Final[str] = f"SELECT version, checksum FROM {LEDGER_TABLE}"
_CURRENT_DATABASE_SQL: Final[str] = "SELECT current_database()"
_SERVER_VERSION_NUM_SQL: Final[str] = "SHOW server_version_num"
_SERVER_VERSION_SQL: Final[str] = "SHOW server_version"
_READ_ONLY_SQL: Final[str] = "SHOW transaction_read_only"
_ISOLATION_SQL: Final[str] = "SHOW transaction_isolation"


# ---------------------------------------------------------------------------
# Request / report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class S8Request:
    """Everything the operator supplies for one verification run.

    ``authorized`` is the explicit live-execution gate; ``application_dsn`` is
    the configured application target and is mandatory; ``designated_dsn`` may
    restate it (never replace it); ``test_dsn`` is compared only.
    """

    authorized: bool = False
    application_dsn: str | None = None
    designated_dsn: str | None = None
    test_dsn: str | None = None
    migrations_dir: Path | None = None


@dataclass(frozen=True)
class S8Report:
    """Immutable, sanitized verification report."""

    outcome: str
    reasons: tuple[str, ...]
    checks: tuple[Mapping[str, Any], ...]
    evidence: Mapping[str, Any]
    cleanup: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return self.outcome == PASS

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the documented report shape (stable key order)."""
        document: dict[str, Any] = {
            "tool": TOOL_ID,
            "contract_version": CONTRACT_VERSION,
            "generated_at_utc": self.evidence.get("generated_at_utc"),
            "outcome": self.outcome,
            "reason_categories": list(self.reasons),
        }
        for block in (
            "target",
            "connection",
            "read_only",
            "server",
            "ledger",
            "g2",
            "digest",
            "g8",
        ):
            document[block] = dict(self.evidence.get(block, {}))
        document["checks"] = [dict(check) for check in self.checks]
        document["cleanup"] = dict(self.cleanup)
        document["concurrency_limitation"] = CONCURRENCY_LIMITATION
        return document


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _Interpreter:
    """Format a server-reported ``server_version_num`` value.

    Kept strict on purpose: the comparison must come from the integer GUC, never
    from the free-form ``server_version`` string.
    """

    @staticmethod
    def parse(value: object) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        text = str(value).strip()
        if not text.isascii() or not text.isdigit():
            return None
        return int(text)


def _rows(connection: Any, sql: str) -> Sequence[Sequence[Any]]:
    """Run one read-only statement and return all rows (no retries)."""
    return connection.execute(sql).fetchall()


def _first(connection: Any, sql: str) -> tuple[Any, ...] | None:
    row = connection.execute(sql).fetchone()
    if row is None:
        return None
    return tuple(row)


def _normalize(dsn: str) -> tuple[str, str, int] | None:
    """Normalized identity, or ``None`` when ambiguous (never an exception)."""
    try:
        return normalized_target_identity(dsn)
    except ValueError:  # MalformedDsnError is a ValueError
        raise _Malformed() from None


class _Malformed(Exception):
    """Internal: the DSN syntax was rejected (maps to a target refusal)."""


@dataclass
class _Context:
    """Mutable run state (never leaves this module)."""

    checks: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    transaction_open: bool = False

    def record(self, check_id: str, name: str, status: str) -> None:
        self.checks.append({"id": check_id, "name": name, "status": status})

    def refuse(self, check_id: str, name: str, reason: str) -> None:
        self.record(check_id, name, "failed")
        self.reasons.append(reason)


def _blank_evidence(now: datetime) -> dict[str, Any]:
    return {
        "generated_at_utc": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "target": {
            "label": None,
            "dbname": None,
            "host": None,
            "port": None,
            "application_target_configured": False,
            "designated_matches_application": None,
            "host_compared": False,
            "test_target_configured": False,
            "distinct_from_test_target": None,
        },
        "connection": {
            "attempts": 0,
            "opened": 0,
            "closed": None,
            "database": None,
            "role": None,
        },
        "read_only": {
            "session_requested": False,
            "session_confirmed": False,
            "transaction_declared": False,
            "transaction_read_only": None,
            "transaction_isolation": None,
        },
        "server": {
            "version_num": None,
            "version": None,
            "minimum_required": MIN_SERVER_VERSION_NUM,
            "satisfied": None,
        },
        "ledger": {
            "evidence": {
                "catalog_presence": "unavailable",
                "to_regclass": "not-read",
                "row_read": "not-attempted",
            },
            "classification": "unavailable",
        },
        "g2": {
            "status": "not_evaluated",
            "shipped_scripts": None,
            "shipped_versions": None,
            "ledger_present": None,
            "applied_versions": None,
            "pending": None,
            "codes": [],
        },
        "digest": {
            "evaluated": False,
            "skipped_because": None,
            "algorithm": "sha256",
            "payload": "V<version>:<checksum> joined by LF, no trailing newline",
            "pinned_digest": MIGRATION_SET_DIGEST,
            "live_matches_pinned": None,
            "shipped_matches_pinned": None,
            "contract_version": CONTRACT_MIGRATION_VERSION,
            "live_max_version": None,
            "max_matches_contract_version": None,
        },
        "g8": {"status": "not_evaluated", "codes": [], "findings": []},
    }


def _end_connection(connection: Any, ctx: _Context) -> dict[str, Any]:
    """Roll back (if a transaction is open) and close; never raises."""
    cleanup: dict[str, Any] = {
        "transaction_ended": "none",
        "connection_closed": None,
        "failed": False,
        "effect": "none",
        "notes": [],
    }
    if ctx.transaction_open:
        try:
            connection.execute(_ROLLBACK_SQL)
            cleanup["transaction_ended"] = "rollback"
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask
            cleanup["transaction_ended"] = "failed"
            cleanup["notes"].append(type(exc).__name__)
    try:
        connection.close()
        cleanup["connection_closed"] = True
    except Exception as exc:  # noqa: BLE001
        cleanup["connection_closed"] = False
        cleanup["notes"].append(type(exc).__name__)
    cleanup["failed"] = (
        cleanup["transaction_ended"] == "failed"
        or cleanup["connection_closed"] is not True
    )
    return cleanup


def _apply_cleanup(outcome: str, cleanup: Mapping[str, Any]) -> tuple[str, str]:
    """The single cleanup rule (contract §13.4).

    A cleanup failure never changes a definite FAIL, never converts PASS into
    FAIL, and always withholds PASS.
    """
    if not cleanup.get("failed"):
        return outcome, "none"
    if outcome.startswith("FAIL"):
        return outcome, "retained-definite-failure"
    if outcome == PASS:
        return BLOCKED_DATABASE, "withheld-pass"
    return outcome, "retained-definite-failure"


# ---------------------------------------------------------------------------
# Verification phases (all statements are read-only and inside the transaction)
# ---------------------------------------------------------------------------


def _establish_read_only(connection: Any, ctx: _Context, evidence: dict) -> str | None:
    """L1 + L2 + L3. Returns a refusal reason, or ``None`` on success."""
    try:
        connection.read_only = True
    except Exception:  # noqa: BLE001
        ctx.refuse("A9", "session-read-only", "READ_ONLY_SESSION_UNCONFIRMED")
        return "READ_ONLY_SESSION_UNCONFIRMED"
    evidence["read_only"]["session_requested"] = True
    if getattr(connection, "read_only", None) is not True:
        ctx.refuse("A9", "session-read-only", "READ_ONLY_SESSION_UNCONFIRMED")
        return "READ_ONLY_SESSION_UNCONFIRMED"
    evidence["read_only"]["session_confirmed"] = True
    ctx.record("A9", "session-read-only", "passed")

    try:
        connection.autocommit = True
        connection.execute(_BEGIN_SQL)
        ctx.transaction_open = True
    except Exception:  # noqa: BLE001
        ctx.refuse("A10", "transaction-declared", "READ_ONLY_TRANSACTION_UNCONFIRMED")
        return "READ_ONLY_TRANSACTION_UNCONFIRMED"
    evidence["read_only"]["transaction_declared"] = True
    ctx.record("A10", "transaction-declared", "passed")

    try:
        read_only_row = _first(connection, _READ_ONLY_SQL)
        isolation_row = _first(connection, _ISOLATION_SQL)
    except Exception:  # noqa: BLE001
        ctx.refuse("A11", "transaction-confirmed", _READ_ONLY_READ_FAILED)
        return "READ_ONLY_TRANSACTION_UNCONFIRMED"
    read_only_value = None if read_only_row is None else str(read_only_row[0]).strip().lower()
    isolation_value = None if isolation_row is None else str(isolation_row[0]).strip().lower()
    evidence["read_only"]["transaction_read_only"] = read_only_value
    evidence["read_only"]["transaction_isolation"] = isolation_value
    if read_only_value != "on" or isolation_value != "repeatable read":
        ctx.refuse("A11", "transaction-confirmed", "READ_ONLY_TRANSACTION_UNCONFIRMED")
        return "READ_ONLY_TRANSACTION_UNCONFIRMED"
    ctx.record("A11", "transaction-confirmed", "passed")
    return None


def _connected_identity(
    connection: Any, info: Any, ctx: _Context, evidence: dict
) -> str | None:
    """Connected dbname (SQL fallback allowed here — read-only is established)."""
    dbname = getattr(info, "dbname", None) if info is not None else None
    if not (isinstance(dbname, str) and dbname.strip()):
        try:
            row = _first(connection, _CURRENT_DATABASE_SQL)
        except Exception:  # noqa: BLE001
            ctx.refuse("A7", "connected-identity", "TARGET_IDENTITY_UNREADABLE")
            return None
        value = None if row is None else row[0]
        dbname = value if isinstance(value, str) and value.strip() else None
    if dbname is None:
        ctx.refuse("A7", "connected-identity", "TARGET_IDENTITY_UNREADABLE")
        return None
    evidence["connection"]["database"] = dbname
    return dbname


def _read_server_version(connection: Any, ctx: _Context, evidence: dict) -> int | None:
    try:
        num_row = _first(connection, _SERVER_VERSION_NUM_SQL)
    except Exception:  # noqa: BLE001
        ctx.refuse("A12", "server-version", "SERVER_VERSION_UNREADABLE")
        return None
    version_num = _Interpreter.parse(None if num_row is None else num_row[0])
    evidence["server"]["version_num"] = version_num
    try:
        text_row = _first(connection, _SERVER_VERSION_SQL)
        if text_row is not None:
            evidence["server"]["version"] = str(text_row[0])
    except Exception:  # noqa: BLE001 - informational only
        pass
    if version_num is None:
        ctx.refuse("A12", "server-version", "SERVER_VERSION_UNREADABLE")
        return None
    if version_num < MIN_SERVER_VERSION_NUM:
        ctx.refuse("A12", "server-version", "SERVER_VERSION_UNSUPPORTED")
        return None
    evidence["server"]["satisfied"] = True
    ctx.record("A12", "server-version", "passed")
    return version_num


def _classify_ledger(
    connection: Any,
    catalog: CatalogSnapshot,
    ctx: _Context,
    evidence: dict,
) -> tuple[str, bool, tuple[tuple[int, str], ...], str | None]:
    """Return ``(classification, ledger_present, applied_rows, refusal)``.

    Presence comes from catalog relation facts (privilege-independent).
    ``to_regclass`` corroborates only; the row read decides readability.
    """
    relations = catalog.relations_named(LEDGER_TABLE)
    ledger_block = evidence["ledger"]
    if not relations:
        ledger_block["evidence"]["catalog_presence"] = "absent"
        presence = "absent"
        ledger_present = False
    elif any(relation.is_table for relation in relations):
        ledger_block["evidence"]["catalog_presence"] = "present-table"
        presence = "present-table"
        ledger_present = True
    else:
        ledger_block["evidence"]["catalog_presence"] = "wrong-kind"
        presence = "wrong-kind"
        ledger_present = False

    try:
        regclass_row = _first(connection, _LEDGER_PRESENT_SQL)
        regclass_value = None if regclass_row is None else regclass_row[0]
        ledger_block["evidence"]["to_regclass"] = (
            "resolved" if regclass_value is not None else "null"
        )
    except Exception:  # noqa: BLE001 - corroboration only
        regclass_value = None
        ledger_block["evidence"]["to_regclass"] = "not-read"

    if presence == "absent":
        if ledger_block["evidence"]["to_regclass"] == "resolved":
            ledger_block["classification"] = "conflicting"
            ctx.refuse("A15", "ledger-evidence", "LEDGER_EVIDENCE_CONFLICTING")
            return ("conflicting", False, (), "VERIFICATION_INCOMPLETE")
        ledger_block["classification"] = "absent"
        ctx.record("A15", "ledger-evidence", "passed")
        return ("absent", False, (), None)

    if presence == "wrong-kind":
        # A view/sequence/materialized view named schema_version is a definite
        # physical defect, not a visibility problem: S3 judges it.
        ledger_block["classification"] = "wrong-kind"
        ctx.record("A15", "ledger-evidence", "passed")
        return ("wrong-kind", False, (), None)

    try:
        raw_rows = _rows(connection, _APPLIED_SQL)
    except Exception:  # noqa: BLE001
        ledger_block["evidence"]["row_read"] = "failed"
        ledger_block["classification"] = "unreadable"
        ctx.refuse("A16", "ledger-rows", "LEDGER_UNREADABLE")
        return ("unreadable", True, (), "LEDGER_UNREADABLE")

    applied = tuple((int(row[0]), str(row[1])) for row in raw_rows)
    ledger_block["evidence"]["row_read"] = "succeeded"
    if ledger_block["evidence"]["to_regclass"] == "null":
        ledger_block["classification"] = "conflicting"
        ctx.refuse("A15", "ledger-evidence", "LEDGER_EVIDENCE_CONFLICTING")
        return ("conflicting", True, applied, "VERIFICATION_INCOMPLETE")
    ledger_block["classification"] = "normal"
    ctx.record("A15", "ledger-evidence", "passed")
    ctx.record("A16", "ledger-rows", "passed")
    return ("normal", True, applied, None)


def _evaluate(
    *,
    ctx: _Context,
    evidence: dict,
    scripts: Sequence[Any],
    snapshot: MigrationSnapshot,
    catalog: CatalogSnapshot,
    classification: str,
    applied: tuple[tuple[int, str], ...],
) -> tuple[str, list[str]]:
    """Stage 2: G2, pinned assertions, G8 — from captured immutable snapshots."""
    reasons: list[str] = []
    g2_block = evidence["g2"]
    digest_block = evidence["digest"]
    g8_block = evidence["g8"]
    g2_block["shipped_scripts"] = len(scripts)
    g2_block["shipped_versions"] = [script.version for script in scripts]

    physical: SchemaEvaluation = evaluate_physical_schema(catalog)
    g8_block["status"] = physical.status.value
    g8_block["codes"] = list(physical.codes)
    g8_block["findings"] = [
        {
            "code": finding.code,
            "object": finding.label,
            "column": finding.column,
            "category": finding.category,
        }
        for finding in physical.findings
    ]
    marker = any(
        finding.code == SCHEMA_OBJECT_PROHIBITED and finding.category == "test_marker"
        for finding in physical.findings
    )

    if classification == "wrong-kind":
        # S3 owns the verdict; G2 is not evaluated against an unusable ledger.
        g2_block["status"] = "not_evaluated"
        digest_block["skipped_because"] = "LEDGER_WRONG_KIND"
        return FAIL_SCHEMA, ["SCHEMA_INCOMPATIBLE"]

    if marker:
        # The whole run inspected the wrong database.
        return BLOCKED_TARGET, ["TARGET_IS_TEST_DATABASE"]

    try:
        state = evaluate_migration_state(snapshot, None)
    except StartupSchemaError as exc:
        g2_block["status"] = "failed"
        g2_block["codes"] = [exc.code]
        digest_block["skipped_because"] = exc.code
        g2_block["ledger_present"] = snapshot.ledger_present
        g2_block["applied_versions"] = [version for version, _ in applied]
        return FAIL_HISTORY, [exc.code]

    g2_block["status"] = "passed"
    g2_block["ledger_present"] = state.ledger_present
    g2_block["applied_versions"] = [version for version, _ in state.applied]
    g2_block["pending"] = list(state.pending)

    # D2/D3 — expressed without max() over a possibly empty collection (§9.5.3).
    digest_block["evaluated"] = True
    if state.applied:
        live_digest = migration_set_digest(state.applied)
        digest_block["live_matches_pinned"] = live_digest == MIGRATION_SET_DIGEST
        live_max = max(version for version, _ in state.applied)
        digest_block["live_max_version"] = live_max
        digest_block["max_matches_contract_version"] = (
            live_max == CONTRACT_MIGRATION_VERSION
        )
    else:  # unreachable after a G2 pass; guarded anyway
        digest_block["live_matches_pinned"] = None
        digest_block["max_matches_contract_version"] = None

    shipped_pairs = tuple((script.version, script.checksum) for script in scripts)
    digest_block["shipped_matches_pinned"] = (
        bool(shipped_pairs)
        and migration_set_digest(shipped_pairs) == MIGRATION_SET_DIGEST
    )

    if digest_block["live_matches_pinned"] is False:
        reasons.append("DIGEST_MISMATCH")
    if digest_block["max_matches_contract_version"] is False:
        reasons.append("CONTRACT_VERSION_MISMATCH")
    if not digest_block["shipped_matches_pinned"]:
        reasons.append("CONTRACT_PIN_INCONSISTENT")
    if reasons:
        return FAIL_HISTORY, reasons

    if physical.status is CompatibilityStatus.COMPATIBLE and physical.findings:
        return BLOCKED_DATABASE, ["VERIFICATION_INCOMPLETE"]
    if physical.status is CompatibilityStatus.INCOMPATIBLE:
        return FAIL_SCHEMA, ["SCHEMA_INCOMPATIBLE"]
    if physical.status is not CompatibilityStatus.COMPATIBLE:
        return BLOCKED_DATABASE, ["SCHEMA_INDETERMINATE", "VERIFICATION_INCOMPLETE"]
    return PASS, []


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def verify_schema(
    request: S8Request,
    *,
    opener: Callable[[str], Any] | None = None,
    now: datetime | None = None,
) -> S8Report:
    """Run one verification and return an immutable sanitized report.

    Never raises for a verification outcome (including a refusal, a connection
    failure, a corrupt catalog or a cleanup fault): those are report values.
    Programming errors from the pure evaluators are reported as
    ``BLOCKED — DATABASE UNAVAILABLE`` / ``VERIFICATION_INCOMPLETE`` rather
    than escaping as tracebacks.
    """
    stamp = now if now is not None else datetime.now(UTC)
    ctx = _Context()
    evidence = _blank_evidence(stamp)
    target_block = evidence["target"]

    def report(outcome: str, reasons: Sequence[str], cleanup: Mapping[str, Any]) -> S8Report:
        final_outcome, effect = _apply_cleanup(outcome, cleanup)
        merged = dict(cleanup)
        merged["effect"] = effect
        final_reasons = list(reasons)
        if cleanup.get("failed") and "CLEANUP_FAILED" not in final_reasons:
            final_reasons.append("CLEANUP_FAILED")
        return S8Report(
            outcome=final_outcome,
            reasons=tuple(final_reasons),
            checks=tuple(ctx.checks),
            evidence=evidence,
            cleanup=merged,
        )

    no_cleanup = {"transaction_ended": "none", "connection_closed": None,
                  "failed": False, "effect": "none", "notes": []}

    # -- A1 authorization -------------------------------------------------
    if not request.authorized:
        ctx.refuse("A1", "authorization", "AUTHORIZATION_MISSING")
        return report(BLOCKED_AUTHORIZATION, ctx.reasons, no_cleanup)
    ctx.record("A1", "authorization", "passed")

    # -- A2/A3 application target ----------------------------------------
    application_dsn = request.application_dsn
    if not isinstance(application_dsn, str) or not application_dsn.strip():
        ctx.refuse("A2", "application-target", "APPLICATION_TARGET_UNCONFIGURED")
        return report(BLOCKED_TARGET, ctx.reasons, no_cleanup)
    try:
        application_identity = _normalize(application_dsn.strip())
    except _Malformed:
        ctx.refuse("A2", "application-target", "APPLICATION_TARGET_MALFORMED")
        return report(BLOCKED_TARGET, ctx.reasons, no_cleanup)
    if application_identity is None:
        ctx.refuse("A2", "application-target", "APPLICATION_TARGET_AMBIGUOUS")
        return report(BLOCKED_TARGET, ctx.reasons, no_cleanup)
    ctx.record("A2", "application-target", "passed")

    # -- A4 designated target --------------------------------------------
    designated_dsn = request.designated_dsn
    if designated_dsn is None or not str(designated_dsn).strip():
        designated_dsn = application_dsn.strip()
        target_block["label"] = "APPLICATION_DATABASE_URL"
    else:
        target_block["label"] = "explicit"
    try:
        designated_identity = _normalize(str(designated_dsn).strip())
    except _Malformed:
        ctx.refuse("A4", "designated-target", "TARGET_MALFORMED")
        return report(BLOCKED_TARGET, ctx.reasons, no_cleanup)
    if designated_identity is None:
        ctx.refuse("A4", "designated-target", "TARGET_AMBIGUOUS")
        return report(BLOCKED_TARGET, ctx.reasons, no_cleanup)
    if designated_identity != application_identity:
        ctx.refuse("A4", "designated-target", "TARGET_NOT_APPLICATION_TARGET")
        return report(BLOCKED_TARGET, ctx.reasons, no_cleanup)
    ctx.record("A4", "designated-target", "passed")

    target_block.update(
        {
            "dbname": designated_identity[0],
            "host": designated_identity[1],
            "port": designated_identity[2],
            "application_target_configured": True,
            "designated_matches_application": True,
            "host_compared": False,
            "test_target_configured": False,
            "distinct_from_test_target": None,
        }
    )

    # -- A5 test-target distinction --------------------------------------
    test_dsn = request.test_dsn
    if isinstance(test_dsn, str) and test_dsn.strip():
        target_block["test_target_configured"] = True
        try:
            test_identity = _normalize(test_dsn.strip())
        except _Malformed:
            ctx.refuse("A5", "test-target", "TEST_TARGET_UNREADABLE")
            return report(BLOCKED_TARGET, ctx.reasons, no_cleanup)
        if test_identity is None:
            ctx.refuse("A5", "test-target", "TEST_TARGET_UNREADABLE")
            return report(BLOCKED_TARGET, ctx.reasons, no_cleanup)
        if test_identity == designated_identity:
            target_block["distinct_from_test_target"] = False
            ctx.refuse("A5", "test-target", "TARGET_EQUALS_TEST_TARGET")
            return report(BLOCKED_TARGET, ctx.reasons, no_cleanup)
        target_block["distinct_from_test_target"] = True
    ctx.record("A5", "test-target", "passed")

    # -- A13 shipped migration set (filesystem only, before connecting) ---
    try:
        scripts = load_migrations(request.migrations_dir)
    except Exception:  # MigrationFileError and any other loader refusal
        ctx.refuse("A13", "shipped-migrations", "SHIPPED_MIGRATION_SET_INVALID")
        return report(FAIL_HISTORY, ctx.reasons, no_cleanup)
    if not scripts:
        ctx.refuse("A13", "shipped-migrations", "SHIPPED_MIGRATION_SET_EMPTY")
        return report(FAIL_HISTORY, ctx.reasons, no_cleanup)
    ctx.record("A13", "shipped-migrations", "passed")

    # -- A6 connect (exactly one attempt) --------------------------------
    open_connection = opener
    if open_connection is None:
        # The driver is imported only on the live path, so importing the
        # verifier stays free of psycopg (as the API's import hygiene requires).
        from src.db import open_connection as _open_connection

        open_connection = _open_connection

    evidence["connection"]["attempts"] = 1
    try:
        connection = open_connection(designated_dsn.strip())
    except Exception:  # noqa: BLE001 - availability is a report value
        ctx.refuse("A6", "single-connection", "CONNECTION_FAILED")
        return report(BLOCKED_DATABASE, ctx.reasons, no_cleanup)
    evidence["connection"]["opened"] = 1
    ctx.record("A6", "single-connection", "passed")

    info = getattr(connection, "info", None)
    outcome: str
    reason_list: list[str]
    try:
        refusal = _establish_read_only(connection, ctx, evidence)
        if refusal is not None:
            return report(BLOCKED_READ_ONLY, ctx.reasons, _end_connection(connection, ctx))

        dbname = _connected_identity(connection, info, ctx, evidence)
        if dbname is None:
            return report(BLOCKED_TARGET, ctx.reasons, _end_connection(connection, ctx))
        if dbname != designated_identity[0]:
            ctx.refuse("A7", "connected-identity", "TARGET_IDENTITY_MISMATCH")
            return report(BLOCKED_TARGET, ctx.reasons, _end_connection(connection, ctx))
        ctx.record("A7", "connected-identity", "passed")

        host = getattr(info, "host", None) if info is not None else None
        port = getattr(info, "port", None) if info is not None else None
        if isinstance(host, str) and host.strip():
            evidence["target"]["host_compared"] = True
            connected = _normalize_host(host, port, dbname)
            if connected is not None and connected != designated_identity:
                ctx.refuse("A8", "connected-address", "TARGET_IDENTITY_MISMATCH")
                return report(BLOCKED_TARGET, ctx.reasons, _end_connection(connection, ctx))
        ctx.record("A8", "connected-address", "passed")

        if _read_server_version(connection, ctx, evidence) is None:
            return report(BLOCKED_DATABASE, ctx.reasons, _end_connection(connection, ctx))

        try:
            catalog = read_public_catalog(connection)
        except CatalogProbeError:
            ctx.refuse("A14", "catalog", "CATALOG_UNREADABLE")
            return report(BLOCKED_DATABASE, ctx.reasons, _end_connection(connection, ctx))
        ctx.record("A14", "catalog", "passed")

        classification, ledger_present, applied, ledger_refusal = _classify_ledger(
            connection, catalog, ctx, evidence
        )
        if ledger_refusal is not None:
            reasons = list(ctx.reasons)
            return report(
                BLOCKED_DATABASE, reasons, _end_connection(connection, ctx)
            )

        tables = frozenset(
            relation.name for relation in catalog.relations if relation.is_table
        )
        snapshot = MigrationSnapshot(
            tables=tables, ledger_present=ledger_present, applied=applied
        )

        cleanup = _end_connection(connection, ctx)

        outcome, reason_list = _evaluate(
            ctx=ctx,
            evidence=evidence,
            scripts=scripts,
            snapshot=snapshot,
            catalog=catalog,
            classification=classification,
            applied=applied,
        )
        return report(outcome, reason_list, cleanup)
    except BaseException as exc:  # noqa: BLE001 - never leak a traceback
        cleanup = _end_connection(connection, ctx)
        ctx.reasons.append("VERIFICATION_INCOMPLETE")
        return report(BLOCKED_DATABASE, list(ctx.reasons), cleanup)


def _normalize_host(
    host: str, port: object, dbname: str
) -> tuple[str, str, int] | None:
    """Normalize driver-reported host/port with the shared identity rules.

    Uses the same collapsing as :func:`src.db_target.normalized_target_identity`
    by round-tripping through a synthesized DSN — the identity rules therefore
    cannot drift between the declared and the connected comparison.
    """
    port_text = "5432" if port is None else str(port).strip()
    if not port_text.isdigit():
        return None
    quoted_host = host.strip()
    if " " in quoted_host:
        quoted_host = f"'{quoted_host}'"
    try:
        return _normalize(f"host={quoted_host} port={port_text} dbname={dbname}")
    except _Malformed:
        return None
