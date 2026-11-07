"""Offline tests for ``tools/run_migrations.py`` (Phase 3 — Step 1.5b-6).

The CLI is exercised through ``main()`` with the settings loader, the executor
and the runner patched, so nothing connects to PostgreSQL. Every documented
exit code is pinned, and the ``status`` path is proven to be read-only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.run_migrations as cli  # noqa: E402

from src.db import DatabaseUnavailableError, MalformedDsnError  # noqa: E402
from src.db_migrate import (  # noqa: E402
    ApplyOutcome,
    MigrationApplicationError,
    MigrationChecksumError,
    MigrationCleanupError,
    MigrationContentionError,
    MigrationIntegrityError,
    MigrationSnapshot,
    load_migrations,
)
from src.db_migrate_executor import MigrationDriverError  # noqa: E402

TARGET = "postgresql://app_user:app-secret@localhost:5432/attendance_app"
TEST_DSN = "postgresql://tier2_user:tier2-secret@localhost:5432/visual_attendance_tier2_test"
V001_CHECKSUM = load_migrations()[0].checksum


@pytest.fixture
def settings(monkeypatch):
    def configure(database_url=TARGET, test_database_url=None):
        monkeypatch.setattr(
            "src.config.load_settings",
            lambda: SimpleNamespace(
                database_url=database_url, test_database_url=test_database_url
            ),
        )

    return configure


@pytest.fixture
def executor_stub(monkeypatch):
    """Patch the executor class with a read-only-capable stub."""
    state = SimpleNamespace(
        created=[],
        snapshot=MigrationSnapshot(
            tables=frozenset({"employee", "schema_version"}),
            ledger_present=True,
            applied=((1, V001_CHECKSUM),),
        ),
    )

    class Stub:
        def __init__(self, dsn, *, connection_opener=None):
            self.dsn = dsn
            self.calls: list[str] = []
            state.created.append(self)

        def read_snapshot(self):
            self.calls.append("read_snapshot")
            return state.snapshot

        def ensure_ledger(self):  # pragma: no cover - asserted never called
            raise AssertionError("status must never create the ledger")

        def list_tables(self):  # pragma: no cover
            raise AssertionError("status must use read_snapshot only")

        def fetch_applied(self):  # pragma: no cover
            raise AssertionError("status must use read_snapshot only")

        def acquire_advisory_lock(self):  # pragma: no cover
            raise AssertionError("status must never take the advisory lock")

        def release_advisory_lock(self):  # pragma: no cover
            raise AssertionError("status must never take the advisory lock")

        def apply_migration(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("status must never apply a migration")

    monkeypatch.setattr("src.db_migrate_executor.PsycopgMigrationExecutor", Stub)
    return state


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_reports_up_to_date_and_is_read_only(settings, executor_stub, capsys) -> None:
    settings()

    assert cli.main(["status"]) == cli.EXIT_OK

    captured = capsys.readouterr()
    assert "ledger: present" in captured.out
    assert "current version: 1" in captured.out
    assert "applied: 1" in captured.out
    assert "pending: none" in captured.out
    assert "status: up-to-date" in captured.out
    assert "dbname=attendance_app" in captured.out

    assert len(executor_stub.created) == 1
    assert executor_stub.created[0].calls == ["read_snapshot"]


def test_status_on_a_pristine_target_reports_pending(settings, executor_stub, capsys) -> None:
    settings()
    executor_stub.snapshot = MigrationSnapshot(
        tables=frozenset(), ledger_present=False, applied=()
    )

    assert cli.main(["status"]) == cli.EXIT_OK

    captured = capsys.readouterr()
    assert "ledger: missing" in captured.out
    assert "current version: 0" in captured.out
    assert "pending: 1" in captured.out
    assert "status: pending" in captured.out


def test_status_json_payload(settings, executor_stub, capsys) -> None:
    settings()

    assert cli.main(["status", "--json"]) == cli.EXIT_OK

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ledger"] == "present"
    assert payload["current_version"] == 1
    assert payload["applied"] == [1]
    assert payload["pending"] == []
    assert payload["snapshot"] == "point-in-time"


def test_status_blocked_when_employee_exists_without_a_ledger(
    settings, executor_stub, capsys
) -> None:
    settings()
    executor_stub.snapshot = MigrationSnapshot(
        tables=frozenset({"employee"}), ledger_present=False, applied=()
    )

    assert cli.main(["status"]) == cli.EXIT_MIGRATION
    assert "refusing to proceed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def test_apply_reports_the_outcome(settings, executor_stub, monkeypatch, capsys) -> None:
    settings()
    calls: list[object] = []

    def fake_migrate(executor):
        calls.append(executor)
        return ApplyOutcome(previous_version=0, applied_versions=(1,), current_version=1)

    monkeypatch.setattr("src.db_migrate.migrate", fake_migrate)

    assert cli.main(["apply"]) == cli.EXIT_OK

    captured = capsys.readouterr()
    assert "applied: 1" in captured.out
    assert "current version: 1" in captured.out
    assert len(calls) == 1
    assert executor_stub.created[0].dsn == TARGET


def test_apply_no_op_is_explicit(settings, executor_stub, monkeypatch, capsys) -> None:
    settings()
    monkeypatch.setattr(
        "src.db_migrate.migrate",
        lambda executor: ApplyOutcome(
            previous_version=1, applied_versions=(), current_version=1
        ),
    )

    assert cli.main(["apply"]) == cli.EXIT_OK
    assert "applied: none" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Target refusals (exit 2)
# ---------------------------------------------------------------------------


def test_missing_application_dsn_is_refused(settings, capsys) -> None:
    settings(database_url=None)

    assert cli.main(["status"]) == cli.EXIT_CONFIG
    assert "no ATTENDANCE_DATABASE_URL" in capsys.readouterr().err


def test_ambiguous_target_is_refused(settings, capsys) -> None:
    settings(database_url="host=localhost dbname=app hostaddr=10.0.0.1")

    assert cli.main(["status"]) == cli.EXIT_CONFIG
    assert "ambiguous" in capsys.readouterr().err


def test_malformed_target_is_refused(settings, capsys) -> None:
    settings(database_url="postgresql://u:p@[::1/app")

    assert cli.main(["status"]) == cli.EXIT_CONFIG
    assert "could not be parsed" in capsys.readouterr().err


def test_target_matching_the_test_database_is_refused(settings, capsys) -> None:
    settings(
        database_url="postgresql://u:p@localhost:5432/visual_attendance_tier2_test",
        test_database_url=TEST_DSN,
    )

    assert cli.main(["status"]) == cli.EXIT_CONFIG
    assert "dedicated test database" in capsys.readouterr().err


def test_target_matching_the_test_database_in_another_spelling_is_refused(
    settings, capsys
) -> None:
    settings(
        database_url="postgresql://u:p@127.0.0.1/app_db",
        test_database_url="postgresql://u:p@localhost:5432/app_db",
    )

    assert cli.main(["status"]) == cli.EXIT_CONFIG
    assert "dedicated test database" in capsys.readouterr().err


def test_unreadable_test_dsn_fails_closed(settings, capsys) -> None:
    settings(test_database_url="host=localhost dbname=app hostaddr=10.0.0.1")

    assert cli.main(["status"]) == cli.EXIT_CONFIG
    assert "cannot be established" in capsys.readouterr().err


def test_absent_test_dsn_skips_the_comparison(settings, executor_stub) -> None:
    settings(test_database_url=None)

    assert cli.main(["status"]) == cli.EXIT_OK


# ---------------------------------------------------------------------------
# Error classification (every documented exit code)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MigrationContentionError("held"), cli.EXIT_CONTENTION),
        (MigrationCleanupError("cleanup"), cli.EXIT_UNCERTAIN),
        (MigrationChecksumError("checksum"), cli.EXIT_MIGRATION),
        (MigrationIntegrityError("integrity"), cli.EXIT_MIGRATION),
        (MigrationApplicationError("apply"), cli.EXIT_MIGRATION),
        (DatabaseUnavailableError("down"), cli.EXIT_UNAVAILABLE),
        (MigrationDriverError("driver"), cli.EXIT_UNAVAILABLE),
        (MalformedDsnError("bad dsn"), cli.EXIT_CONFIG),
        (RuntimeError("unexpected"), cli.EXIT_INTERNAL),
    ],
)
def test_apply_maps_every_error_family(
    settings, executor_stub, monkeypatch, capsys, error, expected
) -> None:
    settings()

    def failing_migrate(executor):
        raise error

    monkeypatch.setattr("src.db_migrate.migrate", failing_migrate)

    assert cli.main(["apply"]) == expected
    assert capsys.readouterr().err.startswith("run-migrations: ")


def test_status_maps_a_state_error(settings, executor_stub, monkeypatch, capsys) -> None:
    settings()

    def failing_read_state(snapshot, migrations_dir=None):
        raise MigrationChecksumError("migration 1's file contents changed")

    monkeypatch.setattr("src.db_migrate.read_state", failing_read_state)

    assert cli.main(["status"]) == cli.EXIT_MIGRATION
    assert "changed" in capsys.readouterr().err


def test_missing_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Output hygiene and import hygiene
# ---------------------------------------------------------------------------


def test_output_never_contains_credentials(settings, executor_stub, capsys) -> None:
    settings()

    assert cli.main(["status"]) == cli.EXIT_OK

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "app-secret" not in combined
    assert "postgresql://" not in combined


def test_import_does_not_load_the_driver() -> None:
    code = (
        "import sys, tools.run_migrations as m;"
        "print('psycopg' in sys.modules, 'src.db' in sys.modules, callable(m.main))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split()[:2] == ["False", "False"]
