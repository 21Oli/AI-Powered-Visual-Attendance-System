"""Employee-metadata import from the Step 1.5c-1 export (Phase 3 — Step 1.5c-2).

Consumes **only** the versioned JSON Lines artifact written by
:mod:`src.employee_export` and reconciles it with the approved
employee-metadata repository (:mod:`src.employee_repository`). It never
reads enrollment manifests, never scans employee directories, never
touches the attendance journal, and — in this step — never connects to
PostgreSQL: the repository is injected, and every behavior below is
verified offline with fakes.

Input contract (exactly the exporter's five keys)
-------------------------------------------------

One JSON object per line, UTF-8, LF::

    {"schema_version": 1, "employee_id": "...", "full_name": "...",
     "is_active": true, "created_at": "<ISO-8601 with offset>"}

Each line is validated strictly. Rejections are typed
(:class:`RecordRejection`): malformed JSON, non-object JSON, unsupported
or non-integer ``schema_version``, missing keys, extra keys, invalid or
normalization-changing ``employee_id`` / ``full_name``, non-bool
``is_active``, and a ``created_at`` that is not a timezone-aware
ISO-8601 string. The supplied ``created_at`` is **preserved** (never
replaced with import time); the value handed to the repository is the
model-validated ``Employee`` built from it.

Duplicate employee IDs inside the artifact are ambiguous identity: every
occurrence is rejected (:attr:`RecordRejection.DUPLICATE_EMPLOYEE_ID`),
so no record silently wins.

Reconciliation policy (approved, exact)
---------------------------------------

============================  =========================================
Existing employee state       Behavior
============================  =========================================
Employee does not exist       Create it (``--apply`` only)
All four stored fields match  Skip as identical
Any field differs             Report a conflict; never updated
Invalid input record          Reject that record, continue
Duplicate ID in the artifact  Reject every occurrence of that ID
``EmployeeAlreadyExists``     Report a conflict (create race); no retry
Unexpected repository error   Stop safely, report the failure
============================  =========================================

This module **never** updates anything: no name update and no
deactivation call exists here, so a conflict can never be converted into
a write. ``created_at`` is never modified. The repository is the only
writer, and it is used for exactly two calls: ``get`` (classification)
and ``create`` (approved inserts).

Modes
-----

* ``validate-only`` — no repository at all: schema/duplicate validation
  only (useful before database wiring exists).
* ``dry-run`` — validates and **classifies** against the repository
  (identical / would-create / conflict) and writes nothing.
* ``apply`` — performs the approved creates.

Reports carry counts, employee IDs, field **names** that differ, line
numbers, and stable reason codes. They never carry names, timestamps,
record contents, DSNs, credentials, or driver exception text (only the
exception's type name, mirroring :mod:`src.db`'s redaction discipline).

The real runtime wiring (building a repository over
:func:`src.db.open_connection`) belongs to Step 1.5c-3 and is
deliberately absent here: no connection is opened, no DSN is read, and
no psycopg import exists in this module.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from src.employee import Employee, EmployeeValidationError
from src.employee_repository import EmployeeAlreadyExists

__all__ = [
    "IMPORT_REPORT_SCHEMA_VERSION",
    "IMPORT_SCHEMA_VERSION",
    "EmployeeImportError",
    "EmployeeRepositoryLike",
    "ImportFileError",
    "ImportInputError",
    "ImportRecord",
    "ImportReport",
    "ImportReportError",
    "ImportReportExistsError",
    "ParsedArtifact",
    "RecordOutcome",
    "RecordRejection",
    "RecordResult",
    "RejectedRecord",
    "parse_artifact",
    "read_artifact",
    "report_payload",
    "run_import",
    "write_report",
]

#: The only artifact schema version this importer accepts.
IMPORT_SCHEMA_VERSION: Final[int] = 1

#: Version of the machine-readable import report this module emits.
IMPORT_REPORT_SCHEMA_VERSION: Final[int] = 1

#: Exactly the five keys the exporter writes — nothing more, nothing less.
_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    {"schema_version", "employee_id", "full_name", "is_active", "created_at"}
)

#: Comparison order for reported conflict fields (stable output).
_COMPARED_FIELDS: Final[tuple[str, ...]] = (
    "employee_id",
    "full_name",
    "is_active",
    "created_at",
)

#: Owner-only report permission (POSIX); best effort on other platforms.
_REPORT_MODE: Final[int] = 0o600


class EmployeeImportError(Exception):
    """Base class for employee-metadata import failures."""


class ImportFileError(EmployeeImportError):
    """The input artifact is missing, unreadable, or not valid UTF-8."""


class ImportInputError(EmployeeImportError, ValueError):
    """The caller's arguments violate the import contract."""


class ImportReportError(EmployeeImportError):
    """The report file could not be written."""


class ImportReportExistsError(ImportReportError):
    """A report file already exists and overwrite was not authorized."""


class RecordRejection(Enum):
    """Typed reason one artifact line produced no record."""

    MALFORMED_JSON = "malformed_json"
    MISSING_KEYS = "missing_keys"
    UNEXPECTED_KEYS = "unexpected_keys"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    INVALID_EMPLOYEE_ID = "invalid_employee_id"
    ID_NORMALIZATION_CHANGED = "id_normalization_changed"
    INVALID_FULL_NAME = "invalid_full_name"
    NAME_NORMALIZATION_CHANGED = "name_normalization_changed"
    INVALID_IS_ACTIVE = "invalid_is_active"
    INVALID_CREATED_AT = "invalid_created_at"
    INVALID_RECORD = "invalid_record"
    DUPLICATE_EMPLOYEE_ID = "duplicate_employee_id"


class RecordOutcome(Enum):
    """What happened (or would happen) for one valid record."""

    CREATED = "created"
    WOULD_CREATE = "would_create"
    IDENTICAL = "identical"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """One strictly validated artifact record.

    ``created_at`` is the parsed, timezone-aware value from the artifact —
    preserved as supplied, never re-stamped at import time.
    """

    employee_id: str
    full_name: str
    is_active: bool
    created_at: datetime

    def to_employee(self) -> Employee:
        """Build the model instance the repository expects (no re-writing)."""
        return Employee(
            employee_id=self.employee_id,
            full_name=self.full_name,
            is_active=self.is_active,
            created_at=self.created_at,
        )


@dataclass(frozen=True, slots=True)
class RejectedRecord:
    """One rejected artifact line: line number, optional ID, reason code."""

    line: int
    employee_id: str | None
    reason: RecordRejection


@dataclass(frozen=True, slots=True)
class ParsedArtifact:
    """Result of parsing an artifact (pure; no repository involved)."""

    records: tuple[ImportRecord, ...]
    rejected: tuple[RejectedRecord, ...]
    line_count: int


@dataclass(frozen=True, slots=True)
class RecordResult:
    """Classification of one valid record (never carries field values)."""

    employee_id: str
    outcome: RecordOutcome
    fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Outcome of one import run — counts and reason codes only."""

    mode: str
    input_path: Path
    line_count: int
    valid_records: int
    results: tuple[RecordResult, ...]
    rejected: tuple[RejectedRecord, ...]
    stopped: bool
    error_type: str | None
    unprocessed: int

    def _with(self, outcome: RecordOutcome) -> tuple[str, ...]:
        return tuple(
            result.employee_id
            for result in self.results
            if result.outcome is outcome
        )

    @property
    def created(self) -> tuple[str, ...]:
        return self._with(RecordOutcome.CREATED)

    @property
    def would_create(self) -> tuple[str, ...]:
        return self._with(RecordOutcome.WOULD_CREATE)

    @property
    def identical(self) -> tuple[str, ...]:
        return self._with(RecordOutcome.IDENTICAL)

    @property
    def conflicts(self) -> tuple[RecordResult, ...]:
        return tuple(
            result
            for result in self.results
            if result.outcome is RecordOutcome.CONFLICT
        )

    @property
    def clean(self) -> bool:
        """Whether the run needs no operator attention (exit code 0)."""
        return (
            not self.stopped
            and not self.rejected
            and not self.conflicts
            and self.unprocessed == 0
        )


@runtime_checkable
class EmployeeRepositoryLike(Protocol):
    """The only repository surface this importer is allowed to use.

    Deliberately narrow: no update, no deactivation, no listing. A real
    :class:`~src.employee_repository.EmployeeRepository` satisfies it; the
    offline tests use fakes that also record any forbidden call.
    """

    def get(self, employee_id: str) -> Employee | None: ...

    def create(self, employee: Employee) -> Employee: ...


# ---------------------------------------------------------------------------
# Parsing and validation (pure)
# ---------------------------------------------------------------------------


def _parse_timestamp(raw_value: object) -> datetime | None:
    """Parse a timezone-aware ISO-8601 timestamp, else ``None``."""
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _parse_line(
    line: str,
) -> tuple[ImportRecord | None, RecordRejection | None, str | None]:
    """Validate one artifact line.

    Returns ``(record, None, employee_id)`` on success or
    ``(None, reason, employee_id_or_None)`` on rejection. The ID is
    returned when it is readable, so rejections stay traceable without
    echoing record contents.
    """
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None, RecordRejection.MALFORMED_JSON, None
    if not isinstance(payload, dict):
        return None, RecordRejection.MALFORMED_JSON, None

    present = frozenset(payload)
    raw_id = payload.get("employee_id")
    readable_id = raw_id if isinstance(raw_id, str) else None
    if not _RECORD_KEYS <= present:
        return None, RecordRejection.MISSING_KEYS, readable_id

    version = payload["schema_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != IMPORT_SCHEMA_VERSION
    ):
        return None, RecordRejection.UNSUPPORTED_SCHEMA_VERSION, readable_id

    if present - _RECORD_KEYS:
        return None, RecordRejection.UNEXPECTED_KEYS, readable_id

    if not isinstance(raw_id, str) or not raw_id.strip():
        return None, RecordRejection.INVALID_EMPLOYEE_ID, None

    raw_name = payload["full_name"]
    if not isinstance(raw_name, str) or not raw_name.strip():
        return None, RecordRejection.INVALID_FULL_NAME, raw_id

    is_active = payload["is_active"]
    if not isinstance(is_active, bool):
        return None, RecordRejection.INVALID_IS_ACTIVE, raw_id

    created_at = _parse_timestamp(payload["created_at"])
    if created_at is None:
        return None, RecordRejection.INVALID_CREATED_AT, raw_id

    try:
        employee = Employee(
            employee_id=raw_id,
            full_name=raw_name,
            is_active=is_active,
            created_at=created_at,
        )
    except EmployeeValidationError:
        return None, RecordRejection.INVALID_RECORD, raw_id

    # The model's normalization must be a no-op: a value it would change is
    # rejected rather than silently rewritten into a different identity.
    if employee.employee_id != raw_id:
        return None, RecordRejection.ID_NORMALIZATION_CHANGED, raw_id
    if employee.full_name != raw_name:
        return None, RecordRejection.NAME_NORMALIZATION_CHANGED, raw_id

    return (
        ImportRecord(
            employee_id=employee.employee_id,
            full_name=employee.full_name,
            is_active=employee.is_active,
            created_at=employee.created_at,
        ),
        None,
        employee.employee_id,
    )


def parse_artifact(text: str) -> ParsedArtifact:
    """Strictly parse artifact text; detect and reject duplicate IDs.

    Blank lines are malformed records (the exporter never writes them).
    Records are returned sorted by ``employee_id`` so import order is
    deterministic regardless of the artifact's own ordering; rejections
    are returned sorted by line number.
    """
    validated: list[tuple[int, ImportRecord]] = []
    rejected: list[RejectedRecord] = []
    lines = text.splitlines()

    for number, line in enumerate(lines, start=1):
        record, reason, employee_id = _parse_line(line)
        if record is not None:
            validated.append((number, record))
        else:
            assert reason is not None  # parser contract: one or the other
            rejected.append(
                RejectedRecord(
                    line=number, employee_id=employee_id, reason=reason
                )
            )

    grouped: dict[str, list[tuple[int, ImportRecord]]] = {}
    for number, record in validated:
        grouped.setdefault(record.employee_id, []).append((number, record))

    records: list[ImportRecord] = []
    for group in grouped.values():
        if len(group) > 1:
            rejected.extend(
                RejectedRecord(
                    line=number,
                    employee_id=record.employee_id,
                    reason=RecordRejection.DUPLICATE_EMPLOYEE_ID,
                )
                for number, record in group
            )
        else:
            records.append(group[0][1])

    records.sort(key=lambda record: record.employee_id)
    rejected.sort(key=lambda item: item.line)
    return ParsedArtifact(
        records=tuple(records),
        rejected=tuple(rejected),
        line_count=len(lines),
    )


def read_artifact(path: Path) -> ParsedArtifact:
    """Read and parse the artifact file (UTF-8, strict).

    Raises:
        ImportFileError: the file is missing, unreadable, or not UTF-8.
            The message carries no file content and only the underlying
            exception's type name.
    """
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ImportFileError(
            "the export artifact is not valid UTF-8 "
            f"(underlying exception type: {type(exc).__name__})"
        ) from exc
    except FileNotFoundError as exc:
        raise ImportFileError(
            "the export artifact does not exist at the requested path"
        ) from exc
    except OSError as exc:
        raise ImportFileError(
            "the export artifact could not be read "
            f"(underlying exception type: {type(exc).__name__})"
        ) from exc
    return parse_artifact(text)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _differing_fields(existing: Employee, record: ImportRecord) -> tuple[str, ...]:
    """Names of the fields that differ (never their values)."""
    differences: list[str] = []
    if existing.employee_id != record.employee_id:
        differences.append("employee_id")
    if existing.full_name != record.full_name:
        differences.append("full_name")
    if existing.is_active != record.is_active:
        differences.append("is_active")
    if existing.created_at != record.created_at:
        differences.append("created_at")
    return tuple(
        name for name in _COMPARED_FIELDS if name in differences
    )


def run_import(
    parsed: ParsedArtifact,
    *,
    repository: EmployeeRepositoryLike | None = None,
    apply: bool = False,
    input_path: Path | None = None,
) -> ImportReport:
    """Classify (and optionally create) every valid record.

    Approved policy, per record:

    * absent → create (only with ``apply=True``; otherwise
      :attr:`RecordOutcome.WOULD_CREATE`);
    * all fields equal → :attr:`RecordOutcome.IDENTICAL` (no write);
    * any field differs → :attr:`RecordOutcome.CONFLICT` (no write);
    * ``EmployeeAlreadyExists`` during a create → conflict (race), with
      no update and no retry;
    * any other repository exception → the run **stops safely**: no
      further records are read or written, ``stopped`` is set, and only
      the exception's type name is reported.

    No update or deactivation call is ever made, so conflicts cannot
    become writes.

    Args:
        parsed: Output of :func:`parse_artifact` / :func:`read_artifact`.
        repository: Injected repository; ``None`` selects validate-only
            mode (no classification, no writes).
        apply: Perform creates. Requires ``repository``.
        input_path: Path recorded in the report (diagnostics only).

    Raises:
        ImportInputError: ``apply=True`` without a repository.
    """
    if apply and repository is None:
        raise ImportInputError(
            "apply requires a repository; validate-only mode cannot write"
        )
    if repository is None:
        mode = "validate-only"
    else:
        mode = "apply" if apply else "dry-run"

    ordered = sorted(parsed.records, key=lambda record: record.employee_id)
    results: list[RecordResult] = []
    stopped = False
    error_type: str | None = None

    if repository is not None:
        for record in ordered:
            try:
                existing = repository.get(record.employee_id)
                if existing is None:
                    if apply:
                        repository.create(record.to_employee())
                        outcome = RecordOutcome.CREATED
                    else:
                        outcome = RecordOutcome.WOULD_CREATE
                    results.append(RecordResult(record.employee_id, outcome))
                    continue
                differences = _differing_fields(existing, record)
                results.append(
                    RecordResult(
                        record.employee_id,
                        (
                            RecordOutcome.IDENTICAL
                            if not differences
                            else RecordOutcome.CONFLICT
                        ),
                        differences,
                    )
                )
            except EmployeeAlreadyExists:
                # A concurrent create won the race: a conflict, never an
                # update and never a retry.
                results.append(
                    RecordResult(
                        record.employee_id,
                        RecordOutcome.CONFLICT,
                        ("employee_id",),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - stop safely, report type
                stopped = True
                error_type = type(exc).__name__
                break

    # Validate-only mode examines every record up front, so nothing is
    # "unprocessed"; the count only means something once reconciliation is
    # attempted against a repository.
    unprocessed = 0 if repository is None else len(ordered) - len(results)

    return ImportReport(
        mode=mode,
        input_path=Path(input_path) if input_path is not None else Path("-"),
        line_count=parsed.line_count,
        valid_records=len(parsed.records),
        results=tuple(results),
        rejected=parsed.rejected,
        stopped=stopped,
        error_type=error_type,
        unprocessed=unprocessed,
    )


# ---------------------------------------------------------------------------
# Report payload and file
# ---------------------------------------------------------------------------


def report_payload(report: ImportReport) -> dict[str, object]:
    """Render the report as a JSON-ready mapping (no names, no values)."""
    conflict_counts: dict[str, int] = {}
    for result in report.results:
        conflict_counts[result.outcome.value] = (
            conflict_counts.get(result.outcome.value, 0) + 1
        )
    return {
        "schema_version": IMPORT_REPORT_SCHEMA_VERSION,
        "mode": report.mode,
        "input": str(report.input_path),
        "line_count": report.line_count,
        "valid_records": report.valid_records,
        "counts": {
            "created": len(report.created),
            "would_create": len(report.would_create),
            "identical": len(report.identical),
            "conflict": len(report.conflicts),
            "rejected": len(report.rejected),
            "unprocessed": report.unprocessed,
        },
        "conflicts": [
            {"employee_id": result.employee_id, "fields": list(result.fields)}
            for result in report.conflicts
        ],
        "rejected": [
            {
                "line": item.line,
                "employee_id": item.employee_id,
                "reason": item.reason.value,
            }
            for item in report.rejected
        ],
        "stopped": report.stopped,
        "error_type": report.error_type,
    }


def write_report(
    payload: dict[str, object], path: Path, *, overwrite: bool = False
) -> Path:
    """Write the report atomically; never replace one silently.

    Raises:
        ImportReportExistsError: the target exists and ``overwrite`` is
            ``False``.
        ImportReportError: the report could not be written.
    """
    target = Path(path)
    if target.exists() and not overwrite:
        raise ImportReportExistsError(
            "a report already exists at the requested path; pass overwrite "
            "(CLI: --overwrite-report) to replace it"
        )
    temporary = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex[:12]}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, _REPORT_MODE)
        except (OSError, NotImplementedError):
            pass
        os.replace(temporary, target)
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ImportReportError(
            "the import report could not be written "
            f"(underlying exception type: {type(exc).__name__})"
        ) from exc
    return target
