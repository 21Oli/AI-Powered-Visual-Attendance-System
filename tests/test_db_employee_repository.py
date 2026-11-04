"""Offline tests for the Step 1.5c-3a PostgreSQL employee-repository wiring.

Everything here is synthetic: an in-memory fake "server" hands out fake
connections, so the real repository SQL, the wiring's connection/
transaction lifecycle, and the importer's reconciliation policy all run
against a fake driver shape. **No PostgreSQL connection is opened, no DSN
is read from the environment, and nothing under ``data/`` is touched.**

The fake models the parts of psycopg 3 this wiring relies on:

* ``Connection.read_only`` is a settable property that reports back;
* ``Connection.execute(sql, params)`` returns a cursor with
  ``fetchone()`` / ``fetchall()``;
* ``commit()``, ``rollback()`` and ``close()`` exist and are observable;
* a unique-constraint violation surfaces as an exception carrying
  ``sqlstate == "23505"`` (psycopg 3's attribute — never psycopg2's
  ``pgcode``).

What the fake deliberately does **not** model: PostgreSQL's server-side
read-only enforcement (a write refused by the server because the
transaction is read-only). That is real-server behaviour and stays
unverified offline; these tests prove only that the wiring requests and
verifies read-only mode *before* the first statement.
"""

from __future__ import annotations

import inspect
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src import db_employee_repository
from src.db import DatabaseUnavailableError
from src.db_employee_repository import (
    EmployeeRepositoryWiringError,
    open_employee_repository,
)
from src.employee import Employee
from src.employee_import import (
    parse_artifact,
    report_payload,
    run_import,
)
from src.employee_repository import (
    EmployeeAlreadyExists,
    EmployeeRepository,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CREATED_DT = datetime(2026, 10, 31, 9, 12, 44, 123456, tzinfo=UTC)
CREATED_AT = CREATED_DT.isoformat()

_GET_PREFIX = "SELECT employee_id, full_name, is_active, created_at"
_INSERT_PREFIX = "INSERT INTO employee"


# ---------------------------------------------------------------------------
# Fake psycopg-3-shaped driver: one shared server, many connections
# ---------------------------------------------------------------------------


class FakeDriverError(Exception):
    """Driver exception carrying only a SQLSTATE (no message payload)."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("simulated driver failure")
        self.sqlstate = sqlstate


class FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class FakeServer:
    """In-memory stand-in for one PostgreSQL database.

    Holds the employee rows plus the ordered event log every connection
    writes to, so ordering claims (read-only before statements, close
    last) are observed from state rather than asserted from call names.
    """

    def __init__(self, employees: tuple[Employee, ...] = ()) -> None:
        self.rows: dict[str, Employee] = {
            employee.employee_id: employee for employee in employees
        }
        self.events: list[tuple[str, object]] = []
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.connections = 0
        self.commits = 0
        self.rollbacks = 0
        # Failure injection (each defaults to "healthy").
        self.fail_read_only_set: BaseException | None = None
        self.ignore_read_only = False
        self.fail_execute: BaseException | None = None
        self.fail_commit: BaseException | None = None
        self.fail_rollback: BaseException | None = None
        self.fail_close: BaseException | None = None
        self.race_ids: set[str] = set()

    # -- connection factory -------------------------------------------------
    def open(self, dsn: str | None = None) -> "FakeConnection":
        self.connections += 1
        self.events.append(("connect", dsn))
        return FakeConnection(self)

    def statements_matching(self, prefix: str) -> list[tuple[str, tuple[object, ...]]]:
        return [item for item in self.statements if item[0].startswith(prefix)]

    # -- statement handling -------------------------------------------------
    def handle(self, sql: str, params: tuple[object, ...]) -> FakeCursor:
        self.statements.append((sql, params))
        if sql.startswith(_INSERT_PREFIX):
            employee = Employee(
                employee_id=params[0],
                full_name=params[1],
                is_active=params[2],
                created_at=params[3],
            )
            if employee.employee_id in self.rows or (
                employee.employee_id in self.race_ids
            ):
                # psycopg 3 shape: the SQLSTATE lives on the exception.
                raise FakeDriverError("23505")
            self.rows[employee.employee_id] = employee
            return FakeCursor([])
        if sql.startswith(_GET_PREFIX):
            row = self.rows.get(params[0])  # type: ignore[arg-type]
            if row is None:
                return FakeCursor([])
            return FakeCursor(
                [(row.employee_id, row.full_name, row.is_active, row.created_at)]
            )
        raise AssertionError(f"unexpected statement in fake server: {sql}")


class FakeConnection:
    """One fake connection: observable read-only flag and lifecycle."""

    def __init__(self, server: FakeServer) -> None:
        self._server = server
        self._read_only = False
        self.closed = False

    # -- psycopg 3's settable, readable property ---------------------------
    @property
    def read_only(self) -> bool:
        return self._read_only

    @read_only.setter
    def read_only(self, value: bool) -> None:
        if self._server.fail_read_only_set is not None:
            raise self._server.fail_read_only_set
        if self._server.ignore_read_only:
            # The driver accepted the request but does not report it back:
            # exactly the "cannot positively verify" shape.
            return
        self._read_only = bool(value)
        self._server.events.append(("read_only", bool(value)))

    # -- statements --------------------------------------------------------
    def execute(self, sql: str, params: tuple[object, ...] = ()) -> FakeCursor:
        assert not self.closed, "use after close"
        if self._server.fail_execute is not None:
            self._server.events.append(("execute_failed", sql))
            raise self._server.fail_execute
        self._server.events.append(("execute", sql))
        return self._server.handle(sql, tuple(params))

    # -- transaction / lifecycle ------------------------------------------
    def commit(self) -> None:
        assert not self.closed, "commit after close"
        if self._server.fail_commit is not None:
            self._server.events.append(("commit_failed", None))
            raise self._server.fail_commit
        self._server.commits += 1
        self._server.events.append(("commit", None))

    def rollback(self) -> None:
        if self._server.fail_rollback is not None:
            raise self._server.fail_rollback
        self._server.rollbacks += 1
        self._server.events.append(("rollback", None))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._server.events.append(("close", None))
        if self._server.fail_close is not None:
            raise self._server.fail_close


class Opener:
    """Records the DSN each attempt received; hands out server connections."""

    def __init__(self, server: FakeServer) -> None:
        self._server = server
        self.dsns: list[str | None] = []

    def __call__(self, dsn: str | None) -> FakeConnection:
        self.dsns.append(dsn)
        return self._server.open(dsn)


def _employee(
    employee_id: str = "E-001",
    *,
    full_name: str = "Ada Lovelace",
    is_active: bool = True,
    created_at: datetime = CREATED_DT,
) -> Employee:
    return Employee(
        employee_id=employee_id,
        full_name=full_name,
        is_active=is_active,
        created_at=created_at,
    )


def _record(employee_id: str = "E-001", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "employee_id": employee_id,
        "full_name": "Ada Lovelace",
        "is_active": True,
        "created_at": CREATED_AT,
    }
    payload.update(overrides)
    return payload


def _artifact(*payloads: dict[str, object]) -> str:
    import json

    return "\n".join(json.dumps(payload) for payload in payloads) + "\n"


def _events(server: FakeServer, kind: str) -> list[tuple[str, object]]:
    return [event for event in server.events if event[0] == kind]


# ---------------------------------------------------------------------------
# 1. Construction opens nothing
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_building_the_repository_opens_no_connection(self) -> None:
        def refuse(dsn: str | None) -> Any:
            raise AssertionError("construction must not open a connection")

        repository = open_employee_repository(connection_opener=refuse)
        assert isinstance(repository, EmployeeRepository)

    def test_get_is_what_actually_opens_the_connection(self) -> None:
        server = FakeServer((_employee(),))
        opener = Opener(server)
        repository = open_employee_repository(connection_opener=opener)
        assert server.connections == 0
        assert repository.get("E-001") is not None
        assert server.connections == 1
        assert opener.dsns == [None]

    @pytest.mark.parametrize("bad", ["yes", "", 0, 1, 1.0, None, []])
    def test_non_boolean_read_only_is_refused(self, bad: object) -> None:
        def refuse(dsn: str | None) -> Any:
            raise AssertionError("a refused wiring must not open a connection")

        with pytest.raises(EmployeeRepositoryWiringError):
            open_employee_repository(  # type: ignore[arg-type]
                read_only=bad, connection_opener=refuse
            )


# ---------------------------------------------------------------------------
# 2. Successful lifecycle: statements, commit, close
# ---------------------------------------------------------------------------


class TestSuccessfulLifecycle:
    def test_get_reads_the_repository_row_over_one_connection(self) -> None:
        server = FakeServer((_employee(),))
        repository = open_employee_repository(connection_opener=Opener(server))

        found = repository.get("E-001")

        assert found == _employee()
        assert server.connections == 1
        sql, params = server.statements[0]
        assert sql.startswith(_GET_PREFIX)
        assert params == ("E-001",)
        assert server.commits == 1
        assert server.rollbacks == 0

    def test_get_returns_none_when_the_row_is_absent(self) -> None:
        server = FakeServer()
        repository = open_employee_repository(connection_opener=Opener(server))
        assert repository.get("E-404") is None
        assert server.commits == 1

    def test_create_binds_parameters_and_commits(self) -> None:
        server = FakeServer()
        repository = open_employee_repository(connection_opener=Opener(server))
        employee = _employee(employee_id="E-002")

        assert repository.create(employee) is employee

        sql, params = server.statements[0]
        assert sql.startswith(_INSERT_PREFIX)
        assert params == (
            "E-002",
            "Ada Lovelace",
            True,
            CREATED_DT,
        )
        assert server.rows["E-002"] == employee
        assert server.commits == 1

    def test_every_operation_closes_its_connection(self) -> None:
        server = FakeServer()
        repository = open_employee_repository(connection_opener=Opener(server))
        repository.create(_employee())
        repository.get("E-001")

        assert server.connections == 2
        closes = _events(server, "close")
        assert len(closes) == 2
        # close is the last thing that happens for each connection.
        kinds = [kind for kind, _ in server.events]
        for index, kind in enumerate(kinds):
            if kind == "connect":
                assert kinds[index + 1 :].count("close") >= 1
        assert kinds[-1] == "close"
        assert kinds.count("commit") == 2

    def test_unique_violation_becomes_employee_already_exists(self) -> None:
        server = FakeServer((_employee(),))
        repository = open_employee_repository(connection_opener=Opener(server))
        with pytest.raises(EmployeeAlreadyExists):
            repository.create(_employee())

    def test_unrelated_sqlstate_is_never_reported_as_a_duplicate(self) -> None:
        server = FakeServer()
        server.fail_execute = FakeDriverError("23502")
        repository = open_employee_repository(connection_opener=Opener(server))

        with pytest.raises(FakeDriverError) as excinfo:
            repository.create(_employee())

        assert not isinstance(excinfo.value, EmployeeAlreadyExists)
        assert server.rollbacks == 1
        assert server.commits == 0

    def test_driver_exception_without_sqlstate_is_not_a_duplicate(self) -> None:
        server = FakeServer()
        failure = RuntimeError("no sqlstate attribute here")
        server.fail_execute = failure
        repository = open_employee_repository(connection_opener=Opener(server))

        with pytest.raises(RuntimeError) as excinfo:
            repository.create(_employee())
        assert excinfo.value is failure


# ---------------------------------------------------------------------------
# 3. Read-only dry runs: enforced and verified before the first statement
# ---------------------------------------------------------------------------


class TestReadOnlyEnforcement:
    def test_read_only_is_requested_and_verified_before_any_statement(self) -> None:
        server = FakeServer((_employee(),))
        repository = open_employee_repository(
            read_only=True, connection_opener=Opener(server)
        )

        repository.get("E-001")

        kinds = [kind for kind, _ in server.events]
        assert kinds.index("read_only") < kinds.index("execute")
        assert _events(server, "read_only") == [("read_only", True)]

    def test_writable_repository_never_requests_read_only(self) -> None:
        server = FakeServer()
        repository = open_employee_repository(connection_opener=Opener(server))
        repository.create(_employee())
        assert _events(server, "read_only") == []

    def test_read_only_setting_failure_blocks_the_query_and_closes(self) -> None:
        server = FakeServer((_employee(),))
        server.fail_read_only_set = RuntimeError("driver refused")
        repository = open_employee_repository(
            read_only=True, connection_opener=Opener(server)
        )

        with pytest.raises(EmployeeRepositoryWiringError):
            repository.get("E-001")

        assert server.statements == []
        assert server.commits == 0
        assert len(_events(server, "close")) == 1

    def test_read_only_that_does_not_report_back_is_refused(self) -> None:
        server = FakeServer((_employee(),))
        server.ignore_read_only = True
        repository = open_employee_repository(
            read_only=True, connection_opener=Opener(server)
        )

        with pytest.raises(EmployeeRepositoryWiringError):
            repository.get("E-001")

        assert server.statements == []
        assert len(_events(server, "close")) == 1

    def test_read_only_failure_does_not_leak_a_message_with_a_dsn(self) -> None:
        server = FakeServer((_employee(),))
        server.fail_read_only_set = RuntimeError("host=db.example user=admin")
        repository = open_employee_repository(
            read_only=True, connection_opener=Opener(server)
        )

        with pytest.raises(EmployeeRepositoryWiringError) as excinfo:
            repository.get("E-001")

        message = str(excinfo.value)
        assert "db.example" not in message
        assert "admin" not in message


# ---------------------------------------------------------------------------
# 4. Failure paths: rollback, close, unchanged propagation
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_execute_failure_rolls_back_and_closes(self) -> None:
        server = FakeServer()
        failure = RuntimeError("statement failed")
        server.fail_execute = failure
        repository = open_employee_repository(connection_opener=Opener(server))

        with pytest.raises(RuntimeError) as excinfo:
            repository.get("E-001")

        assert excinfo.value is failure
        assert server.rollbacks == 1
        assert server.commits == 0
        assert len(_events(server, "close")) == 1

    def test_commit_failure_rolls_back_and_closes(self) -> None:
        server = FakeServer()
        failure = RuntimeError("commit failed")
        server.fail_commit = failure
        repository = open_employee_repository(connection_opener=Opener(server))

        with pytest.raises(RuntimeError) as excinfo:
            repository.create(_employee())

        assert excinfo.value is failure
        assert server.rollbacks == 1
        assert server.commits == 0
        assert len(_events(server, "close")) == 1

    def test_rollback_failure_does_not_mask_the_primary_error(self) -> None:
        server = FakeServer()
        primary = RuntimeError("primary failure")
        server.fail_execute = primary
        server.fail_rollback = RuntimeError("rollback also failed")
        repository = open_employee_repository(connection_opener=Opener(server))

        with pytest.raises(RuntimeError) as excinfo:
            repository.get("E-001")

        assert excinfo.value is primary
        assert len(_events(server, "close")) == 1

    def test_close_failure_does_not_mask_the_primary_error(self) -> None:
        server = FakeServer()
        primary = RuntimeError("primary failure")
        server.fail_execute = primary
        server.fail_close = RuntimeError("close also failed")
        repository = open_employee_repository(connection_opener=Opener(server))

        with pytest.raises(RuntimeError) as excinfo:
            repository.get("E-001")

        assert excinfo.value is primary
        # The cleanup fault stays observable without replacing the original.
        assert any(
            "connection also failed" in note
            for note in getattr(primary, "__notes__", [])
        )

    def test_close_failure_without_another_failure_is_raised(self) -> None:
        server = FakeServer((_employee(),))
        close_failure = RuntimeError("close failed")
        server.fail_close = close_failure
        repository = open_employee_repository(connection_opener=Opener(server))

        with pytest.raises(RuntimeError) as excinfo:
            repository.get("E-001")

        assert excinfo.value is close_failure

    def test_database_unavailable_error_propagates_unchanged(self) -> None:
        failure = DatabaseUnavailableError(
            "PostgreSQL connection failed (redacted conninfo: host=***)"
        )

        def opener(dsn: str | None) -> Any:
            raise failure

        repository = open_employee_repository(connection_opener=opener)
        with pytest.raises(DatabaseUnavailableError) as excinfo:
            repository.get("E-001")
        assert excinfo.value is failure

    def test_an_error_before_any_statement_still_closes(self) -> None:
        server = FakeServer()
        server.fail_read_only_set = RuntimeError("driver refused")
        repository = open_employee_repository(
            read_only=True, connection_opener=Opener(server)
        )

        with pytest.raises(EmployeeRepositoryWiringError):
            repository.get("E-001")

        assert server.statements == []
        assert len(_events(server, "close")) == 1


# ---------------------------------------------------------------------------
# 5. DSN handling: explicit value wins, None stays the layer's business
# ---------------------------------------------------------------------------


class TestDsnHandling:
    def test_explicit_dsn_reaches_the_opener_verbatim(self) -> None:
        server = FakeServer((_employee(),))
        opener = Opener(server)
        dsn = "postgresql://owner@example.invalid:5432/attendance"
        repository = open_employee_repository(dsn, connection_opener=opener)

        repository.get("E-001")

        assert opener.dsns == [dsn]

    def test_no_dsn_is_forwarded_as_none(self) -> None:
        server = FakeServer((_employee(),))
        opener = Opener(server)
        repository = open_employee_repository(connection_opener=opener)

        repository.get("E-001")

        # Resolution of ATTENDANCE_DATABASE_URL stays in src.db, per call.
        assert opener.dsns == [None]


# ---------------------------------------------------------------------------
# 6. Import hygiene and "no second implementation"
# ---------------------------------------------------------------------------


class TestModuleHygiene:
    def test_module_defines_no_sql(self) -> None:
        source = inspect.getsource(db_employee_repository)
        for token in ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM"):
            assert token not in source

    def test_module_has_no_module_level_driver_import(self) -> None:
        source = inspect.getsource(db_employee_repository)
        assert not re.search(
            r"^\s*(?:import|from)\s+psycopg", source, flags=re.MULTILINE
        )
        # The only driver access is the lazy import inside the opener.
        assert source.count("import psycopg") == 0
        assert "from src.db import open_connection" in source

    def test_importing_the_module_does_not_import_psycopg(self) -> None:
        code = (
            "import sys;"
            f"sys.path.insert(0, r'{PROJECT_ROOT}');"
            "import src.db_employee_repository;"
            "print('psycopg' in sys.modules);"
            "print('src.db' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["False", "False"]


# ---------------------------------------------------------------------------
# 7. The importer over the wired repository (still fully offline)
# ---------------------------------------------------------------------------


class TestImporterOverWiring:
    def test_dry_run_reads_and_never_writes(self) -> None:
        server = FakeServer((_employee(employee_id="E-001"),))
        repository = open_employee_repository(
            read_only=True, connection_opener=Opener(server)
        )
        parsed = parse_artifact(
            _artifact(_record("E-001"), _record("E-002"))
        )

        report = run_import(parsed, repository=repository)

        assert report.mode == "dry-run"
        assert report.created == ()
        assert report.would_create == ("E-002",)
        assert report.identical == ("E-001",)
        assert report.clean
        assert server.statements_matching(_INSERT_PREFIX) == []
        assert all(
            sql.startswith(_GET_PREFIX) for sql, _ in server.statements
        )
        assert _events(server, "read_only") == [("read_only", True)] * 2

    def test_apply_creates_only_missing_employees(self) -> None:
        server = FakeServer((_employee(employee_id="E-001"),))
        repository = open_employee_repository(connection_opener=Opener(server))
        parsed = parse_artifact(
            _artifact(_record("E-001"), _record("E-002"))
        )

        report = run_import(parsed, repository=repository, apply=True)

        assert report.created == ("E-002",)
        assert report.identical == ("E-001",)
        assert report.clean
        inserts = server.statements_matching(_INSERT_PREFIX)
        assert len(inserts) == 1
        assert inserts[0][1][0] == "E-002"
        # The provided created_at is preserved, never re-stamped.
        assert inserts[0][1][3] == CREATED_DT
        assert set(server.rows) == {"E-001", "E-002"}

    def test_conflict_through_the_wiring_never_writes(self) -> None:
        server = FakeServer(
            (_employee(employee_id="E-001", full_name="Grace Hopper"),)
        )
        repository = open_employee_repository(connection_opener=Opener(server))
        parsed = parse_artifact(_artifact(_record("E-001")))

        report = run_import(parsed, repository=repository, apply=True)

        assert [result.fields for result in report.conflicts] == [("full_name",)]
        assert server.statements_matching(_INSERT_PREFIX) == []
        assert server.statements_matching("UPDATE") == []
        assert server.rows["E-001"].full_name == "Grace Hopper"

    def test_apply_race_unique_violation_is_a_conflict_without_retry(self) -> None:
        server = FakeServer()
        server.race_ids = {"E-002"}
        repository = open_employee_repository(connection_opener=Opener(server))
        parsed = parse_artifact(_artifact(_record("E-002")))

        report = run_import(parsed, repository=repository, apply=True)

        assert [result.employee_id for result in report.conflicts] == ["E-002"]
        assert len(server.statements_matching(_INSERT_PREFIX)) == 1
        assert "E-002" not in server.rows

    def test_unavailable_database_stops_the_run_and_reports_type_only(
        self,
    ) -> None:
        secret = "postgresql://owner:sup3rsecret@db.example:5432/attendance"

        def opener(dsn: str | None) -> Any:
            raise DatabaseUnavailableError(
                f"PostgreSQL connection failed (redacted conninfo: {secret})"
            )

        repository = open_employee_repository(connection_opener=opener)
        parsed = parse_artifact(_artifact(_record("E-001")))

        report = run_import(parsed, repository=repository, apply=True)

        assert report.stopped
        assert report.error_type == "DatabaseUnavailableError"
        payload = report_payload(report)
        assert "sup3rsecret" not in str(payload)
        assert "db.example" not in str(payload)
