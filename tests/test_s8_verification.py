"""Offline tests for the S8 verification implementation (S8-CONTRACT 1.2).

Every test drives the real verifier, the real S2 catalog probe, the real S3
evaluator, the real G2 rules and the real S1 contract against a **synthetic
connection**. No database is contacted, no DSN is read, no SQL is executed, no
advisory lock exists, and the live entry point is never run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.db_migrate import load_migrations
from src.s8.verification import (
    BLOCKED_AUTHORIZATION,
    BLOCKED_DATABASE,
    BLOCKED_READ_ONLY,
    BLOCKED_TARGET,
    CONCURRENCY_LIMITATION,
    CONTRACT_VERSION,
    FAIL_HISTORY,
    FAIL_SCHEMA,
    PASS,
    S8Request,
    verify_schema,
)
from src.schema_contract import (
    CONTRACT_MIGRATION_VERSION,
    REQUIRED_TABLES,
    ColumnDefault,
)

_APP = "postgresql://user:pw@localhost:5432/attendance"
_OTHER = "postgresql://user:pw@localhost:5432/other"
_TEST = "postgresql://user:pw@localhost:5432/attendance_test"


# ---------------------------------------------------------------------------
# Synthetic connections
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _catalog_rows(*, ledger_kind: str | None = "r", extra_relations=()):
    """Catalog rows matching the real S1 contract for a compatible schema."""
    relations = []
    columns = []
    constraints = []
    indexes = []
    for table in REQUIRED_TABLES:
        relations.append(("public", table.name, "r"))
        for ordinal, column in enumerate(table.columns, start=1):
            default = (
                "true"
                if column.default is ColumnDefault.TRUE
                else None
            )
            columns.append(
                (
                    "public",
                    table.name,
                    column.name,
                    column.pg_type,
                    not column.nullable,
                    default,
                    "",
                    "",
                    ordinal,
                )
            )
        constraints.append(
            (
                "public",
                table.name,
                f"{table.name}_pkey",
                "p",
                f"PRIMARY KEY ({', '.join(table.primary_key)})",
                tuple(table.primary_key),
            )
        )
        indexes.append(
            (
                "public",
                table.name,
                f"{table.name}_pkey",
                True,
                True,
                f"CREATE UNIQUE INDEX {table.name}_pkey ON public.{table.name} "
                f"USING btree ({', '.join(table.primary_key)})",
                tuple(table.primary_key),
            )
        )
    if ledger_kind is not None:
        for relation in relations:
            if relation[1] == "schema_version":
                relations.remove(relation)
                relations.append(("public", "schema_version", ledger_kind))
                break
    else:
        relations = [rel for rel in relations if rel[1] != "schema_version"]
        columns = [col for col in columns if col[1] != "schema_version"]
        constraints = [c for c in constraints if c[1] != "schema_version"]
        indexes = [i for i in indexes if i[1] != "schema_version"]
    relations.extend(extra_relations)
    return relations, columns, constraints, indexes


class FakeConnection:
    """Records statements and answers the S8 read set from canned facts."""

    def __init__(
        self,
        *,
        dbname: str = "attendance",
        info_dbname: str | None = "attendance",
        info_host: str | None = None,
        info_port: object = None,
        version_num: str = "160004",
        transaction_read_only: str = "on",
        transaction_isolation: str = "repeatable read",
        read_only_settable: bool = True,
        regclass: object = "oid-1",
        applied=None,
        applied_fails: bool = False,
        catalog=None,
        rollback_fails: bool = False,
        close_fails: bool = False,
    ) -> None:
        self.statements: list[str] = []
        self._dbname = dbname
        self._version_num = version_num
        self._transaction_read_only = transaction_read_only
        self._transaction_isolation = transaction_isolation
        self._read_only_settable = read_only_settable
        self._regclass = regclass
        self._applied = _real_applied() if applied is None else applied
        self._applied_fails = applied_fails
        self._rollback_fails = rollback_fails
        self._close_fails = close_fails
        self._read_only = None
        self.autocommit = False
        self.closed = False
        relations, columns, constraints, indexes = catalog or _catalog_rows()
        self._relations = relations
        self._columns = columns
        self._constraints = constraints
        self._indexes = indexes
        if info_dbname is None and info_host is None:
            self.info = None
        else:
            self.info = _Info(info_dbname, info_host, info_port)

    # psycopg's read_only is an attribute that can be requested and read back.
    @property
    def read_only(self):
        return self._read_only

    @read_only.setter
    def read_only(self, value):
        if not self._read_only_settable:
            raise RuntimeError("read-only mode unavailable")
        self._read_only = value

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.statements.append(text)
        lowered = text.lower()
        if lowered.startswith("begin isolation level repeatable read read only"):
            return _Cursor([])
        if lowered == "rollback":
            if self._rollback_fails:
                raise RuntimeError("rollback failed")
            return _Cursor([])
        if lowered == "show transaction_read_only":
            return _Cursor([(self._transaction_read_only,)])
        if lowered == "show transaction_isolation":
            return _Cursor([(self._transaction_isolation,)])
        if lowered == "show server_version_num":
            return _Cursor([(self._version_num,)])
        if lowered == "show server_version":
            return _Cursor([("16.4",)])
        if lowered == "select current_database()":
            return _Cursor([(self._dbname,)])
        if lowered == "select current_user, session_user":
            return _Cursor([("app_role", "app_role")])
        if "to_regclass" in lowered:
            return _Cursor([(self._regclass,)])
        if "from schema_version" in lowered:
            if self._applied_fails:
                raise RuntimeError("permission denied for table schema_version")
            return _Cursor(self._applied or [])
        # Order matters: the columns and indexes statements also mention
        # ``relkind``, and the constraint/index statements also mention
        # ``attname``, so the most specific marker has to be tested first.
        if "pg_index" in lowered:
            return _Cursor(self._indexes)
        if "pg_constraint" in lowered:
            return _Cursor(self._constraints)
        if "attname" in lowered:
            return _Cursor(self._columns)
        if "relkind" in lowered:
            return _Cursor(self._relations)
        raise AssertionError(f"unexpected statement: {text}")

    def close(self):
        self.closed = True
        if self._close_fails:
            raise RuntimeError("close failed")


class _Info:
    def __init__(self, dbname, host, port):
        self.dbname = dbname
        self.host = host
        self.port = port


def _real_applied():
    return [(script.version, script.checksum) for script in load_migrations()]


def _run(connection, **kwargs):
    return verify_schema(
        S8Request(
            authorized=kwargs.pop("authorized", True),
            application_dsn=kwargs.pop("application_dsn", _APP),
            designated_dsn=kwargs.pop("designated_dsn", None),
            test_dsn=kwargs.pop("test_dsn", None),
        ),
        opener=kwargs.pop("opener", lambda dsn: connection),
        now=datetime(2026, 11, 11, 12, 0, tzinfo=UTC),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Authorization and target identity
# ---------------------------------------------------------------------------


def test_unauthorized_run_refuses_before_parsing_or_connecting() -> None:
    calls: list[str] = []
    report = _run(
        FakeConnection(),
        authorized=False,
        opener=lambda dsn: calls.append(dsn) or FakeConnection(),
    )
    assert report.outcome == BLOCKED_AUTHORIZATION
    assert "AUTHORIZATION_MISSING" in report.reasons
    assert calls == []
    assert report.evidence["connection"]["attempts"] == 0
    assert report.evidence["connection"]["opened"] == 0


def test_missing_application_target_cannot_prove_identity() -> None:
    report = _run(FakeConnection(), application_dsn=None)
    assert report.outcome == BLOCKED_TARGET
    assert "APPLICATION_TARGET_UNCONFIGURED" in report.reasons
    assert report.evidence["connection"]["attempts"] == 0


def test_malformed_application_target_is_refused() -> None:
    report = _run(FakeConnection(), application_dsn="dbname=app hostaddr=10.0.0.1")
    assert report.outcome == BLOCKED_TARGET
    assert "APPLICATION_TARGET_AMBIGUOUS" in report.reasons


def test_explicit_target_may_not_select_another_database() -> None:
    report = _run(FakeConnection(), designated_dsn=_OTHER)
    assert report.outcome == BLOCKED_TARGET
    assert "TARGET_NOT_APPLICATION_TARGET" in report.reasons
    assert report.evidence["connection"]["attempts"] == 0


def test_explicit_target_restating_the_same_identity_is_accepted() -> None:
    report = _run(
        FakeConnection(),
        designated_dsn="postgresql://someone:else@127.0.0.1/attendance",
    )
    assert report.outcome == PASS
    assert report.evidence["target"]["label"] == "explicit"


def test_designated_equal_to_test_target_is_refused() -> None:
    report = _run(FakeConnection(), test_dsn=_APP)
    assert report.outcome == BLOCKED_TARGET
    assert "TARGET_EQUALS_TEST_TARGET" in report.reasons


def test_unreadable_test_target_is_refused_not_ignored() -> None:
    report = _run(FakeConnection(), test_dsn="service=phase3 dbname=app")
    assert report.outcome == BLOCKED_TARGET
    assert "TEST_TARGET_UNREADABLE" in report.reasons


def test_absent_test_target_is_not_identity_proof() -> None:
    """With no test DSN the application comparison still has to pass."""
    report = _run(FakeConnection(), designated_dsn=_OTHER, test_dsn=None)
    assert report.outcome == BLOCKED_TARGET
    assert report.evidence["target"]["test_target_configured"] is False


# ---------------------------------------------------------------------------
# Connection and read-only guarantees
# ---------------------------------------------------------------------------


def test_connection_failure_is_one_attempt_and_zero_opened() -> None:
    def opener(dsn):
        raise RuntimeError("could not connect")

    report = _run(FakeConnection(), opener=opener)
    assert report.outcome == BLOCKED_DATABASE
    assert "CONNECTION_FAILED" in report.reasons
    assert report.evidence["connection"]["attempts"] == 1
    assert report.evidence["connection"]["opened"] == 0


def test_unsettable_read_only_blocks_the_run() -> None:
    report = _run(FakeConnection(read_only_settable=False))
    assert report.outcome == BLOCKED_READ_ONLY
    assert report.evidence["read_only"]["session_confirmed"] is False


def test_transaction_not_read_only_blocks_the_run() -> None:
    connection = FakeConnection(transaction_read_only="off")
    report = _run(connection)
    assert report.outcome == BLOCKED_READ_ONLY
    assert "READ_ONLY_TRANSACTION_UNCONFIRMED" in report.reasons
    # Nothing beyond the transaction control and property reads ran.
    assert not any("schema_version" in s for s in connection.statements)


def test_isolation_level_must_be_repeatable_read() -> None:
    report = _run(FakeConnection(transaction_isolation="read committed"))
    assert report.outcome == BLOCKED_READ_ONLY


def test_read_only_is_confirmed_before_any_verification_read() -> None:
    connection = FakeConnection(applied=_real_applied())
    _run(connection)
    begin = connection.statements.index(
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
    )
    first_catalog = next(
        index for index, sql in enumerate(connection.statements) if "relkind" in sql
    )
    assert begin < first_catalog
    assert connection.statements.count(
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
    ) == 1


def test_connected_identity_mismatch_is_refused() -> None:
    report = _run(FakeConnection(dbname="other", info_dbname="other"))
    assert report.outcome == BLOCKED_TARGET
    assert "TARGET_IDENTITY_MISMATCH" in report.reasons


def test_identity_falls_back_to_a_read_only_query_after_l3() -> None:
    connection = FakeConnection(info_dbname=None, info_host=None, applied=_real_applied())
    report = _run(connection)
    assert report.outcome == PASS
    assert "SELECT current_database()" in connection.statements


def test_reported_host_mismatch_is_refused() -> None:
    report = _run(FakeConnection(info_host="10.9.9.9"))
    assert report.outcome == BLOCKED_TARGET
    assert "TARGET_IDENTITY_MISMATCH" in report.reasons


def test_no_host_reported_means_host_not_compared() -> None:
    report = _run(FakeConnection(info_host=None))
    assert report.outcome == PASS
    assert report.evidence["target"]["host_compared"] is False


# ---------------------------------------------------------------------------
# Server version
# ---------------------------------------------------------------------------


def test_server_below_twelve_is_unsupported() -> None:
    report = _run(FakeConnection(version_num="110015"))
    assert report.outcome == BLOCKED_DATABASE
    assert "SERVER_VERSION_UNSUPPORTED" in report.reasons
    assert report.evidence["server"]["satisfied"] is None


def test_unreadable_server_version_never_passes() -> None:
    report = _run(FakeConnection(version_num="sixteen"))
    assert report.outcome == BLOCKED_DATABASE
    assert "SERVER_VERSION_UNREADABLE" in report.reasons


def test_twelve_exactly_is_accepted() -> None:
    report = _run(FakeConnection(version_num="120000"))
    assert report.outcome == PASS


# ---------------------------------------------------------------------------
# Ledger evidence classification
# ---------------------------------------------------------------------------


def test_genuinely_absent_ledger_is_a_migration_history_failure() -> None:
    catalog = _catalog_rows(ledger_kind=None)
    report = _run(FakeConnection(regclass=None, catalog=catalog))
    assert report.outcome == FAIL_HISTORY
    # Absent, not wrong-kind: the ledger really is missing from the catalog.
    assert report.evidence["ledger"]["classification"] == "absent"
    assert report.evidence["ledger"]["evidence"]["catalog_presence"] == "absent"
    assert report.evidence["g2"]["status"] == "failed"
    assert report.evidence["g2"]["codes"]
    assert report.evidence["digest"]["evaluated"] is False


def test_existing_but_unreadable_ledger_is_blocked_not_failed() -> None:
    report = _run(FakeConnection(applied_fails=True))
    assert report.outcome == BLOCKED_DATABASE
    assert "LEDGER_UNREADABLE" in report.reasons
    assert report.evidence["ledger"]["classification"] == "unreadable"
    assert report.evidence["g2"]["status"] == "not_evaluated"


def test_catalog_and_regclass_disagreeing_about_presence_is_blocked() -> None:
    report = _run(FakeConnection(regclass=None, applied=_real_applied()))
    assert report.outcome == BLOCKED_DATABASE
    assert "LEDGER_EVIDENCE_CONFLICTING" in report.reasons
    assert report.evidence["ledger"]["classification"] == "conflicting"


def test_wrong_relation_kind_reaches_schema_mismatch() -> None:
    catalog = _catalog_rows(ledger_kind="v", extra_relations=())
    report = _run(
        FakeConnection(catalog=catalog, applied=_real_applied()),
    )
    assert report.outcome == FAIL_SCHEMA
    assert report.evidence["ledger"]["classification"] == "wrong-kind"
    assert "SCHEMA_TABLE_KIND_MISMATCH" in report.evidence["g8"]["codes"]


def test_absent_relation_and_wrong_kind_stay_distinguishable() -> None:
    absent = _run(FakeConnection(regclass=None, catalog=_catalog_rows(ledger_kind=None)))
    wrong = _run(FakeConnection(catalog=_catalog_rows(ledger_kind="S")))
    assert absent.evidence["ledger"]["classification"] == "absent"
    assert wrong.evidence["ledger"]["classification"] == "wrong-kind"
    assert absent.outcome == FAIL_HISTORY
    assert wrong.outcome == FAIL_SCHEMA


# ---------------------------------------------------------------------------
# Success path, digest and contract-version assertions
# ---------------------------------------------------------------------------


def test_healthy_schema_and_ledger_pass() -> None:
    connection = FakeConnection(applied=_real_applied())
    report = _run(connection)
    assert report.outcome == PASS
    assert report.passed is True
    assert report.reasons == ()
    assert report.evidence["g2"]["status"] == "passed"
    assert report.evidence["digest"]["evaluated"] is True
    assert report.evidence["digest"]["live_matches_pinned"] is True
    assert report.evidence["digest"]["shipped_matches_pinned"] is True
    assert report.evidence["digest"]["live_max_version"] == CONTRACT_MIGRATION_VERSION
    assert report.evidence["g8"]["status"] == "compatible"
    assert connection.closed is True


def test_digest_mismatch_fails_migration_history() -> None:
    tampered = [(version, "0" * 64) for version, _ in _real_applied()]
    report = _run(FakeConnection(applied=tampered))
    assert report.outcome == FAIL_HISTORY
    assert "MIGRATION_CHECKSUM_MISMATCH" in report.reasons


def test_pinned_contract_version_equals_the_shipped_set() -> None:
    """D3 offline: the shipped max version really is the pinned constant."""
    applied = _real_applied()
    report = _run(FakeConnection(applied=applied))
    assert report.evidence["digest"]["max_matches_contract_version"] is True
    assert max(version for version, _ in applied) == CONTRACT_MIGRATION_VERSION
    assert report.outcome == PASS


def test_empty_applied_ledger_never_calls_max() -> None:
    """An empty ledger is a G2 refusal, never an empty-sequence crash."""
    report = _run(FakeConnection(applied=[]))
    assert report.outcome == FAIL_HISTORY
    assert report.evidence["g2"]["status"] == "failed"
    assert report.evidence["g2"]["codes"]
    assert report.evidence["digest"]["evaluated"] is False
    assert report.evidence["digest"]["skipped_because"] == (
        report.evidence["g2"]["codes"][0]
    )
    assert report.evidence["digest"]["live_max_version"] is None


# ---------------------------------------------------------------------------
# G8 precedence and cleanup
# ---------------------------------------------------------------------------


def test_test_marker_outranks_every_other_finding() -> None:
    catalog = _catalog_rows(
        extra_relations=[("public", "_phase3_test_guard", "r")]
    )
    report = _run(FakeConnection(catalog=catalog, applied=_real_applied()))
    assert report.outcome == BLOCKED_TARGET
    assert "TARGET_IS_TEST_DATABASE" in report.reasons


def test_missing_employee_table_fails_schema() -> None:
    relations, columns, constraints, indexes = _catalog_rows()
    relations = [rel for rel in relations if rel[1] != "employee"]
    report = _run(
        FakeConnection(
            catalog=(relations, columns, constraints, indexes),
            applied=_real_applied(),
        )
    )
    assert report.outcome == FAIL_SCHEMA
    assert "SCHEMA_TABLE_MISSING" in report.evidence["g8"]["codes"]


def test_cleanup_failure_withholds_pass() -> None:
    report = _run(FakeConnection(applied=_real_applied(), close_fails=True))
    assert report.outcome == BLOCKED_DATABASE
    assert "CLEANUP_FAILED" in report.reasons
    assert report.cleanup["effect"] == "withheld-pass"
    assert report.cleanup["connection_closed"] is False


def test_cleanup_failure_never_masks_a_definite_failure() -> None:
    tampered = [(version, "0" * 64) for version, _ in _real_applied()]
    report = _run(FakeConnection(applied=tampered, rollback_fails=True))
    assert report.outcome == FAIL_HISTORY
    assert report.cleanup["effect"] == "retained-definite-failure"
    assert "CLEANUP_FAILED" in report.reasons


def test_rollback_is_issued_and_connection_closed() -> None:
    connection = FakeConnection(applied=_real_applied())
    _run(connection)
    assert "ROLLBACK" in connection.statements
    assert connection.closed is True


# ---------------------------------------------------------------------------
# Report contract
# ---------------------------------------------------------------------------


def test_report_is_sanitized_and_carries_the_limitation() -> None:
    report = _run(FakeConnection(applied=_real_applied()))
    document = report.to_dict()
    encoded = json.dumps(document)
    for secret in ("pw", "postgresql://", "app_role", "password"):
        assert secret not in encoded
    assert document["tool"] == "s8-schema-verification"
    assert document["contract_version"] == CONTRACT_VERSION
    assert document["concurrency_limitation"] == CONCURRENCY_LIMITATION
    assert document["outcome"] == PASS


def test_blocked_report_still_carries_every_required_block() -> None:
    document = _run(FakeConnection(), authorized=False).to_dict()
    for block in (
        "tool",
        "contract_version",
        "generated_at_utc",
        "outcome",
        "reason_categories",
        "target",
        "connection",
        "read_only",
        "server",
        "ledger",
        "g2",
        "digest",
        "g8",
        "checks",
        "cleanup",
        "concurrency_limitation",
    ):
        assert block in document
    assert document["concurrency_limitation"] == CONCURRENCY_LIMITATION


def test_no_advisory_lock_or_forbidden_statement_is_ever_issued() -> None:
    connection = FakeConnection(applied=_real_applied())
    _run(connection)
    joined = " ".join(connection.statements).lower()
    for forbidden in (
        "advisory",
        "lock table",
        "for update",
        "for share",
        "insert into",
        "delete from",
        "create table",
        "alter table",
        "drop table",
        "information_schema",
        "pg_advisory",
        "set transaction",
    ):
        assert forbidden not in joined, f"forbidden statement issued: {forbidden}"


def test_only_one_connection_is_opened_and_closed() -> None:
    opened: list[FakeConnection] = []

    def opener(dsn):
        connection = FakeConnection(applied=_real_applied())
        opened.append(connection)
        return connection

    report = _run(None, opener=opener)
    assert report.outcome == PASS
    assert len(opened) == 1
    assert opened[0].closed is True
    accounting = report.evidence["connection"]
    assert accounting["attempts"] == 1
    assert accounting["opened"] == 1
    assert accounting["database"] == "attendance"
    # No role name is ever captured: C5 forbids role names in reports.
    assert accounting["role"] is None


def test_shipped_migration_contract_is_internally_consistent() -> None:
    """D4: the shipped set really is the pinned one (offline self-check)."""
    report = _run(FakeConnection(applied=_real_applied()))
    assert report.evidence["digest"]["shipped_matches_pinned"] is True
    assert report.evidence["g2"]["shipped_versions"] == [
        script.version for script in load_migrations()
    ]
