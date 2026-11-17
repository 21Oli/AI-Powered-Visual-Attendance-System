"""Audit manifest schema, canonical hashing and chain verification (P5.5.7.2).

The manifest is the append-only index of sealed audit journal segments
(``docs/AUDIT_JOURNAL_DESIGN.md`` §9.1-§9.2). It is a **separate** record
format living in its own chain: manifest entries are never written to
``audit.jsonl`` and audit events are never written to the manifest.

What this module does
=====================
* Defines the immutable :class:`ManifestEntry` value with the exact sixteen
  approved fields and strict validation (unknown, missing, mistyped,
  oversized or contradictory values are refused).
* Serializes entries with the existing canonical JSON implementation from
  :mod:`src.audit_schema` (sorted keys, ``","``/``":"`` separators, no ASCII
  escaping, no NaN) and hashes the body without ``entry_hash`` with SHA-256.
* Verifies the manifest's own hash chain (:func:`verify_manifest`): contiguous
  ``manifest_sequence``, ``previous_manifest_hash`` links from the approved
  genesis, canonical ``entry_hash`` values, unique sealed segments, unique
  archive references and the audit-chain continuity anchors.
* Validates archive references (:func:`validate_archive_reference`) as pure
  relative paths inside ``archive/`` with the approved
  ``audit-<first>-<last>.jsonl`` name.

What this module deliberately does not do
=========================================
There is **no filesystem access at all**: no manifest or archive is read,
created, moved, renamed, truncated, repaired, deleted or opened, and the
active journal is never touched. Rotation, sealing, archive movement,
retention deletion, recovery and CLI integration remain P5.5.7.3+.

Proving versus assuming
=======================
A verified manifest proves only its own chain and its statements about
segments. It never proves that the bytes of a sealed segment or archive are
present or correct; that requires re-reading and re-verifying the segment
contents (P5.5.7.3+). Manifest ordering — never lexical ordering of segment
labels — is the seal order.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Final, Iterable, Mapping

from src.audit_schema import (
    GENESIS_PREVIOUS_HASH,
    AuditSchemaError,
    AuditTimestampError,
    canonical_json_bytes,
    canonical_json_text,
    format_timestamp,
    parse_timestamp,
)

__all__ = [
    "ARCHIVE_DIRECTORY_NAME",
    "ARCHIVE_FILENAME_PATTERN",
    "GENESIS_MANIFEST_HASH",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "ManifestArchivePathError",
    "ManifestChainError",
    "ManifestContinuityError",
    "ManifestDisposition",
    "ManifestDuplicateError",
    "ManifestEntry",
    "ManifestEntryType",
    "ManifestError",
    "ManifestHashFormatError",
    "ManifestHashMismatchError",
    "ManifestReferenceError",
    "ManifestSchemaError",
    "ManifestSequenceError",
    "ManifestTimestampError",
    "ManifestVerification",
    "compute_entry_hash",
    "validate_archive_reference",
    "verify_entry_hash",
    "verify_manifest",
]

#: The only accepted manifest format version.
MANIFEST_SCHEMA_VERSION: Final[int] = 1

#: Manifest file name inside the audit directory (created by P5.5.7.3).
MANIFEST_FILENAME: Final[str] = "manifest.jsonl"

#: Archive directory name inside the audit directory.
ARCHIVE_DIRECTORY_NAME: Final[str] = "archive"

#: The manifest chain starts at the approved all-zero predecessor, exactly
#: like the audit event chain.
GENESIS_MANIFEST_HASH: Final[str] = GENESIS_PREVIOUS_HASH

#: Approved segment identity: a UTC allocation month plus a 3-digit ordinal.
SEGMENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^([0-9]{4})-(0[1-9]|1[0-2])-([0-9]{3})$"
)

#: Approved archive file name: ``audit-<first_global>-<last_global>.jsonl``.
#: No leading zeros and no other extension, so one segment range has exactly
#: one legal name.
ARCHIVE_FILENAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^audit-([1-9][0-9]*)-([1-9][0-9]*)\.jsonl$"
)

_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]:")
_TOOL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_TOOL_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$"
)

#: Maximum length of the ``segment_file`` relative path.
MAX_ARCHIVE_REFERENCE_LENGTH: Final[int] = 255

_BODY_FIELDS: Final[tuple[str, ...]] = (
    "manifest_version",
    "manifest_sequence",
    "entry_type",
    "segment_id",
    "disposition",
    "segment_file",
    "first_global_sequence",
    "last_global_sequence",
    "record_count",
    "first_previous_hash",
    "last_record_hash",
    "sealed_at",
    "tool",
    "tool_version",
    "previous_manifest_hash",
)
_ENTRY_FIELDS: Final[tuple[str, ...]] = _BODY_FIELDS + ("entry_hash",)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ManifestError(ValueError):
    """Base class for every manifest failure.

    ``position`` is the zero-based index of the offending entry in the
    sequence being verified; it is ``None`` for single-value validation.
    Messages are fixed and contain field names only — never field values.
    """

    def __init__(
        self,
        message: str,
        *,
        position: int | None = None,
        manifest_sequence: int | None = None,
    ) -> None:
        super().__init__(message)
        self.position = position
        self.manifest_sequence = manifest_sequence


class ManifestSchemaError(ManifestError):
    """A manifest entry violates the schema."""


class ManifestHashFormatError(ManifestSchemaError):
    """A hash field is not a lowercase 64-character hexadecimal digest."""


class ManifestTimestampError(ManifestSchemaError):
    """``sealed_at`` is not the approved fixed UTC timestamp form."""


class ManifestArchivePathError(ManifestSchemaError):
    """``segment_file`` is not an approved archive reference."""


class ManifestHashMismatchError(ManifestError):
    """``entry_hash`` does not match the canonical entry body."""


class ManifestChainError(ManifestError):
    """The manifest chain is not verifiable."""


class ManifestSequenceError(ManifestChainError):
    """``manifest_sequence`` is not contiguous from 1."""


class ManifestContinuityError(ManifestChainError):
    """A predecessor link or a segment continuity anchor is broken."""


class ManifestDuplicateError(ManifestChainError):
    """A sealed segment, archive reference or retention decision repeats."""


class ManifestReferenceError(ManifestChainError):
    """A retention decision does not correctly reference its sealed segment."""


# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------


class ManifestEntryType(str, Enum):
    """The approved manifest entry types. There is no other type, ever."""

    SEGMENT_SEALED = "segment_sealed"
    RETENTION_DECISION = "retention_decision"


class ManifestDisposition(str, Enum):
    """Where a sealed segment lives."""

    ARCHIVED = "archived"
    ARCHIVED_OFFSITE = "archived_offsite"


# ---------------------------------------------------------------------------
# Field validators
# ---------------------------------------------------------------------------


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestSchemaError(f"{name} must be an int")
    return value


def _require_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ManifestSchemaError(f"{name} must be a str")
    return value


def _require_hash(value: object, name: str) -> str:
    text = _require_str(value, name)
    if _HASH_PATTERN.fullmatch(text) is None:
        raise ManifestHashFormatError(
            f"{name} must be a lowercase 64-character hexadecimal SHA-256 "
            "digest"
        )
    return text


def _require_segment_id(value: object) -> str:
    text = _require_str(value, "segment_id")
    match = SEGMENT_ID_PATTERN.fullmatch(text)
    if match is None:
        raise ManifestSchemaError(
            "segment_id must use YYYY-MM-NNN: a UTC allocation month and a "
            "three-digit ordinal"
        )
    if match.group(3) == "000":
        raise ManifestSchemaError(
            "segment_id ordinal must be between 001 and 999"
        )
    return text


def _require_timestamp(value: object) -> str:
    try:
        return format_timestamp(parse_timestamp(value))
    except (AuditTimestampError, AuditSchemaError):
        raise ManifestTimestampError(
            "sealed_at must use YYYY-MM-DDTHH:MM:SS.ffffffZ (UTC)"
        ) from None


def _require_member(
    value: object, enum_type: type[Enum], name: str
) -> object:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError:
            pass
    raise ManifestSchemaError(f"{name} is not in the approved vocabulary")


def _entry_type(value: object) -> ManifestEntryType:
    return _require_member(  # type: ignore[return-value]
        value, ManifestEntryType, "entry_type"
    )


def _disposition(value: object) -> ManifestDisposition:
    return _require_member(  # type: ignore[return-value]
        value, ManifestDisposition, "disposition"
    )


def _require_tool(value: object) -> str:
    text = _require_str(value, "tool")
    if _TOOL_PATTERN.fullmatch(text) is None:
        raise ManifestSchemaError(
            "tool must be a lowercase stable tool name of at most 64 "
            "characters"
        )
    return text


def _require_tool_version(value: object) -> str:
    text = _require_str(value, "tool_version")
    if _TOOL_VERSION_PATTERN.fullmatch(text) is None:
        raise ManifestSchemaError(
            "tool_version must be 1-32 characters of letters, digits or "
            "'.', '_', '+', '-'"
        )
    return text


def validate_archive_reference(
    value: object,
    *,
    first_global_sequence: int | None = None,
    last_global_sequence: int | None = None,
) -> str:
    """Return ``value`` if it is an approved archive reference.

    An approved reference is exactly ``archive/audit-<first>-<last>.jsonl``
    (design §9.1/§9.3): two POSIX path components, a fixed ``archive``
    directory, no absolute path, no drive or UNC prefix, no backslash
    separator, no ``.``/``..`` component, no empty component, a lowercase
    ``.jsonl`` extension and decimal sequence bounds without leading zeros.

    When the caller supplies the entry's own sequence bounds, the file name
    must equal them; a name that disagrees with the entry is a contradiction
    and is refused. Nothing is opened, resolved or created.
    """
    if not isinstance(value, str):
        raise ManifestArchivePathError("segment_file must be a str or null")
    if not value:
        raise ManifestArchivePathError("segment_file must be non-empty")
    if len(value) > MAX_ARCHIVE_REFERENCE_LENGTH:
        raise ManifestArchivePathError(
            "segment_file must be at most "
            f"{MAX_ARCHIVE_REFERENCE_LENGTH} characters"
        )
    if _CONTROL_PATTERN.search(value):
        raise ManifestArchivePathError(
            "segment_file must not contain control characters"
        )
    if value.startswith("/") or value.startswith("\\"):
        raise ManifestArchivePathError(
            "segment_file must not be an absolute path"
        )
    if _DRIVE_PATTERN.match(value):
        raise ManifestArchivePathError(
            "segment_file must not carry a drive or scheme prefix"
        )
    if ":" in value:
        raise ManifestArchivePathError(
            "segment_file must not contain ':'"
        )
    if "\\" in value:
        raise ManifestArchivePathError(
            "segment_file must use '/' as the only path separator"
        )

    parts = value.split("/")
    if len(parts) != 2:
        raise ManifestArchivePathError(
            "segment_file must be exactly archive/<name>"
        )
    directory, filename = parts
    if directory != ARCHIVE_DIRECTORY_NAME:
        raise ManifestArchivePathError(
            "segment_file must live in the approved archive directory"
        )
    if filename in {"", ".", ".."}:
        raise ManifestArchivePathError(
            "segment_file must name an archive file"
        )
    match = ARCHIVE_FILENAME_PATTERN.fullmatch(filename)
    if match is None:
        raise ManifestArchivePathError(
            "segment_file must match audit-<first>-<last>.jsonl with decimal "
            "sequence bounds"
        )
    first = int(match.group(1))
    last = int(match.group(2))
    if first < 1:
        raise ManifestArchivePathError(
            "segment_file sequence bounds must be positive"
        )
    if last < first:
        raise ManifestArchivePathError(
            "segment_file sequence range is inverted"
        )
    if first_global_sequence is not None and first != first_global_sequence:
        raise ManifestArchivePathError(
            "segment_file first sequence does not match "
            "first_global_sequence"
        )
    if last_global_sequence is not None and last != last_global_sequence:
        raise ManifestArchivePathError(
            "segment_file last sequence does not match last_global_sequence"
        )
    return value


# ---------------------------------------------------------------------------
# Canonical serialization and hashing
# ---------------------------------------------------------------------------


def _require_string_keys(value: Mapping[object, object]) -> None:
    for key in value:
        if not isinstance(key, str):
            raise ManifestSchemaError(
                "manifest entry keys must be strings"
            )


def _hash_body(body: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def compute_entry_hash(
    source: "ManifestEntry | Mapping[str, object]",
) -> str:
    """Return the canonical SHA-256 digest of a manifest entry body.

    The hash input is every field **except** ``entry_hash``, so a claimed
    digest never influences its own verification and key order is irrelevant.
    A mapping is validated as a complete entry first.
    """
    if isinstance(source, ManifestEntry):
        body = source.as_body()
    elif isinstance(source, Mapping):
        candidate = dict(source)
        candidate.setdefault("entry_hash", GENESIS_MANIFEST_HASH)
        body = ManifestEntry.from_dict(candidate).as_body()
    else:
        raise ManifestSchemaError(
            "entry must be a ManifestEntry or a mapping"
        )
    body.pop("entry_hash", None)
    return _hash_body(body)


def verify_entry_hash(
    source: "ManifestEntry | Mapping[str, object]",
    *,
    position: int | None = None,
) -> str:
    """Raise :class:`ManifestHashMismatchError` unless the digest matches.

    Returns the recomputed digest so chain verification can advance the
    expected predecessor.
    """
    entry = (
        source
        if isinstance(source, ManifestEntry)
        else _coerce_entry(source, position)
    )
    calculated = compute_entry_hash(entry)
    if not hmac.compare_digest(calculated, entry.entry_hash):
        raise ManifestHashMismatchError(
            "entry_hash does not match the canonical manifest entry body",
            position=position,
            manifest_sequence=entry.manifest_sequence,
        )
    return calculated


# ---------------------------------------------------------------------------
# Entry record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One immutable manifest record (design §9.2).

    Construction and :meth:`from_dict` validate every field; :meth:`create`
    also computes ``entry_hash``. The segment-local sequence bounds are not
    stored — a sealed segment always starts at ``segment_sequence`` 1 and its
    last value is ``record_count`` — and are exposed as derived properties so
    the record keeps exactly the approved field set.
    """

    manifest_version: int
    manifest_sequence: int
    entry_type: ManifestEntryType
    segment_id: str
    disposition: ManifestDisposition
    segment_file: str | None
    first_global_sequence: int
    last_global_sequence: int
    record_count: int
    first_previous_hash: str
    last_record_hash: str
    sealed_at: str
    tool: str
    tool_version: str
    previous_manifest_hash: str
    entry_hash: str

    def __post_init__(self) -> None:
        # -- version and manifest sequence ----------------------------------
        version = _require_int(self.manifest_version, "manifest_version")
        if version != MANIFEST_SCHEMA_VERSION:
            raise ManifestSchemaError(
                f"unsupported manifest_version; only "
                f"{MANIFEST_SCHEMA_VERSION} is accepted"
            )
        sequence = _require_int(self.manifest_sequence, "manifest_sequence")
        if sequence < 1:
            raise ManifestSchemaError(
                "manifest_sequence must be a positive integer"
            )

        # -- vocabulary ------------------------------------------------------
        entry_type = _entry_type(self.entry_type)
        disposition = _disposition(self.disposition)
        object.__setattr__(self, "entry_type", entry_type)
        object.__setattr__(self, "disposition", disposition)
        if entry_type is ManifestEntryType.SEGMENT_SEALED:
            if disposition is not ManifestDisposition.ARCHIVED:
                raise ManifestSchemaError(
                    "a segment_sealed entry must have disposition 'archived'"
                )
        elif disposition is not ManifestDisposition.ARCHIVED_OFFSITE:
            raise ManifestSchemaError(
                "a retention_decision entry must have disposition "
                "'archived_offsite'"
            )

        # -- segment identity ------------------------------------------------
        object.__setattr__(
            self, "segment_id", _require_segment_id(self.segment_id)
        )

        # -- global sequence bounds and record count -------------------------
        first = _require_int(
            self.first_global_sequence, "first_global_sequence"
        )
        last = _require_int(self.last_global_sequence, "last_global_sequence")
        count = _require_int(self.record_count, "record_count")
        if first < 1:
            raise ManifestSchemaError(
                "first_global_sequence must be a positive integer"
            )
        if last < first:
            raise ManifestSchemaError(
                "last_global_sequence must not precede first_global_sequence"
            )
        if count < 1:
            raise ManifestSchemaError(
                "record_count must be a positive integer; a sealed segment "
                "contains at least one record"
            )
        if count != last - first + 1:
            raise ManifestSchemaError(
                "record_count must equal last_global_sequence - "
                "first_global_sequence + 1"
            )

        # -- archive reference ----------------------------------------------
        if entry_type is ManifestEntryType.SEGMENT_SEALED:
            if self.segment_file is None:
                raise ManifestArchivePathError(
                    "segment_file is required for a segment_sealed entry"
                )
            object.__setattr__(
                self,
                "segment_file",
                validate_archive_reference(
                    self.segment_file,
                    first_global_sequence=first,
                    last_global_sequence=last,
                ),
            )
        elif self.segment_file is not None:
            raise ManifestArchivePathError(
                "segment_file must be null for a retention_decision entry"
            )

        # -- hashes and timestamp -------------------------------------------
        object.__setattr__(
            self,
            "first_previous_hash",
            _require_hash(self.first_previous_hash, "first_previous_hash"),
        )
        object.__setattr__(
            self,
            "last_record_hash",
            _require_hash(self.last_record_hash, "last_record_hash"),
        )
        object.__setattr__(
            self,
            "previous_manifest_hash",
            _require_hash(
                self.previous_manifest_hash, "previous_manifest_hash"
            ),
        )
        object.__setattr__(
            self, "entry_hash", _require_hash(self.entry_hash, "entry_hash")
        )
        object.__setattr__(
            self, "sealed_at", _require_timestamp(self.sealed_at)
        )

        # -- tool provenance -------------------------------------------------
        object.__setattr__(self, "tool", _require_tool(self.tool))
        object.__setattr__(
            self, "tool_version", _require_tool_version(self.tool_version)
        )

    # -- derived segment-local bounds -----------------------------------------

    @property
    def first_segment_sequence(self) -> int:
        """The segment-local sequence of the first sealed record (always 1)."""
        return 1

    @property
    def last_segment_sequence(self) -> int:
        """The segment-local sequence of the last sealed record."""
        return self.record_count

    # -- construction ---------------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        manifest_sequence: int,
        entry_type: ManifestEntryType | str,
        segment_id: str,
        disposition: ManifestDisposition | str,
        segment_file: str | None,
        first_global_sequence: int,
        last_global_sequence: int,
        record_count: int,
        first_previous_hash: str,
        last_record_hash: str,
        sealed_at: str,
        tool: str,
        tool_version: str,
        previous_manifest_hash: str,
    ) -> "ManifestEntry":
        """Build a validated entry and compute its canonical ``entry_hash``."""
        entry = cls(
            manifest_version=MANIFEST_SCHEMA_VERSION,
            manifest_sequence=manifest_sequence,
            entry_type=entry_type,  # type: ignore[arg-type]
            segment_id=segment_id,
            disposition=disposition,  # type: ignore[arg-type]
            segment_file=segment_file,
            first_global_sequence=first_global_sequence,
            last_global_sequence=last_global_sequence,
            record_count=record_count,
            first_previous_hash=first_previous_hash,
            last_record_hash=last_record_hash,
            sealed_at=sealed_at,
            tool=tool,
            tool_version=tool_version,
            previous_manifest_hash=previous_manifest_hash,
            entry_hash=GENESIS_MANIFEST_HASH,
        )
        return replace(entry, entry_hash=compute_entry_hash(entry))

    @classmethod
    def from_dict(cls, payload: object) -> "ManifestEntry":
        """Parse and validate one manifest entry mapping.

        The key set must be exactly the approved sixteen fields: a missing or
        unknown field is refused rather than ignored, exactly like audit
        events.
        """
        if not isinstance(payload, Mapping):
            raise ManifestSchemaError(
                "manifest entry must be a mapping of fields to values"
            )
        _require_string_keys(payload)
        keys = set(payload)
        missing = [name for name in _ENTRY_FIELDS if name not in keys]
        if missing:
            raise ManifestSchemaError(
                "manifest entry is missing required field(s): "
                + ", ".join(sorted(missing))
            )
        unknown = sorted(name for name in keys if name not in _ENTRY_FIELDS)
        if unknown:
            raise ManifestSchemaError(
                "manifest entry has unknown field(s): " + ", ".join(unknown)
            )
        return cls(
            manifest_version=payload["manifest_version"],  # type: ignore[arg-type]
            manifest_sequence=payload["manifest_sequence"],  # type: ignore[arg-type]
            entry_type=payload["entry_type"],  # type: ignore[arg-type]
            segment_id=payload["segment_id"],  # type: ignore[arg-type]
            disposition=payload["disposition"],  # type: ignore[arg-type]
            segment_file=payload["segment_file"],  # type: ignore[arg-type]
            first_global_sequence=payload["first_global_sequence"],  # type: ignore[arg-type]
            last_global_sequence=payload["last_global_sequence"],  # type: ignore[arg-type]
            record_count=payload["record_count"],  # type: ignore[arg-type]
            first_previous_hash=payload["first_previous_hash"],  # type: ignore[arg-type]
            last_record_hash=payload["last_record_hash"],  # type: ignore[arg-type]
            sealed_at=payload["sealed_at"],  # type: ignore[arg-type]
            tool=payload["tool"],  # type: ignore[arg-type]
            tool_version=payload["tool_version"],  # type: ignore[arg-type]
            previous_manifest_hash=payload[  # type: ignore[arg-type]
                "previous_manifest_hash"
            ],
            entry_hash=payload["entry_hash"],  # type: ignore[arg-type]
        )

    # -- export ---------------------------------------------------------------

    def as_body(self) -> dict[str, object]:
        """Return the JSON body: every field, enums as their string values."""
        return {
            "manifest_version": self.manifest_version,
            "manifest_sequence": self.manifest_sequence,
            "entry_type": self.entry_type.value,
            "segment_id": self.segment_id,
            "disposition": self.disposition.value,
            "segment_file": self.segment_file,
            "first_global_sequence": self.first_global_sequence,
            "last_global_sequence": self.last_global_sequence,
            "record_count": self.record_count,
            "first_previous_hash": self.first_previous_hash,
            "last_record_hash": self.last_record_hash,
            "sealed_at": self.sealed_at,
            "tool": self.tool,
            "tool_version": self.tool_version,
            "previous_manifest_hash": self.previous_manifest_hash,
            "entry_hash": self.entry_hash,
        }

    def to_json_line(self) -> str:
        """Return the canonical single-line JSON text (no trailing newline)."""
        return canonical_json_text(self.as_body())


# ---------------------------------------------------------------------------
# Chain verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManifestVerification:
    """Summary of a successfully verified manifest chain."""

    entry_count: int
    first_manifest_sequence: int | None
    last_manifest_sequence: int | None
    last_entry_hash: str | None
    segment_ids: tuple[str, ...]
    archive_files: tuple[str, ...]
    offsite_segment_ids: tuple[str, ...]


def _coerce_entry(item: object, position: int | None) -> ManifestEntry:
    if isinstance(item, ManifestEntry):
        return item
    if isinstance(item, Mapping):
        try:
            return ManifestEntry.from_dict(item)
        except ManifestSchemaError as error:
            raise type(error)(str(error), position=position) from None
    raise ManifestSchemaError(
        "manifest entries must be ManifestEntry values or parsed mappings",
        position=position,
    )


def verify_manifest(
    entries: Iterable["ManifestEntry | Mapping[str, object]"],
    *,
    expected_first_global_sequence: int = 1,
    expected_first_previous_hash: str = GENESIS_PREVIOUS_HASH,
) -> ManifestVerification:
    """Verify a complete manifest chain and summarise the sealed segments.

    In manifest order (never sorted by segment label), for every entry:

    * it parses into a valid :class:`ManifestEntry`;
    * ``previous_manifest_hash`` equals the expected predecessor (genesis for
      the first entry, then the previous entry's ``entry_hash``);
    * ``manifest_sequence`` is contiguous from 1;
    * ``entry_hash`` matches the canonical body (constant-time comparison);
    * a ``segment_sealed`` entry has not appeared before (seal-once), its
      archive reference is unique, and its global sequence/anchor continues
      from the previous sealed segment: ``first_global_sequence`` equals the
      previous ``last_global_sequence`` + 1 and ``first_previous_hash`` equals
      the previous ``last_record_hash``. The first sealed segment must start
      at ``expected_first_global_sequence`` and link to
      ``expected_first_previous_hash`` (genesis by default);
    * a ``retention_decision`` entry follows the seal of the segment it names,
      is unique per segment, and repeats that seal's sequence bounds, record
      count and hash anchors exactly — a contradicting decision is refused.

    An empty manifest is valid and returns a zero-entry summary: no entry, no
    segment and no archive is invented. Nothing is written, repaired or
    normalised; mappings are parsed into new immutable values and the caller's
    objects are left untouched.

    This proves the manifest's own chain and continuity statements only. It is
    **not** evidence that archive or segment contents exist or were verified.
    """
    expected_global_sequence = _require_int(
        expected_first_global_sequence, "expected_first_global_sequence"
    )
    if expected_global_sequence < 1:
        raise ManifestSchemaError(
            "expected_first_global_sequence must be a positive integer"
        )
    expected_anchor = _require_hash(
        expected_first_previous_hash, "expected_first_previous_hash"
    )

    expected_manifest_hash = GENESIS_MANIFEST_HASH
    expected_manifest_sequence = 1
    sealed: dict[str, ManifestEntry] = {}
    segment_ids: list[str] = []
    archive_files: list[str] = []
    offsite: list[str] = []
    previous_sealed: ManifestEntry | None = None
    last_hash: str | None = None
    entry_count = 0

    for position, item in enumerate(entries):
        entry = _coerce_entry(item, position)

        if entry.previous_manifest_hash != expected_manifest_hash:
            raise ManifestContinuityError(
                "previous_manifest_hash does not match the previous manifest "
                "entry hash",
                position=position,
                manifest_sequence=entry.manifest_sequence,
            )
        if entry.manifest_sequence != expected_manifest_sequence:
            raise ManifestSequenceError(
                "manifest_sequence is not contiguous from 1",
                position=position,
                manifest_sequence=entry.manifest_sequence,
            )
        verify_entry_hash(entry, position=position)

        if entry.entry_type is ManifestEntryType.SEGMENT_SEALED:
            if entry.segment_id in sealed:
                raise ManifestDuplicateError(
                    "a segment may be sealed only once",
                    position=position,
                    manifest_sequence=entry.manifest_sequence,
                )
            if entry.segment_file in archive_files:
                raise ManifestDuplicateError(
                    "a manifest may not reference one archive file twice",
                    position=position,
                    manifest_sequence=entry.manifest_sequence,
                )
            if previous_sealed is None:
                if entry.first_global_sequence != expected_global_sequence:
                    raise ManifestContinuityError(
                        "the first sealed segment does not start at the "
                        "expected global sequence",
                        position=position,
                        manifest_sequence=entry.manifest_sequence,
                    )
                if entry.first_previous_hash != expected_anchor:
                    raise ManifestContinuityError(
                        "the first sealed segment does not link to the "
                        "expected predecessor hash",
                        position=position,
                        manifest_sequence=entry.manifest_sequence,
                    )
            else:
                if (
                    entry.first_global_sequence
                    != previous_sealed.last_global_sequence + 1
                ):
                    raise ManifestContinuityError(
                        "the sealed segment does not continue the global "
                        "sequence of the previous sealed segment",
                        position=position,
                        manifest_sequence=entry.manifest_sequence,
                    )
                if entry.first_previous_hash != previous_sealed.last_record_hash:
                    raise ManifestContinuityError(
                        "the sealed segment does not link to the previous "
                        "segment's final record hash",
                        position=position,
                        manifest_sequence=entry.manifest_sequence,
                    )
            sealed[entry.segment_id] = entry
            segment_ids.append(entry.segment_id)
            archive_files.append(entry.segment_file or "")
            previous_sealed = entry
        else:
            reference = sealed.get(entry.segment_id)
            if reference is None:
                raise ManifestReferenceError(
                    "a retention decision must follow the segment's seal "
                    "entry",
                    position=position,
                    manifest_sequence=entry.manifest_sequence,
                )
            if entry.segment_id in offsite:
                raise ManifestDuplicateError(
                    "a segment may carry only one retention decision",
                    position=position,
                    manifest_sequence=entry.manifest_sequence,
                )
            if (
                entry.record_count != reference.record_count
                or entry.first_global_sequence
                != reference.first_global_sequence
                or entry.last_global_sequence
                != reference.last_global_sequence
                or entry.first_previous_hash != reference.first_previous_hash
                or entry.last_record_hash != reference.last_record_hash
            ):
                raise ManifestContinuityError(
                    "a retention decision contradicts the sealed segment it "
                    "references",
                    position=position,
                    manifest_sequence=entry.manifest_sequence,
                )
            offsite.append(entry.segment_id)

        expected_manifest_hash = entry.entry_hash
        expected_manifest_sequence += 1
        entry_count += 1
        last_hash = entry.entry_hash

    if entry_count == 0:
        return ManifestVerification(
            entry_count=0,
            first_manifest_sequence=None,
            last_manifest_sequence=None,
            last_entry_hash=None,
            segment_ids=(),
            archive_files=(),
            offsite_segment_ids=(),
        )
    return ManifestVerification(
        entry_count=entry_count,
        first_manifest_sequence=1,
        last_manifest_sequence=entry_count,
        last_entry_hash=last_hash,
        segment_ids=tuple(segment_ids),
        archive_files=tuple(archive_files),
        offsite_segment_ids=tuple(offsite),
    )
