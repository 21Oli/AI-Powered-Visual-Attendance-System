"""Operator CLI for the employee-metadata importer (Phase 3 — Step 1.5c-2/3a).

Reads the Step 1.5c-1 JSONL export, validates it strictly, and reconciles
it with the employee-metadata repository under the approved policy:
create when absent, skip when identical, report (never resolve) a
conflict, reject invalid or duplicated records.

Modes
-----

* ``--validate-only`` — **no database**: schema and duplicate validation
  only. It never imports the database wiring, so it needs no DSN, no
  psycopg, and no connectivity.
* default (no flags) — **dry run**: validate and classify against the
  repository (identical / would-create / conflict) without writing. The
  wiring opens every connection read-only, so PostgreSQL itself refuses
  any write even if this tool tried one.
* ``--apply`` — perform the approved creates only, on a read-write
  connection.

Repository-backed modes build their repository from
:func:`src.db_employee_repository.open_employee_repository` — lazily, at
invocation time, so importing this tool touches no database code. When no
repository is supplied at all (direct library call), the runner still
refuses (exit 2) *before* reading anything, and points at
``--validate-only``.

Guarantees
----------

* No SQL, migration, or DDL lives here: the repository remains the only
  statement owner, and this tool only calls ``get`` and ``create``.
* The importer never updates or deactivates an employee; a conflict is
  reported, never resolved.
* Output and reports carry counts, employee IDs, differing field
  **names**, line numbers, and stable reason codes only — never names,
  timestamps, record contents, DSNs, credentials, or driver text.

Exit codes
----------

``0``  clean run (no rejections, no conflicts, nothing unprocessed).
``1``  rejections, conflicts, a stopped run (including an unavailable
       database, reported by exception **type** only), or a report write
       failure.
``2``  CLI/configuration refusal: bad flags, missing/unreadable input,
       report already present, or no repository at all. Nothing is
       written to a database in any exit-2 path.

Usage::

    python tools/import_employee_metadata.py --validate-only
    python tools/import_employee_metadata.py --report reports/import.json
    python tools/import_employee_metadata.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

_PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import AppSettings  # noqa: E402
from src.employee_export import EXPORT_FILENAME  # noqa: E402
from src.employee_import import (  # noqa: E402
    EmployeeRepositoryLike,
    ImportFileError,
    ImportInputError,
    ImportReport,
    ImportReportError,
    ImportReportExistsError,
    read_artifact,
    report_payload,
    run_import,
    write_report,
)

_PREFIX = "[import-cli]"

_NO_REPOSITORY = (
    "no repository was supplied for a repository-backed mode. Nothing was "
    "read from or written to a database. Re-run with --validate-only for a "
    "database-free validation."
)


def _default_repository_factory(
    *, apply: bool
) -> Callable[[], EmployeeRepositoryLike]:
    """Build the production repository factory (wiring imported lazily).

    The import happens here, not at module import time, so
    ``--validate-only`` never loads the database wiring or psycopg. The
    returned factory itself still opens no connection: the wiring resolves
    the DSN and connects on the first repository call, so a configuration
    failure surfaces as a typed stop rather than at construction.

    Read-only is the default: only ``--apply`` gets a writable connection.
    """
    from src.db_employee_repository import open_employee_repository

    return lambda: open_employee_repository(read_only=not apply)


def _summary(report: ImportReport, out: Callable[..., None]) -> None:
    """Print the bounded, name-free run summary."""
    out(
        f"{_PREFIX} mode={report.mode} input={report.input_path} "
        f"lines={report.line_count} valid={report.valid_records}"
    )
    if report.mode != "validate-only":
        out(
            f"{_PREFIX} created={len(report.created)} "
            f"would_create={len(report.would_create)} "
            f"identical={len(report.identical)} "
            f"conflict={len(report.conflicts)}"
        )
    out(
        f"{_PREFIX} rejected={len(report.rejected)} "
        f"unprocessed={report.unprocessed}"
    )
    if report.mode == "validate-only":
        out(
            f"{_PREFIX} validation only: no employee rows were read or "
            "written (no database was contacted)."
        )
    for result in report.conflicts:
        out(
            f"  conflict id={result.employee_id} "
            f"fields={','.join(result.fields)} (not updated)"
        )
    for item in report.rejected:
        identifier = item.employee_id if item.employee_id is not None else "-"
        out(
            f"  rejected line={item.line} id={identifier} "
            f"reason={item.reason.value}"
        )
    if report.stopped:
        out(
            f"{_PREFIX} stopped safely: the repository raised "
            f"{report.error_type or 'an error'}; no further records were "
            "read or written. Already-created rows stay committed; re-run "
            "is safe (they classify as identical)."
        )


def _precheck_report(path: Path, overwrite: bool) -> None:
    """Refuse an existing report *before* any repository work."""
    if path.exists() and not overwrite:
        raise ImportReportExistsError(
            "a report already exists at the requested path; pass "
            "--overwrite-report to replace it"
        )


def run_import_cli(
    *,
    input_path: Path,
    apply: bool = False,
    validate_only: bool = False,
    report_path: Path | None = None,
    overwrite_report: bool = False,
    repository: EmployeeRepositoryLike | None = None,
    out: Callable[..., None] = print,
) -> int:
    """Run one import invocation and return its documented exit code."""
    if apply and validate_only:
        out(
            f"{_PREFIX} refused — --apply and --validate-only are mutually "
            "exclusive. Nothing was done."
        )
        return 2

    try:
        parsed = read_artifact(input_path)
    except ImportFileError as exc:
        out(
            f"{_PREFIX} refused — {exc}. Expected the Step 1.5c-1 export at "
            f"{input_path}."
        )
        return 2

    if not validate_only and repository is None:
        out(f"{_PREFIX} refused — {_NO_REPOSITORY}")
        return 2

    if report_path is not None:
        try:
            _precheck_report(report_path, overwrite_report)
        except ImportReportExistsError as exc:
            out(f"{_PREFIX} refused — {exc}. Nothing was read or written.")
            return 2

    try:
        report = run_import(
            parsed,
            repository=None if validate_only else repository,
            apply=apply,
            input_path=input_path,
        )
    except ImportInputError as exc:
        out(f"{_PREFIX} refused — {exc}. Nothing was written.")
        return 2

    _summary(report, out)

    if report_path is not None:
        try:
            write_report(
                report_payload(report),
                report_path,
                overwrite=overwrite_report,
            )
        except ImportReportExistsError as exc:
            out(
                f"{_PREFIX} the run finished but the report was not written: "
                f"{exc}"
            )
            return 1
        except ImportReportError as exc:
            out(f"{_PREFIX} the run finished but {exc}")
            return 1
        out(f"{_PREFIX} report written to {report_path}")

    if report.mode == "validate-only":
        if report.rejected:
            out(
                f"{_PREFIX} validation found {len(report.rejected)} invalid "
                "record(s); nothing was imported."
            )
            return 1
        out(
            f"{_PREFIX} validation passed for {report.valid_records} "
            "record(s); nothing was imported."
        )
        return 0

    if report.clean:
        if report.mode == "apply":
            out(
                f"{_PREFIX} success: {len(report.created)} employee row(s) "
                "created; every other record was already identical."
            )
        else:
            out(
                f"{_PREFIX} dry run clean: {len(report.would_create)} "
                "row(s) would be created with --apply; nothing was written."
            )
        return 0

    out(
        f"{_PREFIX} attention required: {len(report.conflicts)} conflict(s), "
        f"{len(report.rejected)} rejected record(s), "
        f"{report.unprocessed} unprocessed. Conflicts are reported, never "
        "overwritten."
    )
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import_employee_metadata",
        description=(
            "Validate the Step 1.5c-1 employee-metadata export and reconcile "
            "it with the employee repository (create-if-absent, skip-if-"
            "identical, report conflicts, reject invalid/duplicate records). "
            "Never updates or deactivates an employee."
        ),
    )
    parser.add_argument(
        "--input",
        default=None,
        help=(
            "Export artifact to import (default: "
            "<DATA_DIR>/exports/employee_metadata.jsonl)."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Perform the approved creates only (never updates). Without "
            "this flag the run is a read-only dry run: its connections are "
            "put in server-enforced read-only mode."
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "Validate the artifact without any database access "
            "(schema, types, timestamps, and duplicate IDs)."
        ),
    )
    parser.add_argument(
        "--report",
        default=None,
        help=(
            "Write a machine-readable JSON report to this path (counts, "
            "employee IDs, field names, reason codes only)."
        ),
    )
    parser.add_argument(
        "--overwrite-report",
        action="store_true",
        help=(
            "Replace an existing report file. Without this flag an existing "
            "report is refused (exit 2) before any work starts."
        ),
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    settings: AppSettings | None = None,
    repository_factory: Callable[[], EmployeeRepositoryLike] | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    resolved = settings if settings is not None else AppSettings()
    input_path = (
        Path(args.input)
        if args.input
        else resolved.exports_dir / EXPORT_FILENAME
    )
    report_path = Path(args.report) if args.report else None

    repository: EmployeeRepositoryLike | None = None
    if not args.validate_only:
        # Production wiring is built only when a repository-backed mode is
        # actually invoked; --validate-only never reaches this branch.
        factory = (
            repository_factory
            if repository_factory is not None
            else _default_repository_factory(apply=args.apply)
        )
        repository = factory()

    try:
        return run_import_cli(
            input_path=input_path,
            apply=args.apply,
            validate_only=args.validate_only,
            report_path=report_path,
            overwrite_report=args.overwrite_report,
            repository=repository,
        )
    except Exception as exc:  # noqa: BLE001 - an unexpected failure is nonzero
        print(f"{_PREFIX} import run failed: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
