"""Offline harness-control tests for the Tier-2 verification harness
(Step 1.5c-3b-prep). No PostgreSQL connection is ever attempted here: the
guard runs against injected fake connections, and the live harness module
itself is only executed in a fresh subprocess where the opt-in is provably
absent.

What these tests pin down
-------------------------

1. **Opt-in gating** — the harness cannot run in the ordinary offline
   suite; without ``ATTENDANCE_TIER2_OPT_IN=1`` every item is *skipped
   with a reason*, never passed, and no connection is opened.
2. **Pre-flight ordering** — missing opt-in / test DSN / application DSN
   block *before* the guard's opener runs, and the guard's bar is never
   weakened or re-implemented.
3. **Targeting discipline** — repositories are built with an explicit
   ``dsn=`` that must equal the guarded test target; the application DSN
   reaches only the guard (for the distinctness proof), never the
   repository, never inside this module by default.
4. **Guarded cleanup** — deletion requires a guard-produced target, the
   authorized manifest action, harness-shaped synthetic IDs, and an
   exact re-read match; anything else is left untouched.
5. **Source hygiene** — the module's SQL strings are exactly the two
   bound-parameter statements it claims, with no CREATE/DROP/ALTER/
   TRUNCATE/INSERT anywhere.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from src.config import AppSettings
from src.test_database_guard import (
    GUARD_MARKER_TABLE,
    GUARD_MARKER_VALUE,
    GuardBlocked,
    GuardBlockedReason,
    VerifiedTestTarget,
    verify_test_database,
)

_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_HARNESS_PATH: Final[Path] = (
    _PROJECT_ROOT / "tests" / "test_tier2_employee_repository.py"
)

APP_DSN: Final[str] = "postgresql://attendance_app@127.0.0.1:5432/attendance"
TEST_DSN: Final[str] = (
    "postgresql://attendance_test@127.0.0.1:5433/attendance_test"
)
MARKER_CREATED_AT: Final[datetime] = datetime(2026, 11, 1, 9, 0, 0, tzinfo=UTC)

#: The explicit opt-in, passed wherever a test's *point* is a later stage
#: (pre-flight order, guard, targeting, cleanup) rather than the gate itself.
_OPTED_IN_ENV: Final[dict[str, str]] = {"ATTENDANCE_TIER2_OPT_IN": "1"}


def _load_harness():
    """Import the live harness module by path (project convention).

    The module import performs no I/O: importing it twice (pytest also
    collects it as a skipped test module) is harmless.
    """
    spec = importlib.util.spec_from_file_location(
        "tier2_harness", _HARNESS_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tier2 = _load_harness()


# ---------------------------------------------------------------------------
# Fake guard plumbing (mirrors tests/test_test_database_guard.py's shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DbIdentity:
    dbname: str
    host: str
    port: int


class _FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = list(rows)

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class _FakeGuardConnection:
    """Server-shaped connection the guard positively executes against."""

    def __init__(self, *, ledger_version: int = 1) -> None:
        self.info = _DbIdentity("attendance_test", "127.0.0.1", 5433)
        self.ledger_version = ledger_version
        self._read_only = False
        self.read_only_before_first_query: bool | None = None
        self.executed: list[str] = []
        self.closed = False

    @property
    def read_only(self) -> bool:
        return self._read_only

    @read_only.setter
    def read_only(self, value: object) -> None:
        self._read_only = bool(value)

    def execute(self, sql: str, params: tuple) -> _FakeResult:
        if self.read_only_before_first_query is None:
            self.read_only_before_first_query = self._read_only
        self.executed.append(sql)
        if "current_database" in sql:
            return _FakeResult([(self.info.dbname,)])
        if GUARD_MARKER_TABLE in sql:
            return _FakeResult([(GUARD_MARKER_VALUE, MARKER_CREATED_AT, 1)])
        if "phase3_migrations" in sql:
            return _FakeResult([(self.ledger_version,)])
        if "max(version)" in sql.lower():
            return _FakeResult([(self.ledger_version,)])
        raise AssertionError(f"guard must only issue approved reads: {sql!r}")

    def close(self) -> None:
        self.closed = True


class _FakeOpener:
    def __init__(self, connection: object) -> None:
        self.connection = connection
        self.opened_dsns: list[str] = []
        self.open_count = 0

    def __call__(self, dsn: str) -> Any:
        self.opened_dsns.append(dsn)
        self.open_count += 1
        if isinstance(self.connection, Exception):
            raise self.connection
        return self.connection


def _stdlib_dsn_parser(dsn: str) -> dict[str, object]:
    """Stdlib-only parser keeping the shared pytest process psycopg-free."""
    from urllib.parse import urlsplit

    parts = urlsplit(dsn.strip())
    if not parts.hostname:
        raise ValueError("no host component: not a parseable URI DSN")
    return {
        "dbname": (parts.path.lstrip("/") or None) or "",
        "host": parts.hostname,
        "port": parts.port,
        "user": parts.username,
        "password": None,
    }


def _genuine_target() -> VerifiedTestTarget:
    """Produce a real, unforgeable VerifiedTestTarget (no psycopg)."""
    opener = _FakeOpener(_FakeGuardConnection())
    return verify_test_database(
        APP_DSN, TEST_DSN, connection_factory=opener, dsn_parser=_stdlib_dsn_parser
    )


# ---------------------------------------------------------------------------
# 0. VerifiedTestTarget is only obtainable through the guard
# ---------------------------------------------------------------------------


def test_genuine_target_is_guard_produced_and_read_only() -> None:
    opener = _FakeOpener(_FakeGuardConnection())
    target = verify_test_database(
        APP_DSN, TEST_DSN, connection_factory=opener, dsn_parser=_stdlib_dsn_parser
    )
    assert isinstance(target, VerifiedTestTarget)
    assert target.dbname == "attendance_test"
    # The guard connects *only* to the test DSN, in enforced read-only mode
    # before its first query — never the application DSN.
    assert opener.opened_dsns == [TEST_DSN]
    connection = opener.connection
    assert isinstance(connection, _FakeGuardConnection)
    assert connection.read_only_before_first_query is True


def test_verified_target_cannot_be_forged_by_callers() -> None:
    with pytest.raises(ValueError) as err:
        VerifiedTestTarget(  # type: ignore[call-arg]
            dbname="attendance_test",
            host="127.0.0.1",
            port=5433,
            marker_created_at=MARKER_CREATED_AT,
            marker_schema_version=1,
            ledger_schema_version=1,
        )
    assert "verify_test_database" in str(err.value)


# ---------------------------------------------------------------------------
# 1. Opt-in gating
# ---------------------------------------------------------------------------


def test_opt_in_requires_exactly_one() -> None:
    assert tier2.tier2_opt_in_enabled({}) is False
    assert tier2.tier2_opt_in_enabled({"ATTENDANCE_TIER2_OPT_IN": "1"}) is True
    assert tier2.tier2_opt_in_enabled({"ATTENDANCE_TIER2_OPT_IN": "0"}) is False
    assert tier2.tier2_opt_in_enabled({"ATTENDANCE_TIER2_OPT_IN": "true"}) is False
    assert tier2.tier2_opt_in_enabled({"ATTENDANCE_TIER2_OPT_IN": "01"}) is False


def test_module_marks_skip_with_a_reason_not_a_pass() -> None:
    marks = getattr(tier2, "pytestmark", [])
    names = [getattr(mark, "name", "") for mark in marks]
    assert "tier2" in names
    skipif = [mark for mark in marks if getattr(mark, "name", "") == "skipif"]
    assert skipif, "the whole module must carry a skipif mark"
    reasons = " ".join(str(mark.kwargs.get("reason", "")) for mark in skipif)
    assert "ATTENDANCE_TIER2_OPT_IN" in reasons


def test_module_skips_itself_in_the_ordinary_suite() -> None:
    """Fresh subprocess, opt-in and DSN env provably absent: every item
    skips, nothing passes, nothing errors, nothing connects."""
    env = {key: value for key, value in os.environ.items()}
    for key in (
        "ATTENDANCE_TIER2_OPT_IN",
        "ATTENDANCE_TEST_DATABASE_URL",
        "ATTENDANCE_DATABASE_URL",
    ):
        env.pop(key, None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(_HARNESS_PATH),
            "-q",
            "--no-header",
            "-ra",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(_PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    stdout = (completed.stdout + completed.stderr).lower()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "skipped" in stdout
    assert "passed" not in stdout
    assert "error" not in stdout
    # The skip reason names the opt-in switch (visible via -ra).
    assert "attendance_tier2_opt_in" in stdout


# ---------------------------------------------------------------------------
# 2. Pre-flight blocks before the guard (or any opener) runs
# ---------------------------------------------------------------------------


def _settings(
    *, database_url: str | None = APP_DSN, test_database_url: str | None = TEST_DSN
) -> AppSettings:
    return AppSettings(
        database_url=database_url, test_database_url=test_database_url
    )


def _failing_verifier(*args: object, **kwargs: object) -> VerifiedTestTarget:
    raise AssertionError(f"verifier must not run: {args} {kwargs}")


def test_missing_opt_in_blocks_first() -> None:
    assert (
        tier2.tier2_block_reason(_settings(), environ={}) == "OPT_IN_MISSING"
    )
    with pytest.raises(tier2.Tier2NotConfigured) as err:
        tier2.prepare_verified_target(
            settings=_settings(), verifier=_failing_verifier, environ={}
        )
    assert err.value.reason == "OPT_IN_MISSING"


def test_missing_test_dsn_blocks_before_the_guard() -> None:
    settings = _settings(test_database_url=None)
    assert (
        tier2.tier2_block_reason(settings, environ=_OPTED_IN_ENV)
        == "TEST_DSN_MISSING"
    )
    with pytest.raises(tier2.Tier2NotConfigured) as err:
        tier2.prepare_verified_target(
            settings=settings, verifier=_failing_verifier, environ=_OPTED_IN_ENV
        )
    assert err.value.reason == "TEST_DSN_MISSING"


def test_missing_app_dsn_blocks_before_the_guard() -> None:
    """The application DSN is required *only* for the guard's distinctness
    proof — but it blocks before connecting too, deterministically."""
    settings = _settings(database_url=None)
    assert (
        tier2.tier2_block_reason(settings, environ=_OPTED_IN_ENV)
        == "APP_DSN_MISSING"
    )
    with pytest.raises(tier2.Tier2NotConfigured) as err:
        tier2.prepare_verified_target(
            settings=settings, verifier=_failing_verifier, environ=_OPTED_IN_ENV
        )
    assert err.value.reason == "APP_DSN_MISSING"


def test_guard_block_propagates_without_repository_access() -> None:
    def blocked_verifier(app: object, test: object) -> VerifiedTestTarget:
        raise GuardBlocked(
            GuardBlockedReason.IDENTITY_AMBIGUOUS,
            "the declared identity cannot be positively established",
        )

    with pytest.raises(GuardBlocked) as err:
        tier2.prepare_verified_target(
            settings=_settings(), verifier=blocked_verifier, environ=_OPTED_IN_ENV
        )
    assert err.value.reason == GuardBlockedReason.IDENTITY_AMBIGUOUS


def test_blocked_outcome_skips_with_the_typed_reason() -> None:
    """A missing prerequisite is reported as *skipped* — never a pass."""

    def blocked_verifier(app: object, test: object) -> VerifiedTestTarget:
        raise GuardBlocked(
            GuardBlockedReason.TARGETS_NOT_DISTINCT,
            "both DSNs resolve to the same target",
        )

    with pytest.raises(pytest.skip.Exception) as err:
        tier2.verified_target_or_skip(
            settings=_settings(), verifier=blocked_verifier, environ=_OPTED_IN_ENV
        )
    assert "TARGETS_NOT_DISTINCT" in str(err.value)


def test_pre_flight_skip_reports_missing_test_dsn() -> None:
    with pytest.raises(pytest.skip.Exception) as err:
        tier2.verified_target_or_skip(
            settings=_settings(test_database_url=None), environ=_OPTED_IN_ENV
        )
    assert "TEST_DSN_MISSING" in str(err.value)


# ---------------------------------------------------------------------------
# 3. Explicit DSN targeting — the application DSN never becomes a repository
# ---------------------------------------------------------------------------


class _RepositoryRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, *args: object, **kwargs: object) -> Any:
        self.calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("no repository call should ever execute SQL")


def test_repository_is_built_with_the_explicit_test_dsn_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RepositoryRecorder()
    monkeypatch.setattr(tier2, "open_employee_repository", recorder)

    with pytest.raises(AssertionError):
        tier2.open_tier2_repository(
            _genuine_target(),
            test_dsn=TEST_DSN,
        )
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["kwargs"]["dsn"] == TEST_DSN
    assert call["kwargs"]["dsn"] != APP_DSN
    assert call["args"] == ()  # the DSN is a keyword argument, never positional

    with pytest.raises(AssertionError):
        tier2.open_tier2_repository(
            _genuine_target(), test_dsn=TEST_DSN, read_only=True
        )
    assert recorder.calls[-1]["kwargs"]["read_only"] is True


def test_repository_builder_never_receives_the_application_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RepositoryRecorder()
    monkeypatch.setattr(tier2, "open_employee_repository", recorder)
    verifier_calls: list[tuple[object, object]] = []

    def recording_verifier(app: object, test: object) -> VerifiedTestTarget:
        verifier_calls.append((app, test))
        return _genuine_target()

    tier2.prepare_verified_target(
        settings=_settings(), verifier=recording_verifier, environ=_OPTED_IN_ENV
    )

    # The application DSN travelled only to the guard, and the repository
    # builder was never invoked during preparation at all.
    assert verifier_calls == [(APP_DSN, TEST_DSN)]
    assert recorder.calls == []


def test_repository_builder_refuses_missing_test_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RepositoryRecorder()
    monkeypatch.setattr(tier2, "open_employee_repository", recorder)
    with pytest.raises(tier2.Tier2NotConfigured) as err:
        tier2.open_tier2_repository(_genuine_target(), test_dsn=None)
    assert err.value.reason == "TEST_DSN_MISSING"
    assert recorder.calls == []


def test_repository_builder_refuses_unverified_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RepositoryRecorder()
    monkeypatch.setattr(tier2, "open_employee_repository", recorder)
    for forged in (None, object(), "guarded", 123):
        with pytest.raises(tier2.Tier2NotConfigured) as err:
            tier2.open_tier2_repository(  # type: ignore[arg-type]
                forged, test_dsn=TEST_DSN
            )
        assert err.value.reason == "NOT_VERIFIED"
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# 4. Guarded cleanup
# ---------------------------------------------------------------------------


@dataclass
class _FakeStoreConnection:
    """In-memory `employee` table for cleanup flow tests."""

    rows: dict[str, tuple[str, bool, datetime]]
    fail_on_execute: bool = False

    def __post_init__(self) -> None:
        self.executed: list[tuple[str, object]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, sql: str, params: object) -> _FakeResult:
        self.executed.append((sql, params))
        if self.fail_on_execute:
            raise RuntimeError("simulated server failure")
        assert "= ANY(%s)" in sql, sql
        assert isinstance(params, tuple) and len(params) == 1
        identifiers = list(params[0])
        statement = sql.strip().upper()
        if statement.startswith("SELECT"):
            present = [
                (identifier, *self.rows[identifier])
                for identifier in sorted(self.rows)
                if identifier in identifiers
            ]
            return _FakeResult(present)
        if statement.startswith("DELETE"):
            for identifier in identifiers:
                self.rows.pop(identifier, None)
            return _FakeResult([])
        raise AssertionError(f"unexpected statement: {sql!r}")

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _run_rows() -> Any:
    return tier2.new_synthetic_run("DEADBEEF")


def _expected_map() -> dict[str, tuple[str, bool, datetime]]:
    run = _run_rows()
    rows: dict[str, tuple[str, bool, datetime]] = {}
    for slot in (1, 2, 3):
        employee = run.employee(slot)
        rows[employee.employee_id] = (
            employee.full_name,
            employee.is_active,
            employee.created_at,
        )
    return rows


def _sid(slot: int) -> str:
    """Fixed run's synthetic ID for a slot (the run id is pinned to DEADBEEF)."""
    return f"T2-DEADBEEF-{slot:02d}"


def _store_server(
    expected: dict[str, tuple[str, bool, datetime]],
    *,
    drop_slots: tuple[int, ...] = (),  # slots absent from the store entirely
    mutate_slots: tuple[int, ...] = (),  # slots whose name someone else changed
    extra: dict[str, tuple[str, bool, datetime]] | None = None,
) -> _FakeStoreConnection:
    dropped = {f"T2-DEADBEEF-{slot:02d}" for slot in drop_slots}
    rows = {
        identifier: value
        for identifier, value in expected.items()
        if identifier not in dropped
    }
    for slot in mutate_slots:
        identifier = _sid(slot)
        name, active, created = rows[identifier]
        rows[identifier] = (name + " Edited", active, created)
    if extra:
        rows.update(extra)
    return _FakeStoreConnection(rows)


def test_cleanup_requires_a_verified_target() -> None:
    expected = {"T2-DEADBEEF-01": ("Tier2 Synthetic DEADBEEF 01", True, _run_rows().created_at())}
    for forged in (None, object(), "owned", 42):
        with pytest.raises(tier2.Tier2NotConfigured) as err:
            tier2.cleanup_synthetic_employees(  # type: ignore[arg-type]
                forged, expected, test_dsn=TEST_DSN
            )
        assert err.value.reason == "NOT_VERIFIED"


def test_cleanup_requires_the_authorized_manifest_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emptying the manifest must block before any connection opens."""
    target = _genuine_target()
    monkeypatch.setattr(tier2, "DESTRUCTIVE_MANIFEST", ())
    with pytest.raises(GuardBlocked) as err:
        tier2.cleanup_synthetic_employees(
            target, _expected_map(), test_dsn=TEST_DSN
        )
    assert err.value.reason == GuardBlockedReason.ACTION_NOT_IN_MANIFEST


def test_cleanup_rejects_non_synthetic_identifiers() -> None:
    """Only harness-shaped IDs are ever eligible, before any connection."""
    expected = {
        "EMP-0042": ("Ada Lovelace", True, _run_rows().created_at()),
        **_expected_map(),
    }
    with pytest.raises(tier2.Tier2NotConfigured) as err:
        tier2.cleanup_synthetic_employees(
            _genuine_target(), expected, test_dsn=TEST_DSN
        )
    assert err.value.reason == "INVALID_SYNTHETIC_ID"


def test_cleanup_with_no_candidates_never_opens() -> None:
    class ExplodingOpener:
        def __call__(self, dsn: str) -> Any:
            raise AssertionError("no connection for an empty candidate list")

    report = tier2.cleanup_synthetic_employees(
        _genuine_target(), {}, test_dsn=TEST_DSN, connection_opener=ExplodingOpener()
    )
    assert report == tier2.CleanupReport()
    assert report.clean


def test_cleanup_deletes_only_exactly_matching_own_rows() -> None:
    expected = _expected_map()
    run = _run_rows()
    extra = {
        run.employee_id(98): (run.full_name(98), True, run.created_at()),
        run.employee_id(99): (run.full_name(99), False, run.created_at()),
    }
    connection = _store_server(
        expected, drop_slots=(3,), mutate_slots=(2,), extra=extra
    )
    opener = _FakeOpener(connection)

    report = tier2.cleanup_synthetic_employees(
        _genuine_target(),
        expected,
        test_dsn=TEST_DSN,
        connection_opener=opener,
    )

    # Slot 1 matched exactly → deleted. Slot 2 was changed by someone else
    # → left. Slot 3 is absent → absent. Sibling prefix IDs (98/99) were
    # never candidates and remain untouched and unreported.
    assert report.deleted == (_sid(1),)
    assert report.left == (_sid(2),)
    assert report.absent == (_sid(3),)
    assert report.clean is False
    assert set(connection.rows) == {_sid(2), _sid(98), _sid(99)}

    # Exactly one bound-parameter DELETE, restricted to the owned ID array.
    delete_calls = [
        (sql, params)
        for sql, params in connection.executed
        if sql.strip().upper().startswith("DELETE")
    ]
    assert len(delete_calls) == 1
    delete_sql, delete_params = delete_calls[0]
    assert "LIKE" not in delete_sql.upper()
    assert "= ANY(%s)" in delete_sql
    assert list(delete_params[0]) == [_sid(1)]  # only the owned ID
    assert connection.committed and connection.closed


def test_cleanup_leaves_everything_when_ownership_fails() -> None:
    """No stored row matches what this run wrote → nothing is deleted."""
    expected = _expected_map()
    connection = _store_server(expected, mutate_slots=(1, 2, 3))
    opener = _FakeOpener(connection)

    report = tier2.cleanup_synthetic_employees(
        _genuine_target(),
        expected,
        test_dsn=TEST_DSN,
        connection_opener=opener,
    )

    assert report.deleted == ()
    assert report.left == (_sid(1), _sid(2), _sid(3))
    assert report.absent == ()
    assert set(connection.rows) == set(expected)
    assert not any(
        sql.strip().upper().startswith("DELETE")
        for sql, _ in connection.executed
    )
    assert connection.committed is False
    assert connection.closed


def test_cleanup_closes_the_connection_on_failure() -> None:
    expected = _expected_map()
    connection = _store_server(expected)
    connection.fail_on_execute = True
    opener = _FakeOpener(connection)

    with pytest.raises(RuntimeError):
        tier2.cleanup_synthetic_employees(
            _genuine_target(),
            _expected_map(),
            test_dsn=TEST_DSN,
            connection_opener=opener,
        )

    assert connection.rolled_back
    assert not connection.committed
    assert connection.closed


# ---------------------------------------------------------------------------
# 5. Source hygiene — the module's SQL is exactly what it claims to be
# ---------------------------------------------------------------------------


def test_module_sql_is_two_bound_parameter_statements() -> None:
    assert "= ANY(%s)" in tier2._SELECT_ROWS_SQL
    assert "= ANY(%s)" in tier2._DELETE_ROWS_SQL
    for sql in (tier2._SELECT_ROWS_SQL, tier2._DELETE_ROWS_SQL):
        assert "LIKE" not in sql.upper()
        assert any(
            word in sql for word in ("employee_id",)
        )
        assert "FROM employee" in sql


def test_module_never_contains_other_schema_or_write_sql() -> None:
    """Every string literal in the module's AST must be free of schema
    creation/destruction and of any write besides the owned-row DELETE."""
    source = _HARNESS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    banned = (
        "insert into",
        "update employee",
        "create table",
        "drop table",
        "drop database",
        "alter table",
        "truncate table",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.lower()
            for fragment in banned:
                assert fragment not in text, (fragment, node.value[:100])
    # The only allowed DELETE targets the harness's own rows, by ID array.
    assert source.count("DELETE FROM employee") == 1


def test_module_imports_make_no_connection_by_default() -> None:
    """Importing the harness module executes no settings load, no guard,
    and no opener: only defs, constants, and pytest marks."""
    assert not hasattr(tier2, "load_settings_result")  # nothing cached at import
    assert tier2.tier2_block_reason(
        AppSettings(database_url=None, test_database_url=None), environ={}
    ) == "OPT_IN_MISSING"  # the pure reason function answers without any I/O
