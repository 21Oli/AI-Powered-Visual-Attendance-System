#!/usr/bin/env python
"""Read-only audit installation verification CLI (P5.5.6/P5.5.7).

Verifies the active journal ``audit.jsonl``, the manifest ``manifest.jsonl`` and
every sealed archive under ``archive/`` using the approved schema, manifest and
chain primitives. It never creates, appends, truncates, rewrites, repairs,
renames, chmods, registers, deletes or locks anything: the only filesystem
operations are ``os.stat``/``os.open(O_RDONLY)`` reads.

What is checked
===============

Active journal (P5.5.6, unchanged)

* the path exists and is a regular readable file;
* UTF-8 decoding, JSON syntax per line, no empty lines and a final newline;
* every record passes the full :class:`~src.audit_schema.AuditEvent` schema;
* genesis predecessor, every predecessor link and every canonical SHA-256
  digest (the schema's own constant-time verification);
* global sequence contiguity; segment-local sequence from 1, contiguous; a
  segment id never reappears;
* ``segment_id`` uses the accepted ``YYYY-MM-NNN`` allocation form;
* timestamps use the fixed UTC microsecond form and are non-decreasing within
  each segment (the allocation month is never compared with event stamps);
* no duplicate ``audit_id`` in the file or against sealed history.

Manifest and archives (P5.5.7)

* the manifest is canonical JSONL, newline terminated, with a valid,
  contiguous hash chain from the ``"0"*64`` genesis;
* every ``segment_sealed`` entry's archive exists as a regular file (symlinks
  are refused), parses, and matches the declaration exactly: segment id,
  record count, global bounds, ``first_previous_hash`` and
  ``last_record_hash``;
* every record's digest, predecessor link and sequence is verified, the
  segment starts at ``segment_sequence`` 1 and every record belongs to the
  declared segment;
* the active journal continues the sealed chain (sequence and predecessor);
* archives that exist but are not declared, stray archive entries, duplicate
  audit ids across segments and invalid retention decisions are reported.

Three coverage modes (design §9.5)
==================================

1. **full-record-chain** — every available record of every segment.
2. **segment-manifest-continuity** — manifest chain plus each declared
   archive's contents matched against its declaration.
3. **anchor-only** — only when a segment's archive is absent after a valid
   owner-approved ``archived_offsite`` retention decision. Its anchors and
   their continuity are verified; its records were **not** content-verified and
   the run says so explicitly. Anchor-only coverage is never described as full
   historical verification.

Concurrency
===========

No lock is taken: a shared lock could interfere with the writer's non-blocking
exclusive append lock. Each file is read through one handle and its size, mtime
and identity are compared before/after the read and against a fresh ``stat`` of
the path; if anything changed the run is not reported as PASS. This is not a
filesystem snapshot and no cross-file atomicity is claimed.

Exit codes (stable)
=================== ======================================================
0                    PASS
1                    FAIL - integrity, corruption or schema failure
2                    FAIL - file missing/unreadable/changed where content is
                     required, i.e. no reliable verification was possible
3                    usage error
4                    unexpected internal error
5                    FAIL - RECOVERY REQUIRED: a documented crash state
                     (torn manifest tail, unregistered archive, sealed
                     segment with a missing active journal) needs an explicit
                     operator action
=================== ======================================================

Output never contains record bodies, metadata values, usernames, tokens,
cookies, biometric data, raw OS exception text or stack traces. There is no
repair mode of any kind.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Final, Sequence

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.audit_archive import (  # noqa: E402 - after the sys.path bootstrap
    ACTIVE_SEGMENT_MISSING,
    ARCHIVE_ANCHOR_MISMATCH,
    ARCHIVE_BOUNDS_MISMATCH,
    ARCHIVE_CHANGED,
    ARCHIVE_COUNT_MISMATCH,
    ARCHIVE_DUPLICATE_AUDIT_ID,
    ARCHIVE_HASH_MISMATCH,
    ARCHIVE_INVALID_GENESIS,
    ARCHIVE_INVALID_UTF8,
    ARCHIVE_MALFORMED_JSON,
    ARCHIVE_MISSING,
    ARCHIVE_NOT_REGULAR_FILE,
    ARCHIVE_SCHEMA_INVALID,
    ARCHIVE_SEGMENT_INVALID,
    ARCHIVE_SEQUENCE_INVALID,
    ARCHIVE_TORN_TAIL,
    ARCHIVE_UNEXPECTED_FILE,
    ARCHIVE_UNREADABLE,
    ARCHIVE_UNREGISTERED,
    BROKEN_PREDECESSOR,
    DUPLICATE_AUDIT_ID,
    EMPTY_LINE,
    HASH_MISMATCH,
    INVALID_GENESIS,
    INVALID_UTF8,
    JOURNAL_CHANGED,
    JOURNAL_MISSING,
    JOURNAL_NOT_REGULAR_FILE,
    JOURNAL_UNREADABLE,
    LayoutProblem,
    MALFORMED_JSON,
    MANIFEST_BROKEN_CHAIN,
    MANIFEST_CORRUPT,
    MANIFEST_DUPLICATE,
    MANIFEST_HASH_MISMATCH,
    MANIFEST_INTEGRITY,
    MANIFEST_REFERENCE_INVALID,
    MANIFEST_SCHEMA_INVALID,
    MANIFEST_SEQUENCE_INVALID,
    MANIFEST_TORN_TAIL,
    MANIFEST_UNREADABLE,
    SCHEMA_INVALID,
    SEGMENT_INVALID,
    SEQUENCE_INVALID,
    TIMESTAMP_REGRESSION,
    TORN_TAIL,
    HistoryState,
    verify_history,
)
from src.audit_layout import AuditLayout  # noqa: E402
from src.audit_schema import (  # noqa: E402
    GENESIS_PREVIOUS_HASH,
    AuditChainError,
    AuditError,
    AuditEvent,
    AuditHashMismatchError,
    AuditPreviousHashMismatchError,
    AuditSchemaError,
    AuditSegmentError,
    AuditSequenceError,
    verify_chain,
)

__all__ = [
    "DEFAULT_JOURNAL_PATH",
    "EXIT_INTERNAL_ERROR",
    "EXIT_INTEGRITY_FAILURE",
    "EXIT_OK",
    "EXIT_RECOVERY_REQUIRED",
    "EXIT_UNAVAILABLE",
    "EXIT_USAGE_ERROR",
    "main",
    "verify_journal",
]

#: The configured active journal, independent of the current working directory.
DEFAULT_JOURNAL_PATH: Final[Path] = (
    PROJECT_ROOT / "data" / "audit" / "audit.jsonl"
)

EXIT_OK: Final[int] = 0
EXIT_INTEGRITY_FAILURE: Final[int] = 1
EXIT_UNAVAILABLE: Final[int] = 2
EXIT_USAGE_ERROR: Final[int] = 3
EXIT_INTERNAL_ERROR: Final[int] = 4
EXIT_RECOVERY_REQUIRED: Final[int] = 5

INVALID_ARGUMENTS: Final[str] = "INVALID_ARGUMENTS"
INTERNAL_ERROR: Final[str] = "INTERNAL_ERROR"

_EXPLANATIONS: Final[dict[str, str]] = {
    JOURNAL_MISSING: "The audit journal does not exist; no journal was created.",
    JOURNAL_NOT_REGULAR_FILE: "The journal path is not a regular file.",
    JOURNAL_UNREADABLE: (
        "The audit journal could not be read (permission or I/O failure)."
    ),
    JOURNAL_CHANGED: (
        "The journal changed while it was being read; the result is not a "
        "consistent snapshot."
    ),
    INVALID_UTF8: "The audit journal is not valid UTF-8.",
    EMPTY_LINE: "The audit journal contains an empty line.",
    TORN_TAIL: "The audit journal ends with an incomplete record.",
    MALFORMED_JSON: "A journal line is not valid JSON.",
    SCHEMA_INVALID: "A record does not satisfy the approved audit event schema.",
    HASH_MISMATCH: "A record's stored digest does not match its canonical body.",
    INVALID_GENESIS: (
        "The first record does not carry the expected genesis predecessor."
    ),
    BROKEN_PREDECESSOR: (
        "A record does not link to its immediate predecessor's hash."
    ),
    SEQUENCE_INVALID: "The global or segment-local sequence is not contiguous.",
    SEGMENT_INVALID: (
        "A segment boundary or identity rule is violated; a segment id may "
        "have repeated or a sealed id was reused."
    ),
    DUPLICATE_AUDIT_ID: "The audit history contains a duplicate audit id.",
    TIMESTAMP_REGRESSION: "Timestamps are not non-decreasing within a segment.",
    MANIFEST_UNREADABLE: "The audit manifest could not be read.",
    MANIFEST_TORN_TAIL: (
        "The manifest ends with an incomplete entry. This is a documented "
        "crash state: do not truncate it; investigate and follow the recovery "
        "runbook."
    ),
    MANIFEST_CORRUPT: (
        "The manifest is not valid JSONL (malformed line or empty line)."
    ),
    MANIFEST_SCHEMA_INVALID: (
        "A manifest entry does not satisfy the approved manifest schema."
    ),
    MANIFEST_HASH_MISMATCH: (
        "A manifest entry hash does not match its canonical body."
    ),
    MANIFEST_BROKEN_CHAIN: (
        "The manifest's predecessor link or segment continuity is broken."
    ),
    MANIFEST_SEQUENCE_INVALID: (
        "The manifest sequence is not contiguous from 1."
    ),
    MANIFEST_DUPLICATE: (
        "A segment is sealed twice, an archive is declared twice, or a "
        "segment carries two retention decisions."
    ),
    MANIFEST_REFERENCE_INVALID: (
        "A retention decision does not correctly reference an existing seal."
    ),
    MANIFEST_INTEGRITY: "The manifest chain could not be verified.",
    ARCHIVE_MISSING: (
        "A sealed segment's archive is missing and no approved offsite "
        "retention decision explains it."
    ),
    ARCHIVE_NOT_REGULAR_FILE: (
        "An archive path is not a regular file (symlinks are refused)."
    ),
    ARCHIVE_UNREADABLE: "An archive or the archive directory could not be read.",
    ARCHIVE_CHANGED: "An archive changed while it was being read.",
    ARCHIVE_INVALID_UTF8: "An archive is not valid UTF-8.",
    ARCHIVE_MALFORMED_JSON: "An archive line is not valid JSON.",
    ARCHIVE_TORN_TAIL: "An archive ends with an incomplete record.",
    ARCHIVE_SCHEMA_INVALID: (
        "An archived record does not satisfy the approved event schema."
    ),
    ARCHIVE_HASH_MISMATCH: (
        "An archived record's digest does not match its canonical body."
    ),
    ARCHIVE_INVALID_GENESIS: (
        "The first archived record does not link to the expected predecessor."
    ),
    ARCHIVE_SEQUENCE_INVALID: (
        "An archive's sequence continuity is broken."
    ),
    ARCHIVE_SEGMENT_INVALID: (
        "An archive's record belongs to a different segment than declared."
    ),
    ARCHIVE_DUPLICATE_AUDIT_ID: (
        "The audit history contains a duplicate audit id."
    ),
    ARCHIVE_COUNT_MISMATCH: (
        "An archive's record count does not match the manifest."
    ),
    ARCHIVE_BOUNDS_MISMATCH: (
        "An archive's sequence bounds do not match the manifest."
    ),
    ARCHIVE_ANCHOR_MISMATCH: (
        "An archive's hash anchors do not match the manifest."
    ),
    ARCHIVE_UNREGISTERED: (
        "An archive file exists that the manifest does not declare, or "
        "archives exist without a manifest. This is a documented interrupted-"
        "rotation state: register it explicitly or investigate."
    ),
    ARCHIVE_UNEXPECTED_FILE: (
        "The archive directory contains an entry that is not an audit archive."
    ),
    ACTIVE_SEGMENT_MISSING: (
        "A segment is sealed but the active journal is missing. This is a "
        "documented crash state: recreate it with --create-active."
    ),
    INVALID_ARGUMENTS: "The command line arguments are not valid.",
    INTERNAL_ERROR: (
        "Verification could not be completed because of an internal error."
    ),
}

_UNAVAILABLE_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        JOURNAL_MISSING,
        JOURNAL_NOT_REGULAR_FILE,
        JOURNAL_UNREADABLE,
        JOURNAL_CHANGED,
        ARCHIVE_UNREADABLE,
        ARCHIVE_CHANGED,
        MANIFEST_UNREADABLE,
    }
)

_NEXT_STEP = (
    "Stop audited writes and investigate using the approved operator procedure."
)
_NEXT_STEP_RECOVERY = (
    "Stop audited writes and follow the recovery runbook in "
    "docs/AUDIT_JOURNAL_DESIGN.md §9.4; do not delete, truncate or "
    "re-initialize audit files."
)


# ---------------------------------------------------------------------------
# Read-only file seams (module level so tests can inject failures)
# ---------------------------------------------------------------------------


def _open_readonly(path: Path) -> IO[bytes]:
    """Open the journal for reading only; never creates the file."""
    return open(path, "rb")


#: Test seam: called after the bytes are read and before the change check.
_AFTER_READ: Callable[[], None] | None = None


# ---------------------------------------------------------------------------
# Verification model
# ---------------------------------------------------------------------------


class VerificationFailure(Exception):
    """A sanitized verification failure; never rendered from schema messages."""

    def __init__(
        self,
        category: str,
        *,
        line: int | None = None,
        segment_id: str | None = None,
        recovery: bool = False,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.line = line
        self.segment_id = segment_id
        self.recovery = recovery


@dataclass(frozen=True)
class JournalSummary:
    """What a successful verification established (no record contents)."""

    path: Path
    record_count: int
    first_global_sequence: int | None
    last_global_sequence: int | None
    segment_ids: tuple[str, ...]
    first_timestamp: str | None
    last_timestamp: str | None
    last_record_hash: str | None
    manifest_entries: int = 0
    archived_segments: int = 0
    active_segment_id: str | None = None
    final_manifest_hash: str | None = None
    anchor_only_segment_ids: tuple[str, ...] = ()
    content_verified_segments: int = 0
    coverage: tuple[str, ...] = ()


def _read_snapshot(path: Path) -> bytes:
    """Read the whole journal once and refuse a changed file."""
    try:
        initial = os.stat(path)
    except FileNotFoundError:
        raise VerificationFailure(JOURNAL_MISSING) from None
    except OSError:
        raise VerificationFailure(JOURNAL_UNREADABLE) from None
    if not stat.S_ISREG(initial.st_mode):
        raise VerificationFailure(JOURNAL_NOT_REGULAR_FILE)

    try:
        handle = _open_readonly(path)
    except FileNotFoundError:
        raise VerificationFailure(JOURNAL_MISSING) from None
    except OSError:
        raise VerificationFailure(JOURNAL_UNREADABLE) from None

    try:
        try:
            before = os.fstat(handle.fileno())
            raw = handle.read()
        except OSError:
            raise VerificationFailure(JOURNAL_UNREADABLE) from None
        if _AFTER_READ is not None:
            _AFTER_READ()
        try:
            after = os.fstat(handle.fileno())
        except OSError:
            raise VerificationFailure(JOURNAL_UNREADABLE) from None
    finally:
        try:
            handle.close()
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
        raise VerificationFailure(JOURNAL_CHANGED)

    try:
        final = os.stat(path)
    except OSError:
        raise VerificationFailure(JOURNAL_CHANGED) from None
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
        raise VerificationFailure(JOURNAL_CHANGED)
    return raw


def _parse_records(raw: bytes) -> list[AuditEvent]:
    """Parse JSONL with exactly the writer's structural rules."""
    if raw and not raw.endswith(b"\n"):
        raise VerificationFailure(TORN_TAIL, line=raw.count(b"\n") + 1)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise VerificationFailure(INVALID_UTF8) from None

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    events: list[AuditEvent] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            raise VerificationFailure(EMPTY_LINE, line=index)
        try:
            payload = json.loads(line)
        except ValueError:
            raise VerificationFailure(MALFORMED_JSON, line=index) from None
        try:
            events.append(AuditEvent.from_dict(payload))
        except AuditError:
            raise VerificationFailure(SCHEMA_INVALID, line=index) from None
    return events


def _verify_chain(
    events: list[AuditEvent],
    *,
    expected_previous_hash: str = GENESIS_PREVIOUS_HASH,
    expected_next_sequence: int = 1,
) -> None:
    """Run the schema chain verifier and map its typed errors."""
    try:
        verify_chain(
            events,
            expected_previous_hash=expected_previous_hash,
            expected_next_sequence=expected_next_sequence,
        )
    except AuditPreviousHashMismatchError as exc:
        category = (
            INVALID_GENESIS
            if exc.position == 0 and expected_next_sequence == 1
            else BROKEN_PREDECESSOR
        )
        raise VerificationFailure(category, line=_line(exc.position)) from None
    except AuditHashMismatchError as exc:
        raise VerificationFailure(HASH_MISMATCH, line=_line(exc.position)) from None
    except AuditSequenceError as exc:
        raise VerificationFailure(
            SEQUENCE_INVALID, line=_line(exc.position)
        ) from None
    except AuditSegmentError as exc:
        raise VerificationFailure(SEGMENT_INVALID, line=_line(exc.position)) from None
    except AuditSchemaError as exc:
        raise VerificationFailure(SCHEMA_INVALID, line=_line(exc.position)) from None
    except AuditChainError:
        raise VerificationFailure(SCHEMA_INVALID) from None
    except AuditError:
        raise VerificationFailure(SCHEMA_INVALID) from None


def _line(position: int | None) -> int | None:
    return None if position is None else position + 1


def _verify_journal_invariants(
    events: list[AuditEvent],
    *,
    sealed_segment_ids: Sequence[str] = (),
    known_audit_ids: frozenset[str] = frozenset(),
) -> None:
    """Sealed-id reuse, duplicate ids and per-segment timestamp monotonicity."""
    sealed = set(sealed_segment_ids)
    seen_ids: set[str] = set(known_audit_ids)
    segment_id: str | None = None
    previous_timestamp: str | None = None
    for index, event in enumerate(events):
        if event.segment_id in sealed:
            raise VerificationFailure(
                SEGMENT_INVALID,
                line=index + 1,
                segment_id=event.segment_id,
            )
        if event.audit_id in seen_ids:
            raise VerificationFailure(DUPLICATE_AUDIT_ID, line=index + 1)
        seen_ids.add(event.audit_id)
        if event.segment_id != segment_id:
            segment_id = event.segment_id
            previous_timestamp = None
        if (
            previous_timestamp is not None
            and event.timestamp < previous_timestamp
        ):
            raise VerificationFailure(TIMESTAMP_REGRESSION, line=index + 1)
        previous_timestamp = event.timestamp


def _history_failure(problem: LayoutProblem) -> VerificationFailure:
    return VerificationFailure(
        problem.category,
        line=problem.line,
        segment_id=problem.segment_id,
        recovery=problem.recovery,
    )


def verify_journal(path: Path | str = DEFAULT_JOURNAL_PATH) -> JournalSummary:
    """Verify the installation that owns ``path`` read-only.

    Raises :class:`VerificationFailure` on any defect; never creates, repairs
    or modifies anything.
    """
    journal_path = Path(path)
    layout = AuditLayout.from_journal(journal_path)

    try:
        history: HistoryState = verify_history(layout)
    except LayoutProblem as problem:
        raise _history_failure(problem) from None

    if not os.path.lexists(journal_path):
        if history.seals:
            raise VerificationFailure(
                ACTIVE_SEGMENT_MISSING, recovery=True
            )
        raise VerificationFailure(JOURNAL_MISSING)

    raw = _read_snapshot(journal_path)
    events = _parse_records(raw)
    _verify_chain(
        events,
        expected_previous_hash=history.expected_previous_hash,
        expected_next_sequence=history.expected_next_sequence,
    )
    _verify_journal_invariants(
        events,
        sealed_segment_ids=history.sealed_segment_ids,
        known_audit_ids=history.audit_ids,
    )

    archived_ids = tuple(
        summary.segment_id for summary in history.segments
    )
    active_ids = tuple(dict.fromkeys(event.segment_id for event in events))
    segment_ids = archived_ids + active_ids

    available = tuple(
        summary for summary in history.segments if not summary.anchor_only
    )

    if not events:
        return JournalSummary(
            path=journal_path,
            record_count=history.total_records,
            first_global_sequence=(
                history.segments[0].first_global_sequence
                if history.segments
                else None
            ),
            last_global_sequence=(
                history.segments[-1].last_global_sequence
                if history.segments
                else None
            ),
            segment_ids=segment_ids,
            first_timestamp=(
                available[0].first_timestamp if available else None
            ),
            last_timestamp=(
                available[-1].last_timestamp if available else None
            ),
            last_record_hash=(
                history.segments[-1].last_record_hash
                if history.segments
                else None
            ),
            manifest_entries=len(history.manifest_entries),
            archived_segments=len(history.segments),
            active_segment_id=None,
            final_manifest_hash=history.last_manifest_hash,
            anchor_only_segment_ids=history.anchor_only_segment_ids,
            content_verified_segments=len(history.segments)
            - len(history.anchor_only_segment_ids),
            coverage=_coverage(history, has_active=False),
        )

    total = history.total_records + len(events)
    first_sequence = (
        history.segments[0].first_global_sequence
        if history.segments
        else events[0].sequence
    )
    first_timestamp = (
        available[0].first_timestamp if available else events[0].timestamp
    )
    return JournalSummary(
        path=journal_path,
        record_count=total,
        first_global_sequence=first_sequence,
        last_global_sequence=events[-1].sequence,
        segment_ids=segment_ids,
        first_timestamp=first_timestamp,
        last_timestamp=events[-1].timestamp,
        last_record_hash=events[-1].record_hash,
        manifest_entries=len(history.manifest_entries),
        archived_segments=len(history.segments),
        active_segment_id=events[-1].segment_id,
        final_manifest_hash=history.last_manifest_hash,
        anchor_only_segment_ids=history.anchor_only_segment_ids,
        content_verified_segments=len(history.segments)
        - len(history.anchor_only_segment_ids)
        + len(active_ids),
        coverage=_coverage(history, has_active=True),
    )


def _coverage(history: HistoryState, *, has_active: bool) -> tuple[str, ...]:
    modes: list[str] = []
    if history.anchor_only:
        modes.append("anchor-only")
    else:
        modes.append("full-record-chain")
    if history.manifest_entries:
        modes.append("segment-manifest-continuity")
    if not history.manifest_entries and not has_active:
        modes.append("empty-installation")
    return tuple(modes)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _UsageError(Exception):
    """Raised instead of argparse's default exit(2)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # noqa: ARG002 - fixed output
        raise _UsageError()


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m tools.audit_verify",
        description=(
            "Read-only verification of the audit JSONL installation: active "
            "journal, manifest and sealed archives. Never creates, repairs or "
            "modifies anything."
        ),
    )
    parser.add_argument(
        "--journal",
        metavar="PATH",
        default=None,
        help=(
            "active journal to verify; its directory is the audit installation "
            "(default: PROJECT_ROOT/data/audit/audit.jsonl)"
        ),
    )
    return parser


def _failure_report(failure: VerificationFailure) -> None:
    print("Audit journal verification: FAIL")
    print(f"Category: {failure.category}")
    if failure.segment_id is not None:
        print(f"Segment: {failure.segment_id}")
    if failure.line is not None:
        print(f"Line: {failure.line}")
    detail = _EXPLANATIONS.get(failure.category, _EXPLANATIONS[INTERNAL_ERROR])
    print(f"Detail: {detail}")
    next_step = _NEXT_STEP_RECOVERY if failure.recovery else _NEXT_STEP
    print(f"Next step: {next_step}")


def _pass_report(summary: JournalSummary) -> None:
    print("Audit journal verification: PASS")
    print(f"Journal: {summary.path.resolve()}")
    print(f"Manifest entries: {summary.manifest_entries}")
    print(f"Archived segments: {summary.archived_segments}")
    print(
        "Active segment: "
        + (summary.active_segment_id or "none (empty active journal)")
    )
    if summary.record_count == 0:
        print("Chain: valid empty chain (0 records)")
        print("Segments: 0")
    else:
        print(f"Records verified: {summary.record_count}")
        print(
            "Global sequence: "
            f"{summary.first_global_sequence}..{summary.last_global_sequence}"
        )
        print(f"Segments: {len(summary.segment_ids)}")
        print(f"First segment: {summary.segment_ids[0]}")
        print(f"Last segment: {summary.segment_ids[-1]}")
        if summary.first_timestamp:
            print(f"First timestamp: {summary.first_timestamp}")
        if summary.last_timestamp:
            print(f"Last timestamp: {summary.last_timestamp}")
        if summary.last_record_hash:
            print(f"Final record hash: {summary.last_record_hash}")
    print(f"Final manifest hash: {summary.final_manifest_hash or 'none'}")
    print("Coverage modes: " + ", ".join(summary.coverage))
    print(
        "Anchor-only segments: "
        + (", ".join(summary.anchor_only_segment_ids) or "(none)")
    )
    print(
        "Segments not content-verified: "
        + (", ".join(summary.anchor_only_segment_ids) or "(none)")
    )
    if summary.anchor_only_segment_ids:
        print(
            "Coverage note: anchor-only segments were verified through their "
            "manifest anchors and chain continuity only; their records were "
            "NOT content-verified and this run is not a full historical "
            "verification."
        )
    print("Read-only: no journal, manifest or archive was modified or repaired.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError:
        _failure_report(VerificationFailure(INVALID_ARGUMENTS))
        return EXIT_USAGE_ERROR

    path = Path(args.journal) if args.journal else DEFAULT_JOURNAL_PATH
    try:
        summary = verify_journal(path)
    except VerificationFailure as failure:
        _failure_report(failure)
        if failure.category in _UNAVAILABLE_CATEGORIES:
            return EXIT_UNAVAILABLE
        if failure.recovery:
            return EXIT_RECOVERY_REQUIRED
        return EXIT_INTEGRITY_FAILURE
    except Exception:  # pragma: no cover - defensive, sanitized
        _failure_report(VerificationFailure(INTERNAL_ERROR))
        return EXIT_INTERNAL_ERROR

    _pass_report(summary)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
