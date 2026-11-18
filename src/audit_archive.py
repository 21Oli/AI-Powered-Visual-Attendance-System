"""Archive reading and historical chain verification (P5.5.7 stages B and G).

This module turns the pure manifest model of :mod:`src.audit_manifest` and the
layout of :mod:`src.audit_layout` into **file-level verification** of an audit
installation:

* the manifest file itself (strict JSONL, torn tail, canonical chain);
* every declared archive ``archive/audit-<first>-<last>.jsonl`` (regular file,
  UTF-8, one canonical record per line, final newline, full event schema, every
  digest, predecessor links, global and segment-local sequences, segment
  identity, record count, bounds and both hash anchors);
* the active journal, including its boundary against sealed history;
* files that look like an archive but are not registered;
* duplicate ``audit_id`` values across segments.

Everything here is **read-only**. No file is created, appended, truncated,
repaired, renamed, chmod-ed or deleted, and no lock is taken (a shared lock
could interfere with the writer, so the reader instead confirms that each file
it read did not change while it was being read). A file that changes during
verification is reported, never silently accepted.

Coverage honesty
================

A manifest that verifies proves its own chain and its statements. Only a
successful :func:`verify_declared_archive` proves that the bytes of a sealed
segment match the declaration. When a segment's archive is absent after an
owner-approved ``archived_offsite`` retention decision, the segment is recorded
as **anchor-only** coverage: its manifest anchors and their continuity are
verified, but its records were not content-verified and the caller must say so.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, IO, Iterable, Sequence

from src.audit_layout import AuditLayout
from src.audit_manifest import (
    ManifestArchivePathError,
    ManifestChainError,
    ManifestContinuityError,
    ManifestDuplicateError,
    ManifestEntry,
    ManifestEntryType,
    ManifestError,
    ManifestHashMismatchError,
    ManifestReferenceError,
    ManifestSchemaError,
    ManifestSequenceError,
    verify_manifest,
)
from src.audit_schema import (
    GENESIS_PREVIOUS_HASH,
    AuditChainError,
    AuditEvent,
    AuditHashMismatchError,
    AuditPreviousHashMismatchError,
    AuditSchemaError,
    AuditSegmentError,
    AuditSequenceError,
    verify_chain,
)

__all__ = [
    "ACTIVE_SEGMENT_MISSING",
    "ARCHIVE_CHANGED",
    "ARCHIVE_CONFLICT",
    "ARCHIVE_COUNT_MISMATCH",
    "ARCHIVE_BOUNDS_MISMATCH",
    "ARCHIVE_ANCHOR_MISMATCH",
    "ARCHIVE_BROKEN_PREDECESSOR",
    "ARCHIVE_DUPLICATE_AUDIT_ID",
    "ARCHIVE_EMPTY_LINE",
    "ARCHIVE_HASH_MISMATCH",
    "ARCHIVE_INVALID_GENESIS",
    "ARCHIVE_INVALID_UTF8",
    "ARCHIVE_MALFORMED_JSON",
    "ARCHIVE_MISSING",
    "ARCHIVE_NOT_REGULAR_FILE",
    "ARCHIVE_SCHEMA_INVALID",
    "ARCHIVE_SEGMENT_INVALID",
    "ARCHIVE_SEQUENCE_INVALID",
    "ARCHIVE_TORN_TAIL",
    "ARCHIVE_UNEXPECTED_FILE",
    "ARCHIVE_UNREADABLE",
    "ARCHIVE_UNREGISTERED",
    "ActiveJournalState",
    "HistoryState",
    "IntegrityProblem",
    "LayoutProblem",
    "RecoveryProblem",
    "SegmentSummary",
    "UnavailableProblem",
    "parse_event_lines",
    "read_archive_bytes",
    "read_manifest_entries",
    "verify_active_journal",
    "verify_archive_file",
    "verify_declared_archive",
    "verify_history",
]

# ---------------------------------------------------------------------------
# Categories (stable strings printed by the CLI)
# ---------------------------------------------------------------------------

JOURNAL_MISSING: Final[str] = "JOURNAL_MISSING"
JOURNAL_NOT_REGULAR_FILE: Final[str] = "JOURNAL_NOT_REGULAR_FILE"
JOURNAL_UNREADABLE: Final[str] = "JOURNAL_UNREADABLE"
JOURNAL_CHANGED: Final[str] = "JOURNAL_CHANGED"
INVALID_UTF8: Final[str] = "INVALID_UTF8"
EMPTY_LINE: Final[str] = "EMPTY_LINE"
TORN_TAIL: Final[str] = "TORN_TAIL"
MALFORMED_JSON: Final[str] = "MALFORMED_JSON"
SCHEMA_INVALID: Final[str] = "SCHEMA_INVALID"
HASH_MISMATCH: Final[str] = "HASH_MISMATCH"
INVALID_GENESIS: Final[str] = "INVALID_GENESIS"
BROKEN_PREDECESSOR: Final[str] = "BROKEN_PREDECESSOR"
SEQUENCE_INVALID: Final[str] = "SEQUENCE_INVALID"
SEGMENT_INVALID: Final[str] = "SEGMENT_INVALID"
DUPLICATE_AUDIT_ID: Final[str] = "DUPLICATE_AUDIT_ID"
TIMESTAMP_REGRESSION: Final[str] = "TIMESTAMP_REGRESSION"

ARCHIVE_MISSING: Final[str] = "ARCHIVE_MISSING"
ARCHIVE_NOT_REGULAR_FILE: Final[str] = "ARCHIVE_NOT_REGULAR_FILE"
ARCHIVE_UNREADABLE: Final[str] = "ARCHIVE_UNREADABLE"
ARCHIVE_CHANGED: Final[str] = "ARCHIVE_CHANGED"
ARCHIVE_INVALID_UTF8: Final[str] = "ARCHIVE_INVALID_UTF8"
ARCHIVE_EMPTY_LINE: Final[str] = "ARCHIVE_EMPTY_LINE"
ARCHIVE_TORN_TAIL: Final[str] = "ARCHIVE_TORN_TAIL"
ARCHIVE_MALFORMED_JSON: Final[str] = "ARCHIVE_MALFORMED_JSON"
ARCHIVE_SCHEMA_INVALID: Final[str] = "ARCHIVE_SCHEMA_INVALID"
ARCHIVE_HASH_MISMATCH: Final[str] = "ARCHIVE_HASH_MISMATCH"
ARCHIVE_INVALID_GENESIS: Final[str] = "ARCHIVE_INVALID_GENESIS"
ARCHIVE_BROKEN_PREDECESSOR: Final[str] = "ARCHIVE_BROKEN_PREDECESSOR"
ARCHIVE_SEQUENCE_INVALID: Final[str] = "ARCHIVE_SEQUENCE_INVALID"
ARCHIVE_SEGMENT_INVALID: Final[str] = "ARCHIVE_SEGMENT_INVALID"
ARCHIVE_DUPLICATE_AUDIT_ID: Final[str] = "ARCHIVE_DUPLICATE_AUDIT_ID"
ARCHIVE_TIMESTAMP_REGRESSION: Final[str] = "ARCHIVE_TIMESTAMP_REGRESSION"
ARCHIVE_COUNT_MISMATCH: Final[str] = "ARCHIVE_COUNT_MISMATCH"
ARCHIVE_BOUNDS_MISMATCH: Final[str] = "ARCHIVE_BOUNDS_MISMATCH"
ARCHIVE_ANCHOR_MISMATCH: Final[str] = "ARCHIVE_ANCHOR_MISMATCH"
ARCHIVE_UNREGISTERED: Final[str] = "ARCHIVE_UNREGISTERED"
ARCHIVE_UNEXPECTED_FILE: Final[str] = "ARCHIVE_UNEXPECTED_FILE"
ARCHIVE_CONFLICT: Final[str] = "ARCHIVE_CONFLICT"

MANIFEST_UNREADABLE: Final[str] = "MANIFEST_UNREADABLE"
MANIFEST_TORN_TAIL: Final[str] = "MANIFEST_TORN_TAIL"
MANIFEST_CORRUPT: Final[str] = "MANIFEST_CORRUPT"
MANIFEST_SCHEMA_INVALID: Final[str] = "MANIFEST_SCHEMA_INVALID"
MANIFEST_HASH_MISMATCH: Final[str] = "MANIFEST_HASH_MISMATCH"
MANIFEST_BROKEN_CHAIN: Final[str] = "MANIFEST_BROKEN_CHAIN"
MANIFEST_SEQUENCE_INVALID: Final[str] = "MANIFEST_SEQUENCE_INVALID"
MANIFEST_DUPLICATE: Final[str] = "MANIFEST_DUPLICATE"
MANIFEST_REFERENCE_INVALID: Final[str] = "MANIFEST_REFERENCE_INVALID"
MANIFEST_INTEGRITY: Final[str] = "MANIFEST_INTEGRITY"

ACTIVE_SEGMENT_MISSING: Final[str] = "ACTIVE_SEGMENT_MISSING"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LayoutProblem(Exception):
    """A sanitized installation problem.

    ``category`` is one of the stable strings above; ``line`` is a 1-based line
    inside the offending file (``None`` when not line-specific) and
    ``segment_id`` names the manifest segment when known. Messages are fixed
    sentences and never contain record bodies, paths from OS errors or
    exception text.
    """

    def __init__(
        self,
        category: str,
        message: str,
        *,
        line: int | None = None,
        segment_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.line = line
        self.segment_id = segment_id
        self.recovery = False


class UnavailableProblem(LayoutProblem):
    """A file could not be read; nothing is known about its contents."""


class IntegrityProblem(LayoutProblem):
    """The installation's contents do not satisfy an integrity rule."""


class RecoveryProblem(LayoutProblem):
    """A documented crash-recovery state requiring an explicit operator action."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.recovery = True


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _read_snapshot(
    path: Path,
    *,
    missing: str,
    not_regular: str,
    unreadable: str,
    changed: str,
    segment_id: str | None = None,
) -> bytes:
    """Read a whole file once and refuse any change during the read.

    Symlinks are refused: an archive path must name a regular file, and a
    substituted link is a path substitution, not an audit artifact.
    """
    try:
        initial = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        raise UnavailableProblem(
            missing,
            "the expected audit file is missing",
            segment_id=segment_id,
        ) from None
    except OSError:
        raise UnavailableProblem(
            unreadable,
            "the expected audit file could not be inspected",
            segment_id=segment_id,
        ) from None
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
        raise UnavailableProblem(
            not_regular,
            "the expected audit path is not a regular file",
            segment_id=segment_id,
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise UnavailableProblem(
            missing,
            "the expected audit file is missing",
            segment_id=segment_id,
        ) from None
    except OSError:
        raise UnavailableProblem(
            unreadable,
            "the expected audit file could not be opened for reading",
            segment_id=segment_id,
        ) from None

    handle: IO[bytes] | None = None
    try:
        try:
            handle = os.fdopen(descriptor, "rb")
        except OSError:
            raise UnavailableProblem(
                unreadable,
                "the expected audit file could not be opened for reading",
                segment_id=segment_id,
            ) from None
        descriptor = -1
        try:
            before = os.fstat(handle.fileno())
            raw = handle.read()
            after = os.fstat(handle.fileno())
        except OSError:
            raise UnavailableProblem(
                unreadable,
                "the expected audit file could not be read",
                segment_id=segment_id,
            ) from None
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        elif descriptor >= 0:  # pragma: no cover - fdopen failure path
            try:
                os.close(descriptor)
            except OSError:
                pass

    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RecoveryProblem(
            changed,
            "the audit file changed while it was being read",
            segment_id=segment_id,
        )
    try:
        final = os.stat(path, follow_symlinks=False)
    except OSError:
        raise RecoveryProblem(
            changed,
            "the audit file changed while it was being read",
            segment_id=segment_id,
        ) from None
    if (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
    ) != (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
        initial.st_mtime_ns,
    ):
        raise RecoveryProblem(
            changed,
            "the audit file changed while it was being read",
            segment_id=segment_id,
        )
    return raw


def read_archive_bytes(path: Path, *, segment_id: str | None = None) -> bytes:
    """Read one archive file with change detection (never creates it)."""
    return _read_snapshot(
        path,
        missing=ARCHIVE_MISSING,
        not_regular=ARCHIVE_NOT_REGULAR_FILE,
        unreadable=ARCHIVE_UNREADABLE,
        changed=ARCHIVE_CHANGED,
        segment_id=segment_id,
    )


@dataclass(frozen=True)
class _Vocabulary:
    invalid_utf8: str
    empty_line: str
    torn_tail: str
    malformed_json: str
    schema_invalid: str
    hash_mismatch: str
    invalid_genesis: str
    broken_predecessor: str
    sequence_invalid: str
    segment_invalid: str
    duplicate_audit_id: str
    timestamp_regression: str


_ACTIVE_VOCABULARY: Final[_Vocabulary] = _Vocabulary(
    invalid_utf8=INVALID_UTF8,
    empty_line=EMPTY_LINE,
    torn_tail=TORN_TAIL,
    malformed_json=MALFORMED_JSON,
    schema_invalid=SCHEMA_INVALID,
    hash_mismatch=HASH_MISMATCH,
    invalid_genesis=INVALID_GENESIS,
    broken_predecessor=BROKEN_PREDECESSOR,
    sequence_invalid=SEQUENCE_INVALID,
    segment_invalid=SEGMENT_INVALID,
    duplicate_audit_id=DUPLICATE_AUDIT_ID,
    timestamp_regression=TIMESTAMP_REGRESSION,
)

_ARCHIVE_VOCABULARY: Final[_Vocabulary] = _Vocabulary(
    invalid_utf8=ARCHIVE_INVALID_UTF8,
    empty_line=ARCHIVE_EMPTY_LINE,
    torn_tail=ARCHIVE_TORN_TAIL,
    malformed_json=ARCHIVE_MALFORMED_JSON,
    schema_invalid=ARCHIVE_SCHEMA_INVALID,
    hash_mismatch=ARCHIVE_HASH_MISMATCH,
    invalid_genesis=ARCHIVE_INVALID_GENESIS,
    broken_predecessor=ARCHIVE_BROKEN_PREDECESSOR,
    sequence_invalid=ARCHIVE_SEQUENCE_INVALID,
    segment_invalid=ARCHIVE_SEGMENT_INVALID,
    duplicate_audit_id=ARCHIVE_DUPLICATE_AUDIT_ID,
    timestamp_regression=ARCHIVE_TIMESTAMP_REGRESSION,
)


def parse_event_lines(
    raw: bytes,
    *,
    vocabulary: _Vocabulary = _ACTIVE_VOCABULARY,
    segment_id: str | None = None,
) -> list[AuditEvent]:
    """Parse canonical JSONL with the writer's structural rules.

    An empty byte string is a valid empty chain. A non-empty file must end with
    a newline (a torn tail is reported, never repaired); empty lines, invalid
    UTF-8, malformed JSON and schema-invalid records are refused.
    """
    if raw and not raw.endswith(b"\n"):
        raise IntegrityProblem(
            vocabulary.torn_tail,
            "the audit file ends with an incomplete record",
            line=raw.count(b"\n") + 1,
            segment_id=segment_id,
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise IntegrityProblem(
            vocabulary.invalid_utf8,
            "the audit file is not valid UTF-8",
            segment_id=segment_id,
        ) from None

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    events: list[AuditEvent] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            raise IntegrityProblem(
                vocabulary.empty_line,
                "the audit file contains an empty record",
                line=index,
                segment_id=segment_id,
            )
        try:
            payload = json.loads(line)
        except ValueError:
            raise IntegrityProblem(
                vocabulary.malformed_json,
                "an audit line is not valid JSON",
                line=index,
                segment_id=segment_id,
            ) from None
        try:
            events.append(AuditEvent.from_dict(payload))
        except AuditSchemaError:
            raise IntegrityProblem(
                vocabulary.schema_invalid,
                "an audit record does not satisfy the approved schema",
                line=index,
                segment_id=segment_id,
            ) from None
    return events


def _verify_event_chain(
    events: Sequence[AuditEvent],
    *,
    vocabulary: _Vocabulary,
    expected_previous_hash: str,
    expected_next_sequence: int,
    segment_id: str | None,
) -> None:
    try:
        verify_chain(
            list(events),
            expected_previous_hash=expected_previous_hash,
            expected_next_sequence=expected_next_sequence,
        )
    except AuditPreviousHashMismatchError as exc:
        category = (
            vocabulary.invalid_genesis
            if exc.position == 0 and expected_next_sequence == 1
            else vocabulary.broken_predecessor
        )
        raise IntegrityProblem(
            category,
            "a record does not link to its expected predecessor hash",
            line=None if exc.position is None else exc.position + 1,
            segment_id=segment_id,
        ) from None
    except AuditHashMismatchError as exc:
        raise IntegrityProblem(
            vocabulary.hash_mismatch,
            "a record's stored digest does not match its canonical body",
            line=None if exc.position is None else exc.position + 1,
            segment_id=segment_id,
        ) from None
    except AuditSequenceError as exc:
        raise IntegrityProblem(
            vocabulary.sequence_invalid,
            "the global or segment-local sequence is not contiguous",
            line=None if exc.position is None else exc.position + 1,
            segment_id=segment_id,
        ) from None
    except AuditSegmentError as exc:
        raise IntegrityProblem(
            vocabulary.segment_invalid,
            "a segment identity rule is violated",
            line=None if exc.position is None else exc.position + 1,
            segment_id=segment_id,
        ) from None
    except AuditChainError as exc:
        raise IntegrityProblem(
            vocabulary.segment_invalid,
            "the audit chain could not be verified",
            segment_id=segment_id,
        ) from None
    except AuditSchemaError:
        raise IntegrityProblem(
            vocabulary.schema_invalid,
            "an audit record does not satisfy the approved schema",
            segment_id=segment_id,
        ) from None


def _check_audit_ids_and_timestamps(
    events: Sequence[AuditEvent],
    *,
    vocabulary: _Vocabulary,
    segment_id: str | None,
    known_audit_ids: set[str] | None = None,
) -> set[str]:
    seen: set[str] = set(known_audit_ids or ())
    previous_timestamp: str | None = None
    for index, event in enumerate(events):
        if event.audit_id in seen:
            raise IntegrityProblem(
                vocabulary.duplicate_audit_id,
                "the audit history contains a duplicate audit id",
                line=index + 1,
                segment_id=segment_id,
            )
        seen.add(event.audit_id)
        if previous_timestamp is not None and event.timestamp < previous_timestamp:
            raise IntegrityProblem(
                vocabulary.timestamp_regression,
                "timestamps are not non-decreasing within a segment",
                line=index + 1,
                segment_id=segment_id,
            )
        previous_timestamp = event.timestamp
    return seen


# ---------------------------------------------------------------------------
# Segment summaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SegmentSummary:
    """Verified facts about one sealed or active segment (no record bodies)."""

    segment_id: str
    record_count: int
    first_global_sequence: int
    last_global_sequence: int
    first_previous_hash: str
    last_record_hash: str
    first_timestamp: str
    last_timestamp: str
    anchor_only: bool = False
    audit_ids: frozenset[str] = field(default_factory=frozenset)


def summarise_segment(
    events: Sequence[AuditEvent],
    *,
    expected_segment_id: str,
    expected_first_sequence: int,
    expected_previous_hash: str,
    vocabulary: _Vocabulary = _ARCHIVE_VOCABULARY,
) -> SegmentSummary:
    """Verify one contiguous segment body and summarise it."""
    if not events:
        raise IntegrityProblem(
            vocabulary.segment_invalid,
            "a sealed segment must contain at least one record",
            segment_id=expected_segment_id,
        )
    for index, event in enumerate(events):
        if event.segment_id != expected_segment_id:
            raise IntegrityProblem(
                vocabulary.segment_invalid,
                "a record belongs to a different segment than declared",
                line=index + 1,
                segment_id=expected_segment_id,
            )
    if events[0].segment_sequence != 1:
        raise IntegrityProblem(
            vocabulary.sequence_invalid,
            "a sealed segment must start at segment sequence 1",
            line=1,
            segment_id=expected_segment_id,
        )
    _verify_event_chain(
        events,
        vocabulary=vocabulary,
        expected_previous_hash=expected_previous_hash,
        expected_next_sequence=expected_first_sequence,
        segment_id=expected_segment_id,
    )
    audit_ids = _check_audit_ids_and_timestamps(
        events, vocabulary=vocabulary, segment_id=expected_segment_id
    )
    return SegmentSummary(
        segment_id=expected_segment_id,
        record_count=len(events),
        first_global_sequence=events[0].sequence,
        last_global_sequence=events[-1].sequence,
        first_previous_hash=events[0].previous_hash,
        last_record_hash=events[-1].record_hash,
        first_timestamp=events[0].timestamp,
        last_timestamp=events[-1].timestamp,
        audit_ids=frozenset(audit_ids),
    )


def verify_archive_file(
    path: Path,
    *,
    expected_segment_id: str,
    expected_first_sequence: int,
    expected_previous_hash: str,
    known_audit_ids: set[str] | None = None,
) -> SegmentSummary:
    """Read and fully verify one archive file."""
    raw = _read_snapshot(
        path,
        missing=ARCHIVE_MISSING,
        not_regular=ARCHIVE_NOT_REGULAR_FILE,
        unreadable=ARCHIVE_UNREADABLE,
        changed=ARCHIVE_CHANGED,
        segment_id=expected_segment_id,
    )
    events = parse_event_lines(
        raw, vocabulary=_ARCHIVE_VOCABULARY, segment_id=expected_segment_id
    )
    summary = summarise_segment(
        events,
        expected_segment_id=expected_segment_id,
        expected_first_sequence=expected_first_sequence,
        expected_previous_hash=expected_previous_hash,
    )
    if known_audit_ids:
        duplicates = summary.audit_ids & known_audit_ids
        if duplicates:
            raise IntegrityProblem(
                ARCHIVE_DUPLICATE_AUDIT_ID,
                "the audit history contains a duplicate audit id",
                segment_id=expected_segment_id,
            )
    return summary


def _compare_archive_to_entry(
    summary: SegmentSummary, entry: ManifestEntry
) -> None:
    """Require the archive's own facts to equal the manifest declaration."""
    if summary.segment_id != entry.segment_id:
        raise IntegrityProblem(
            ARCHIVE_SEGMENT_INVALID,
            "the archive's segment identity does not match the manifest",
            segment_id=entry.segment_id,
        )
    if summary.record_count != entry.record_count:
        raise IntegrityProblem(
            ARCHIVE_COUNT_MISMATCH,
            "the archive's record count does not match the manifest",
            segment_id=entry.segment_id,
        )
    if (
        summary.first_global_sequence != entry.first_global_sequence
        or summary.last_global_sequence != entry.last_global_sequence
    ):
        raise IntegrityProblem(
            ARCHIVE_BOUNDS_MISMATCH,
            "the archive's sequence bounds do not match the manifest",
            segment_id=entry.segment_id,
        )
    if (
        summary.first_previous_hash != entry.first_previous_hash
        or summary.last_record_hash != entry.last_record_hash
    ):
        raise IntegrityProblem(
            ARCHIVE_ANCHOR_MISMATCH,
            "the archive's hash anchors do not match the manifest",
            segment_id=entry.segment_id,
        )


def verify_declared_archive(
    entry: ManifestEntry,
    layout: AuditLayout,
    *,
    known_audit_ids: set[str] | None = None,
) -> SegmentSummary:
    """Verify the archive declared by one ``segment_sealed`` manifest entry."""
    if entry.entry_type is not ManifestEntryType.SEGMENT_SEALED:
        raise IntegrityProblem(
            ARCHIVE_CONFLICT,
            "only a sealed segment declares an archive",
            segment_id=entry.segment_id,
        )
    if entry.segment_file is None:  # pragma: no cover - schema forbids it
        raise IntegrityProblem(
            ARCHIVE_CONFLICT,
            "a sealed segment must declare its archive",
            segment_id=entry.segment_id,
        )
    path = layout.archive_path_for_reference(entry.segment_file)
    if path.parent != layout.archive_directory:
        raise IntegrityProblem(
            ARCHIVE_CONFLICT,
            "the archive reference escapes the archive directory",
            segment_id=entry.segment_id,
        )
    summary = verify_archive_file(
        path,
        expected_segment_id=entry.segment_id,
        expected_first_sequence=entry.first_global_sequence,
        expected_previous_hash=entry.first_previous_hash,
        known_audit_ids=known_audit_ids,
    )
    _compare_archive_to_entry(summary, entry)
    return summary


# ---------------------------------------------------------------------------
# Manifest reading
# ---------------------------------------------------------------------------


def read_manifest_entries(path: Path) -> list[ManifestEntry]:
    """Read and parse the manifest file (never repaired, never rewritten)."""
    raw = _read_snapshot(
        path,
        missing=MANIFEST_UNREADABLE,
        not_regular=MANIFEST_UNREADABLE,
        unreadable=MANIFEST_UNREADABLE,
        changed=MANIFEST_UNREADABLE,
    )
    if raw and not raw.endswith(b"\n"):
        raise RecoveryProblem(
            MANIFEST_TORN_TAIL,
            "the manifest ends with an incomplete entry; operator investigation "
            "is required and the entry must not be truncated automatically",
            line=raw.count(b"\n") + 1,
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise IntegrityProblem(
            MANIFEST_CORRUPT, "the manifest is not valid UTF-8"
        ) from None

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    entries: list[ManifestEntry] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            raise IntegrityProblem(
                MANIFEST_CORRUPT,
                "the manifest contains an empty line",
                line=index,
            )
        try:
            payload = json.loads(line)
        except ValueError:
            raise IntegrityProblem(
                MANIFEST_CORRUPT,
                "a manifest line is not valid JSON",
                line=index,
            ) from None
        try:
            entries.append(ManifestEntry.from_dict(payload))
        except ManifestSchemaError:
            raise IntegrityProblem(
                MANIFEST_SCHEMA_INVALID,
                "a manifest entry does not satisfy the approved schema",
                line=index,
            ) from None
    return entries


def _verify_manifest_chain(entries: Iterable[ManifestEntry]):
    try:
        return verify_manifest(entries)
    except ManifestSequenceError:
        raise IntegrityProblem(
            MANIFEST_SEQUENCE_INVALID,
            "the manifest sequence is not contiguous from 1",
        ) from None
    except ManifestDuplicateError:
        raise IntegrityProblem(
            MANIFEST_DUPLICATE,
            "the manifest declares a segment or archive twice",
        ) from None
    except ManifestReferenceError:
        raise IntegrityProblem(
            MANIFEST_REFERENCE_INVALID,
            "a retention decision does not reference its sealed segment",
        ) from None
    except ManifestContinuityError:
        raise IntegrityProblem(
            MANIFEST_BROKEN_CHAIN,
            "the manifest's chain or segment continuity is broken",
        ) from None
    except ManifestHashMismatchError:
        raise IntegrityProblem(
            MANIFEST_HASH_MISMATCH,
            "a manifest entry hash does not match its canonical body",
        ) from None
    except ManifestArchivePathError:
        raise IntegrityProblem(
            MANIFEST_SCHEMA_INVALID,
            "a manifest archive reference is not approved",
        ) from None
    except ManifestChainError:
        raise IntegrityProblem(
            MANIFEST_INTEGRITY, "the manifest chain could not be verified"
        ) from None
    except ManifestError:
        raise IntegrityProblem(
            MANIFEST_INTEGRITY, "the manifest could not be verified"
        ) from None


# ---------------------------------------------------------------------------
# Whole-history verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoryState:
    """Verified state of the manifest and every declared archive."""

    manifest_entries: tuple[ManifestEntry, ...] = ()
    segments: tuple[SegmentSummary, ...] = ()
    anchor_only_segment_ids: tuple[str, ...] = ()
    offsite_segment_ids: tuple[str, ...] = ()
    last_manifest_hash: str | None = None
    audit_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def seals(self) -> tuple[ManifestEntry, ...]:
        return tuple(
            entry
            for entry in self.manifest_entries
            if entry.entry_type is ManifestEntryType.SEGMENT_SEALED
        )

    @property
    def last_seal(self) -> ManifestEntry | None:
        seals = self.seals
        return seals[-1] if seals else None

    @property
    def sealed_segment_ids(self) -> tuple[str, ...]:
        return tuple(entry.segment_id for entry in self.seals)

    @property
    def expected_next_sequence(self) -> int:
        seal = self.last_seal
        return 1 if seal is None else seal.last_global_sequence + 1

    @property
    def expected_previous_hash(self) -> str:
        seal = self.last_seal
        return GENESIS_PREVIOUS_HASH if seal is None else seal.last_record_hash

    @property
    def total_records(self) -> int:
        return sum(summary.record_count for summary in self.segments)

    @property
    def anchor_only(self) -> bool:
        return bool(self.anchor_only_segment_ids)


def _unregistered_archives(
    layout: AuditLayout, declared: set[str]
) -> None:
    """Refuse archives that exist but are not declared, and stray files."""
    directory = layout.archive_directory
    try:
        stat_result = os.stat(directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise UnavailableProblem(
            ARCHIVE_UNREADABLE, "the archive directory could not be inspected"
        ) from None
    if stat.S_ISLNK(stat_result.st_mode) or not stat.S_ISDIR(stat_result.st_mode):
        raise IntegrityProblem(
            ARCHIVE_UNEXPECTED_FILE,
            "the archive path is not a plain directory",
        )
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        raise UnavailableProblem(
            ARCHIVE_UNREADABLE, "the archive directory could not be listed"
        ) from None
    for name in names:
        if name in declared:
            continue
        if _looks_like_archive(name):
            raise RecoveryProblem(
                ARCHIVE_UNREGISTERED,
                "an archive file exists that the manifest does not declare; an "
                "interrupted rotation requires the explicit registration command",
            )
        raise IntegrityProblem(
            ARCHIVE_UNEXPECTED_FILE,
            "the archive directory contains an unexpected entry",
        )


def _looks_like_archive(name: str) -> bool:
    return name.startswith("audit-") and name.endswith(".jsonl")


def verify_history(
    layout: AuditLayout, *, allow_unregistered: bool = False
) -> HistoryState:
    """Verify the manifest and every declared archive; never mutates anything.

    ``allow_unregistered`` is used only by the explicit registration recovery:
    it permits archives that the manifest does not declare yet to exist while
    the *declared* history is verified. The verifier and the writer always use
    the default, which reports such files as a recovery state.
    """
    manifest_path = layout.manifest
    try:
        manifest_exists = os.path.lexists(manifest_path)
    except OSError:  # pragma: no cover - lexists does not normally raise
        manifest_exists = False

    if not manifest_exists:
        directory = layout.archive_directory
        try:
            archive_present = directory.exists() and any(directory.iterdir())
        except OSError:
            raise UnavailableProblem(
                ARCHIVE_UNREADABLE, "the archive directory could not be listed"
            ) from None
        if archive_present and not allow_unregistered:
            raise RecoveryProblem(
                ARCHIVE_UNREGISTERED,
                "audit archives exist without a manifest; the installation "
                "cannot be verified and requires operator investigation",
            )
        return HistoryState()

    entries = read_manifest_entries(manifest_path)
    verification = _verify_manifest_chain(entries)

    offsite = set(verification.offsite_segment_ids)
    declared: set[str] = set()
    segments: list[SegmentSummary] = []
    anchor_only: list[str] = []
    audit_ids: set[str] = set()

    for entry in entries:
        if entry.entry_type is not ManifestEntryType.SEGMENT_SEALED:
            continue
        assert entry.segment_file is not None
        declared.add(Path(entry.segment_file).name)
        path = layout.archive_path_for_reference(entry.segment_file)
        file_missing = not os.path.lexists(path)

        if file_missing and entry.segment_id not in offsite:
            raise IntegrityProblem(
                ARCHIVE_MISSING,
                "a sealed segment's archive is missing and no retention "
                "decision explains it",
                segment_id=entry.segment_id,
            )

        if file_missing and entry.segment_id in offsite:
            # Owner-approved offsite retention: anchors only, no contents.
            anchor_only.append(entry.segment_id)
            segments.append(
                SegmentSummary(
                    segment_id=entry.segment_id,
                    record_count=entry.record_count,
                    first_global_sequence=entry.first_global_sequence,
                    last_global_sequence=entry.last_global_sequence,
                    first_previous_hash=entry.first_previous_hash,
                    last_record_hash=entry.last_record_hash,
                    first_timestamp="",
                    last_timestamp="",
                    anchor_only=True,
                )
            )
            continue

        summary = verify_declared_archive(
            entry, layout, known_audit_ids=audit_ids
        )
        audit_ids |= summary.audit_ids
        segments.append(summary)

    if not allow_unregistered:
        _unregistered_archives(layout, declared)

    return HistoryState(
        manifest_entries=tuple(entries),
        segments=tuple(segments),
        anchor_only_segment_ids=tuple(anchor_only),
        offsite_segment_ids=tuple(verification.offsite_segment_ids),
        last_manifest_hash=verification.last_entry_hash,
        audit_ids=frozenset(audit_ids),
    )


# ---------------------------------------------------------------------------
# Active journal
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActiveJournalState:
    """Verified state of the appendable journal."""

    exists: bool
    events: tuple[AuditEvent, ...]
    summary: SegmentSummary | None

    @property
    def is_empty(self) -> bool:
        return not self.events

    @property
    def last_sequence(self) -> int:
        return 0 if not self.events else self.events[-1].sequence

    @property
    def last_hash(self) -> str:
        return (
            GENESIS_PREVIOUS_HASH if not self.events else self.events[-1].record_hash
        )

    @property
    def segment_id(self) -> str | None:
        return None if not self.events else self.events[-1].segment_id

    @property
    def last_timestamp(self) -> str | None:
        return None if not self.events else self.events[-1].timestamp


def verify_active_journal(
    layout: AuditLayout, history: HistoryState
) -> ActiveJournalState:
    """Verify the active journal and its boundary with sealed history.

    A missing journal after a seal is the documented recovery state (design
    §9.4 row 3) and raises :class:`RecoveryProblem`; it is never created here.
    """
    path = layout.journal
    if not os.path.lexists(path):
        if history.seals:
            raise RecoveryProblem(
                ACTIVE_SEGMENT_MISSING,
                "a segment is sealed but the active journal is missing; the "
                "explicit recovery command must recreate it",
            )
        raise UnavailableProblem(
            JOURNAL_MISSING, "the audit journal does not exist"
        )

    raw = _read_snapshot(
        path,
        missing=JOURNAL_MISSING,
        not_regular=JOURNAL_NOT_REGULAR_FILE,
        unreadable=JOURNAL_UNREADABLE,
        changed=JOURNAL_CHANGED,
    )
    events = parse_event_lines(raw, vocabulary=_ACTIVE_VOCABULARY)
    if not events:
        return ActiveJournalState(exists=True, events=(), summary=None)

    sealed = set(history.sealed_segment_ids)
    for index, event in enumerate(events):
        if event.segment_id in sealed:
            raise IntegrityProblem(
                SEGMENT_INVALID,
                "the active journal reuses a sealed segment identifier",
                line=index + 1,
            )

    _verify_event_chain(
        events,
        vocabulary=_ACTIVE_VOCABULARY,
        expected_previous_hash=history.expected_previous_hash,
        expected_next_sequence=history.expected_next_sequence,
        segment_id=events[0].segment_id,
    )
    if events[0].segment_sequence != 1:
        raise IntegrityProblem(
            SEQUENCE_INVALID,
            "the active journal must start at segment sequence 1",
            line=1,
        )
    _check_audit_ids_and_timestamps(
        events,
        vocabulary=_ACTIVE_VOCABULARY,
        segment_id=events[0].segment_id,
        known_audit_ids=set(history.audit_ids),
    )

    summary = SegmentSummary(
        segment_id=events[0].segment_id,
        record_count=len(events),
        first_global_sequence=events[0].sequence,
        last_global_sequence=events[-1].sequence,
        first_previous_hash=events[0].previous_hash,
        last_record_hash=events[-1].record_hash,
        first_timestamp=events[0].timestamp,
        last_timestamp=events[-1].timestamp,
        audit_ids=frozenset(event.audit_id for event in events),
    )
    if len({event.segment_id for event in events}) != 1:
        raise IntegrityProblem(
            SEGMENT_INVALID,
            "the active journal must contain exactly one segment",
        )
    return ActiveJournalState(exists=True, events=tuple(events), summary=summary)
