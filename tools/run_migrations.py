#!/usr/bin/env python
"""Inspect and apply schema migrations on the configured application database.

Phase 3, Step 1.5b-6. This runner owns exactly two operations:

``status``
    Read-only. Takes **no** advisory lock, creates **no** ledger and writes
    nothing: it reads one consistent snapshot (``REPEATABLE READ READ ONLY``)
    through :meth:`src.db_migrate_executor.PsycopgMigrationExecutor.read_snapshot`
    and derives everything else with the pure
    :func:`src.db_migrate.read_state`, which reuses the existing migration
    validation and reconciliation rules.

``apply``
    Runs the unchanged :func:`src.db_migrate.migrate` orchestration over the
    real executor: advisory lock first, then ledger, reconciliation, and one
    transaction per pending migration.

Target discipline
-----------------
The target is ``ATTENDANCE_DATABASE_URL`` only, and it is required: there is
no default and no fallback. A target whose normalized identity matches the
configured ``ATTENDANCE_TEST_DATABASE_URL`` is refused, because that variable
names the dedicated, disposable Tier-2 database — production migrations must
never run there. A configured test DSN whose identity **cannot** be
established also refuses: an unreadable test target is treated as a possible
collision, never as an absent one. There is deliberately no override flag.

Output never contains a DSN, a user name, a password, or driver text.

Exit codes (stable; documented in README.md)
--------------------------------------------
===== ==========================================================
0     success (``status`` read, or ``apply`` completed)
1     unexpected internal error
2     configuration / target refusal (missing, ambiguous, malformed,
      or identical to the dedicated test database)
3     another migration run holds the advisory lock
4     database unavailable / driver failure
5     migration refused or failed (file, checksum, integrity, gap,
      missing script, application failure)
6     run completed but lock cleanup failed (outcome unconfirmed)
===== ==========================================================
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

__all__ = ["main"]

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_CONFIG = 2
EXIT_CONTENTION = 3
EXIT_UNAVAILABLE = 4
EXIT_MIGRATION = 5
EXIT_UNCERTAIN = 6

_PREFIX = "run-migrations"


class _Refused(Exception):
    """A configuration/identity refusal decided before any connection."""


# ---------------------------------------------------------------------------
# Settings and target resolution (no connection is opened here)
# ---------------------------------------------------------------------------


def _load_settings():
    """Load validated settings, classifying configuration failure."""
    try:
        from src.config import load_settings

        return load_settings()
    except ValueError:  # ConfigError is a ValueError; nothing else is caught
        raise _Refused(
            "configuration refused: the validated settings layer rejected "
            "the environment"
        ) from None


def _display_target(dsn: str) -> str:
    """Sanitized target description: dbname/host/port only, never credentials."""
    from src.db import parse_dsn

    mapping = parse_dsn(dsn)
    return (
        f"dbname={mapping.get('dbname', '?')} "
        f"host={mapping.get('host', '?')} "
        f"port={mapping.get('port', 5432)}"
    )


def _resolve_target(settings) -> str:
    """Return the application DSN, or raise :class:`_Refused`.

    Fails closed at every step: a missing target, an ambiguous target, and a
    configured-but-unreadable *test* target are all refusals.
    """
    from src.db import MalformedDsnError
    from src.db_target import normalized_target_identity

    target = getattr(settings, "database_url", None)
    if not isinstance(target, str) or not target.strip():
        raise _Refused(
            "migration target refused: no ATTENDANCE_DATABASE_URL configured"
        )

    try:
        identity = normalized_target_identity(target)
    except MalformedDsnError:
        raise _Refused(
            "migration target refused: the configured DSN could not be parsed"
        ) from None
    if identity is None:
        raise _Refused(
            "migration target refused: the configured target identity is "
            "ambiguous"
        )

    test_dsn = getattr(settings, "test_database_url", None)
    if isinstance(test_dsn, str) and test_dsn.strip():
        try:
            test_identity = normalized_target_identity(test_dsn)
        except MalformedDsnError:
            raise _Refused(
                "migration target refused: ATTENDANCE_TEST_DATABASE_URL is "
                "set but could not be parsed"
            ) from None
        if test_identity is None:
            raise _Refused(
                "migration target refused: ATTENDANCE_TEST_DATABASE_URL is "
                "set but its identity cannot be established"
            )
        if identity == test_identity:
            raise _Refused(
                "refusing to run migrations against the dedicated test database"
            )

    return target.strip()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _run_status(args, out=None) -> int:
    """Print a read-only, point-in-time migration status.

    Uses only ``read_snapshot()`` (one read-only transaction) and the pure
    ``read_state()``; it never creates the ledger, never takes the lock and
    never writes.
    """
    if out is None:
        out = print

    settings = _load_settings()
    dsn = _resolve_target(settings)

    from src.db_migrate import read_state
    from src.db_migrate_executor import PsycopgMigrationExecutor

    snapshot = PsycopgMigrationExecutor(dsn=dsn).read_snapshot()
    state = read_state(snapshot)

    payload = {
        "target": _display_target(dsn),
        "snapshot": "point-in-time",
        "ledger": "present" if state.ledger_present else "missing",
        "current_version": state.previous_version,
        "applied": [version for version, _ in state.applied],
        "pending": list(state.pending),
        "status": "up-to-date" if not state.pending else "pending",
    }

    if getattr(args, "json", False):
        out(json.dumps(payload, sort_keys=True))
        return EXIT_OK

    out(f"target: {payload['target']}")
    out(f"snapshot: {payload['snapshot']}")
    out(f"ledger: {payload['ledger']}")
    out(f"current version: {payload['current_version']}")
    out(f"applied: {', '.join(str(v) for v in payload['applied']) or 'none'}")
    out(f"pending: {', '.join(str(v) for v in payload['pending']) or 'none'}")
    out(f"status: {payload['status']}")
    return EXIT_OK


def _run_apply(args, out=None) -> int:
    """Apply pending migrations through the unchanged ``migrate()`` runner."""
    if out is None:
        out = print

    settings = _load_settings()
    dsn = _resolve_target(settings)

    from src.db_migrate import migrate
    from src.db_migrate_executor import PsycopgMigrationExecutor

    outcome = migrate(PsycopgMigrationExecutor(dsn=dsn))

    applied = ", ".join(str(version) for version in outcome.applied_versions)
    out(f"target: {_display_target(dsn)}")
    out(f"previous version: {outcome.previous_version}")
    out(f"applied: {applied or 'none'}")
    out(f"current version: {outcome.current_version}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Error classification (complete map; see the module docstring)
# ---------------------------------------------------------------------------


def _exit_code_for(exc: BaseException) -> int:
    from src.config import ConfigError
    from src.db import DatabaseUnavailableError, MalformedDsnError
    from src.db_migrate import (
        MigrationApplicationError,
        MigrationChecksumError,
        MigrationCleanupError,
        MigrationContentionError,
        MigrationError,
        MigrationFileError,
        MigrationGapError,
        MigrationIntegrityError,
        MigrationMissingScriptError,
    )
    from src.db_migrate_executor import MigrationDriverError, MigrationTargetError

    if isinstance(exc, MigrationContentionError):
        return EXIT_CONTENTION
    if isinstance(exc, MigrationCleanupError):
        return EXIT_UNCERTAIN
    if isinstance(exc, (MigrationTargetError, MalformedDsnError, ConfigError)):
        return EXIT_CONFIG
    if isinstance(exc, (DatabaseUnavailableError, MigrationDriverError)):
        return EXIT_UNAVAILABLE
    if isinstance(
        exc,
        (
            MigrationFileError,
            MigrationChecksumError,
            MigrationIntegrityError,
            MigrationMissingScriptError,
            MigrationGapError,
            MigrationApplicationError,
            MigrationError,  # any future member defaults to the migration class
        ),
    ):
        return EXIT_MIGRATION
    return EXIT_INTERNAL


def _message_for(exc: BaseException) -> str:
    """Bounded, credential-free description for the console."""
    from src.db import DatabaseUnavailableError
    from src.db_migrate import (
        MigrationApplicationError,
        MigrationChecksumError,
        MigrationCleanupError,
        MigrationContentionError,
        MigrationFileError,
        MigrationGapError,
        MigrationIntegrityError,
        MigrationMissingScriptError,
    )
    from src.db_migrate_executor import MigrationDriverError

    if isinstance(exc, MigrationContentionError):
        return (
            "another migration run holds the advisory lock; nothing was changed"
        )
    if isinstance(exc, MigrationCleanupError):
        return (
            "the run completed but lock cleanup failed; the outcome is "
            "unconfirmed"
        )
    if isinstance(exc, DatabaseUnavailableError):
        return "database unavailable"
    if isinstance(exc, MigrationDriverError):
        return "database operation failed"
    if isinstance(
        exc,
        (
            MigrationFileError,
            MigrationChecksumError,
            MigrationIntegrityError,
            MigrationMissingScriptError,
            MigrationGapError,
            MigrationApplicationError,
        ),
    ):
        # These messages are static, documented as sanitized, and may name a
        # version number — never a path, SQL text, DSN, or driver text.
        return str(exc)
    return "unexpected internal error"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PREFIX,
        description=(
            "Inspect or apply schema migrations for the configured "
            "application database (ATTENDANCE_DATABASE_URL)."
        ),
        epilog=(
            "Forward-only migrations; operational rollback is restore from a "
            "verified backup. Never point ATTENDANCE_DATABASE_URL at a shared "
            "or production database from a development shell."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser(
        "status", help="report ledger presence, applied versions and pending work"
    )
    status.add_argument(
        "--json",
        action="store_true",
        help="print the status payload as JSON",
    )

    subparsers.add_parser("apply", help="apply every pending migration")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "status":
            return _run_status(args)
        return _run_apply(args)
    except _Refused as exc:
        print(f"{_PREFIX}: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:  # classified below; never a traceback on the console
        print(f"{_PREFIX}: {_message_for(exc)}", file=sys.stderr)
        return _exit_code_for(exc)


if __name__ == "__main__":
    raise SystemExit(main())
