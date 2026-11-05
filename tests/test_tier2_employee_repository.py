"""Tier-2 PostgreSQL verification harness (Step 1.5c-3b-prep).

**Prepared, not executed.** The dedicated test database is not provisioned,
so this module is opt-in and skips itself in every ordinary offline run.

Opt-in contract
---------------

Three independent conditions must hold before a single byte leaves the
process:

1. ``ATTENDANCE_TIER2_OPT_IN=1`` — explicit, per-shell opt-in. Without it
   every item here is *skipped* (never silently "passed"), so the offline
   suite cannot drift into connecting to anything.
2. ``ATTENDANCE_TEST_DATABASE_URL`` — the dedicated test target.
3. ``ATTENDANCE_DATABASE_URL`` — required **only** so the existing guard
   can prove the test target differs from the application target. It is
   never handed to a repository.

Then, and only then, :func:`src.test_database_guard.verify_test_database`
runs its ordered positive verification (declared identity → real
connection → read-only enforcement → connected identity → marker literal →
marker/ledger schema agreement). Any blocked outcome skips the whole
module with the guard's own typed reason code; the harness does not
weaken, bypass, or re-implement any of those checks.

Targeting discipline
--------------------

Every repository here is built with an **explicit** ``dsn=`` argument:
``open_employee_repository`` defaults to resolving
``ATTENDANCE_DATABASE_URL``, so the default is never used in this module.
There is no fallback: a missing test DSN blocks before any opener runs.

What this module never does
---------------------------

No ``CREATE``/``DROP``/``ALTER``/``TRUNCATE``, no migration execution, no
ledger or marker creation, no provisioning or repair, no updates, no
deactivations, no ``list`` (which would enumerate real employees). The
only write this harness can perform is the guarded cleanup of rows *it*
created: bound-parameter ``DELETE`` restricted to synthetic IDs it owns,
gated by :func:`src.test_database_guard.require_destructive_action`
against an explicit, single-entry manifest, and preceded by a re-read that
must match the exact tuple written by this run.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from src.config import AppSettings, load_settings
from src.db_employee_repository import open_employee_repository
from src.employee import Employee
from src.employee_import import ParsedArtifact, parse_artifact, run_import
from src.employee_repository import (
    EmployeeAlreadyExists,
    EmployeeNotFound,
    EmployeeRepository,
)
from src.test_database_guard import (
    GuardBlocked,
    VerifiedTestTarget,
    require_destructive_action,
    verify_test_database,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Explicit opt-in switch. Anything other than ``"1"`` leaves Tier-2 off.
TIER2_OPT_IN_ENV: Final[str] = "ATTENDANCE_TIER2_OPT_IN"

#: Every synthetic ID starts with this prefix (plus an 8-hex run id).
SYNTHETIC_ID_PREFIX: Final[str] = "T2"

#: Harness-generated ID shape: ``T2-<8 hex>-<2 digits>``. Cleanup accepts
#: nothing else, so a mistyped or hand-written ID can never be deleted.
_SYNTHETIC_ID_RE: Final[re.Pattern[str]] = re.compile(
    rf"^{SYNTHETIC_ID_PREFIX}-[0-9A-F]{{8}}-\d{{2}}$"
)

#: The one destructive operation this harness may request, named
#: explicitly and authorized by an explicit single-entry manifest.
DELETE_ACTION: Final[str] = "delete_synthetic_test_employees"
DESTRUCTIVE_MANIFEST: Final[tuple[str, ...]] = (DELETE_ACTION,)

_SELECT_ROWS_SQL: Final[str] = (
    "SELECT employee_id, full_name, is_active, created_at "
    "FROM employee WHERE employee_id = ANY(%s)"
)
_DELETE_ROWS_SQL: Final[str] = (
    "DELETE FROM employee WHERE employee_id = ANY(%s)"
)

_BLOCK_MESSAGES: Final[dict[str, str]] = {
    "OPT_IN_MISSING": (
        f"{TIER2_OPT_IN_ENV}=1 is not set; Tier-2 verification is opt-in"
    ),
    "TEST_DSN_MISSING": "no explicit ATTENDANCE_TEST_DATABASE_URL is configured",
    "APP_DSN_MISSING": (
        "ATTENDANCE_DATABASE_URL must also be configured so the guard can "
        "prove the test target is distinct from it"
    ),
    "NOT_VERIFIED": (
        "no positively verified test target was supplied; refusing to touch "
        "a database"
    ),
    "INVALID_SYNTHETIC_ID": (
        "the cleanup candidate list contains an ID this harness cannot have "
        "created"
    ),
}


class Tier2NotConfigured(RuntimeError):
    """A pre-flight condition is missing; nothing was opened or queried.

    ``reason`` is a stable code (guard codes where they already exist, plus
    ``OPT_IN_MISSING``/``NOT_VERIFIED``/``INVALID_SYNTHETIC_ID``) so a
    blocked run is indistinguishable from a hand-run guard refusal.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(
            f"{reason}: {message or _BLOCK_MESSAGES.get(reason, 'blocked')}"
        )


# ---------------------------------------------------------------------------
# Pre-flight (no I/O, no connection, no DSN value read beyond presence)
# ---------------------------------------------------------------------------


def tier2_opt_in_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the explicit Tier-2 opt-in switch is set to exactly ``"1"``."""
    source = os.environ if environ is None else environ
    return str(source.get(TIER2_OPT_IN_ENV, "")).strip() == "1"


def tier2_block_reason(
    settings: AppSettings, environ: Mapping[str, str] | None = None
) -> str | None:
    """Return the first missing pre-flight code, or ``None`` when complete.

    Presence only: a configured DSN's value is never read here, and nothing
    is opened. Order matters — opt-in, then the test target, then the
    companion application target the guard needs for its distinctness proof.
    """
    if not tier2_opt_in_enabled(environ):
        return "OPT_IN_MISSING"
    if not _configured(settings.test_database_url):
        return "TEST_DSN_MISSING"
    if not _configured(settings.database_url):
        return "APP_DSN_MISSING"
    return None


def _configured(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def prepare_verified_target(
    *,
    settings: AppSettings | None = None,
    verifier: Callable[[str | None, str | None], VerifiedTestTarget] | None = None,
    environ: Mapping[str, str] | None = None,
) -> VerifiedTestTarget:
    """Pre-flight, then the approved guard; never connects on a missing input.

    Raises:
        Tier2NotConfigured: a pre-flight condition is missing (no opener ran).
        GuardBlocked: the guard refused (its ordered checks decide).
    """
    resolved = settings if settings is not None else load_settings()
    reason = tier2_block_reason(resolved, environ)
    if reason is not None:
        raise Tier2NotConfigured(reason)
    verify = verify_test_database if verifier is None else verifier
    return verify(resolved.database_url, resolved.test_database_url)


def verified_target_or_skip(
    *,
    settings: AppSettings | None = None,
    verifier: Callable[[str | None, str | None], VerifiedTestTarget] | None = None,
    environ: Mapping[str, str] | None = None,
) -> VerifiedTestTarget:
    """Fixture helper: turn any blocked outcome into an explicit skip.

    A skip carries the typed reason code, so a missing prerequisite is
    visible as *skipped with a reason* — never as a green test.
    """
    try:
        return prepare_verified_target(
            settings=settings, verifier=verifier, environ=environ
        )
    except (Tier2NotConfigured, GuardBlocked) as exc:
        pytest.skip(f"tier2 blocked: {exc}")


# ---------------------------------------------------------------------------
# Repository construction (explicit test DSN only)
# ---------------------------------------------------------------------------


def _require_verified(target: object) -> VerifiedTestTarget:
    """Refuse anything the guard did not produce."""
    if not isinstance(target, VerifiedTestTarget):
        raise Tier2NotConfigured("NOT_VERIFIED")
    return target


def open_tier2_repository(
    target: VerifiedTestTarget,
    *,
    test_dsn: str | None,
    read_only: bool = False,
) -> EmployeeRepository:
    """Build the approved repository against the guarded test target.

    The DSN is passed **explicitly**: the wiring's default resolution would
    read ``ATTENDANCE_DATABASE_URL``, which this harness must never use.
    """
    _require_verified(target)
    if not _configured(test_dsn):
        raise Tier2NotConfigured("TEST_DSN_MISSING")
    return open_employee_repository(dsn=test_dsn, read_only=read_only)


# ---------------------------------------------------------------------------
# Synthetic identifiers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SyntheticRun:
    """A single run's synthetic identifier namespace.

    The 8-hex run id makes IDs globally unique across runs, and the shape
    check in :func:`cleanup_synthetic_employees` makes them provably
    harness-generated rather than hand-typed.
    """

    run_id: str

    def employee_id(self, slot: int) -> str:
        return f"{SYNTHETIC_ID_PREFIX}-{self.run_id}-{slot:02d}"

    def full_name(self, slot: int) -> str:
        return f"Tier2 Synthetic {self.run_id} {slot:02d}"

    def created_at(self, *, offset_hours: int = 0) -> datetime:
        return datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC) + timedelta(
            hours=offset_hours
        )

    def employee(self, slot: int, **overrides: object) -> Employee:
        values: dict[str, object] = {
            "employee_id": self.employee_id(slot),
            "full_name": self.full_name(slot),
            "is_active": True,
            "created_at": self.created_at(),
        }
        values.update(overrides)
        return Employee(**values)  # type: ignore[arg-type]


def new_synthetic_run(run_id: str | None = None) -> SyntheticRun:
    """Create a run namespace (random by default, injectable for tests)."""
    value = uuid.uuid4().hex[:8].upper() if run_id is None else run_id
    if not re.fullmatch(r"[0-9A-F]{8}", value):
        raise Tier2NotConfigured(
            "INVALID_SYNTHETIC_ID", "run_id must be 8 uppercase hex characters"
        )
    return SyntheticRun(value)


# ---------------------------------------------------------------------------
# Guarded cleanup of this run's own rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """What cleanup actually did — every ID accounted for, none invented."""

    deleted: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    left: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.left


def _validate_synthetic_ids(identifiers: Any) -> tuple[str, ...]:
    if isinstance(identifiers, (str, bytes)) or not isinstance(
        identifiers, (list, tuple, set, frozenset)
    ):
        raise Tier2NotConfigured(
            "INVALID_SYNTHETIC_ID", "cleanup candidates must be a collection"
        )
    ordered = tuple(sorted(identifiers))
    for identifier in ordered:
        if not isinstance(identifier, str) or not _SYNTHETIC_ID_RE.match(
            identifier
        ):
            raise Tier2NotConfigured(
                "INVALID_SYNTHETIC_ID",
                "every cleanup candidate must match the harness-generated "
                "synthetic ID shape",
            )
    return ordered


def _row_tuple(row: Any) -> tuple[Any, ...]:
    return (row[1], row[2], row[3])


def _default_connection_opener(dsn: str) -> Any:
    from src.db import open_connection  # lazy: driver only on a real run

    return open_connection(dsn)


def cleanup_synthetic_employees(
    target: VerifiedTestTarget | None,
    expected: Mapping[str, tuple[str, bool, datetime]],
    *,
    test_dsn: str | None,
    connection_opener: Callable[[str], Any] | None = None,
) -> CleanupReport:
    """Delete only this run's synthetic rows, after positive ownership.

    Safe by construction:

    * refuses without a guard-produced :class:`VerifiedTestTarget`;
    * requires the single authorized action in the manifest
      (:func:`require_destructive_action`) **before** connecting;
    * accepts only harness-shaped synthetic IDs;
    * re-reads each candidate and deletes only rows whose stored
      ``(full_name, is_active, created_at)`` equals what this run wrote —
      a row that was changed by anyone else is left untouched;
    * one bound-parameter ``DELETE ... = ANY(%s)``, no ``LIKE``, no prefix
      matching, no ``TRUNCATE``/``DROP``;
    * closes the connection on success and on failure, rolling back first.
    """
    _require_verified(target)
    identifiers = _validate_synthetic_ids(tuple(expected))
    require_destructive_action(target, DELETE_ACTION, DESTRUCTIVE_MANIFEST)
    if not identifiers:
        return CleanupReport()
    if not _configured(test_dsn):
        raise Tier2NotConfigured("TEST_DSN_MISSING")

    opener = connection_opener or _default_connection_opener
    connection = opener(test_dsn)  # type: ignore[arg-type]
    try:
        present: dict[str, tuple[Any, ...]] = {
            str(row[0]): _row_tuple(row)
            for row in connection.execute(
                _SELECT_ROWS_SQL, (list(identifiers),)
            ).fetchall()
        }
        owned = tuple(
            identifier
            for identifier in identifiers
            if present.get(identifier) == tuple(expected[identifier])
        )
        unowned_present = tuple(
            identifier
            for identifier in identifiers
            if identifier in present and identifier not in owned
        )
        absent = tuple(
            identifier
            for identifier in identifiers
            if identifier not in present
        )
        if not owned:
            return CleanupReport((), absent, unowned_present)

        connection.execute(_DELETE_ROWS_SQL, (list(owned),))
        connection.commit()
        remaining = {
            str(row[0])
            for row in connection.execute(
                _SELECT_ROWS_SQL, (list(owned),)
            ).fetchall()
        }
        deleted = tuple(i for i in owned if i not in remaining)
        left = tuple(sorted(unowned_present + tuple(
            i for i in owned if i in remaining
        )))
        return CleanupReport(deleted, absent, left)
    except BaseException:
        try:
            connection.rollback()
        except Exception:
            pass
        raise
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Artifact helper (synthetic JSONL, temporary directory only)
# ---------------------------------------------------------------------------


def artifact_for(
    path: Path, records: tuple[Mapping[str, object], ...]
) -> ParsedArtifact:
    """Write synthetic JSONL to ``path`` and return its parsed form."""
    lines = [
        json.dumps(
            {
                "schema_version": 1,
                "employee_id": record["employee_id"],
                "full_name": record["full_name"],
                "is_active": record["is_active"],
                "created_at": record["created_at"],
            }
        )
        for record in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return parse_artifact(path.read_text(encoding="utf-8"))


def record_for(employee: Employee) -> dict[str, object]:
    """Artifact record matching ``employee`` exactly (five-key contract)."""
    return {
        "employee_id": employee.employee_id,
        "full_name": employee.full_name,
        "is_active": employee.is_active,
        "created_at": employee.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Live scenarios (skipped unless the opt-in and the guarded target exist)
# ---------------------------------------------------------------------------

_SKIP_REASON: Final[str] = (
    "Tier-2 PostgreSQL verification is opt-in: set "
    f"{TIER2_OPT_IN_ENV}=1 with a guarded ATTENDANCE_TEST_DATABASE_URL"
)

pytestmark = [
    pytest.mark.tier2,
    pytest.mark.skipif(not tier2_opt_in_enabled(), reason=_SKIP_REASON),
]


@pytest.fixture(scope="module")
def tier2_settings() -> AppSettings:
    return load_settings()


@pytest.fixture(scope="module")
def verified_target(tier2_settings: AppSettings) -> VerifiedTestTarget:
    return verified_target_or_skip(settings=tier2_settings)


@pytest.fixture(scope="module")
def test_dsn(tier2_settings: AppSettings) -> str:
    value = tier2_settings.test_database_url
    if not _configured(value):
        pytest.skip("tier2 blocked: TEST_DSN_MISSING")
    return value  # type: ignore[return-value]


@pytest.fixture(scope="module")
def run() -> SyntheticRun:
    return new_synthetic_run()


@pytest.fixture
def repository(
    verified_target: VerifiedTestTarget, test_dsn: str
) -> EmployeeRepository:
    return open_tier2_repository(verified_target, test_dsn=test_dsn)


@pytest.fixture
def read_only_repository(
    verified_target: VerifiedTestTarget, test_dsn: str
) -> EmployeeRepository:
    return open_tier2_repository(
        verified_target, test_dsn=test_dsn, read_only=True
    )


@pytest.fixture
def synthetic_rows(
    verified_target: VerifiedTestTarget, test_dsn: str
) -> Iterator[Callable[[Employee], Employee]]:
    """Record intended rows, then delete this run's own rows in teardown."""
    intended: dict[str, tuple[str, bool, datetime]] = {}

    def record(employee: Employee) -> Employee:
        intended[employee.employee_id] = (
            employee.full_name,
            employee.is_active,
            employee.created_at,
        )
        return employee

    yield record

    if not intended:
        return
    report = cleanup_synthetic_employees(
        verified_target, intended, test_dsn=test_dsn
    )
    if report.left:
        pytest.fail(f"tier2 cleanup left synthetic rows untouched: {report.left}")


class _SpyRepository:
    """Delegating wrapper that records calls and forbids mutations.

    Used to prove (a) a create race is reported as a conflict and (b) the
    importer never reaches a name update or a deactivation.
    """

    def __init__(
        self, real: EmployeeRepository, *, miss_all_gets: bool = False
    ) -> None:
        self._real = real
        self._miss_all_gets = miss_all_gets
        self.calls: list[tuple[str, str]] = []
        self.forbidden: list[str] = []

    def get(self, employee_id: str) -> Employee | None:
        self.calls.append(("get", employee_id))
        if self._miss_all_gets:
            return None
        return self._real.get(employee_id)

    def create(self, employee: Employee) -> Employee:
        self.calls.append(("create", employee.employee_id))
        return self._real.create(employee)

    def update_full_name(self, *args: object, **kwargs: object) -> Employee:
        self.forbidden.append("update_full_name")
        raise AssertionError("importer must never update a name")

    def deactivate(self, *args: object, **kwargs: object) -> Employee:
        self.forbidden.append("deactivate")
        raise AssertionError("importer must never deactivate")

    def list(self, *args: object, **kwargs: object) -> list[Employee]:
        self.forbidden.append("list")
        raise AssertionError("Tier-2 must never enumerate employees")


# -- 1. Real connection opens and closes ------------------------------------


def test_real_connection_opens_closes_and_answers(
    repository: EmployeeRepository, run: SyntheticRun
) -> None:
    absent = run.employee_id(90)
    assert repository.get(absent) is None
    # A second operation proves the previous connection was closed cleanly
    # (no leaked transaction, no exhausted pool).
    assert repository.get(absent) is None


# -- 2. PostgreSQL rejects a write inside a read-only transaction -----------


def test_postgresql_rejects_write_in_read_only_transaction(
    read_only_repository: EmployeeRepository,
    repository: EmployeeRepository,
    run: SyntheticRun,
) -> None:
    employee = run.employee(1)
    with pytest.raises(Exception) as excinfo:  # driver type is not pinned here
        read_only_repository.create(employee)
    assert getattr(excinfo.value, "sqlstate", None) == "25006", (
        "PostgreSQL must refuse the write with read_only_sql_transaction, "
        f"got {type(excinfo.value).__name__}"
    )
    assert not isinstance(excinfo.value, EmployeeAlreadyExists)
    # The write genuinely did not happen.
    assert repository.get(employee.employee_id) is None


# -- 3. Dry run classifies without creating --------------------------------


def test_dry_run_classifies_missing_employee_without_creating(
    read_only_repository: EmployeeRepository,
    repository: EmployeeRepository,
    run: SyntheticRun,
    tmp_path: Path,
) -> None:
    employee = run.employee(2)
    parsed = artifact_for(tmp_path / "dry-run.jsonl", (record_for(employee),))

    report = run_import(parsed, repository=read_only_repository)

    assert report.mode == "dry-run"
    assert report.would_create == (employee.employee_id,)
    assert report.created == ()
    assert repository.get(employee.employee_id) is None


# -- 4. Apply creates the missing synthetic employee -----------------------


def test_apply_creates_missing_employee(
    repository: EmployeeRepository,
    run: SyntheticRun,
    synthetic_rows: Callable[[Employee], Employee],
    tmp_path: Path,
) -> None:
    employee = run.employee(3)
    parsed = artifact_for(tmp_path / "apply.jsonl", (record_for(employee),))
    synthetic_rows(employee)

    report = run_import(parsed, repository=repository, apply=True)

    assert report.created == (employee.employee_id,)
    stored = repository.get(employee.employee_id)
    assert stored == employee
    assert stored is not None and stored.created_at == employee.created_at


# -- 5. A repeated import is identical, never a duplicate ------------------


def test_repeated_import_is_identical(
    repository: EmployeeRepository,
    run: SyntheticRun,
    synthetic_rows: Callable[[Employee], Employee],
    tmp_path: Path,
) -> None:
    employee = run.employee(4)
    parsed = artifact_for(tmp_path / "repeat.jsonl", (record_for(employee),))
    synthetic_rows(employee)
    assert run_import(parsed, repository=repository, apply=True).created == (
        employee.employee_id,
    )

    second = run_import(parsed, repository=repository, apply=True)

    assert second.identical == (employee.employee_id,)
    assert second.created == ()
    assert second.conflicts == ()
    assert repository.get(employee.employee_id) == employee


# -- 6. Any differing field is a conflict and never updates ---------------


@pytest.mark.parametrize("field", ["full_name", "is_active", "created_at"])
def test_conflicting_field_is_reported_and_never_updated(
    field: str,
    repository: EmployeeRepository,
    run: SyntheticRun,
    synthetic_rows: Callable[[Employee], Employee],
    tmp_path: Path,
) -> None:
    slot = {"full_name": 5, "is_active": 6, "created_at": 7}[field]
    employee = run.employee(slot)
    synthetic_rows(employee)
    assert repository.create(employee) == employee

    if field == "full_name":
        incoming = run.employee(slot, full_name=run.full_name(slot) + " Edited")
    elif field == "is_active":
        incoming = run.employee(slot, is_active=False)
    else:
        incoming = run.employee(slot, created_at=run.created_at(offset_hours=1))
    parsed = artifact_for(tmp_path / f"conflict-{field}.jsonl", (record_for(incoming),))

    report = run_import(parsed, repository=repository, apply=True)

    assert [result.fields for result in report.conflicts] == [(field,)]
    assert report.created == ()
    assert repository.get(employee.employee_id) == employee  # never updated


# -- 7. Duplicate create: typed error, and a race becomes a conflict ------


def test_duplicate_create_raises_employee_already_exists(
    repository: EmployeeRepository,
    run: SyntheticRun,
    synthetic_rows: Callable[[Employee], Employee],
) -> None:
    employee = run.employee(8)
    synthetic_rows(employee)
    repository.create(employee)

    with pytest.raises(EmployeeAlreadyExists):
        repository.create(employee)

    assert repository.get(employee.employee_id) == employee


def test_create_race_is_reported_as_conflict(
    repository: EmployeeRepository,
    run: SyntheticRun,
    synthetic_rows: Callable[[Employee], Employee],
    tmp_path: Path,
) -> None:
    employee = run.employee(9)
    synthetic_rows(employee)
    assert repository.create(employee) == employee
    parsed = artifact_for(tmp_path / "race.jsonl", (record_for(employee),))
    # get() misses, so the importer issues a create that hits the real
    # unique constraint — exactly the approved race path.
    spy = _SpyRepository(repository, miss_all_gets=True)

    report = run_import(parsed, repository=spy, apply=True)

    assert [result.employee_id for result in report.conflicts] == [
        employee.employee_id
    ]
    assert spy.calls == [("get", employee.employee_id), ("create", employee.employee_id)]
    assert repository.get(employee.employee_id) == employee


# -- 8. Rollback and connection cleanup on success and failure ------------


def test_failed_transaction_leaves_no_state_and_connection_reusable(
    read_only_repository: EmployeeRepository,
    repository: EmployeeRepository,
    run: SyntheticRun,
    synthetic_rows: Callable[[Employee], Employee],
) -> None:
    employee = run.employee(10)
    synthetic_rows(employee)
    with pytest.raises(Exception):
        read_only_repository.create(employee)  # read-only refusal, rolled back

    assert repository.create(employee) == employee
    assert repository.get(employee.employee_id) == employee


def test_missing_employee_update_is_typed_and_does_not_write(
    repository: EmployeeRepository, run: SyntheticRun
) -> None:
    # A non-existent synthetic ID keeps this inside the harness namespace.
    with pytest.raises(EmployeeNotFound):
        repository.update_full_name(run.employee_id(11), "Tier2 Synthetic Nobody")
    assert repository.get(run.employee_id(11)) is None


# -- 9. The importer never updates or deactivates -------------------------


def test_importer_never_updates_or_deactivates(
    repository: EmployeeRepository,
    run: SyntheticRun,
    synthetic_rows: Callable[[Employee], Employee],
    tmp_path: Path,
) -> None:
    employee = run.employee(12)
    synthetic_rows(employee)
    assert repository.create(employee) == employee
    incoming = run.employee(12, full_name=run.full_name(12) + " Edited")
    parsed = artifact_for(tmp_path / "no-mutation.jsonl", (record_for(incoming),))
    spy = _SpyRepository(repository)

    report = run_import(parsed, repository=spy, apply=True)

    assert [result.fields for result in report.conflicts] == [("full_name",)]
    assert spy.forbidden == []
    assert repository.get(employee.employee_id) == employee
