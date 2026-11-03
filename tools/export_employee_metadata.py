"""Export employee metadata from enrollment manifests (Phase 3 — Step 1.5c-1).

Read-only operator CLI. It scans ``<DATA_DIR>/employees/<encoded-id>/
employee.json`` (Step 07 manifests, ``format_version: 1``) and writes one
versioned five-key JSONL record per employee to
``<DATA_DIR>/exports/employee_metadata.jsonl`` (override with
``--output``).

What it never does
------------------

* Never writes to, repairs, or deletes anything under ``employees/``,
  ``face_samples/``, ``embeddings/``, or the attendance journal.
* Never opens a face sample, an embedding, or the attendance journal.
* Never connects to PostgreSQL and never reads a DSN.
* Never prints employee names: the report carries directory names,
  employee-counts, and typed reason codes only.

Personal data
-------------

The artifact **does** contain names (that is the payload of the future
import). It belongs only in the git-ignored ``data/exports/`` tree, and
it is permission-restricted to the owner where the platform supports it.

Exit codes
----------

``0``  export written, no rejections.
``1``  no artifact written (rejections, nothing valid), or a write
       failure; a partial export written with ``--allow-partial`` also
       exits ``1`` so the rejections cannot be missed.
``2``  refusal: the target exists without ``--overwrite``, or argparse
       usage error.

Usage::

    python tools/export_employee_metadata.py                 # dry scan + export
    python tools/export_employee_metadata.py --overwrite     # replace existing
    python tools/export_employee_metadata.py --allow-partial # valid records only
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
from src.employee_export import (  # noqa: E402
    ExportError,
    ExportOutputExistsError,
    ExportResult,
    default_output_path,
    export_employee_metadata,
)

_PREFIX = "[export-cli]"


def _report(result: ExportResult, out: Callable[..., None]) -> None:
    """Bounded, name-free summary of one export run."""
    out(
        f"{_PREFIX} scanned {result.scanned} employee manifest entr"
        f"{'y' if result.scanned == 1 else 'ies'} under "
        f"{result.source_dir}"
    )
    out(
        f"{_PREFIX} validated {result.exported} record(s); "
        f"rejected {len(result.rejected)} manifest(s)"
    )
    if result.written:
        out(f"{_PREFIX} wrote {result.output_path}")
        if result.permissions_restricted:
            out(f"{_PREFIX} permissions restricted to owner-only (0600)")
        else:
            out(
                f"{_PREFIX} note: owner-only permissions are not supported "
                "on this platform; protect the file with filesystem ACLs"
            )
    else:
        out(f"{_PREFIX} no artifact was written to {result.output_path}")
    if result.rejected:
        out(
            f"{_PREFIX} rejections (directory name + reason only; no "
            "employee data is shown):"
        )
        for rejection in result.rejected:
            out(f"  dir={rejection.dir_name} reason={rejection.reason.value}")


def run_export(
    *,
    settings: AppSettings,
    output: Path | None = None,
    overwrite: bool = False,
    allow_partial: bool = False,
    out: Callable[..., None] = print,
) -> int:
    """Run one export and return its documented exit code."""
    try:
        result = export_employee_metadata(
            settings,
            output=output,
            overwrite=overwrite,
            allow_partial=allow_partial,
        )
    except ExportOutputExistsError as exc:
        out(
            f"{_PREFIX} refused — {exc}. Nothing was read or written; "
            "re-run with --overwrite to replace the existing artifact."
        )
        return 2
    except ExportError as exc:
        out(f"{_PREFIX} export failed: {exc}")
        return 1

    _report(result, out)

    if result.written and not result.rejected:
        if result.scanned == 0:
            out(
                f"{_PREFIX} nothing to export: no employee manifests exist "
                "under the configured data root. An empty artifact was "
                "written (0 records)."
            )
        else:
            out(
                f"{_PREFIX} success: {result.exported} employee metadata "
                "record(s) exported. The artifact contains names; keep it "
                "out of version control and dispose of it when no longer "
                "needed."
            )
        return 0
    if result.written:
        out(
            f"{_PREFIX} partial export written with --allow-partial: "
            f"{result.exported} record(s); {len(result.rejected)} "
            "manifest(s) were rejected and are NOT in the artifact. Fix "
            "the causes and re-run before importing."
        )
        return 1
    out(
        f"{_PREFIX} refused to write: {len(result.rejected)} manifest(s) "
        "are invalid, so the artifact was not created (fail-closed). "
        "Nothing was imported. Re-run with --allow-partial to export only "
        "the valid records."
    )
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export_employee_metadata",
        description=(
            "Read-only export of employee metadata from the published "
            "enrollment manifests into a versioned JSONL artifact. The "
            "enrollment tree is only read; biometric files, embeddings, "
            "the attendance journal, and PostgreSQL are never touched."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Artifact path (default: <DATA_DIR>/exports/"
            "employee_metadata.jsonl)."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Replace an existing artifact. Without this flag an existing "
            "target is refused (exit 2) and nothing is written."
        ),
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "Write the valid records even when some manifests are rejected "
            "(exit 1). Default is fail-closed: any rejection means no "
            "artifact at all."
        ),
    )
    return parser


def main(
    argv: list[str] | None = None, *, settings: AppSettings | None = None
) -> int:
    args = _build_parser().parse_args(argv)
    resolved = settings if settings is not None else AppSettings()
    output = Path(args.output) if args.output else default_output_path(resolved)
    try:
        return run_export(
            settings=resolved,
            output=output,
            overwrite=args.overwrite,
            allow_partial=args.allow_partial,
        )
    except Exception as exc:  # noqa: BLE001 - an unexpected failure is nonzero
        print(f"{_PREFIX} export run failed: {type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
