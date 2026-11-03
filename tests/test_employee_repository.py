"""Phase 3, Step 1.5b — offline employee-repository tests
(``src/employee_repository.py``).

Fake connections, synthetic records — no PostgreSQL, no drivers, no
cameras, no real runtime data. Coverage follows the authorized matrix:
create / duplicate / get (present, missing) / keyset pagination with
default + clamped + invalid sizes / full-name-only update semantics /
deactivation / unavailable-DB propagation / no biometric or attendance
data in any SQL / fresh-process import hygiene.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from src.config import PAGINATION
from src.db import DatabaseUnavailableError
from src.employee import Employee, EmployeeValidationError
from src.employee_repository import (
    EmployeeAlreadyExists,
    EmployeeNotFound,
    EmployeeRepository,
)

CREATED_AT = datetime(2026, 11, 1, 9, 30, 0, tzinfo=UTC)

_FORBIDDEN_TERMS: tuple[str, ...] = (
    "embedding",
    "face",
    "image",
    "photo",
    "vector",
    "attendance",
    "journal",
    "check_in",
    "check_out",
    "camera",
)


class Row:
    """A result row shape psycopg would return (tuple-like)."""

    def __init__(self, *values: object) -> None:
        self._values = values

    def __len__(self) -> int:
        return len(self._values)

    def __getitem__(self, index: int) -> object:
        return self._values[index]


class FakeResult:
    def __init__(self, rows: list[Row]) -> None:
        self._rows = list(rows)

    def fetchone(self) -> Row | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Row]:
        return list(self._rows)


class UniqueViolation(RuntimeError):
    """Fake driver error shaped like **psycopg 3**'s
    ``psycopg.errors.UniqueViolation``: the SQLSTATE lives on
    ``sqlstate``. psycopg 3 has no ``pgcode`` attribute (that is
    psycopg2), so the fake must not manufacture one — a fake that sets
    ``pgcode`` would let the repository's duplicate detection pass
    offline while failing against the pinned driver."""

    sqlstate = "23505"


class NotNullViolation(RuntimeError):
    """Fake driver error with an unrelated SQLSTATE (23502)."""

    sqlstate = "23502"


class LegacyPsycopg2StyleError(RuntimeError):
    """Fake carrying only psycopg2's ``pgcode`` — psycopg 3 never does."""

    pgcode = "23505"


class FakeConnection:
    """Minimal connection/cursor shape backing the repository contract.

    Stores only approved metadata rows keyed by employee_id.
    """

    def __init__(self, data: dict[str, tuple] | None = None) -> None:
        self.data: dict[str, tuple] = dict(data or {})
        self.executed: list[tuple[str, tuple]] = []
        self.unavailable = False
        self.opened = False
        #: When set, INSERTs raise this instead of the built-in duplicate
        #: error — lets tests exercise real psycopg 3 error shapes.
        self.insert_error: Exception | None = None

    def __enter__(self) -> FakeConnection:
        self.opened = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql: str, params: tuple) -> FakeResult:
        if self.unavailable:
            raise DatabaseUnavailableError("synthetic: postgres unreachable")
        self.executed.append((sql, params))
        sql_l = " ".join(sql.split()).lower()
        if sql_l.startswith("insert"):
            if self.insert_error is not None:
                raise self.insert_error
            employee_id = params[0]
            if employee_id in self.data:
                raise UniqueViolation("duplicate key value violates unique constraint")
            self.data[employee_id] = params
            return FakeResult([])
        if sql_l.startswith("select"):
            if "where employee_id >" in sql_l:
                after = params[0]
                rows = [
                    Row(*values)
                    for employee_id, values in sorted(self.data.items())
                    if employee_id > after
                ][: params[1]]
            elif "where employee_id =" in sql_l:
                values = self.data.get(params[0])
                rows = [Row(*values)] if values is not None else []
            else:
                rows = [
                    Row(*values)
                    for _, values in sorted(self.data.items())
                ][: params[0]]
            return FakeResult(rows)
        if sql_l.startswith("update"):
            employee_id = params[-1]
            values = self.data.get(employee_id)
            if values is None:
                return FakeResult([])
            if "set full_name" in sql_l:
                new_values = (values[0], params[0], values[2], values[3])
            else:
                new_values = (values[0], values[1], False, values[3])
            self.data[employee_id] = new_values
            return FakeResult([Row(*new_values)])
        raise AssertionError(f"unexpected SQL in fake: {sql!r}")


class FakeFactory:
    def __init__(self, data: dict[str, tuple] | None = None) -> None:
        self.connection = FakeConnection(data)
        self.build_count = 0

    def __call__(self) -> FakeConnection:
        self.build_count += 1
        return self.connection


def make_employee(
    employee_id: str = "E-001",
    full_name: str = "Ada Lovelace",
    is_active: bool = True,
) -> Employee:
    return Employee(
        employee_id=employee_id,
        full_name=full_name,
        is_active=is_active,
        created_at=CREATED_AT,
    )


def repository_with(
    data: dict[str, tuple] | None = None,
) -> tuple[EmployeeRepository, FakeFactory]:
    factory = FakeFactory(data)
    return EmployeeRepository(factory), factory


def test_create_inserts_metadata_columns_only() -> None:
    repo, factory = repository_with()
    employee = make_employee()
    returned = repo.create(employee)
    assert returned is employee
    ((sql, params),) = factory.connection.executed
    assert "INSERT INTO employee" in sql
    assert params == ("E-001", "Ada Lovelace", True, CREATED_AT)


def test_create_duplicate_maps_to_typed_already_exists() -> None:
    existing = make_employee()
    repo, _ = repository_with({"E-001": ("E-001", "Ada Lovelace", True, CREATED_AT)})
    with pytest.raises(EmployeeAlreadyExists):
        repo.create(existing)


def test_fake_driver_model_has_no_psycopg2_pgcode_attribute() -> None:
    """H-1 regression guard for the test double itself: the fake driver
    must model psycopg 3 (``sqlstate``), because a fake carrying
    ``pgcode`` was what let the original defect pass 20/20 offline."""
    violation = UniqueViolation("duplicate key value violates unique constraint")
    assert getattr(violation, "sqlstate", None) == "23505"
    assert not hasattr(violation, "pgcode")


def test_create_psycopg3_unique_violation_maps_to_already_exists() -> None:
    repo, factory = repository_with()
    factory.connection.insert_error = UniqueViolation(
        "duplicate key value violates unique constraint "
        '"employee_pkey"'
    )
    with pytest.raises(EmployeeAlreadyExists):
        repo.create(make_employee())


def test_create_unrelated_sqlstate_propagates_untouched() -> None:
    """A non-23505 SQLSTATE is never a duplicate: it must surface as the
    driver error, not as ``EmployeeAlreadyExists``."""
    repo, factory = repository_with()
    error = NotNullViolation("null value in column \"full_name\"")
    factory.connection.insert_error = error
    with pytest.raises(NotNullViolation) as caught:
        repo.create(make_employee())
    assert caught.value is error


def test_create_psycopg2_style_pgcode_only_is_not_a_duplicate() -> None:
    """Detection reads psycopg 3's ``sqlstate``; an exception carrying
    only psycopg2's ``pgcode`` is not a classified duplicate."""
    repo, factory = repository_with()
    error = LegacyPsycopg2StyleError("synthetic: psycopg2-style attribute only")
    factory.connection.insert_error = error
    with pytest.raises(LegacyPsycopg2StyleError):
        repo.create(make_employee())


def test_create_exception_without_sqlstate_propagates_untouched() -> None:
    repo, factory = repository_with()
    error = RuntimeError("synthetic: no driver metadata at all")
    factory.connection.insert_error = error
    with pytest.raises(RuntimeError) as caught:
        repo.create(make_employee())
    assert caught.value is error


def test_create_non_employee_input_refused_before_any_db_call() -> None:
    repo, factory = repository_with()
    with pytest.raises(EmployeeValidationError):
        repo.create("E-4044")  # type: ignore[arg-type]
    assert factory.connection.executed == []


def test_get_returns_model_for_existing_employee() -> None:
    repo, _ = repository_with(
        {"E-001": ("E-001", "Ada Lovelace", True, CREATED_AT)}
    )
    employee = repo.get("E-001")
    assert employee is not None
    assert employee.employee_id == "E-001"
    assert employee.full_name == "Ada Lovelace"
    assert employee.is_active is True
    assert employee.created_at == CREATED_AT


def test_get_missing_employee_returns_none() -> None:
    repo, _ = repository_with({})
    assert repo.get("E-404") is None


def test_get_blank_or_non_string_id_refused() -> None:
    repo, factory = repository_with()
    for bad_id in ("", "   ", 7, None):
        with pytest.raises(EmployeeValidationError):
            repo.get(bad_id)  # type: ignore[arg-type]
    assert factory.connection.executed == []


def test_list_default_page_size_and_keyset_ordering() -> None:
    rows = {f"E-{n:03d}": (f"E-{n:03d}", f"Name {n}", True, CREATED_AT) for n in range(1, 31)}
    repo, _ = repository_with(rows)
    page = repo.list()
    assert len(page) == PAGINATION.default_page_size == 20
    assert [employee.employee_id for employee in page] == sorted(
        employee.employee_id for employee in page
    )
    page2 = repo.list(after=page[-1].employee_id)
    assert page2[0].employee_id > page[-1].employee_id
    assert len(page2) == 10  # rows 21..30 — the remaining page
    assert len(page2) <= PAGINATION.max_page_size


def test_list_limit_clamped_to_hard_max() -> None:
    rows = {f"E-{n:03d}": (f"E-{n:03d}", f"Name {n}", True, CREATED_AT) for n in range(1, 151)}
    repo, factory = repository_with(rows)
    page = repo.list(limit=5_000)
    assert len(page) == PAGINATION.max_page_size == 100
    ((_, params),) = factory.connection.executed
    assert params == (100,)


def test_list_invalid_limit_refused_before_db_call() -> None:
    repo, factory = repository_with()
    for bad in (0, -3, "20", 2.5, True):
        with pytest.raises(EmployeeValidationError):
            repo.list(limit=bad)  # type: ignore[arg-type]
    assert factory.connection.executed == []


def test_list_invalid_after_refused_before_db_call() -> None:
    repo, factory = repository_with()
    for bad in ("   ", "", 13, object()):
        with pytest.raises(EmployeeValidationError):
            repo.list(after=bad)  # type: ignore[union-attr]
    assert factory.connection.executed == []


def test_update_full_name_changes_only_the_name() -> None:
    repo, factory = repository_with(
        {"E-001": ("E-001", "Ada Lovelace", True, CREATED_AT)}
    )
    updated = repo.update_full_name("E-001", "  Ada   King  ")
    assert updated.full_name == "Ada King"  # model normalization rules
    assert updated.employee_id == "E-001"
    assert updated.is_active is True
    assert updated.created_at == CREATED_AT  # untouched by the update
    ((sql, params),) = factory.connection.executed
    assert params == ("Ada King", "E-001")
    assert "is_active" in sql  # present only in the RETURNING list
    assert "SET full_name =" in " ".join(sql.split())
    assert factory.connection.data["E-001"] == (
        "E-001",
        "Ada King",
        True,
        CREATED_AT,
    )


def test_update_full_name_missing_employee_typed_not_found() -> None:
    repo, _ = repository_with({})
    with pytest.raises(EmployeeNotFound):
        repo.update_full_name("E-404", "Nobody")


def test_update_full_name_blank_name_refused_before_db_call() -> None:
    repo, factory = repository_with(
        {"E-001": ("E-001", "Ada Lovelace", True, CREATED_AT)}
    )
    with pytest.raises(EmployeeValidationError):
        repo.update_full_name("E-001", "   ")
    assert factory.connection.executed == []


def test_deactivate_sets_is_active_false_and_returns_read_back() -> None:
    repo, factory = repository_with(
        {"E-001": ("E-001", "Ada Lovelace", True, CREATED_AT)}
    )
    deactivated = repo.deactivate("E-001")
    assert deactivated.is_active is False
    assert deactivated.employee_id == "E-001"
    assert deactivated.created_at == CREATED_AT
    assert factory.connection.data["E-001"][2] is False


def test_deactivate_missing_employee_typed_not_found() -> None:
    repo, _ = repository_with({})
    with pytest.raises(EmployeeNotFound):
        repo.deactivate("E-404")


def test_deactivate_blank_id_refused_before_db_call() -> None:
    repo, factory = repository_with()
    with pytest.raises(EmployeeValidationError):
        repo.deactivate("  ")
    assert factory.connection.executed == []


def test_database_unavailable_propagates_untouched() -> None:
    repo, factory = repository_with()
    factory.connection.unavailable = True
    with pytest.raises(DatabaseUnavailableError):
        repo.get("E-001")
    with pytest.raises(DatabaseUnavailableError):
        repo.create(make_employee())


def test_no_connection_on_construction() -> None:
    factory_calls: list[int] = []

    class NeverConnectFactory:
        def __call__(self) -> None:
            factory_calls.append(1)
            raise AssertionError("repository must not connect")

    EmployeeRepository(NeverConnectFactory())  # type: ignore[arg-type]
    assert factory_calls == []


def test_sql_makes_no_biometric_or_attendance_reference() -> None:
    import src.employee_repository as module

    sql_names = [
        name
        for name in dir(module)
        if name.isupper() and ("_SQL" in name or name.endswith("_PREFIX"))
    ]
    assert {"_INSERT_SQL", "_GET_SQL", "_LIST_SQL_PREFIX", "_UPDATE_NAME_SQL", "_DEACTIVATE_SQL"} <= set(sql_names)
    texts = [str(getattr(module, name)) for name in sql_names]
    for text in texts:
        lowered = text.lower()
        for term in _FORBIDDEN_TERMS:
            assert term not in lowered, f"{term!r} must never appear in repository SQL"


def test_import_hygiene_fresh_process() -> None:
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "import src.employee_repository;"
        "banned = {'psycopg', 'cv2', 'fastapi', 'uvicorn',"
        "'src.db', 'src.attendance', 'src.attendance_store', 'src.camera'};"
        "loaded = banned & set(sys.modules);"
        "assert not loaded, f'unexpected imports: {loaded}';"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")
