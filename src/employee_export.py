"""Employee-metadata export from enrollment manifests (Phase 3 — Step 1.5c-1).

Read-only source discovery for the deferred PostgreSQL importer. This
module turns the *already published* enrollment manifests into one
versioned JSON Lines artifact; it never writes to the enrollment tree,
never touches the database, and never reads a face sample or an
embedding.

Source (the only one)
---------------------

``<DATA_DIR>/employees/<encoded-id>/employee.json`` — the Step 07
manifest, ``format_version: 1``, of which **only the ``employee``
section** is read (``src.enrollment.EnrollmentService._persist`` writes it
from ``Employee.to_dict()``). The sibling ``samples`` array, the
``sample_count``, the face-sample PNGs and the ``.npz`` embeddings are
never opened, parsed for values, or copied. The attendance journal is
not a metadata source (its five-key record carries an employee ID and
nothing else).

Why an export artifact instead of importing the manifests directly

* The database-writing step then needs **no access to the CV data tree**
  at all — it reads one reviewable file.
* The owner can inspect the artifact before any row is written.
* The format gets a version and a pinned schema the importer and its
  tests can rely on.

Output contract (approved Step 1.5c-1)
--------------------------------------

Exactly five keys per line, one JSON object per line, UTF-8, LF, no
header, no trailing blank line::

    {"schema_version": 1, "employee_id": "...", "full_name": "...",
     "is_active": true, "created_at": "<verbatim manifest ISO-8601>"}

* ``schema_version`` is the integer :data:`EXPORT_SCHEMA_VERSION` (1).
* ``employee_id`` comes from **inside** the manifest, never from the
  directory name; the directory name is only used to *check*
  consistency against the writer's own encoding
  (:func:`src.enrollment.safe_dir_name`).
* ``created_at`` is the manifest's original enrollment timestamp
  **preserved verbatim** — never replaced with export time (approved
  decision: the timestamp is application-authored at enrollment).
* Records are ordered by ``employee_id`` (Unicode code point). This is a
  stable export order, **not** a claim about PostgreSQL collation
  ordering; the importer must not rely on the file's order.
* Nothing else is emitted: no sample counts, no sample records, no
  paths, no image or embedding data, no biometric data of any kind.

Rejections are typed (:class:`ExportRejection`) and reported by
directory name and reason code only — **never** with the employee name
or the manifest contents.

Safety rules
------------

* Reads only ``employee.json`` files that are direct children of the
  employee manifest root; symlinked entries and symlinked manifests are
  refused, so a link cannot lead the scan outside the configured root.
* Source manifests are opened read-only and are never modified.
* Output is written to a temporary file in the destination directory,
  fsynced, permission-restricted where the platform supports it, and
  atomically replaced into place — an interrupted export never leaves a
  partially written final artifact.
* An existing export is never overwritten silently: without
  ``overwrite=True`` the export refuses with
  :class:`ExportOutputExistsError`.
* Fail-closed by default: if **any** manifest is rejected, nothing is
  written. ``allow_partial=True`` is the explicit opt-in that writes
  the valid records only (the rejections are still reported).
* No database access, no DSN handling, and no environment fallback
  lives here.

The export file contains personal data (employee names). It belongs
under the git-ignored ``data/exports/`` directory only, and the report
of this module never echoes its contents.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Final, Sequence

from src.config import SETTINGS, AppSettings
from src.employee import Employee, EmployeeValidationError

__all__ = [
    "EXPORT_FILENAME",
    "EXPORT_SCHEMA_VERSION",
    "CollectionResult",
    "ExportError",
    "ExportOutputExistsError",
    "ExportRecord",
    "ExportRejection",
    "ExportResult",
    "ExportWriteError",
    "RejectedManifest",
    "collect_records",
    "default_output_path",
    "export_employee_metadata",
    "render_jsonl",
    "write_export",
]

#: Version of the JSONL export schema this module emits.
EXPORT_SCHEMA_VERSION: Final[int] = 1

#: Default artifact name inside ``AppSettings.exports_dir``.
EXPORT_FILENAME: Final[str] = "employee_metadata.jsonl"

#: The manifest schema version this exporter understands (Step 07).
_SUPPORTED_MANIFEST_FORMAT_VERSION: Final[int] = 1

#: Exactly the fields the export carries (the employee section contract).
_EMPLOYEE_FIELDS: Final[frozenset[str]] = frozenset(
    {"employee_id", "full_name", "is_active", "created_at"}
)

#: Owner-only artifact permission (POSIX); best effort on other platforms.
_OUTPUT_MODE: Final[int] = 0o600

#: Owner-only directory permission for a newly created export root.
_DIRECTORY_MODE: Final[int] = 0o700

#: Probe name used only to run the model's employee-ID normalization.
_NORMALIZATION_PROBE_NAME: Final[str] = "normalization probe"


class ExportError(Exception):
    """Base class for employee-metadata export failures."""


class ExportOutputExistsError(ExportError):
    """An export target already exists and overwrite was not authorized.

    Never raised as a silent overwrite: the caller must pass
    ``overwrite=True`` (CLI: ``--overwrite``) to replace an artifact.
    """


class ExportWriteError(ExportError):
    """Writing, restricting, or atomically publishing the artifact failed.

    On this path the temporary file is removed and the previous target
    (if any) is left untouched.
    """


class ExportRejection(Enum):
    """Typed, machine-readable reason a manifest produced no record.

    Values are stable strings for operator reports and future importer
    diagnostics. None of them carries manifest content.
    """

    NOT_A_DIRECTORY = "not_a_directory"
    SYMLINK_REFUSED = "symlink_refused"
    MANIFEST_MISSING = "manifest_missing"
    MANIFEST_UNREADABLE = "manifest_unreadable"
    MALFORMED_JSON = "malformed_json"
    UNSUPPORTED_FORMAT_VERSION = "unsupported_format_version"
    MISSING_EMPLOYEE_SECTION = "missing_employee_section"
    MISSING_FIELDS = "missing_fields"
    UNEXPECTED_FIELDS = "unexpected_fields"
    INVALID_EMPLOYEE_ID = "invalid_employee_id"
    ID_NORMALIZATION_CHANGED = "id_normalization_changed"
    DIRECTORY_MISMATCH = "directory_mismatch"
    INVALID_FULL_NAME = "invalid_full_name"
    NAME_NORMALIZATION_CHANGED = "name_normalization_changed"
    INVALID_IS_ACTIVE = "invalid_is_active"
    INVALID_CREATED_AT = "invalid_created_at"
    DUPLICATE_EMPLOYEE_ID = "duplicate_employee_id"


@dataclass(frozen=True, slots=True)
class RejectedManifest:
    """One manifest that produced no record: directory name + reason only."""

    dir_name: str
    reason: ExportRejection


@dataclass(frozen=True, slots=True)
class ExportRecord:
    """One validated employee-metadata record (the four approved fields).

    Attributes:
        employee_id: Exact identifier from the manifest (normalization
            that would change it is rejected, never applied).
        full_name: Exact name from the manifest (same rule).
        is_active: Real boolean from the manifest.
        created_at: The manifest's original ISO-8601 timestamp, kept
            verbatim so the export round-trips byte-exactly.
    """

    employee_id: str
    full_name: str
    is_active: bool
    created_at: str

    def to_payload(self) -> dict[str, object]:
        """The exact five-key JSONL payload (schema version first)."""
        return {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "employee_id": self.employee_id,
            "full_name": self.full_name,
            "is_active": self.is_active,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Outcome of scanning the manifest root (no database, no writes)."""

    records: tuple[ExportRecord, ...]
    rejected: tuple[RejectedManifest, ...]
    scanned: int


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Outcome of one export run — counts and reasons, never names.

    Attributes:
        source_dir: The employee manifest root that was scanned.
        output_path: The artifact path that was (or would be) written.
        scanned: Directory entries examined under the manifest root.
        exported: Records written to the artifact.
        rejected: Every rejected manifest with its typed reason.
        written: Whether the artifact was actually published.
        permissions_restricted: Whether owner-only permissions could be
            applied to the artifact (``False`` where the platform does
            not support it).
    """

    source_dir: Path
    output_path: Path
    scanned: int
    exported: int
    rejected: tuple[RejectedManifest, ...]
    written: bool
    permissions_restricted: bool


# ---------------------------------------------------------------------------
# Validation (pure; reuses the writer's own conventions)
# ---------------------------------------------------------------------------


def _normalized_employee_id(raw_id: str) -> str:
    """Run the model's employee-ID normalization (never re-implemented)."""
    try:
        return Employee(
            employee_id=raw_id, full_name=_NORMALIZATION_PROBE_NAME
        ).employee_id
    except EmployeeValidationError as exc:  # pragma: no cover - pre-validated
        raise ExportError(
            "the Employee model rejected a pre-validated employee_id "
            f"(underlying exception type: {type(exc).__name__})"
        ) from exc


def _normalized_full_name(employee_id: str, raw_name: str) -> str:
    """Run the model's full-name normalization (never re-implemented)."""
    try:
        return Employee(
            employee_id=employee_id, full_name=raw_name
        ).full_name
    except EmployeeValidationError as exc:  # pragma: no cover - pre-validated
        raise ExportError(
            "the Employee model rejected a pre-validated full_name "
            f"(underlying exception type: {type(exc).__name__})"
        ) from exc


class _UnencodableId(Exception):
    """Internal sentinel: the ID cannot be encoded as a directory name."""


def _expected_dir_name(employee_id: str) -> str:
    """The writer's exact directory encoding for an ID.

    Imported lazily so this metadata-only module does not pull the
    camera/OpenCV import chain into metadata paths at import time.
    """
    from src.enrollment import EnrollmentInputError, safe_dir_name

    try:
        return safe_dir_name(employee_id)
    except EnrollmentInputError:
        raise _UnencodableId from None


def _parse_created_at(raw_value: object) -> str | None:
    """Validate the timestamp and return the verbatim string, else ``None``.

    Accepted: a non-blank string parsing as ISO-8601 **with** a UTC
    offset (``Z`` included). Naive timestamps are rejected — the approved
    table contract stores tz-aware ``TIMESTAMPTZ`` values only.
    """
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return raw_value


def _record_from_payload(
    dir_name: str, payload: object
) -> ExportRecord | ExportRejection:
    """Validate one parsed manifest into a record or a typed rejection."""
    if not isinstance(payload, dict):
        return ExportRejection.MALFORMED_JSON

    version = payload.get("format_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != _SUPPORTED_MANIFEST_FORMAT_VERSION
    ):
        return ExportRejection.UNSUPPORTED_FORMAT_VERSION

    section = payload.get("employee")
    if not isinstance(section, dict):
        return ExportRejection.MISSING_EMPLOYEE_SECTION

    present = frozenset(section)
    if not _EMPLOYEE_FIELDS <= present:
        return ExportRejection.MISSING_FIELDS
    if present - _EMPLOYEE_FIELDS:
        return ExportRejection.UNEXPECTED_FIELDS

    raw_id = section["employee_id"]
    if not isinstance(raw_id, str) or not raw_id.strip():
        return ExportRejection.INVALID_EMPLOYEE_ID
    if _normalized_employee_id(raw_id) != raw_id:
        return ExportRejection.ID_NORMALIZATION_CHANGED
    try:
        expected_dir = _expected_dir_name(raw_id)
    except _UnencodableId:
        return ExportRejection.INVALID_EMPLOYEE_ID
    if expected_dir != dir_name:
        return ExportRejection.DIRECTORY_MISMATCH

    raw_name = section["full_name"]
    if not isinstance(raw_name, str) or not raw_name.strip():
        return ExportRejection.INVALID_FULL_NAME
    if _normalized_full_name(raw_id, raw_name) != raw_name:
        return ExportRejection.NAME_NORMALIZATION_CHANGED

    is_active = section["is_active"]
    if not isinstance(is_active, bool):
        return ExportRejection.INVALID_IS_ACTIVE

    created_at = _parse_created_at(section["created_at"])
    if created_at is None:
        return ExportRejection.INVALID_CREATED_AT

    return ExportRecord(
        employee_id=raw_id,
        full_name=raw_name,
        is_active=is_active,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Scanning (read-only; never follows symlinks out of the root)
# ---------------------------------------------------------------------------


def _split_duplicate_ids(
    validated: Sequence[tuple[str, ExportRecord]],
) -> tuple[list[ExportRecord], list[RejectedManifest]]:
    """Separate unambiguous records from employee IDs claimed more than once.

    Every manifest that shares an ``employee_id`` with another is dropped
    and reported as :attr:`ExportRejection.DUPLICATE_EMPLOYEE_ID` — no
    record wins silently. The guard is defensive: because a record's
    directory must equal the writer's deterministic encoding of its own
    ID, two valid manifests cannot legitimately share an ID today; the
    check keeps that true if the encoding ever changes or a manifest is
    hand-crafted.
    """
    grouped: dict[str, list[tuple[str, ExportRecord]]] = {}
    for dir_name, record in validated:
        grouped.setdefault(record.employee_id, []).append((dir_name, record))

    records: list[ExportRecord] = []
    rejected: list[RejectedManifest] = []
    for group in grouped.values():
        if len(group) > 1:
            rejected.extend(
                RejectedManifest(dir_name, ExportRejection.DUPLICATE_EMPLOYEE_ID)
                for dir_name, _ in group
            )
        else:
            records.append(group[0][1])
    return records, rejected


def _read_payload(path: Path) -> object | ExportRejection:
    """Read and parse one manifest file into a JSON value or a rejection."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ExportRejection.MANIFEST_UNREADABLE
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return ExportRejection.MALFORMED_JSON


def collect_records(employees_dir: Path) -> CollectionResult:
    """Scan the employee manifest root and validate every manifest.

    Read-only: no file is created, modified, or deleted, and nothing
    outside ``employees_dir`` is opened. A missing root is not an error
    — it simply yields zero scanned entries (a repository that has never
    enrolled anyone).

    Duplicate employee IDs are **ambiguous identity** and are therefore
    rejected in full: every manifest claiming the same ID is dropped and
    reported with :attr:`ExportRejection.DUPLICATE_EMPLOYEE_ID`, so no
    record wins silently.

    Raises:
        ExportError: the root exists but cannot be listed.
    """
    root = Path(employees_dir)
    if not root.exists():
        return CollectionResult(records=(), rejected=(), scanned=0)
    if root.is_symlink():
        return CollectionResult(
            records=(),
            rejected=(
                RejectedManifest(
                    dir_name=root.name, reason=ExportRejection.SYMLINK_REFUSED
                ),
            ),
            scanned=1,
        )
    if not root.is_dir():
        return CollectionResult(
            records=(),
            rejected=(
                RejectedManifest(
                    dir_name=root.name,
                    reason=ExportRejection.NOT_A_DIRECTORY,
                ),
            ),
            scanned=1,
        )

    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise ExportError(
            "the employee manifest root could not be listed "
            f"(underlying exception type: {type(exc).__name__})"
        ) from exc

    from src.enrollment import MANIFEST_FILENAME

    scanned = 0
    validated: list[tuple[str, ExportRecord]] = []
    rejected: list[RejectedManifest] = []

    for entry in entries:
        scanned += 1
        if entry.is_symlink():
            rejected.append(
                RejectedManifest(entry.name, ExportRejection.SYMLINK_REFUSED)
            )
            continue
        if not entry.is_dir():
            rejected.append(
                RejectedManifest(entry.name, ExportRejection.NOT_A_DIRECTORY)
            )
            continue

        manifest = entry / MANIFEST_FILENAME
        if manifest.is_symlink():
            rejected.append(
                RejectedManifest(entry.name, ExportRejection.SYMLINK_REFUSED)
            )
            continue
        if not manifest.is_file():
            rejected.append(
                RejectedManifest(entry.name, ExportRejection.MANIFEST_MISSING)
            )
            continue

        parsed = _read_payload(manifest)
        if isinstance(parsed, ExportRejection):
            rejected.append(RejectedManifest(entry.name, parsed))
            continue
        outcome = _record_from_payload(entry.name, parsed)
        if isinstance(outcome, ExportRecord):
            validated.append((entry.name, outcome))
        else:
            rejected.append(RejectedManifest(entry.name, outcome))

    # Ambiguous identity: reject every manifest sharing an employee ID.
    records, duplicate_rejections = _split_duplicate_ids(validated)
    rejected.extend(duplicate_rejections)

    records.sort(key=lambda record: record.employee_id)
    rejected.sort(key=lambda item: (item.dir_name, item.reason.value))
    return CollectionResult(
        records=tuple(records),
        rejected=tuple(rejected),
        scanned=scanned,
    )


# ---------------------------------------------------------------------------
# Rendering and atomic publication
# ---------------------------------------------------------------------------


def render_jsonl(records: Sequence[ExportRecord]) -> str:
    """Render records as JSON Lines (UTF-8 text, LF, exactly five keys)."""
    return "".join(
        json.dumps(record.to_payload(), ensure_ascii=False) + "\n"
        for record in records
    )


def _restrict_permissions(path: Path) -> bool:
    """Apply owner-only permissions where the platform supports it.

    Returns ``True`` when the mode was applied. Windows has no POSIX
    permission bits; ``os.chmod`` raises or silently maps to the
    read-only flag, so the failure is reported rather than hidden.
    """
    try:
        os.chmod(path, _OUTPUT_MODE)
    except (OSError, NotImplementedError):
        return False
    return True


def default_output_path(settings: AppSettings = SETTINGS) -> Path:
    """The approved default artifact location under the configured root."""
    return settings.exports_dir / EXPORT_FILENAME


def _precheck_output(path: Path, overwrite: bool) -> None:
    """Refuse (before any scanning or writing) when the target exists."""
    if path.exists() or path.is_symlink():
        if not overwrite:
            raise ExportOutputExistsError(
                "an export artifact already exists at the configured "
                "location; pass overwrite=True (CLI: --overwrite) to "
                "replace it"
            )
    if path.is_dir() and not path.is_symlink():
        raise ExportOutputExistsError(
            "the export target path is a directory; refusing to replace it"
        )


def write_export(
    records: Sequence[ExportRecord],
    output_path: Path,
    *,
    overwrite: bool = False,
) -> bool:
    """Atomically publish the artifact; return the permission outcome.

    The JSONL text is written to a temporary file in the destination
    directory, flushed and fsynced, permission-restricted where
    supported, then moved into place with :func:`os.replace`. A failure
    at any point removes the temporary file and leaves any previous
    artifact untouched.

    Raises:
        ExportOutputExistsError: the target exists and ``overwrite`` is
            ``False`` (checked before anything is created).
        ExportWriteError: directory creation, the write, the flush, the
            permission step, or the atomic replace failed.
    """
    target = Path(output_path)
    _precheck_output(target, overwrite)

    temporary = target.parent / (
        f".{target.name}.tmp-{uuid.uuid4().hex[:12]}"
    )
    permission_ok = False
    try:
        target.parent.mkdir(
            parents=True, exist_ok=True, mode=_DIRECTORY_MODE
        )
        payload = render_jsonl(records)
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        permission_ok = _restrict_permissions(temporary)
        os.replace(temporary, target)
    except ExportError:
        raise
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ExportWriteError(
            "the export artifact could not be written atomically "
            f"(underlying exception type: {type(exc).__name__}); the "
            "previous artifact, if any, is unchanged"
        ) from exc
    return permission_ok


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def export_employee_metadata(
    settings: AppSettings = SETTINGS,
    *,
    output: Path | None = None,
    overwrite: bool = False,
    allow_partial: bool = False,
) -> ExportResult:
    """Scan, validate, and (unless blocked) publish the metadata export.

    Behavior (approved Step 1.5c-1):

    * The output target is pre-checked, so an existing artifact is never
      replaced without ``overwrite``.
    * Scanning is read-only and confined to the employee manifest root.
    * **Fail-closed by default:** if any manifest is rejected, nothing is
      written. ``allow_partial=True`` writes the valid records only and
      still reports every rejection.
    * If entries were scanned but no record validated, nothing is
      written — an empty artifact would falsely read as "no employees".
      A genuinely empty manifest root does write an empty artifact
      (``scanned == 0``), which is the honest representation of "no
      enrollments exist yet".

    Returns:
        :class:`ExportResult` — counts, typed rejections, and whether the
        artifact was published. Never contains employee names.

    Raises:
        ExportOutputExistsError: the target exists and ``overwrite`` is
            ``False``.
        ExportError: the manifest root could not be listed.
        ExportWriteError: the artifact could not be written.
    """
    target = Path(output) if output is not None else default_output_path(settings)
    _precheck_output(target, overwrite)

    collected = collect_records(settings.employees_dir)

    blocked = bool(collected.rejected) and not allow_partial
    nothing_valid = not collected.records and collected.scanned > 0
    if blocked or nothing_valid:
        return ExportResult(
            source_dir=settings.employees_dir,
            output_path=target,
            scanned=collected.scanned,
            exported=0,
            rejected=collected.rejected,
            written=False,
            permissions_restricted=False,
        )

    permission_ok = write_export(
        collected.records, target, overwrite=overwrite
    )
    return ExportResult(
        source_dir=settings.employees_dir,
        output_path=target,
        scanned=collected.scanned,
        exported=len(collected.records),
        rejected=collected.rejected,
        written=True,
        permissions_restricted=permission_ok,
    )
