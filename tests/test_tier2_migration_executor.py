"""Live Tier-2 verification of the migration executor (Phase 3 — Step 1.5b-6).

**Read/lock-only.** This module may open the dedicated test database and:

* read a status snapshot (one ``REPEATABLE READ READ ONLY`` transaction);
* validate the existing ledger without creating or altering anything;
* take and release the session advisory lock, including a contention check;
* attempt one migration that **fails inside its own transaction** and is
  rolled back, proving atomicity without committing anything;
* fingerprint the catalog before and after, which must be identical.

It never runs ``migrate()`` on a target with pending work (it skips instead),
never executes DDL, never inserts/updates/deletes a row, never touches a
guard row, and never connects to the application database — every executor is
bound to the guard-verified test DSN through
:func:`tier2_support.bind_target_to_dsn`.

Opt-in contract (unchanged): ``ATTENDANCE_TIER2_OPT_IN=1`` plus a guarded
``ATTENDANCE_TEST_DATABASE_URL``; a blocked guard skips with its typed reason
code rather than passing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.db_migrate import (
    MigrationChecksumError,
    MigrationSnapshot,
    migrate,
    read_state,
)
from src.db_migrate_executor import MigrationDriverError, PsycopgMigrationExecutor
from src.db_target import same_target

from tier2_support import (
    CatalogInventory,
    Tier2NotConfigured,
    bind_target_to_dsn,
    catalog_inventory,
    is_configured,
    tier2_opt_in_enabled,
    verified_target_or_skip,
)

from src.config import AppSettings, load_settings
from src.test_database_guard import VerifiedTestTarget

_SKIP_REASON = (
    "Tier-2 PostgreSQL verification is opt-in: set ATTENDANCE_TIER2_OPT_IN=1 "
    "with a guarded ATTENDANCE_TEST_DATABASE_URL"
)
_SKIP_FIXTURE_REASON = "tier2 blocked: TEST_DSN_MISSING"

pytestmark = [
    pytest.mark.tier2,
    pytest.mark.skipif(not tier2_opt_in_enabled(), reason=_SKIP_REASON),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tier2_settings() -> AppSettings:
    return load_settings()


@pytest.fixture(scope="module")
def verified_target(tier2_settings: AppSettings) -> VerifiedTestTarget:
    return verified_target_or_skip(settings=tier2_settings)


@pytest.fixture(scope="module")
def test_dsn(tier2_settings: AppSettings) -> str:
    value = tier2_settings.test_database_url
    if not is_configured(value):
        pytest.skip(_SKIP_FIXTURE_REASON)
    return value  # type: ignore[return-value]


@pytest.fixture(scope="module")
def bound_dsn(verified_target: VerifiedTestTarget, test_dsn: str) -> str:
    return bind_target_to_dsn(verified_target, test_dsn)


@pytest.fixture(scope="module")
def executor(bound_dsn: str) -> PsycopgMigrationExecutor:
    return PsycopgMigrationExecutor(bound_dsn)


@pytest.fixture(scope="module")
def inventory_baseline(test_dsn: str) -> CatalogInventory:
    return catalog_inventory(test_dsn)


# ---------------------------------------------------------------------------
# Target binding
# ---------------------------------------------------------------------------


def test_executor_is_bound_to_the_verified_test_target(
    bound_dsn: str, test_dsn: str, tier2_settings: AppSettings
) -> None:
    assert bound_dsn == test_dsn
    assert same_target(bound_dsn, test_dsn) is True
    assert tier2_settings.database_url is not None
    assert same_target(bound_dsn, tier2_settings.database_url) is False, (
        "the verified test target must never denote the application database"
    )


def test_repr_never_reveals_the_target(executor: PsycopgMigrationExecutor) -> None:
    text = repr(executor)
    assert "postgresql://" not in text
    assert "host=" not in text


def test_a_different_database_is_refused_before_connecting(
    verified_target: VerifiedTestTarget,
) -> None:
    wrong = f"host={verified_target.host} port={verified_target.port} dbname=not_the_test_db"

    with pytest.raises(Tier2NotConfigured) as excinfo:
        bind_target_to_dsn(verified_target, wrong)

    assert excinfo.value.reason == "TARGET_MISMATCH"


def test_an_unverified_target_is_refused(test_dsn: str) -> None:
    with pytest.raises(Tier2NotConfigured) as excinfo:
        bind_target_to_dsn(SimpleNamespace(dbname="x"), test_dsn)

    assert excinfo.value.reason == "NOT_VERIFIED"


def test_a_blank_dsn_is_refused(verified_target: VerifiedTestTarget) -> None:
    with pytest.raises(Tier2NotConfigured) as excinfo:
        bind_target_to_dsn(verified_target, "   ")

    assert excinfo.value.reason == "TEST_DSN_MISSING"


# ---------------------------------------------------------------------------
# Read-only status
# ---------------------------------------------------------------------------


def test_status_snapshot_is_consistent(
    executor: PsycopgMigrationExecutor, inventory_baseline: CatalogInventory
) -> None:
    snapshot = executor.read_snapshot()

    assert snapshot.ledger_present is True
    assert "schema_version" in snapshot.tables
    assert "employee" in snapshot.tables
    assert snapshot.applied, "the provisioned ledger must record V001"
    assert all(isinstance(version, int) for version, _ in snapshot.applied)
    # Same view the inventory reads: the ledger has not moved.
    assert snapshot.applied == inventory_baseline.ledger_rows


def test_read_state_matches_the_live_snapshot(
    executor: PsycopgMigrationExecutor,
) -> None:
    snapshot = executor.read_snapshot()
    state = read_state(snapshot)

    assert state.ledger_present is True
    assert state.previous_version == max(version for version, _ in snapshot.applied)
    assert state.applied == snapshot.applied
    assert all(version > state.previous_version for version in state.pending)


# ---------------------------------------------------------------------------
# Ledger: no-op validation, never creation
# ---------------------------------------------------------------------------


def test_ensure_ledger_is_a_no_op_on_the_existing_ledger(
    executor: PsycopgMigrationExecutor,
    test_dsn: str,
    inventory_baseline: CatalogInventory,
) -> None:
    # The runtime role has no CREATE privilege in the test database, so this
    # passing at all proves the presence check precedes any CREATE TABLE.
    executor.ensure_ledger()

    assert catalog_inventory(test_dsn) == inventory_baseline


# ---------------------------------------------------------------------------
# Fan-out: migrate() is a no-op on a fully applied target
# ---------------------------------------------------------------------------


def test_migrate_is_a_no_op_on_a_fully_applied_target(
    executor: PsycopgMigrationExecutor,
    test_dsn: str,
    inventory_baseline: CatalogInventory,
) -> None:
    state = read_state(executor.read_snapshot())
    if state.pending:
        pytest.skip(
            "the test target has pending migrations; applying them is out of "
            "scope for this read/lock-only harness"
        )

    outcome = migrate(executor)

    assert outcome.applied_versions == ()
    assert outcome.previous_version == state.previous_version
    assert outcome.current_version == state.previous_version
    assert catalog_inventory(test_dsn) == inventory_baseline


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------


def test_advisory_lock_contention_and_release(bound_dsn: str) -> None:
    first = PsycopgMigrationExecutor(bound_dsn)
    second = PsycopgMigrationExecutor(bound_dsn)

    assert first.acquire_advisory_lock() is True
    try:
        assert second.acquire_advisory_lock() is False, (
            "a second runner must not acquire the same advisory lock"
        )
    finally:
        first.release_advisory_lock()

    # The lock is genuinely free again once the holder releases it.
    assert second.acquire_advisory_lock() is True
    second.release_advisory_lock()


def test_release_is_idempotent_live(bound_dsn: str) -> None:
    executor = PsycopgMigrationExecutor(bound_dsn)
    executor.release_advisory_lock()  # never acquired
    executor.acquire_advisory_lock()
    executor.release_advisory_lock()
    executor.release_advisory_lock()  # already closed


# ---------------------------------------------------------------------------
# Atomicity: a failed migration commits nothing
# ---------------------------------------------------------------------------


def test_failed_apply_rolls_back_and_records_nothing(
    executor: PsycopgMigrationExecutor,
    test_dsn: str,
    inventory_baseline: CatalogInventory,
) -> None:
    # Division by zero aborts inside the transaction; no DDL is attempted and
    # the ledger insert is never reached, so the rollback is the whole story.
    with pytest.raises(MigrationDriverError):
        executor.apply_migration(
            version=999,
            sql_text="SELECT 1 / 0;",
            checksum="0" * 64,
            applied_at=datetime.now(UTC),
        )

    assert catalog_inventory(test_dsn) == inventory_baseline

    snapshot = executor.read_snapshot()
    assert 999 not in {version for version, _ in snapshot.applied}


# ---------------------------------------------------------------------------
# Checksum refusal (read-only, on the live snapshot)
# ---------------------------------------------------------------------------


def test_tampered_checksum_is_refused(executor: PsycopgMigrationExecutor) -> None:
    snapshot = executor.read_snapshot()
    tampered = MigrationSnapshot(
        tables=snapshot.tables,
        ledger_present=True,
        applied=tuple(
            (version, "0" * 64 if index == 0 else checksum)
            for index, (version, checksum) in enumerate(snapshot.applied)
        ),
    )

    with pytest.raises(MigrationChecksumError):
        read_state(tampered)


def test_catalog_inventory_covers_the_project_categories(
    test_dsn: str, inventory_baseline: CatalogInventory
) -> None:
    inventory = catalog_inventory(test_dsn)

    assert "public" in inventory.schema_names
    assert ("employee", "r") in inventory.relations
    assert inventory.ledger_count == len(inventory.ledger_rows) >= 1
    assert inventory.guard_count == len(inventory.guard_rows) == 1
    # A fresh read agrees with the module baseline on every category.
    assert inventory.relations == inventory_baseline.relations
    assert inventory.functions == inventory_baseline.functions
    assert inventory.types == inventory_baseline.types


# ---------------------------------------------------------------------------
# Artifact preservation (must remain last in file order)
# ---------------------------------------------------------------------------


def test_catalog_inventory_is_unchanged_since_the_baseline(
    test_dsn: str, inventory_baseline: CatalogInventory
) -> None:
    assert catalog_inventory(test_dsn) == inventory_baseline
