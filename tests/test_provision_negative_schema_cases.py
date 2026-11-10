"""Offline tests for the S7 Milestone C provisioning lifecycle and CLI.

No test here opens a connection, reads a DSN, or imports the driver through
the tool's own import graph. Every connection is a recording fake, so the
rollback/commit/close counts are direct evidence rather than inference.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import replace as dc_replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import tools.provision_negative_schema_cases as tool
import tools.negative_schema_scenarios as s7
from src.db_migrate import MigrationIntegrityError
from src.db_migrate import load_migrations
from src.db_schema_catalog import CatalogProbeError, CatalogSnapshot, RelationFact

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = PROJECT_ROOT / "tools" / "provision_negative_schema_cases.py"
SHIPPED = {script.version: script.checksum for script in load_migrations()}[1]

_BASELINE_FACTS = s7.MigrationFacts(
    ledger_present=True,
    applied=((1, SHIPPED),),
    tables=("employee", "schema_version"),
)


def _baseline_catalog() -> CatalogSnapshot:
    return s7.declared_snapshot(s7.BASELINE_STATE, declared_by="test")


def _empty_catalog() -> CatalogSnapshot:
    return CatalogSnapshot(relations=(), columns=(), constraints=(), indexes=())


class _Rows:
    """Minimal cursor fake: an optional single row plus an empty result set."""

    def __init__(self, rows=()) -> None:
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)


class FakeConnection:
    """Recording psycopg-shaped connection with injectable failures."""

    def __init__(
        self,
        name: str = "conn",
        *,
        events: list[str] | None = None,
        commit_error: bool = False,
        rollback_error: bool = False,
        close_error: bool = False,
        fail_statement: str | None = None,
        identity: tuple | None = None,
    ) -> None:
        self.name = name
        self.identity = identity
        self.autocommit: bool | None = None
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self.events = events if events is not None else []
        self._commit_error = commit_error
        self._rollback_error = rollback_error
        self._close_error = close_error
        self._fail_statement = fail_statement

    # -- psycopg surface ---------------------------------------------------
    def execute(self, sql: str, params: object = None) -> _Rows:
        self.statements.append(sql)
        if self._fail_statement is not None and self._fail_statement in sql:
            raise RuntimeError("fake statement failure")
        if "current_database" in sql and self.identity is not None:
            return _Rows([self.identity])
        return _Rows()

    def commit(self) -> None:
        self.commits += 1
        self.events.append(f"{self.name}:commit")
        if self._commit_error:
            raise RuntimeError("fake commit failure")

    def rollback(self) -> None:
        self.rollbacks += 1
        self.events.append(f"{self.name}:rollback")
        if self._rollback_error:
            raise RuntimeError("fake rollback failure")

    def close(self) -> None:
        self.closed += 1
        self.events.append(f"{self.name}:close")
        if self._close_error:
            raise RuntimeError("fake close failure")


class Opener:
    """Hands out pre-built connections in order and records the order."""

    def __init__(self, *connections: FakeConnection, events: list[str] | None = None):
        self._queue = list(connections)
        self.opened: list[FakeConnection] = []
        self.events = events if events is not None else []

    def __call__(self) -> FakeConnection:
        if not self._queue:
            raise AssertionError("the tool opened more connections than declared")
        connection = self._queue.pop(0)
        self.opened.append(connection)
        self.events.append(f"open:{connection.name}")
        return connection


def _environment(
    opener: Opener,
    *,
    catalog: CatalogSnapshot | None = None,
    facts: s7.MigrationFacts | None = None,
    probe=None,
    reader=None,
    ensure=None,
    shipped: str = SHIPPED,
) -> tool.ProvisioningEnvironment:
    """Build an environment whose probe/reader ignore connection identity."""
    return tool.ProvisioningEnvironment(
        open_connection=opener,
        catalog_probe=probe or (lambda connection: catalog),
        facts_reader=reader or (lambda connection: facts),
        ensure_ledger=ensure,
        shipped_checksum=shipped,
    )


def _run(scenario_id: str, environment) -> tool.ScenarioResult:
    return tool.run_scenario(s7.scenario_by_id(scenario_id), environment)


# ---------------------------------------------------------------------------
# Happy path: commit only after a complete match, then re-verify separately
# ---------------------------------------------------------------------------


def test_migrate_scenario_commits_only_after_full_match() -> None:
    events: list[str] = []
    defect = FakeConnection("defect", events=events)
    verify = FakeConnection("verify", events=events)
    opener = Opener(defect, verify, events=events)

    result = _run(
        "N00",
        _environment(opener, catalog=_baseline_catalog(), facts=_BASELINE_FACTS),
    )

    assert result.outcome is tool.Outcome.READY
    assert result.exit_code == tool.EXIT_READY == 0
    assert result.verified is True
    assert result.committed is True
    assert defect.commits == 1
    assert defect.rollbacks == 0
    assert defect.closed == 1
    assert verify.commits == 0
    assert verify.rollbacks == 0
    assert verify.closed == 1
    assert opener.opened == [defect, verify]
    # The separate verification connection is opened only after the commit.
    assert events.index("defect:commit") < events.index("open:verify")
    # In-transaction and post-commit reports agree, and both live in the result.
    assert result.in_transaction is not None and result.in_transaction.matched
    assert result.post_commit is not None and result.post_commit.matched


def test_guards_and_defect_statements_run_in_the_declared_order() -> None:
    events: list[str] = []
    defect = FakeConnection("defect", events=events)
    verify = FakeConnection("verify", events=events)
    opener = Opener(defect, verify, events=events)
    scenario = s7.scenario_by_id("N01")

    result = _run(
        "N01",
        _environment(opener, catalog=_baseline_catalog(), facts=_BASELINE_FACTS),
    )

    assert result.statements_executed == len(scenario.statements)
    assert defect.autocommit is False
    assert defect.statements[: len(tool.SESSION_GUARDS)] == list(tool.SESSION_GUARDS)
    assert list(defect.statements[len(tool.SESSION_GUARDS):]) == list(scenario.statements)
    assert result.guards_applied is True


def test_a_committed_g8_defect_reports_the_declared_codes_and_status() -> None:
    scenario = s7.scenario_by_id("N09")
    catalog = s7.declared_snapshot(scenario.expected_object_state, declared_by="N09")
    facts = s7.MigrationFacts(
        ledger_present=True,
        applied=((1, SHIPPED),),
        tables=("attendance_event", "employee", "schema_version"),
    )
    defect = FakeConnection("defect")
    verify = FakeConnection("verify")

    result = _run(
        "N09",
        _environment(Opener(defect, verify), catalog=catalog, facts=facts),
    )

    assert result.outcome is tool.Outcome.READY
    assert result.in_transaction is not None
    assert result.in_transaction.codes == ("SCHEMA_OBJECT_PROHIBITED",)
    assert result.in_transaction.status == "incompatible"
    assert defect.commits == 1


def test_an_indeterminate_g8_defect_is_not_silently_accepted() -> None:
    scenario = s7.scenario_by_id("N12")
    catalog = s7.declared_snapshot(scenario.expected_object_state, declared_by="N12")
    defect = FakeConnection("defect")

    result = _run(
        "N12",
        _environment(
            Opener(defect, FakeConnection("verify")),
            catalog=catalog,
            facts=_BASELINE_FACTS,
        ),
    )

    assert result.outcome is tool.Outcome.READY
    assert result.in_transaction is not None
    assert result.in_transaction.status == "indeterminate"
    assert result.in_transaction.codes == ("SCHEMA_TABLE_KIND_UNCONFIRMED",)


def test_the_shipped_checksum_is_required_for_the_declared_history() -> None:
    """An unset shipped checksum can never satisfy a ``<shipped>`` declaration."""
    defect = FakeConnection("defect")
    result = _run(
        "N00",
        _environment(
            Opener(defect), catalog=_baseline_catalog(), facts=_BASELINE_FACTS,
            shipped="0" * 64,
        ),
    )

    assert result.outcome is tool.Outcome.EXPECTED_MISMATCH
    assert result.committed is False
    assert defect.commits == 0
    assert defect.rollbacks == 1


# ---------------------------------------------------------------------------
# Mismatch and rollback
# ---------------------------------------------------------------------------


def test_mismatch_rolls_back_exactly_once_and_never_commits() -> None:
    defect = FakeConnection("defect")
    result = _run(
        "N01",
        _environment(
            Opener(defect), catalog=_baseline_catalog(), facts=_BASELINE_FACTS
        ),
    )

    assert result.outcome is tool.Outcome.EXPECTED_MISMATCH
    assert result.exit_code == tool.EXIT_EXPECTED_MISMATCH == 5
    assert result.committed is False
    assert defect.commits == 0
    assert defect.rollbacks == 1
    assert defect.closed == 1
    assert "expected_state_mismatch" in result.diagnostics
    assert result.in_transaction is not None
    assert "relations[employee]" in result.in_transaction.descriptors


def test_mismatch_result_carries_only_identifiers() -> None:
    defect = FakeConnection("defect")
    result = _run(
        "N01",
        _environment(
            Opener(defect), catalog=_baseline_catalog(), facts=_BASELINE_FACTS
        ),
    )
    payload = json.dumps(result.as_dict())

    assert "DROP TABLE" not in payload
    assert "postgresql://" not in payload
    assert "employee" in payload  # the identifier is the diagnostic, not the SQL


# ---------------------------------------------------------------------------
# Failure handling: no failure may ever be reported as ready
# ---------------------------------------------------------------------------


def test_commit_failure_is_unconfirmed_and_never_ready() -> None:
    opener = Opener(FakeConnection("defect", commit_error=True))
    result = _run(
        "N00",
        _environment(opener, catalog=_baseline_catalog(), facts=_BASELINE_FACTS),
    )

    assert result.outcome is tool.Outcome.OUTCOME_UNCONFIRMED
    assert result.exit_code == tool.EXIT_OUTCOME_UNCONFIRMED == 7
    assert result.verified is False
    assert result.committed is None
    assert "commit_unconfirmed" in result.diagnostics
    # No rollback is attempted after an uncertain commit, and no second
    # connection is opened for a verification that could only mislead.
    assert opener.opened[0].rollbacks == 0
    assert len(opener.opened) == 1
    assert opener.opened[0].closed == 1


def test_rollback_failure_never_masks_the_mismatch() -> None:
    defect = FakeConnection("defect", rollback_error=True)
    result = _run(
        "N01",
        _environment(
            Opener(defect), catalog=_baseline_catalog(), facts=_BASELINE_FACTS
        ),
    )

    assert result.outcome is tool.Outcome.UNSAFE_STATE
    assert result.exit_code == tool.EXIT_UNSAFE_STATE == 3
    assert result.verified is False
    assert "expected_state_mismatch" in result.diagnostics
    assert "rollback_failed" in result.cleanup_errors
    assert defect.rollbacks == 1


def test_close_failure_after_commit_is_unsafe_not_ready() -> None:
    defect = FakeConnection("defect", close_error=True)
    opener = Opener(defect, FakeConnection("verify"))
    result = _run(
        "N00",
        _environment(opener, catalog=_baseline_catalog(), facts=_BASELINE_FACTS),
    )

    assert result.outcome is tool.Outcome.UNSAFE_STATE
    assert result.verified is False
    assert result.committed is True
    assert "committed_but_close_failed" in result.diagnostics
    assert "close_failed" in result.cleanup_errors
    assert len(opener.opened) == 1  # cannot claim a state it did not re-read


def test_close_failure_on_the_mismatch_path_is_unsafe() -> None:
    defect = FakeConnection("defect", close_error=True)
    result = _run(
        "N01",
        _environment(
            Opener(defect), catalog=_baseline_catalog(), facts=_BASELINE_FACTS
        ),
    )

    assert result.outcome is tool.Outcome.UNSAFE_STATE
    assert result.committed is False
    assert defect.commits == 0
    assert defect.rollbacks == 1
    assert "close_failed" in result.cleanup_errors


def test_defect_statement_failure_rolls_back_once_and_is_unsafe() -> None:
    defect = FakeConnection("defect", fail_statement="DROP TABLE")
    result = _run(
        "N01",
        _environment(
            Opener(defect), catalog=_baseline_catalog(), facts=_BASELINE_FACTS
        ),
    )

    assert result.outcome is tool.Outcome.UNSAFE_STATE
    assert result.verified is False
    assert result.committed is False
    assert result.statements_executed == 0
    assert "defect_statement_failed" in result.diagnostics
    assert defect.rollbacks == 1
    assert defect.commits == 0
    assert defect.closed == 1


def test_guard_failure_rolls_back_once_and_is_unsafe() -> None:
    defect = FakeConnection("defect", fail_statement="SET LOCAL")
    result = _run(
        "N00",
        _environment(
            Opener(defect), catalog=_baseline_catalog(), facts=_BASELINE_FACTS
        ),
    )

    assert result.outcome is tool.Outcome.UNSAFE_STATE
    assert result.guards_applied is False
    assert "guard_failed" in result.diagnostics
    assert defect.rollbacks == 1
    assert defect.commits == 0


def test_catalog_probe_failure_is_unavailable_and_rolled_back() -> None:
    defect = FakeConnection("defect")

    def _probe(connection):
        raise CatalogProbeError("fake catalog failure")

    result = _run(
        "N00",
        _environment(Opener(defect), probe=_probe, facts=_BASELINE_FACTS),
    )

    assert result.outcome is tool.Outcome.UNAVAILABLE
    assert result.exit_code == tool.EXIT_UNAVAILABLE == 4
    assert result.verified is False
    assert "catalog_unreadable" in result.diagnostics
    assert defect.rollbacks == 1
    assert defect.commits == 0
    assert defect.closed == 1


def test_unexpected_probe_failure_is_not_treated_as_an_empty_catalog() -> None:
    defect = FakeConnection("defect")

    def _probe(connection):
        raise RuntimeError("driver text that must never surface")

    result = _run(
        "N00",
        _environment(Opener(defect), probe=_probe, facts=_BASELINE_FACTS),
    )

    assert result.outcome is tool.Outcome.UNAVAILABLE
    assert "catalog_unreadable" in result.diagnostics
    assert "driver text" not in json.dumps(result.as_dict())


def test_migration_facts_read_failure_is_unavailable_and_rolled_back() -> None:
    defect = FakeConnection("defect")

    def _reader(connection):
        raise RuntimeError("fake ledger failure")

    result = _run(
        "N00",
        _environment(Opener(defect), catalog=_baseline_catalog(), reader=_reader),
    )

    assert result.outcome is tool.Outcome.UNAVAILABLE
    assert "migration_state_unreadable" in result.diagnostics
    assert defect.rollbacks == 1
    assert defect.commits == 0


def test_post_commit_verification_mismatch_is_exit_six() -> None:
    defect = FakeConnection("defect")
    verify = FakeConnection("verify")
    scenario = s7.scenario_by_id("N01")
    # The defect connection observes the declared N01 state (database only
    # tables, so the ledger is the sole relation); the separate verification
    # connection then observes a different, wrong state.
    declared_facts = s7.MigrationFacts(
        ledger_present=True,
        applied=((1, SHIPPED),),
        tables=("schema_version",),
    )

    def _probe(connection):
        if connection is defect:
            return s7.declared_snapshot(
                scenario.expected_object_state, declared_by="N01"
            )
        return _baseline_catalog()

    result = _run(
        "N01",
        _environment(
            Opener(defect, verify), probe=_probe, facts=declared_facts
        ),
    )

    assert result.outcome is tool.Outcome.POST_COMMIT_MISMATCH
    assert result.exit_code == tool.EXIT_POST_COMMIT_MISMATCH == 6
    assert result.committed is True
    assert result.verified is False
    assert result.post_commit is not None and not result.post_commit.matched
    assert defect.commits == 1


def test_post_commit_read_failure_is_unsafe_not_ready() -> None:
    defect = FakeConnection("defect")
    verify = FakeConnection("verify")

    def _probe(connection):
        if connection is verify:
            raise CatalogProbeError("fake post-commit failure")
        return _baseline_catalog()

    result = _run(
        "N00",
        _environment(Opener(defect, verify), probe=_probe, facts=_BASELINE_FACTS),
    )

    assert result.outcome is tool.Outcome.UNSAFE_STATE
    assert result.committed is True
    assert result.verified is False
    assert "post_commit_unreadable" in result.diagnostics
    assert verify.closed == 1


def test_verification_connection_is_read_only_and_never_commits() -> None:
    defect = FakeConnection("defect")
    verify = FakeConnection("verify")
    _run(
        "N00",
        _environment(
            Opener(defect, verify), catalog=_baseline_catalog(), facts=_BASELINE_FACTS
        ),
    )

    assert verify.autocommit is True
    assert verify.statements[0] == tool._VERIFY_BEGIN
    assert verify.statements[-1] == tool._VERIFY_END
    assert verify.commits == 0
    assert verify.rollbacks == 0
    written = [
        statement
        for statement in verify.statements[1:-1]
        if statement.strip().split(" ", 1)[0].upper()
        in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRUNCATE"}
    ]
    assert written == []


# ---------------------------------------------------------------------------
# G01 (no baseline) and G04b (ledger only)
# ---------------------------------------------------------------------------


def test_g01_baseline_none_opens_no_defect_transaction() -> None:
    read_only = FakeConnection("read_only")
    result = _run(
        "G01",
        _environment(
            Opener(read_only),
            catalog=_empty_catalog(),
            facts=s7.MigrationFacts(ledger_present=False, applied=(), tables=()),
        ),
    )

    assert result.outcome is tool.Outcome.READY
    assert result.committed is False
    assert result.guards_applied is False
    assert read_only.commits == 0
    assert read_only.rollbacks == 0
    assert read_only.statements[0] == tool._VERIFY_BEGIN
    assert result.in_transaction is not None
    assert result.in_transaction.gate_code == "MIGRATION_LEDGER_MISSING"


def test_g01_refuses_a_database_that_is_not_empty() -> None:
    read_only = FakeConnection("read_only")
    result = _run(
        "G01",
        _environment(
            Opener(read_only), catalog=_baseline_catalog(), facts=_BASELINE_FACTS
        ),
    )

    assert result.outcome is tool.Outcome.EXPECTED_MISMATCH
    assert result.committed is False


def test_g04b_uses_ensure_ledger_then_a_separate_read_only_pass() -> None:
    verify = FakeConnection("verify")
    opener = Opener(verify)

    def _ensure() -> None:
        pass

    result = _run(
        "G04b",
        _environment(
            opener,
            catalog=s7.declared_snapshot(s7.LEDGER_ONLY_STATE, declared_by="G04b"),
            facts=s7.MigrationFacts(
                ledger_present=True, applied=(), tables=("schema_version",)
            ),
            ensure=_ensure,
        ),
    )

    assert result.outcome is tool.Outcome.READY
    assert result.committed is True
    assert opener.opened == [verify]  # ensure_ledger owns its own connection
    assert verify.autocommit is True
    assert verify.commits == 0
    assert result.post_commit is not None
    assert result.post_commit.gate_code == "MIGRATIONS_PENDING"


def test_g04b_without_the_ensure_ledger_seam_is_a_usage_error() -> None:
    opener = Opener()
    result = _run(
        "G04b",
        _environment(opener, catalog=_baseline_catalog(), facts=_BASELINE_FACTS),
    )

    assert result.outcome is tool.Outcome.USAGE
    assert "ensure_ledger_unavailable" in result.diagnostics
    assert opener.opened == []


def test_g04b_ensure_ledger_failure_is_unconfirmed() -> None:
    def _ensure() -> None:
        raise RuntimeError("fake ensure_ledger failure")

    result = _run(
        "G04b",
        _environment(
            Opener(), catalog=_baseline_catalog(), facts=_BASELINE_FACTS,
            ensure=_ensure,
        ),
    )

    assert result.outcome is tool.Outcome.OUTCOME_UNCONFIRMED
    assert result.exit_code == 7
    assert result.committed is None
    assert result.verified is False


# ---------------------------------------------------------------------------
# G05: the declared ledger-shape refusal, on the real reader contract
# ---------------------------------------------------------------------------


def test_g05_is_ready_when_the_reader_refuses_the_declared_shape() -> None:
    scenario = s7.scenario_by_id("G05")
    catalog = s7.declared_snapshot(scenario.expected_object_state, declared_by="G05")

    def _reader(connection):
        raise MigrationIntegrityError("ledger shape refused (fake)")

    defect = FakeConnection("defect")
    verify = FakeConnection("verify")
    result = _run(
        "G05",
        _environment(
            Opener(defect, verify), catalog=catalog, reader=_reader
        ),
    )

    assert result.outcome is tool.Outcome.READY
    assert defect.commits == 1
    assert result.in_transaction is not None
    assert result.in_transaction.ledger_shape_refused is True
    assert result.in_transaction.gate_code == "MIGRATION_HISTORY_INVALID"
    assert result.in_transaction.facts is None


def test_a_shape_refusal_outside_the_declared_path_is_a_mismatch() -> None:
    """The shape refusal only satisfies scenarios that declare that path."""
    catalog = _baseline_catalog()

    def _reader(connection):
        raise MigrationIntegrityError("ledger shape refused (fake)")

    result = _run(
        "N00",
        _environment(Opener(FakeConnection("defect")), catalog=catalog, reader=_reader),
    )

    assert result.outcome is tool.Outcome.EXPECTED_MISMATCH
    assert result.in_transaction is not None
    assert result.in_transaction.gate_code == "MIGRATION_HISTORY_INVALID"


# ---------------------------------------------------------------------------
# Selection refusals happen before any connection
# ---------------------------------------------------------------------------


def test_deferred_scenario_is_refused_before_any_connection() -> None:
    opener = Opener()
    result = _run(
        "N14",
        _environment(opener, catalog=_baseline_catalog(), facts=_BASELINE_FACTS),
    )

    assert result.outcome is tool.Outcome.USAGE
    assert result.exit_code == tool.EXIT_USAGE == 2
    assert "scenario_deferred" in result.diagnostics
    assert opener.opened == []


def test_incomplete_declaration_is_refused_with_the_unresolved_properties() -> None:
    scenario = s7.scenario_by_id("N13")
    opener = Opener()
    result = tool.run_scenario(
        scenario,
        _environment(opener, catalog=_baseline_catalog(), facts=_BASELINE_FACTS),
    )

    assert result.outcome is tool.Outcome.USAGE
    assert "declaration_incomplete" in result.diagnostics
    assert any("default_expression" in token for token in result.diagnostics)
    assert opener.opened == []


def test_unknown_baseline_mode_is_refused() -> None:
    import dataclasses

    scenario = dataclasses.replace(s7.scenario_by_id("N00"), baseline_mode="other")
    opener = Opener()
    result = tool.run_scenario(
        scenario, _environment(opener, catalog=_baseline_catalog(), facts=_BASELINE_FACTS)
    )

    assert result.outcome is tool.Outcome.USAGE
    assert "unknown_baseline_mode" in result.diagnostics
    assert opener.opened == []


# ---------------------------------------------------------------------------
# Exit-code contract and guards
# ---------------------------------------------------------------------------


def test_exit_codes_match_the_approved_contract() -> None:
    assert (
        tool.EXIT_READY,
        tool.EXIT_USAGE,
        tool.EXIT_UNSAFE_STATE,
        tool.EXIT_UNAVAILABLE,
        tool.EXIT_EXPECTED_MISMATCH,
        tool.EXIT_POST_COMMIT_MISMATCH,
        tool.EXIT_OUTCOME_UNCONFIRMED,
    ) == (0, 2, 3, 4, 5, 6, 7)
    assert tool.Outcome.READY.exit_code == 0
    assert tool.Outcome.USAGE.exit_code == 2
    assert tool.Outcome.UNSAFE_STATE.exit_code == 3
    assert tool.Outcome.UNAVAILABLE.exit_code == 4
    assert tool.Outcome.EXPECTED_MISMATCH.exit_code == 5
    assert tool.Outcome.POST_COMMIT_MISMATCH.exit_code == 6
    assert tool.Outcome.OUTCOME_UNCONFIRMED.exit_code == 7


def test_session_guards_are_three_transaction_local_statements() -> None:
    assert isinstance(tool.SESSION_GUARDS, tuple)
    assert len(tool.SESSION_GUARDS) == 3
    for guard in tool.SESSION_GUARDS:
        assert guard.startswith("SET LOCAL ")


def test_result_dict_is_json_serializable_and_never_names_a_dsn() -> None:
    defect = FakeConnection("defect")
    result = _run(
        "N00",
        _environment(
            Opener(defect, FakeConnection("verify")),
            catalog=_baseline_catalog(),
            facts=_BASELINE_FACTS,
        ),
    )
    payload = json.dumps(result.as_dict(), sort_keys=True)

    assert "postgresql://" not in payload
    assert "CREATE TABLE" not in payload
    assert json.loads(payload)["outcome"] == "ready"


# ---------------------------------------------------------------------------
# CLI: offline commands, opt-in gating, injected environment
# ---------------------------------------------------------------------------


def test_list_prints_the_whole_matrix_offline(capsys) -> None:
    assert tool.main(["list"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert len(lines) == s7.ALL_COUNT == 26
    assert lines[0].startswith("N00")
    assert any("G05" in line for line in lines)


def test_list_filters_the_matrix(capsys) -> None:
    assert tool.main(["list", "--matrix", "deferred"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]

    assert len(lines) == s7.DEFERRED_COUNT == 1
    assert lines[0].startswith("N14")


def test_list_json_is_structured(capsys) -> None:
    assert tool.main(["list", "--json", "--matrix", "mandatory"]) == 0
    rows = json.loads(capsys.readouterr().out)

    assert len(rows) == s7.MANDATORY_COUNT == 22
    assert set(rows[0]) >= {
        "id", "layer", "matrix", "baseline_mode", "database", "verdict",
    }
    assert rows[0]["id"] == "N00"
    assert all(row["matrix"] == "mandatory" for row in rows)


def test_print_databases_names_one_database_per_scenario(capsys) -> None:
    assert tool.main(["print-databases"]) == 0
    lines = capsys.readouterr().out.split()

    assert len(lines) == s7.ALL_COUNT
    assert lines[0] == s7.scenario_by_id("N00").database_name
    assert len(set(lines)) == len(lines)


def test_run_without_allow_live_is_refused_even_with_a_dsn_present(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv(tool.DSN_ENV_VAR, "postgresql://sentinel/never-opened:pw@host/db")

    assert tool.main(["run", "--scenario", "N00"]) == tool.EXIT_USAGE
    captured = capsys.readouterr()

    assert "--allow-live" in captured.err
    assert "sentinel" not in captured.err + captured.out


def test_run_uses_the_injected_environment(capsys) -> None:
    environment = _environment(
        Opener(FakeConnection("defect"), FakeConnection("verify")),
        catalog=_baseline_catalog(),
        facts=_BASELINE_FACTS,
    )

    assert tool.main(["run", "--scenario", "N00", "--json"], environment=environment) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["scenario"] == "N00"
    assert payload["outcome"] == "ready"
    assert payload["exit_code"] == 0


def test_run_unknown_scenario_is_a_usage_error(capsys) -> None:
    assert tool.main(["run", "--scenario", "N99"], environment=None) == tool.EXIT_USAGE
    assert "unknown scenario N99" in capsys.readouterr().err


def test_run_refuses_a_deferred_scenario_before_opening_a_connection() -> None:
    opener = Opener()
    environment = _environment(
        opener, catalog=_baseline_catalog(), facts=_BASELINE_FACTS
    )

    assert tool.main(["run", "--scenario", "N14"], environment=environment) == 2
    assert opener.opened == []


def test_unknown_command_exits_two() -> None:
    assert tool.main(["nope"]) == 2


def test_human_report_is_sanitized(capsys) -> None:
    environment = _environment(
        Opener(FakeConnection("defect")),
        catalog=_baseline_catalog(),
        facts=_BASELINE_FACTS,
    )

    assert tool.main(["run", "--scenario", "N01"], environment=environment) == 5
    out = capsys.readouterr().out

    assert "provision-negative: scenario N01" in out
    assert "provision-negative: outcome expected_mismatch (exit 5)" in out
    assert "provision-negative: mismatch relations[employee]" in out
    assert "DROP TABLE" not in out
    assert "postgresql://" not in out


# ---------------------------------------------------------------------------
# Offline isolation: no config, no driver, no environment read at import
# ---------------------------------------------------------------------------


def test_module_import_pulls_in_no_config_and_no_driver() -> None:
    script = (
        "import sys\n"
        "import tools.provision_negative_schema_cases\n"
        "names = ['psycopg', 'src.config', 'src.db_migrate', "
        "'src.db_migrate_executor', 'src.startup_schema']\n"
        "print(' '.join(str(int(name in sys.modules)) for name in names))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "0 0 0 0 0", completed.stdout + completed.stderr


def test_top_level_imports_exclude_the_live_layer() -> None:
    tree = ast.parse(TOOL_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    forbidden = {
        "psycopg",
        "psycopg2",
        "src.config",
        "src.db",
        "src.db_migrate",
        "src.db_migrate_executor",
        "src.startup_schema",
    }
    assert imported & forbidden == set()
    assert {"argparse", "json", "os", "sys"} <= imported


def test_offline_list_never_touches_the_negative_dsn_environment() -> None:
    sentinel = "postgresql://sentinel:not-a-real-secret@example.invalid/never"
    environment = dict(os.environ)
    environment[tool.DSN_ENV_VAR] = sentinel

    completed = subprocess.run(
        [sys.executable, str(TOOL_PATH), "list", "--json"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert sentinel not in completed.stdout + completed.stderr
    assert len(json.loads(completed.stdout)) == s7.ALL_COUNT


def test_print_databases_never_touches_the_negative_dsn_environment() -> None:
    sentinel = "postgresql://sentinel:not-a-real-secret@example.invalid/never"
    environment = dict(os.environ)
    environment[tool.DSN_ENV_VAR] = sentinel

    completed = subprocess.run(
        [sys.executable, str(TOOL_PATH), "print-databases"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert sentinel not in completed.stdout + completed.stderr
    assert len(completed.stdout.split()) == s7.ALL_COUNT


def test_every_provisionable_migrate_scenario_reaches_ready_through_the_lifecycle() -> None:
    """A sweep over the whole matrix: guards first, then a commit + re-read.

    ``G05`` is excluded because its evidence is the ledger-shape refusal, which
    the dedicated test above covers; ``G01``/``G04b`` use the other baseline
    modes. N13/N14 are refused as incomplete/deferred declarations.
    """
    checked = 0
    for scenario in s7.SCENARIOS:
        if scenario.baseline_mode != "migrate" or scenario.scenario_id == "G05":
            continue
        if scenario.deferred_unverified or s7.unresolved_properties(
            scenario.expected_object_state
        ):
            continue

        applied = s7.resolve_expected_applied(
            scenario.expected_migration_facts, shipped_checksum=SHIPPED
        )
        facts = s7.MigrationFacts(
            ledger_present=scenario.expected_migration_facts.ledger_present,
            applied=applied,
            tables=scenario.expected_migration_facts.tables,
        )
        catalog = s7.declared_snapshot(
            scenario.expected_object_state, declared_by=scenario.scenario_id
        )
        defect = FakeConnection("defect")
        verify = FakeConnection("verify")
        result = tool.run_scenario(
            scenario,
            _environment(Opener(defect, verify), catalog=catalog, facts=facts),
        )

        assert result.outcome is tool.Outcome.READY, (
            scenario.scenario_id,
            result.as_dict(),
        )
        assert defect.statements[:3] == list(tool.SESSION_GUARDS)
        assert defect.commits == 1 and defect.rollbacks == 0
        checked += 1

    assert checked == 21  # 24 migrate-mode cases, minus G05, N13 and N14


# ---------------------------------------------------------------------------
# Milestone D: target policy (static identity)
# ---------------------------------------------------------------------------

_N00_NAME = s7.scenario_by_id("N00").database_name


def _parser_for(database: str):
    return lambda dsn: (database, "_local_server_", 5432)


def test_validate_target_accepts_the_declared_database() -> None:
    decision = tool.validate_target(
        "postgresql://u:p@localhost:5432/visual_attendance_negative_n00",
        s7.scenario_by_id("N00"),
        identity_parser=_parser_for(_N00_NAME),
    )

    assert decision.ok is True
    assert decision.reason is None
    assert decision.database == _N00_NAME


def test_validate_target_refuses_a_different_database() -> None:
    decision = tool.validate_target(
        "postgresql://u:p@localhost:5432/attendance",
        s7.scenario_by_id("N00"),
        identity_parser=_parser_for("attendance"),
    )

    assert decision.ok is False
    assert decision.reason == "target_database_mismatch"


def test_validate_target_refuses_a_forbidden_database_first() -> None:
    decision = tool.validate_target(
        "postgresql://u:p@localhost:5432/attendance",
        s7.scenario_by_id("N00"),
        identity_parser=_parser_for("attendance"),
        forbidden_databases=("attendance", "attendance_test"),
    )

    assert decision.ok is False
    assert decision.reason == "target_forbidden_database"


def test_validate_target_refuses_an_ambiguous_identity() -> None:
    decision = tool.validate_target(
        "postgresql://u:p@localhost/visual_attendance_negative_n00",
        s7.scenario_by_id("N00"),
        identity_parser=lambda dsn: None,
    )

    assert decision.reason == "target_ambiguous"


def test_validate_target_refuses_a_malformed_dsn() -> None:
    def _raise(dsn):
        raise ValueError("fake parser refusal")

    decision = tool.validate_target(
        "not-a-dsn", s7.scenario_by_id("N00"), identity_parser=_raise
    )

    assert decision.reason == "target_malformed"


def test_validate_target_refuses_a_blank_dsn() -> None:
    decision = tool.validate_target(
        "   ", s7.scenario_by_id("N00"), identity_parser=_parser_for(_N00_NAME)
    )

    assert decision.reason == "target_missing"


def test_default_identity_parser_refuses_a_rerouting_dsn() -> None:
    """The production parser (not a fake) refuses an unprovable host."""
    decision = tool.validate_target(
        "postgresql://u:fake-password@localhost:5432/visual_attendance_negative_n00"
        "?hostaddr=10.0.0.9",
        s7.scenario_by_id("N00"),
    )

    assert decision.ok is False
    assert decision.reason == "target_ambiguous"


def test_default_identity_parser_accepts_a_loopback_database() -> None:
    decision = tool.validate_target(
        f"postgresql://u:fake-password@127.0.0.1:5432/{_N00_NAME}",
        s7.scenario_by_id("N00"),
    )

    assert decision.ok is True


# ---------------------------------------------------------------------------
# Milestone D: target policy (live identity and version)
# ---------------------------------------------------------------------------

_N01 = s7.scenario_by_id("N01")
_N01_CATALOG = s7.declared_snapshot(_N01.expected_object_state, declared_by="N01")
_N01_FACTS = s7.MigrationFacts(
    ledger_present=True, applied=((1, SHIPPED),), tables=("schema_version",)
)


def _identity_env(
    opener: Opener,
    *,
    identity_reader,
    catalog: CatalogSnapshot = _N01_CATALOG,
    facts: s7.MigrationFacts = _N01_FACTS,
) -> tool.ProvisioningEnvironment:
    return tool.ProvisioningEnvironment(
        open_connection=opener,
        catalog_probe=lambda connection: catalog,
        facts_reader=lambda connection: facts,
        shipped_checksum=SHIPPED,
        target_database=_N01.database_name,
        server_identity_reader=identity_reader,
    )


def test_identity_refusal_precedes_any_defect_sql() -> None:
    defect = FakeConnection("defect")
    result = tool.run_scenario(
        _N01,
        _identity_env(
            Opener(defect),
            identity_reader=lambda c: tool.ServerIdentity("attendance", "public", 160000),
        ),
    )

    assert result.outcome is tool.Outcome.USAGE
    assert result.exit_code == 2
    assert "target_database_mismatch" in result.diagnostics
    assert defect.statements == list(tool.SESSION_GUARDS)
    assert defect.commits == 0 and defect.rollbacks == 1 and defect.closed == 1


def test_schema_mismatch_is_refused() -> None:
    defect = FakeConnection("defect")
    result = tool.run_scenario(
        _N01,
        _identity_env(
            Opener(defect),
            identity_reader=lambda c: tool.ServerIdentity(
                _N01.database_name, "other_schema", 160000
            ),
        ),
    )

    assert result.outcome is tool.Outcome.USAGE
    assert "target_schema_mismatch" in result.diagnostics


def test_server_version_below_the_global_floor_is_refused() -> None:
    result = tool.run_scenario(
        _N01,
        _identity_env(
            Opener(FakeConnection("defect")),
            identity_reader=lambda c: tool.ServerIdentity(
                _N01.database_name, "public", 110000
            ),
        ),
    )

    assert result.outcome is tool.Outcome.USAGE
    assert "server_version_below_floor" in result.diagnostics


def test_a_scenario_floor_above_the_global_floor_wins() -> None:
    stricter = dc_replace(_N01, min_server_version=150000)
    result = tool.run_scenario(
        stricter,
        _identity_env(
            Opener(FakeConnection("defect")),
            identity_reader=lambda c: tool.ServerIdentity(
                _N01.database_name, "public", 140000
            ),
        ),
    )

    assert result.outcome is tool.Outcome.USAGE
    assert "server_version_below_floor" in result.diagnostics


def test_unreadable_identity_is_never_treated_as_permission() -> None:
    def _broken(connection):
        raise RuntimeError("identity query failed")

    result = tool.run_scenario(
        _N01, _identity_env(Opener(FakeConnection("defect")), identity_reader=_broken)
    )

    assert result.outcome is tool.Outcome.USAGE
    assert "target_identity_unverified" in result.diagnostics


def test_the_real_identity_probe_runs_before_the_defect_sql() -> None:
    defect = FakeConnection(
        "defect", identity=(_N01.database_name, "public", "160000")
    )
    verify = FakeConnection(
        "verify", identity=(_N01.database_name, "public", "160000")
    )
    result = tool.run_scenario(_N01, _identity_env(Opener(defect, verify), identity_reader=None))

    assert result.outcome is tool.Outcome.READY
    assert defect.statements[3] == tool._IDENTITY_SQL
    assert defect.statements[4] == "DROP TABLE public.employee"
    assert verify.statements[1] == tool._IDENTITY_SQL


def test_a_verification_connection_on_another_database_cannot_confirm() -> None:
    defect = FakeConnection(
        "defect", identity=(_N01.database_name, "public", "160000")
    )
    verify = FakeConnection("verify", identity=("attendance", "public", "160000"))
    result = tool.run_scenario(_N01, _identity_env(Opener(defect, verify), identity_reader=None))

    assert defect.commits == 1
    assert result.outcome is tool.Outcome.UNSAFE_STATE
    assert result.verified is False
    assert "target_database_mismatch" in result.diagnostics


# ---------------------------------------------------------------------------
# Milestone D: cleanup authorization (frozen manifest only)
# ---------------------------------------------------------------------------

_N02 = s7.scenario_by_id("N02")
_N09 = s7.scenario_by_id("N09")
_N09_CATALOG = s7.declared_snapshot(_N09.expected_object_state, declared_by="N09")
_N09_FACTS = s7.MigrationFacts(
    ledger_present=True,
    applied=((1, SHIPPED),),
    tables=("attendance_event", "employee", "schema_version"),
)


def test_cleanup_plan_is_authorized_in_manifest_order() -> None:
    plan = tool.plan_cleanup(
        _N09, _N09_CATALOG, _N09_FACTS, shipped_checksum=SHIPPED
    )

    assert plan.authorized is True
    assert plan.refusal is None
    assert plan.state == "expected"
    assert plan.statements == (
        "DROP TABLE public.employee",
        "DROP TABLE public.schema_version",
        "DROP TABLE public.attendance_event",
    )


def test_cleanup_drops_a_view_before_its_dependency() -> None:
    catalog = s7.declared_snapshot(_N02.expected_object_state, declared_by="N02")
    plan = tool.plan_cleanup(
        _N02,
        catalog,
        s7.MigrationFacts(True, ((1, SHIPPED),), ("schema_version",)),
        shipped_checksum=SHIPPED,
    )

    assert plan.authorized is True
    assert plan.statements == (
        "DROP VIEW public.employee",
        "DROP TABLE public.schema_version",
    )


def test_cleanup_statements_are_never_broad_or_cascading() -> None:
    catalog = s7.declared_snapshot(_N09.expected_object_state, declared_by="N09")
    plan = tool.plan_cleanup(_N09, catalog, _N09_FACTS, shipped_checksum=SHIPPED)
    joined = " | ".join(plan.statements).upper()

    assert "CASCADE" not in joined
    assert "IF EXISTS" not in joined
    assert "TRUNCATE" not in joined
    assert "DROP SCHEMA" not in joined
    assert "DROP DATABASE" not in joined
    assert "*" not in joined
    assert all(statement.startswith(("DROP TABLE public.", "DROP VIEW public.")) for statement in plan.statements)


def test_cleanup_refuses_an_unexpected_object() -> None:
    catalog = dc_replace(
        _N09_CATALOG,
        relations=_N09_CATALOG.relations
        + (RelationFact(schema="public", name="notes", kind="r"),),
    )
    plan = tool.plan_cleanup(_N09, catalog, _N09_FACTS, shipped_checksum=SHIPPED)

    assert plan.authorized is False
    assert plan.refusal == "cleanup_unexpected_object"
    assert plan.statements == ()


def test_cleanup_refuses_a_relation_kind_mismatch() -> None:
    catalog = s7.declared_snapshot(_N02.expected_object_state, declared_by="N02")
    catalog = dc_replace(
        catalog,
        relations=tuple(
            dc_replace(relation, kind="r") if relation.name == "employee" else relation
            for relation in catalog.relations
        ),
    )
    plan = tool.plan_cleanup(
        _N02,
        catalog,
        s7.MigrationFacts(True, ((1, SHIPPED),), ("schema_version",)),
        shipped_checksum=SHIPPED,
    )

    assert plan.authorized is False
    assert plan.refusal == "cleanup_kind_mismatch"


def test_cleanup_refuses_a_state_the_scenario_does_not_allow() -> None:
    catalog = dc_replace(
        _N09_CATALOG,
        columns=tuple(
            dc_replace(column, pg_type="bigint")
            if column.table == "employee" and column.name == "employee_id"
            else column
            for column in _N09_CATALOG.columns
        ),
    )
    plan = tool.plan_cleanup(_N09, catalog, _N09_FACTS, shipped_checksum=SHIPPED)

    assert plan.authorized is False
    assert plan.refusal == "cleanup_state_not_allowed"


def test_cleanup_refuses_an_unsupported_manifest_kind() -> None:
    scenario = dc_replace(_N09, cleanup_manifest=(("employee", "X"),))
    plan = tool.plan_cleanup(
        scenario, _N09_CATALOG, _N09_FACTS, shipped_checksum=SHIPPED
    )

    assert plan.authorized is False
    assert plan.refusal == "cleanup_manifest_unsupported_kind"


# ---------------------------------------------------------------------------
# Milestone D: cleanup execution
# ---------------------------------------------------------------------------


def _empty() -> tuple[CatalogSnapshot, s7.MigrationFacts]:
    return _empty_catalog(), s7.MigrationFacts(False, (), ())


def _cleanup_environment(opener: Opener, mapping):
    return tool.ProvisioningEnvironment(
        open_connection=opener,
        catalog_probe=lambda connection: mapping[connection][0],
        facts_reader=lambda connection: mapping[connection][1],
        shipped_checksum=SHIPPED,
    )


def test_cleanup_drops_only_the_manifest_and_verifies_the_empty_state() -> None:
    declared = s7.declared_snapshot(_N02.expected_object_state, declared_by="N02")
    classify = FakeConnection("classify")
    drop = FakeConnection("drop")
    verify = FakeConnection("verify")
    empty_catalog, empty_facts = _empty()
    mapping = {
        classify: (declared, s7.MigrationFacts(True, ((1, SHIPPED),), ("schema_version",))),
        drop: (empty_catalog, empty_facts),
        verify: (empty_catalog, empty_facts),
    }

    result = tool.run_cleanup(
        _N02, _cleanup_environment(Opener(classify, drop, verify), mapping)
    )

    assert result.outcome is tool.Outcome.READY
    assert result.committed is True
    assert result.recovery is tool.Recovery.EXPECTED_COMMITTED
    assert drop.statements == list(tool.SESSION_GUARDS) + [
        "DROP VIEW public.employee",
        "DROP TABLE public.schema_version",
    ]
    assert drop.commits == 1 and drop.rollbacks == 0 and drop.closed == 1
    assert classify.commits == 0 and verify.commits == 0
    assert result.cleanup is not None and result.cleanup.authorized


def test_cleanup_refusal_opens_only_the_classification_connection() -> None:
    catalog = dc_replace(
        _N09_CATALOG,
        relations=_N09_CATALOG.relations
        + (RelationFact(schema="public", name="notes", kind="r"),),
    )
    classify = FakeConnection("classify")
    opener = Opener(classify)
    mapping = {classify: (catalog, _N09_FACTS)}

    result = tool.run_cleanup(_N09, _cleanup_environment(opener, mapping))

    assert result.outcome is tool.Outcome.UNSAFE_STATE
    assert result.recovery is tool.Recovery.UNSAFE_CLEANUP_STATE
    assert "cleanup_unexpected_object" in result.diagnostics
    assert opener.opened == [classify]
    assert classify.commits == 0


def test_cleanup_from_the_ledger_only_state_is_partial_recovery() -> None:
    ledger_catalog = s7.declared_snapshot(s7.LEDGER_ONLY_STATE, declared_by="G04b")
    classify = FakeConnection("classify")
    drop = FakeConnection("drop")
    verify = FakeConnection("verify")
    empty_catalog, empty_facts = _empty()
    mapping = {
        classify: (ledger_catalog, s7.MigrationFacts(True, (), ("schema_version",))),
        drop: (empty_catalog, empty_facts),
        verify: (empty_catalog, empty_facts),
    }

    result = tool.run_cleanup(
        _N09, _cleanup_environment(Opener(classify, drop, verify), mapping)
    )

    assert result.outcome is tool.Outcome.READY
    assert result.recovery is tool.Recovery.PARTIAL_RECOVERY
    assert drop.statements == list(tool.SESSION_GUARDS) + [
        "DROP TABLE public.schema_version"
    ]
    assert result.cleanup is not None
    assert result.cleanup.already_absent == (
        ("employee", "r"),
        ("attendance_event", "r"),
    )


def test_cleanup_with_no_declared_manifest_is_refused_offline() -> None:
    opener = Opener()
    result = tool.run_cleanup(
        s7.scenario_by_id("G01"), _cleanup_environment(opener, {})
    )

    assert result.outcome is tool.Outcome.USAGE
    assert "cleanup_not_declared" in result.diagnostics
    assert opener.opened == []


def test_cleanup_with_an_unreadable_ledger_is_refused() -> None:
    def _reader(connection):
        raise MigrationIntegrityError("ledger shape refused (fake)")

    classify = FakeConnection("classify")
    environment = tool.ProvisioningEnvironment(
        open_connection=Opener(classify),
        catalog_probe=lambda connection: _empty_catalog(),
        facts_reader=_reader,
        shipped_checksum=SHIPPED,
    )
    result = tool.run_cleanup(_N09, environment)

    assert result.outcome is tool.Outcome.UNSAFE_STATE
    assert result.recovery is tool.Recovery.UNSAFE_CLEANUP_STATE
    assert "cleanup_state_unverifiable" in result.diagnostics
    assert classify.commits == 0


def test_cleanup_commit_failure_is_unconfirmed() -> None:
    declared = s7.declared_snapshot(_N02.expected_object_state, declared_by="N02")
    classify = FakeConnection("classify")
    drop = FakeConnection("drop", commit_error=True)
    empty_catalog, empty_facts = _empty()
    mapping = {
        classify: (declared, s7.MigrationFacts(True, ((1, SHIPPED),), ("schema_version",))),
        drop: (empty_catalog, empty_facts),
    }
    opener = Opener(classify, drop)

    result = tool.run_cleanup(_N02, _cleanup_environment(opener, mapping))

    assert result.outcome is tool.Outcome.OUTCOME_UNCONFIRMED
    assert result.exit_code == 7
    assert result.committed is None
    assert result.recovery is tool.Recovery.UNCERTAIN_COMMIT
    assert len(opener.opened) == 2


def test_cleanup_post_commit_mismatch_is_exit_six() -> None:
    declared = s7.declared_snapshot(_N02.expected_object_state, declared_by="N02")
    classify = FakeConnection("classify")
    drop = FakeConnection("drop")
    verify = FakeConnection("verify")
    empty_catalog, empty_facts = _empty()
    mapping = {
        classify: (declared, s7.MigrationFacts(True, ((1, SHIPPED),), ("schema_version",))),
        drop: (empty_catalog, empty_facts),
        verify: (declared, s7.MigrationFacts(True, ((1, SHIPPED),), ("schema_version",))),
    }

    result = tool.run_cleanup(
        _N02, _cleanup_environment(Opener(classify, drop, verify), mapping)
    )

    assert result.outcome is tool.Outcome.POST_COMMIT_MISMATCH
    assert result.exit_code == 6
    assert result.verified is False


# ---------------------------------------------------------------------------
# Milestone D: recovery classification
# ---------------------------------------------------------------------------


def test_recovery_classification_never_optimistic() -> None:
    assert (
        tool.classify_recovery(tool.Outcome.READY, True)
        is tool.Recovery.EXPECTED_COMMITTED
    )
    assert (
        tool.classify_recovery(tool.Outcome.READY, False)
        is tool.Recovery.EXPECTED_ROLLED_BACK
    )
    assert (
        tool.classify_recovery(tool.Outcome.EXPECTED_MISMATCH, False)
        is tool.Recovery.EXPECTED_ROLLED_BACK
    )
    assert (
        tool.classify_recovery(tool.Outcome.OUTCOME_UNCONFIRMED, None)
        is tool.Recovery.UNCERTAIN_COMMIT
    )
    assert (
        tool.classify_recovery(tool.Outcome.POST_COMMIT_MISMATCH, True)
        is tool.Recovery.POST_COMMIT_MISMATCH
    )
    for outcome in (
        tool.Outcome.UNSAFE_STATE,
        tool.Outcome.UNAVAILABLE,
        tool.Outcome.USAGE,
    ):
        assert (
            tool.classify_recovery(outcome, False)
            is tool.Recovery.UNEXPECTED_OBJECT_STATE
        )


def test_a_ready_run_reports_expected_committed() -> None:
    result = _run(
        "N00",
        _environment(
            Opener(FakeConnection("defect"), FakeConnection("verify")),
            catalog=_baseline_catalog(),
            facts=_BASELINE_FACTS,
        ),
    )

    assert result.recovery is tool.Recovery.EXPECTED_COMMITTED
    assert result.as_dict()["recovery"] == "expected_committed"


def test_a_rolled_back_mismatch_reports_expected_rolled_back() -> None:
    result = _run(
        "N01",
        _environment(
            Opener(FakeConnection("defect")),
            catalog=_baseline_catalog(),
            facts=_BASELINE_FACTS,
        ),
    )

    assert result.recovery is tool.Recovery.EXPECTED_ROLLED_BACK


# ---------------------------------------------------------------------------
# Milestone D: reports
# ---------------------------------------------------------------------------

_FIXED_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _ready_result() -> tool.ScenarioResult:
    return _run(
        "N00",
        _environment(
            Opener(FakeConnection("defect"), FakeConnection("verify")),
            catalog=_baseline_catalog(),
            facts=_BASELINE_FACTS,
        ),
    )


def test_report_document_is_versioned_and_complete() -> None:
    scenario = s7.scenario_by_id("N00")
    document = tool.build_report(
        _ready_result(), scenario, timestamp=_FIXED_TIME
    )

    assert document["report_schema_version"] == tool.REPORT_SCHEMA_VERSION == 1
    assert document["tool_version"] == tool.TOOL_VERSION
    assert document["timestamp"] == "2026-01-02T03:04:05Z"
    assert document["scenario"]["id"] == "N00"
    assert document["scenario"]["database"] == scenario.database_name
    assert document["scenario"]["expected_status"] == "compatible"
    assert document["observed"]["matched"] is True
    assert document["post_commit"]["matched"] is True
    assert document["commit"] == {
        "committed": True,
        "statements_executed": 0,
        "guards_applied": True,
    }
    assert document["recovery"] == "expected_committed"
    assert document["exit_code"] == 0
    assert document["verified"] is True


def test_report_carries_the_cleanup_decision() -> None:
    declared = s7.declared_snapshot(_N02.expected_object_state, declared_by="N02")
    classify = FakeConnection("classify")
    drop = FakeConnection("drop")
    verify = FakeConnection("verify")
    empty_catalog, empty_facts = _empty()
    mapping = {
        classify: (declared, s7.MigrationFacts(True, ((1, SHIPPED),), ("schema_version",))),
        drop: (empty_catalog, empty_facts),
        verify: (empty_catalog, empty_facts),
    }
    result = tool.run_cleanup(
        _N02, _cleanup_environment(Opener(classify, drop, verify), mapping)
    )
    document = tool.build_report(result, _N02, timestamp=_FIXED_TIME)

    assert document["cleanup"]["authorized"] is True
    assert document["cleanup"]["statements"] == 2
    assert document["cleanup"]["state"] == "expected"


def test_write_report_creates_json_and_human_files(tmp_path: Path) -> None:
    scenario = s7.scenario_by_id("N00")
    json_path, text_path = tool.write_report(
        tmp_path / "reports", _ready_result(), scenario, timestamp=_FIXED_TIME
    )

    assert json_path.exists() and text_path.exists()
    assert json_path.name.startswith("N00-20260102T030405")
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["report_schema_version"] == 1
    assert "provision-negative: outcome ready" in text_path.read_text(encoding="utf-8")


def test_write_report_failure_is_an_os_error(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError):
        tool.write_report(
            blocked, _ready_result(), s7.scenario_by_id("N00"), timestamp=_FIXED_TIME
        )


def test_reports_never_contain_a_dsn_or_sql(tmp_path: Path) -> None:
    scenario = s7.scenario_by_id("N00")
    json_path, text_path = tool.write_report(
        tmp_path, _ready_result(), scenario, timestamp=_FIXED_TIME
    )
    combined = json_path.read_text(encoding="utf-8") + text_path.read_text(
        encoding="utf-8"
    )

    assert "postgresql://" not in combined
    assert "CREATE TABLE" not in combined
    assert "password" not in combined.lower()


# ---------------------------------------------------------------------------
# Milestone D: CLI integration
# ---------------------------------------------------------------------------


def test_cli_cleanup_requires_allow_live(monkeypatch, capsys) -> None:
    monkeypatch.setenv(
        tool.DSN_ENV_VAR, "postgresql://sentinel:pw@localhost:5432/attendance"
    )

    assert tool.main(["cleanup", "--scenario", "N02"]) == tool.EXIT_USAGE
    captured = capsys.readouterr()

    assert "--allow-live" in captured.err
    assert "sentinel" not in captured.err


def test_cli_cleanup_writes_reports_with_an_injected_environment(
    tmp_path: Path, capsys
) -> None:
    declared = s7.declared_snapshot(_N02.expected_object_state, declared_by="N02")
    classify = FakeConnection("classify")
    drop = FakeConnection("drop")
    verify = FakeConnection("verify")
    empty_catalog, empty_facts = _empty()
    mapping = {
        classify: (declared, s7.MigrationFacts(True, ((1, SHIPPED),), ("schema_version",))),
        drop: (empty_catalog, empty_facts),
        verify: (empty_catalog, empty_facts),
    }
    environment = _cleanup_environment(Opener(classify, drop, verify), mapping)

    exit_code = tool.main(
        ["cleanup", "--scenario", "N02", "--json", "--report-dir", str(tmp_path)],
        environment=environment,
    )

    assert exit_code == 0
    written = sorted(path.name for path in tmp_path.iterdir())
    assert len(written) == 2
    assert any(name.endswith(".json") for name in written)
    assert any(name.endswith(".txt") for name in written)
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "ready"


def test_cli_report_write_failure_never_reports_success(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    environment = _environment(
        Opener(FakeConnection("defect"), FakeConnection("verify")),
        catalog=_baseline_catalog(),
        facts=_BASELINE_FACTS,
    )

    exit_code = tool.main(
        ["run", "--scenario", "N00", "--report-dir", str(blocked)],
        environment=environment,
    )

    assert exit_code == tool.EXIT_UNSAFE_STATE


def test_cli_report_write_failure_keeps_the_primary_failure(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    environment = _environment(
        Opener(FakeConnection("defect")),
        catalog=_baseline_catalog(),
        facts=_BASELINE_FACTS,
    )

    exit_code = tool.main(
        ["run", "--scenario", "N01", "--report-dir", str(blocked)],
        environment=environment,
    )

    assert exit_code == tool.EXIT_EXPECTED_MISMATCH


def test_cli_refuses_a_wrong_target_before_connecting(monkeypatch, capsys) -> None:
    monkeypatch.setenv(
        tool.DSN_ENV_VAR, "postgresql://u:p@localhost:5432/attendance"
    )

    exit_code = tool.main(["run", "--scenario", "N00", "--allow-live"])
    captured = capsys.readouterr()

    assert exit_code == tool.EXIT_USAGE
    assert "target_database_mismatch" in captured.err
    assert "postgresql://" not in captured.err + captured.out
