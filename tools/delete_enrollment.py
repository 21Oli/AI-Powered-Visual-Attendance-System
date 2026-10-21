"""Step 18 CLI: delete one employee's enrollment artifacts.

The deliberate, operator-owned counterpart to
``tools/enroll_employee.py``. It composes the existing validation and
verification services — it invents no new validation scheme and touches
nothing outside the selected employee's own artifacts:

* identity validation reuses :func:`src.enrollment.safe_dir_name` (the
  exact ``Employee`` model rules + traversal-proof encoding) — an
  invalid or malicious ID is rejected before any filesystem work;
* artifact paths are *derived*, never accepted from user input: the
  three configured enrollment roots only (``employees_dir``,
  ``face_samples_dir``, ``embeddings_dir`` from :class:`AppSettings`),
  each scoped to the validated encoded directory/record name;
* final verification reuses the *real*
  :meth:`src.embedding_storage.EmbeddingStorageService.
  list_employee_ids` — the same candidate list the live recognizer
  uses — plus direct artifact-existence checks, so success is claimed
  only when the ID is provably gone.

No deletion API exists in the existing services (they are
save/load/exists/list only), so this tool performs strictly
path-scoped file and directory removal — exactly the artifacts Step 07
and Step 09 can produce for one employee, nothing else.

Two-stage confirmation (review first, then delete)
--------------------------------------------------

* **Preview** (default, or with ``--execute`` declined): prints the
  exact artifact paths and their present/missing status and performs
  *no* change. Exit 0.
* ``--execute`` without ``--confirm-delete``: refused with exit 2 —
  nothing runs.
* ``--execute --confirm-delete`` (two *separate* explicit flags):
  deletes. The flags enable the flow only; they never bypass ID
  validation, scoping checks, or the target-existence requirements.

Deletion order (mirrors enrollment's publish order, reversed)
-------------------------------------------------------------

Enrollment publishes the sample directory first and the manifest
(``employee.json``) last — the manifest is the "enrollment exists"
marker, so a crash during publish leaves orphaned samples without a
recognized enrollment, never a declared enrollment without images.
Deletion mirrors this in reverse so a mid-deletion crash can only
leave that same publish-safe state:

1. the embedding record files (``.npz`` + ``.meta.json``) — the
   recognizer's candidates;
2. the employee directory (manifest last of the *enrollment* pair);
3. the face-sample directory.

Shared model files, configuration, logs, other employees' records, and
every file outside the three derived locations are never touched.

On any failure the tool reports exactly which artifacts were removed
and which remain, and never prints success. Recovery limitation: there
is no undo — deleted biometric artifacts are not recoverable except by
re-enrolling the (re-consenting) participant.

Biometric discipline: file contents (crops, vectors) are never read,
printed, or logged; output carries paths' *names*, counts, and
statuses only.

Exit codes: ``0`` success (ID provably absent), ``1`` typed failure
(including "nothing to delete"), ``2`` argparse/confirmation refusal.

Usage::

    python tools/delete_enrollment.py --employee-id "EMP-0042"   # preview
    python tools/delete_enrollment.py --employee-id "EMP-0042" --execute --confirm-delete
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Callable

_PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import AppSettings, SETTINGS  # noqa: E402
from src.embedding_storage import (  # noqa: E402
    EmbeddingStorageService,
)
from src.employee import Employee, EmployeeValidationError  # noqa: E402
from src.enrollment import (  # noqa: E402
    MANIFEST_FILENAME,
    EnrollmentError,
    EnrollmentInputError,
    safe_dir_name,
)

_CONFIRM_REFUSAL = (
    "confirmation refused: --execute was given without --confirm-delete "
    "(two separate confirmations are required). Nothing was run."
)

_NOTHING_TO_DELETE = (
    "nothing to delete: no enrollment artifacts exist for employee "
    "'{id}' in the configured data directories (no published manifest, "
    "no sample directory, no stored embedding record)."
)

_RECOVERY_NOTE = (
    "deletion is irreversible: deleted enrollment artifacts cannot be "
    "recovered except by re-enrolling the (re-consenting) participant. "
    "Re-run this tool after fixing the cause to finish removing any "
    "remaining artifacts."
)


class _Artifacts:
    """Every artifact path the employee's own data can occupy.

    Held as a plain attribute-holder (not a ``@dataclass``) so the
    module stays loadable through ``importlib`` without a registered
    module name — the same double-loading pattern the other tool tests
    use. Attributes (all read-only by convention):

    * ``employee_dir`` — encoded per-employee directory under
      ``employees_dir`` (contains only ``employee.json``);
    * ``manifest_path`` — the published Step 07 eligibility marker;
    * ``samples_dir`` — encoded per-employee directory under
      ``face_samples_dir`` (only ``sample_*.png``);
    * ``record_path`` / ``metadata_path`` — the ``.npz`` array file
      and ``.meta.json`` sidecar under ``embeddings_dir``;
    * ``dir_name`` — the validated, traversal-proof encoded stem
      shared by all derived paths.
    """

    def __init__(
        self,
        *,
        employee_dir: Path,
        manifest_path: Path,
        samples_dir: Path,
        record_path: Path,
        metadata_path: Path,
        dir_name: str,
    ) -> None:
        self.employee_dir = employee_dir
        self.manifest_path = manifest_path
        self.samples_dir = samples_dir
        self.record_path = record_path
        self.metadata_path = metadata_path
        self.dir_name = dir_name


def _derive_artifacts(settings: AppSettings, employee_id: str) -> _Artifacts:
    """Derive every artifact path strictly from validated identity.

    Validation mirrors the established convention exactly:
    ``EnrollmentService`` (and storage/bridge) validate the ID through
    the *full* ``Employee`` model rules first, and only then encode it
    traversal-proof via :func:`safe_dir_name` (the encoder alone does
    NOT screen model rules such as whitespace-only IDs). Any identity
    failure raises a typed input error before any filesystem work.
    """
    try:
        Employee(employee_id=employee_id, full_name="deletion validation")
    except EmployeeValidationError as exc:
        raise EnrollmentInputError(
            f"employee_id violates the Employee model rules: {exc}"
        ) from exc
    try:
        dir_name = safe_dir_name(employee_id)
    except EnrollmentError as exc:
        raise EnrollmentInputError(
            f"employee_id violates the directory-safety "
            f"rules: {exc}"
        ) from exc

    # Defensive containment: every derived parent must be the exact
    # configured root (the encode is already traversal-proof; this
    # belt-and-braces check keeps an unexpected name change loud).
    roots = {
        settings.employees_dir.resolve(): "employees_dir",
        settings.face_samples_dir.resolve(): "face_samples_dir",
        settings.embeddings_dir.resolve(): "embeddings_dir",
    }
    for parent in (
        settings.employees_dir / dir_name,
        settings.face_samples_dir / dir_name,
    ):
        if parent.resolve().parent not in roots:
            raise EnrollmentInputError(
                f"derived path {parent.name!r} escaped its configured "
                "data root; refusing to operate"
            )
    record_path = settings.embeddings_dir / f"{dir_name}.npz"
    metadata_path = settings.embeddings_dir / f"{dir_name}.meta.json"
    for derived in (record_path, metadata_path):
        if derived.resolve().parent not in roots:
            raise EnrollmentInputError(
                f"derived record path {derived.name!r} escaped its "
                "configured data root; refusing to operate"
            )

    return _Artifacts(
        employee_dir=settings.employees_dir / dir_name,
        manifest_path=settings.employees_dir / dir_name / MANIFEST_FILENAME,
        samples_dir=settings.face_samples_dir / dir_name,
        record_path=record_path,
        metadata_path=metadata_path,
        dir_name=dir_name,
    )


def _group_exists(artifacts: _Artifacts) -> dict[str, bool]:
    """Present/missing status per artifact group (no contents read)."""
    return {
        "manifest": artifacts.manifest_path.is_file()
        or artifacts.employee_dir.is_dir(),
        "samples": artifacts.samples_dir.is_dir(),
        "embedding": artifacts.record_path.is_file()
        or artifacts.metadata_path.is_file(),
    }


def _sample_count(samples_dir: Path) -> int:
    """Count published PNG samples (names only — never contents)."""
    if not samples_dir.is_dir():
        return 0
    return sum(1 for p in samples_dir.iterdir() if p.suffix == ".png")


def _preview(
    employee_id: str, artifacts: _Artifacts, out: Callable[..., None]
) -> None:
    groups = _group_exists(artifacts)
    out(
        f"[delete-cli] PREVIEW for employee '{employee_id}' "
        "(no changes were made):"
    )
    if groups["manifest"]:
        out(
            f"  manifest      : {artifacts.manifest_path.name} "
            f"under {artifacts.employee_dir}"
        )
    else:
        out("  manifest      : absent")
    if groups["samples"]:
        count = _sample_count(artifacts.samples_dir)
        out(
            f"  face samples  : directory {artifacts.samples_dir} "
            f"({count} sample PNG file(s))"
        )
    else:
        out("  face samples  : absent")
    if groups["embedding"]:
        out(
            f"  embedding     : {artifacts.record_path.name} "
            f"(+ {artifacts.metadata_path.name}) under "
            f"{artifacts.record_path.parent}"
        )
    else:
        out("  embedding     : absent")
    out(
        "  note: shared model files, configuration, logs, and every "
        "other employee's data are never touched."
    )
    out(
        "  to actually delete, re-run with both --execute and "
        "--confirm-delete."
    )


def run_enrollment_deletion(
    employee_id: str,
    *,
    execute: bool = False,
    confirm_delete: bool = False,
    settings: AppSettings = SETTINGS,
    storage_factory: Callable[[], EmbeddingStorageService] | None = None,
    out: Callable[..., None] = print,
) -> int:
    """Run the preview → confirm → delete → verify operator flow.

    Args:
        employee_id: Exact business identifier (``007``, ``EMP-0042``);
            validated through the project's identity conventions before
            anything else happens.
        execute: ``True`` enables the deletion stage.
        confirm_delete: ``True`` is the second, separate confirmation.
        settings: :class:`AppSettings` locating the three data roots.
        storage_factory: Injectable storage builder (tests inject
            doubles; the default builds the real service).
        out: Output sink (bounded, biometric-free text only).

    Returns:
        Exit code: 0 success, 1 typed failure, 2 refusal.
    """
    # -- 1. Identity validation + derivation (before anything else). --
    try:
        artifacts = _derive_artifacts(settings, employee_id)
    except EnrollmentError as exc:
        out(f"[delete-cli] rejected — invalid employee ID: {exc}")
        return 1

    groups = _group_exists(artifacts)

    # -- 2. Target checks (flags never bypass these). --
    if not any(groups.values()):
        out(_NOTHING_TO_DELETE.format(id=employee_id))
        return 1
    if not execute or not confirm_delete:
        if execute and not confirm_delete:
            out(_CONFIRM_REFUSAL)
            return 2
        _preview(employee_id, artifacts, out)
        return 0

    # -- 3. Delete, in the reversal-safe order. --
    removed: list[str] = []
    try:
        # (a) embedding record files first (recognition candidates).
        for label, path in (
            ("embedding record", artifacts.record_path),
            ("embedding metadata", artifacts.metadata_path),
        ):
            if path.is_file():
                path.unlink()
                removed.append(label)

        # (b) the employee directory (manifest last of this pair: a
        #     crash below leaves orphaned samples but no declared
        #     enrollment — the publish-safe state).
        if artifacts.employee_dir.is_dir():
            shutil.rmtree(artifacts.employee_dir)
            removed.append("manifest")
        elif artifacts.manifest_path.is_file():
            artifacts.manifest_path.unlink()
            removed.append("manifest")

        # (c) face samples last.
        if artifacts.samples_dir.is_dir():
            shutil.rmtree(artifacts.samples_dir)
            removed.append("face samples")

    except OSError as exc:
        remaining = _group_exists(artifacts)
        left = [name for name, present in remaining.items() if present]
        out(
            f"[delete-cli] deletion failed for employee '{employee_id}': "
            f"{exc} Removed so far: "
            f"{', '.join(removed) if removed else 'nothing'}. "
            f"Remaining artifact groups: {', '.join(left) if left else 'none'}. "
            f"{_RECOVERY_NOTE}"
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - mirrors run_integration_demo.py
        remaining = _group_exists(artifacts)
        left = [name for name, present in remaining.items() if present]
        out(
            f"[delete-cli] unexpected deletion failure for employee "
            f"'{employee_id}': {exc} Removed so far: "
            f"{', '.join(removed) if removed else 'nothing'}. "
            f"Remaining artifact groups: {', '.join(left) if left else 'none'}. "
            f"{_RECOVERY_NOTE}"
        )
        return 1

    # -- 4. Final verification through the real recognition path. --
    storage = storage_factory() if storage_factory else _real_storage(settings)
    if employee_id in storage.list_employee_ids():
        out(
            f"[delete-cli] final candidate verification failed for "
            f"employee '{employee_id}': the ID is still discoverable "
            "through the recognition pipeline's candidate list after "
            f"removal. {_RECOVERY_NOTE}"
        )
        return 1
    leftover = [name for name, present in _group_exists(artifacts).items() if present]
    if leftover:
        out(
            f"[delete-cli] final artifact verification failed for "
            f"employee '{employee_id}': these artifact groups are still "
            f"present ({', '.join(leftover)}). {_RECOVERY_NOTE}"
        )
        return 1

    # -- 5. Success: bounded facts only. --
    out(
        f"[delete-cli] success: all enrollment artifacts for employee "
        f"'{employee_id}' were deleted ({'; '.join(removed)}). The "
        "employee is no longer a recognition candidate and the ID can "
        "be enrolled again. Deleted biometric artifacts are not "
        "recoverable."
    )
    return 0


def _real_storage(settings: AppSettings) -> EmbeddingStorageService:
    return EmbeddingStorageService(settings=settings)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delete_enrollment",
        description=(
            "Delete one employee's enrollment artifacts (samples, "
            "manifest, stored embedding) after preview and two-stage "
            "confirmation. Identity is validated with the project's "
            "conventions; only artifacts derived from the validated ID "
            "and located in the configured data directories are "
            "touched."
        ),
    )
    parser.add_argument(
        "--employee-id",
        required=True,
        help=(
            "Exact business identifier of the employee (e.g. '007' or "
            "'EMP-0042'); leading zeros/hyphens/case are preserved."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Enable the deletion stage. Without --confirm-delete as "
            "well, the run is refused (exit 2, nothing happens)."
        ),
    )
    parser.add_argument(
        "--confirm-delete",
        action="store_true",
        help=(
            "Second, separate confirmation required together with "
            "--execute. Flags never bypass ID validation or target "
            "checks."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return run_enrollment_deletion(
            args.employee_id,
            execute=args.execute,
            confirm_delete=args.confirm_delete,
            settings=AppSettings(),
        )
    except Exception as exc:  # noqa: BLE001 - a failed run is a nonzero exit
        print(f"[delete-cli] deletion run failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
