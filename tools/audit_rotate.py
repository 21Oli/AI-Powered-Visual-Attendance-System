#!/usr/bin/env python
"""Operator-controlled audit rotation, recovery and retention CLI (P5.5.7).

This tool is the **only** way to rotate the audit journal. It never runs from
application startup, an API route or a background task: an operator invokes it
while the application is stopped.

Commands
========

``python -m tools.audit_rotate --status``
    Verify the whole installation read-only and print its state. Changes
    nothing (not even the lock file).

``python -m tools.audit_rotate --dry-run``
    Verify every rotation precondition and print the plan. Changes nothing.

``python -m tools.audit_rotate``
    Seal the active journal: rename it to
    ``archive/audit-<first>-<last>.jsonl``, append the ``segment_sealed``
    manifest entry, mark the archive read-only where supported and create the
    next empty active journal.

``python -m tools.audit_rotate --register-existing [PATH]``
    Recovery for "archive present, seal missing" (crash after the rename).
    Re-verifies the named archive (or the only unregistered one), then appends
    exactly one seal. Refuses ambiguity and never seals a segment twice.

``python -m tools.audit_rotate --create-active``
    Recovery for "segment sealed, active journal missing". Creates an empty
    active journal after verifying history; never overwrites an existing file.

``python -m tools.audit_rotate --retention SEGMENT_ID``
    Record an owner-approved offsite retention decision for an already sealed
    segment whose archive is still present and content-verified. Nothing is
    moved or deleted.

Exit codes (stable)
=================== ======================================================
0                    success (or a successful dry run / status)
1                    integrity failure; no file was changed
2                    unavailable or contended (writer running, I/O failure)
3                    usage error
4                    unexpected internal error
5                    recovery required: a documented crash state needs an
                     explicit operator action (see the recovery runbook)
=================== ======================================================

Output never contains record bodies, audit or employee identifiers, metadata
values, credentials, raw OS error text or stack traces.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final, Sequence

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.audit_archive import (  # noqa: E402 - after the sys.path bootstrap
    LayoutProblem,
    verify_active_journal,
    verify_history,
)
from src.audit_layout import AuditLayout  # noqa: E402
from src.audit_rotation import (  # noqa: E402
    RotationContendedError,
    RotationError,
    RotationIntegrityError,
    RotationOperations,
    RotationRecoveryRequiredError,
    RotationUnavailableError,
)

__all__ = [
    "DEFAULT_AUDIT_DIRECTORY",
    "EXIT_INTERNAL_ERROR",
    "EXIT_INTEGRITY_FAILURE",
    "EXIT_OK",
    "EXIT_RECOVERY_REQUIRED",
    "EXIT_UNAVAILABLE",
    "EXIT_USAGE_ERROR",
    "main",
]

#: The configured audit directory, independent of the current working directory.
DEFAULT_AUDIT_DIRECTORY: Final[Path] = PROJECT_ROOT / "data" / "audit"

EXIT_OK: Final[int] = 0
EXIT_INTEGRITY_FAILURE: Final[int] = 1
EXIT_UNAVAILABLE: Final[int] = 2
EXIT_USAGE_ERROR: Final[int] = 3
EXIT_INTERNAL_ERROR: Final[int] = 4
EXIT_RECOVERY_REQUIRED: Final[int] = 5


class _UsageError(Exception):
    """Raised instead of argparse's default exit(2)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # noqa: ARG002 - fixed output
        raise _UsageError()


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m tools.audit_rotate",
        description=(
            "Operator-controlled rotation, crash recovery and retention "
            "recording for the audit JSONL journal. Stop the application "
            "first. Nothing is ever deleted automatically."
        ),
    )
    parser.add_argument(
        "--audit-dir",
        metavar="PATH",
        default=None,
        help=(
            "audit installation directory (default: "
            "PROJECT_ROOT/data/audit)"
        ),
    )
    parser.add_argument(
        "--journal",
        metavar="PATH",
        default=None,
        help="active journal path; its parent directory is the installation",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--status",
        action="store_true",
        help="verify the installation read-only and print its state",
    )
    actions.add_argument(
        "--dry-run",
        action="store_true",
        help="verify rotation preconditions and print the plan only",
    )
    actions.add_argument(
        "--register-existing",
        metavar="PATH",
        nargs="?",
        const="",
        default=None,
        help=(
            "recovery: append the missing seal for an archive left by an "
            "interrupted rotation (path optional when unambiguous)"
        ),
    )
    actions.add_argument(
        "--create-active",
        action="store_true",
        help=(
            "recovery: create the empty active journal after a seal when it "
            "is missing"
        ),
    )
    actions.add_argument(
        "--retention",
        metavar="SEGMENT_ID",
        default=None,
        help=(
            "record an owner-approved archived_offsite retention decision for "
            "a sealed segment (never deletes or moves anything)"
        ),
    )
    return parser


def _layout(args: argparse.Namespace) -> AuditLayout:
    if args.journal:
        return AuditLayout.from_journal(Path(args.journal))
    if args.audit_dir:
        return AuditLayout.for_directory(Path(args.audit_dir))
    return AuditLayout.for_directory(DEFAULT_AUDIT_DIRECTORY)


def _print_status(layout: AuditLayout) -> int:
    try:
        history = verify_history(layout)
        active = verify_active_journal(layout, history)
    except LayoutProblem as problem:
        print("Audit installation: FAIL")
        print(f"Category: {problem.category}")
        if problem.segment_id:
            print(f"Segment: {problem.segment_id}")
        if problem.line is not None:
            print(f"Line: {problem.line}")
        return (
            EXIT_RECOVERY_REQUIRED if problem.recovery else EXIT_INTEGRITY_FAILURE
        )

    print("Audit installation: OK")
    print(f"Audit directory: {layout.directory.resolve()}")
    print(f"Manifest entries: {len(history.manifest_entries)}")
    print(f"Archived segments: {len(history.segments)}")
    print(f"Sealed records: {history.total_records}")
    print(
        "Active segment: "
        + (active.segment_id or "none (empty active journal)")
    )
    print(f"Active records: {len(active.events)}")
    print(
        "Next global sequence: "
        + str(
            active.last_sequence + 1
            if active.events
            else history.expected_next_sequence
        )
    )
    print(
        "Anchor-only segments: "
        + (", ".join(history.anchor_only_segment_ids) or "(none)")
    )
    return EXIT_OK


def _print_recovery(error: RotationRecoveryRequiredError) -> None:
    print("Audit rotation: RECOVERY REQUIRED")
    print(f"Category: {error.category or 'RECOVERY_REQUIRED'}")
    print(f"Detail: {error}")
    print(
        "Next step: follow the recovery runbook in "
        "docs/AUDIT_JOURNAL_DESIGN.md §9.4; do not delete, truncate or "
        "re-initialize audit files."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError:
        print("Audit rotation: FAIL")
        print("Category: INVALID_ARGUMENTS")
        print("Detail: The command line arguments are not valid.")
        return EXIT_USAGE_ERROR

    layout = _layout(args)
    operations = RotationOperations(layout)

    try:
        if args.status:
            return _print_status(layout)

        if args.register_existing is not None:
            reference = args.register_existing or None
            entry = operations.register_existing(reference)
            print("Audit rotation: REGISTERED")
            print(f"Segment: {entry.segment_id}")
            print(
                "Global sequence: "
                f"{entry.first_global_sequence}..{entry.last_global_sequence}"
            )
            print(f"Records sealed: {entry.record_count}")
            print(f"Archive: {entry.segment_file}")
            print(f"Manifest sequence: {entry.manifest_sequence}")
            return EXIT_OK

        if args.create_active:
            path, next_id = operations.create_active()
            print("Audit recovery: ACTIVE CREATED")
            print(f"Active journal: {path.name}")
            print(f"Next segment: {next_id}")
            print("The first append continues the sealed chain.")
            return EXIT_OK

        if args.retention is not None:
            entry = operations.record_retention(args.retention)
            print("Audit retention: RECORDED")
            print(f"Segment: {entry.segment_id}")
            print(f"Disposition: {entry.disposition.value}")
            print(
                "Archive retained in place; nothing was moved or deleted. "
                "Contents were content-verified before this decision."
            )
            return EXIT_OK

        if args.dry_run:
            plan = operations.rotate(dry_run=True)
            print("Audit rotation: DRY RUN (no file changed)")
            print(f"Segment to seal: {plan.segment_id}")
            print(f"Records: {plan.record_count}")
            print(
                "Global sequence: "
                f"{plan.first_global_sequence}..{plan.last_global_sequence}"
            )
            print(f"First predecessor: {plan.first_previous_hash}")
            print(f"Final record hash: {plan.last_record_hash}")
            print(f"Archive target: {plan.archive_reference}")
            print(f"Next segment: {plan.next_segment_id}")
            print("Preconditions verified; nothing was renamed or written.")
            return EXIT_OK

        plan = operations.rotate()
        print("Audit rotation: SEALED")
        print(f"Segment: {plan.segment_id}")
        print(f"Records: {plan.record_count}")
        print(
            "Global sequence: "
            f"{plan.first_global_sequence}..{plan.last_global_sequence}"
        )
        print(f"Final record hash: {plan.last_record_hash}")
        print(f"Archive: {plan.archive_reference}")
        print(
            "Archive read-only: "
            + ("yes" if plan.read_only_marked else "not supported or refused")
        )
        print(
            "Directory fsync: "
            + ("completed" if plan.directory_fsync else "not supported")
        )
        print(f"Next segment: {plan.next_segment_id}")
        print("New active journal created empty; the chain continues.")
        return EXIT_OK
    except RotationRecoveryRequiredError as error:
        _print_recovery(error)
        return EXIT_RECOVERY_REQUIRED
    except RotationIntegrityError as error:
        print("Audit operation: FAIL")
        print(f"Category: {error.category or 'INTEGRITY_FAILURE'}")
        print(f"Detail: {error}")
        return EXIT_INTEGRITY_FAILURE
    except RotationContendedError as error:
        print("Audit operation: FAIL")
        print("Category: CONTENDED")
        print(f"Detail: {error}")
        return EXIT_UNAVAILABLE
    except RotationUnavailableError as error:
        print("Audit operation: FAIL")
        print(f"Category: {error.category or 'UNAVAILABLE'}")
        print(f"Detail: {error}")
        return EXIT_UNAVAILABLE
    except RotationError as error:  # pragma: no cover - defensive
        print("Audit operation: FAIL")
        print(f"Category: {error.category or 'UNAVAILABLE'}")
        print("Detail: The audit operation could not be completed.")
        return EXIT_UNAVAILABLE
    except Exception:  # pragma: no cover - defensive, sanitized
        print("Audit operation: FAIL")
        print("Category: INTERNAL_ERROR")
        print(
            "Detail: The operation could not be completed because of an "
            "internal error."
        )
        return EXIT_INTERNAL_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
