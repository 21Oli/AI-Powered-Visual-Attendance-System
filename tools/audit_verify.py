#!/usr/bin/env python
"""Read-only audit journal verification CLI (P5.5.6).

Verifies the complete active journal ``data/audit/audit.jsonl`` using the
approved schema and chain primitives from :mod:`src.audit_schema`. It never
creates, appends, truncates, rewrites, repairs, re-locks or deletes anything:
the only filesystem operations are ``os.stat`` and a binary read-only open.

What is checked
===============
* the path exists and is a regular, readable file;
* UTF-8 decoding, JSON syntax per line, no empty lines and a final newline;
* every record passes the full :class:`~src.audit_schema.AuditEvent` schema;
* genesis predecessor, every predecessor link and every canonical SHA-256
  digest (the schema's own constant-time verification);
* global sequence from 1, contiguous; segment-local sequence from 1,
  contiguous; a segment id never reappears after a transition;
* ``segment_id`` uses the accepted ``YYYY-MM-NNN`` allocation form;
* timestamps use the fixed UTC microsecond form and are non-decreasing within
  each segment (the segment allocation month is never compared to the event
  month);
* no duplicate ``audit_id`` anywhere in the journal.

Concurrency
===========
No lock is taken: a shared/read lock could interfere with the writer's
non-blocking exclusive append lock, and this tool must not block audited
writes. The journal is read through one handle; its size, mtime and file
identity are compared before and after the read, and the path is re-statted
after the close. If anything changed, the run is not reported as PASS and no
snapshot consistency is claimed.

Exit codes (stable)
=================== ======================================================
0                    PASS - the active journal verified
1                    FAIL - integrity, corruption or schema failure
2                    FAIL - journal missing, not a regular file, unreadable,
                     or changed during verification
3                    usage error (invalid command line arguments)
4                    unexpected internal error (verification incomplete)
=================== ======================================================

Output never contains record bodies, metadata values, usernames, tokens,
cookies, biometric data, raw OS exception text or stack traces.

Manifest, archive, rotation and retention verification are P5.5.7 scope and
are deliberately absent. There is no repair mode of any kind.
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

from src.audit_schema import (  # noqa: E402 - after the sys.path bootstrap
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

# Stable failure categories. The CLI prints exactly one per failed run.
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
INVALID_ARGUMENTS: Final[str] = "INVALID_ARGUMENTS"
INTERNAL_ERROR: Final[str] = "INTERNAL_ERROR"

_EXPLANATIONS: Final[dict[str, str]] = {
    JOURNAL_MISSING: (
        "The audit journal does not exist; no journal was created."
    ),
    JOURNAL_NOT_REGULAR_FILE: (
        "The journal path is not a regular file."
    ),
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
    SCHEMA_INVALID: (
        "A record does not satisfy the approved audit event schema."
    ),
    HASH_MISMATCH: (
        "A record's stored digest does not match its canonical body."
    ),
    INVALID_GENESIS: (
        "The first record does not carry the expected genesis predecessor."
    ),
    BROKEN_PREDECESSOR: (
        "A record does not link to its immediate predecessor's hash."
    ),
    SEQUENCE_INVALID: (
        "The global or segment-local sequence is not contiguous."
    ),
    SEGMENT_INVALID: (
        "A segment boundary or identity rule is violated; a segment id may "
        "have repeated."
    ),
    DUPLICATE_AUDIT_ID: "The audit journal contains a duplicate audit id.",
    TIMESTAMP_REGRESSION: (
        "Timestamps are not non-decreasing within a segment."
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
    }
)

_NEXT_STEP = (
    "Stop audited writes and investigate using the approved operator procedure."
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
    ) -> None:
        super().__init__(category)
        self.category = category
        self.line = line


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
        raise VerificationFailure(
            TORN_TAIL, line=raw.count(b"\n") + 1
        )
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


def _verify_chain(events: list[AuditEvent]) -> None:
    """Run the schema chain verifier and map its typed errors."""
    try:
        verify_chain(events)
    except AuditPreviousHashMismatchError as exc:
        category = INVALID_GENESIS if exc.position == 0 else BROKEN_PREDECESSOR
        raise VerificationFailure(
            category, line=_line(exc.position)
        ) from None
    except AuditHashMismatchError as exc:
        raise VerificationFailure(
            HASH_MISMATCH, line=_line(exc.position)
        ) from None
    except AuditSequenceError as exc:
        raise VerificationFailure(
            SEQUENCE_INVALID, line=_line(exc.position)
        ) from None
    except AuditSegmentError as exc:
        raise VerificationFailure(
            SEGMENT_INVALID, line=_line(exc.position)
        ) from None
    except AuditSchemaError as exc:
        raise VerificationFailure(
            SCHEMA_INVALID, line=_line(exc.position)
        ) from None
    except AuditChainError:
        raise VerificationFailure(SCHEMA_INVALID) from None
    except AuditError:
        raise VerificationFailure(SCHEMA_INVALID) from None


def _line(position: int | None) -> int | None:
    return None if position is None else position + 1


def _verify_journal_invariants(events: list[AuditEvent]) -> None:
    """Duplicate ids and per-segment timestamp monotonicity."""
    seen_ids: set[str] = set()
    segment_id: str | None = None
    previous_timestamp: str | None = None
    for index, event in enumerate(events):
        if event.audit_id in seen_ids:
            raise VerificationFailure(
                DUPLICATE_AUDIT_ID, line=index + 1
            )
        seen_ids.add(event.audit_id)
        if event.segment_id != segment_id:
            segment_id = event.segment_id
            previous_timestamp = None
        if (
            previous_timestamp is not None
            and event.timestamp < previous_timestamp
        ):
            raise VerificationFailure(
                TIMESTAMP_REGRESSION, line=index + 1
            )
        previous_timestamp = event.timestamp


def verify_journal(path: Path | str = DEFAULT_JOURNAL_PATH) -> JournalSummary:
    """Verify ``path`` read-only; raise :class:`VerificationFailure` on any defect."""
    journal_path = Path(path)
    raw = _read_snapshot(journal_path)
    events = _parse_records(raw)
    _verify_chain(events)
    _verify_journal_invariants(events)

    if not events:
        return JournalSummary(
            path=journal_path,
            record_count=0,
            first_global_sequence=None,
            last_global_sequence=None,
            segment_ids=(),
            first_timestamp=None,
            last_timestamp=None,
            last_record_hash=None,
        )

    segment_ids = tuple(dict.fromkeys(event.segment_id for event in events))
    return JournalSummary(
        path=journal_path,
        record_count=len(events),
        first_global_sequence=events[0].sequence,
        last_global_sequence=events[-1].sequence,
        segment_ids=segment_ids,
        first_timestamp=events[0].timestamp,
        last_timestamp=events[-1].timestamp,
        last_record_hash=events[-1].record_hash,
    )


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
            "Read-only verification of the active audit JSONL journal. "
            "Never creates, repairs or modifies the journal."
        ),
    )
    parser.add_argument(
        "--journal",
        metavar="PATH",
        default=None,
        help=(
            "journal to verify (default: "
            "PROJECT_ROOT/data/audit/audit.jsonl)"
        ),
    )
    return parser


def _failure_report(category: str, line: int | None) -> None:
    print("Audit journal verification: FAIL")
    print(f"Category: {category}")
    if line is not None:
        print(f"Line: {line}")
    print(f"Detail: {_EXPLANATIONS[category]}")
    print(f"Next step: {_NEXT_STEP}")


def _pass_report(summary: JournalSummary) -> None:
    print("Audit journal verification: PASS")
    print(f"Journal: {summary.path.resolve()}")
    if summary.record_count == 0:
        print("Chain: valid empty chain (0 records)")
        print("Segments: 0")
        print("Read-only: the journal was not created, modified or repaired.")
        return
    print(f"Records verified: {summary.record_count}")
    print(
        "Global sequence: "
        f"{summary.first_global_sequence}..{summary.last_global_sequence}"
    )
    print(f"Segments: {len(summary.segment_ids)}")
    print(f"First segment: {summary.segment_ids[0]}")
    print(f"Last segment: {summary.segment_ids[-1]}")
    print(f"First timestamp: {summary.first_timestamp}")
    print(f"Last timestamp: {summary.last_timestamp}")
    print(f"Final record hash: {summary.last_record_hash}")
    print("Read-only: the journal was not modified or repaired.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError:
        _failure_report(INVALID_ARGUMENTS, None)
        return EXIT_USAGE_ERROR

    path = Path(args.journal) if args.journal else DEFAULT_JOURNAL_PATH
    try:
        summary = verify_journal(path)
    except VerificationFailure as failure:
        _failure_report(failure.category, failure.line)
        if failure.category in _UNAVAILABLE_CATEGORIES:
            return EXIT_UNAVAILABLE
        return EXIT_INTEGRITY_FAILURE
    except Exception:  # pragma: no cover - defensive, sanitized
        _failure_report(INTERNAL_ERROR, None)
        return EXIT_INTERNAL_ERROR

    _pass_report(summary)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
