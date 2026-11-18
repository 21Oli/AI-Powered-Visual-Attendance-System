"""Audit journal storage layout and segment-id allocation (P5.5.7).

This module is the single place that knows where audit artifacts live and how
the next ``YYYY-MM-NNN`` segment identifier is derived. It is pure: it
computes paths and labels, and never creates, opens, moves or deletes a file.

Layout (``docs/AUDIT_JOURNAL_DESIGN.md`` §9.1)::

    <audit dir>/
        audit.jsonl                     active segment (append-only)
        manifest.jsonl                  append-only segment index
        archive/audit-<first>-<last>.jsonl  sealed segments (immutable)
        .rotation.lock                  operator rotation / manifest lock

The audit directory is always the parent of the journal path, so
``--journal PATH`` and the default path describe the same installation and a
caller cannot accidentally read a manifest from a different directory.

Segment allocation
==================

An identifier is ``<UTC allocation month>-<ordinal>`` (design §16). The
ordinal space is ``001``-``999`` and identifiers are globally unique.

Allocation rules implemented here:

* a genuinely new installation (no sealed segment yet) opens its first segment
  in the **current UTC month** with ordinal ``001``;
* every later segment is the **successor of the last sealed segment** — the
  same allocation month and the next ordinal — because an empty active journal
  carries no identifier of its own, so the only restart-stable derivation is
  one computed from persisted state (design §9.4 rows 3-4 and §16.3). This is
  also what makes an interrupted rotation and a re-run agree;
* exhausting ``999`` raises :class:`SegmentAllocationError`; nothing is ever
  reused, renamed or wrapped around.

Ordering is never derived from the label: order comes from the event hash
chain, the global sequence and the manifest seal order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Iterable, Sequence

from src.audit_manifest import (
    ARCHIVE_DIRECTORY_NAME,
    ARCHIVE_FILENAME_PATTERN,
    MANIFEST_FILENAME,
    MAX_ARCHIVE_REFERENCE_LENGTH,
    SEGMENT_ID_PATTERN,
)

__all__ = [
    "JOURNAL_FILENAME",
    "MAX_SEGMENT_ORDINAL",
    "ROTATION_LOCK_FILENAME",
    "AuditLayout",
    "SegmentAllocationError",
    "allocation_month",
    "next_segment_id",
    "successor_segment_id",
]

#: Active journal file name inside the audit directory.
JOURNAL_FILENAME: Final[str] = "audit.jsonl"

#: Operator rotation / manifest append lock file inside the audit directory.
ROTATION_LOCK_FILENAME: Final[str] = ".rotation.lock"

#: Highest allocation ordinal a month may carry.
MAX_SEGMENT_ORDINAL: Final[int] = 999

_ORDINAL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^([0-9]{3})$")


class SegmentAllocationError(ValueError):
    """No further segment identifier can be allocated for this month.

    The message is fixed and contains no path and no data value.
    """


@dataclass(frozen=True, slots=True)
class AuditLayout:
    """The audit installation rooted at one directory."""

    directory: Path

    @classmethod
    def for_directory(cls, directory: Path | str) -> "AuditLayout":
        return cls(Path(directory))

    @classmethod
    def from_journal(cls, journal_path: Path | str) -> "AuditLayout":
        """The installation that owns ``journal_path`` (its parent directory)."""
        return cls(Path(journal_path).parent)

    # -- paths -------------------------------------------------------------

    @property
    def journal(self) -> Path:
        return self.directory / JOURNAL_FILENAME

    @property
    def manifest(self) -> Path:
        return self.directory / MANIFEST_FILENAME

    @property
    def archive_directory(self) -> Path:
        return self.directory / ARCHIVE_DIRECTORY_NAME

    @property
    def rotation_lock(self) -> Path:
        return self.directory / ROTATION_LOCK_FILENAME

    def archive_path(self, first_global_sequence: int, last_global_sequence: int) -> Path:
        """``archive/audit-<first>-<last>.jsonl`` for a segment range."""
        return self.archive_directory / self.archive_filename(
            first_global_sequence, last_global_sequence
        )

    @staticmethod
    def archive_filename(first_global_sequence: int, last_global_sequence: int) -> str:
        if first_global_sequence < 1 or last_global_sequence < first_global_sequence:
            raise SegmentAllocationError(
                "archive sequence bounds are not a valid range"
            )
        return f"audit-{first_global_sequence}-{last_global_sequence}.jsonl"

    @staticmethod
    def archive_reference(first_global_sequence: int, last_global_sequence: int) -> str:
        """The manifest ``segment_file`` value for a segment range."""
        name = AuditLayout.archive_filename(
            first_global_sequence, last_global_sequence
        )
        reference = f"{ARCHIVE_DIRECTORY_NAME}/{name}"
        if len(reference) > MAX_ARCHIVE_REFERENCE_LENGTH:
            raise SegmentAllocationError("archive reference is too long")
        if ARCHIVE_FILENAME_PATTERN.fullmatch(name) is None:
            raise SegmentAllocationError("archive file name is not approved")
        return reference

    def archive_path_for_reference(self, reference: str) -> Path:
        """Resolve an already-validated manifest ``segment_file`` value.

        The reference is expected to have been validated by
        :func:`src.audit_manifest.validate_archive_reference`; the result has
        exactly two components below the audit directory and can never escape
        it.
        """
        return self.directory / Path(reference)


# ---------------------------------------------------------------------------
# Segment identifier allocation
# ---------------------------------------------------------------------------


def allocation_month(moment: datetime) -> str:
    """Return the ``YYYY-MM`` UTC allocation month for ``moment``.

    A naive datetime is refused: a segment label must never depend on an
    ambiguous local time.
    """
    if not isinstance(moment, datetime):
        raise SegmentAllocationError("allocation instant must be a datetime")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise SegmentAllocationError("allocation instant must be UTC-aware")
    utc = moment.astimezone(UTC)
    return f"{utc.year:04d}-{utc.month:02d}"


def _parse_segment_id(segment_id: str) -> tuple[str, int]:
    if not isinstance(segment_id, str):
        raise SegmentAllocationError("segment identifier must be a str")
    match = SEGMENT_ID_PATTERN.fullmatch(segment_id)
    if match is None:
        raise SegmentAllocationError("segment identifier is not approved")
    ordinal_text = match.group(3)
    ordinal_match = _ORDINAL_PATTERN.fullmatch(ordinal_text)
    if ordinal_match is None:
        raise SegmentAllocationError("segment ordinal is not approved")
    ordinal = int(ordinal_text)
    if ordinal < 1:
        raise SegmentAllocationError("segment ordinal must be at least 001")
    return f"{match.group(1)}-{match.group(2)}", ordinal


def successor_segment_id(segment_id: str) -> str:
    """The next identifier after ``segment_id``: same month, ordinal + 1."""
    month, ordinal = _parse_segment_id(segment_id)
    if ordinal >= MAX_SEGMENT_ORDINAL:
        raise SegmentAllocationError(
            "segment ordinal 999 is exhausted for this allocation month; an "
            "operator decision is required"
        )
    return f"{month}-{ordinal + 1:03d}"


def next_segment_id(
    sealed_segment_ids: Sequence[str],
    *,
    moment: datetime,
    reserved: Iterable[str] = (),
) -> str:
    """Allocate the identifier of the next active segment.

    ``sealed_segment_ids`` must be in **manifest seal order**; the last element
    is the sealed predecessor. No seal means a new installation, which opens in
    the current UTC month with ordinal ``001``.

    ``reserved`` lists identifiers that must not be chosen again (for example a
    pending or already-present active segment id); a collision is refused
    instead of silently choosing another label.
    """
    sealed = set(sealed_segment_ids)
    if len(sealed) != len(sealed_segment_ids):
        raise SegmentAllocationError("segment identifiers repeat")
    used = sealed | set(reserved)

    if not sealed_segment_ids:
        candidate = f"{allocation_month(moment)}-001"
        if candidate in used:
            raise SegmentAllocationError("next segment identifier is already used")
        return candidate

    last = sealed_segment_ids[-1]
    candidate = successor_segment_id(last)
    if candidate in used:
        raise SegmentAllocationError("next segment identifier is already used")
    return candidate
