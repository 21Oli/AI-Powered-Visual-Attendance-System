"""Tests for the P5.5.7.2 audit manifest schema and integrity foundation.

Every value here is synthetic. No test reads, creates or modifies anything in
the real ``data/audit`` directory, and no manifest or archive file is opened:
the module under test performs no filesystem access at all.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.audit_manifest import (
    ARCHIVE_FILENAME_PATTERN,
    GENESIS_MANIFEST_HASH,
    MANIFEST_SCHEMA_VERSION,
    ManifestArchivePathError,
    ManifestChainError,
    ManifestContinuityError,
    ManifestDisposition,
    ManifestDuplicateError,
    ManifestEntry,
    ManifestEntryType,
    ManifestError,
    ManifestHashFormatError,
    ManifestHashMismatchError,
    ManifestReferenceError,
    ManifestSchemaError,
    ManifestSequenceError,
    ManifestTimestampError,
    compute_entry_hash,
    validate_archive_reference,
    verify_entry_hash,
    verify_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_TIMESTAMP = "2026-11-16T12:00:00.000000Z"
HASH_A = "a" * 64
HASH_B = "b" * 64
GENESIS = GENESIS_MANIFEST_HASH

_AUTO = object()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(**overrides: object) -> ManifestEntry:
    """Build a valid manifest entry, deriving consistent defaults."""
    values: dict[str, object] = {
        "manifest_version": MANIFEST_SCHEMA_VERSION,
        "manifest_sequence": 1,
        "entry_type": "segment_sealed",
        "segment_id": "2026-11-001",
        "disposition": _AUTO,
        "segment_file": _AUTO,
        "first_global_sequence": 1,
        "last_global_sequence": 3,
        "record_count": None,
        "first_previous_hash": GENESIS,
        "last_record_hash": HASH_A,
        "sealed_at": BASE_TIMESTAMP,
        "tool": "audit_rotate",
        "tool_version": "1.0.0",
        "previous_manifest_hash": GENESIS,
        "entry_hash": _AUTO,
    }
    values.update(overrides)

    first = values["first_global_sequence"]
    last = values["last_global_sequence"]
    assert isinstance(first, int) and isinstance(last, int)
    if values["record_count"] is None:
        values["record_count"] = last - first + 1

    entry_type = values["entry_type"]
    retaining = str(entry_type) == "retention_decision"
    if values["disposition"] is _AUTO:
        values["disposition"] = (
            "archived_offsite" if retaining else "archived"
        )
    if values["segment_file"] is _AUTO:
        values["segment_file"] = (
            None if retaining else f"archive/audit-{first}-{last}.jsonl"
        )
    recompute = values["entry_hash"] is _AUTO
    if recompute:
        values["entry_hash"] = GENESIS
    entry = ManifestEntry.from_dict(values)
    if recompute:
        object.__setattr__(entry, "entry_hash", compute_entry_hash(entry))
        entry = ManifestEntry.from_dict(entry.as_body())
    return entry


def _rehash(entry: ManifestEntry, **changes: object) -> ManifestEntry:
    """Replace validated fields and recompute the canonical entry hash."""
    clone = replace(entry)
    for name, value in changes.items():
        object.__setattr__(clone, name, value)
    body = clone.as_body()
    body["entry_hash"] = GENESIS
    object.__setattr__(clone, "entry_hash", compute_entry_hash(body))
    return clone


def _forge(entry: ManifestEntry, **changes: object) -> ManifestEntry:
    """Bypass field validation on purpose (fault injection for chain checks)."""
    clone = replace(entry)
    for name, value in changes.items():
        object.__setattr__(clone, name, value)
    if "entry_hash" not in changes:
        object.__setattr__(clone, "entry_hash", compute_entry_hash(clone))
    return clone


def _link(entries: list[ManifestEntry]) -> list[ManifestEntry]:
    """Assign contiguous sequences and predecessor hashes."""
    linked: list[ManifestEntry] = []
    previous = GENESIS
    for index, entry in enumerate(entries, start=1):
        linked.append(
            _rehash(
                entry,
                manifest_sequence=index,
                previous_manifest_hash=previous,
            )
        )
        previous = linked[-1].entry_hash
    return linked


def _two_segments() -> list[ManifestEntry]:
    first = _entry(
        segment_id="2026-11-001",
        first_global_sequence=1,
        last_global_sequence=3,
        last_record_hash=HASH_A,
    )
    second = _entry(
        segment_id="2026-11-002",
        first_global_sequence=4,
        last_global_sequence=6,
        first_previous_hash=HASH_A,
        last_record_hash=HASH_B,
    )
    return _link([first, second])


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_valid_segment_sealed_entry_fields() -> None:
    entry = _entry()
    assert entry.manifest_version == MANIFEST_SCHEMA_VERSION == 1
    assert entry.manifest_sequence == 1
    assert entry.entry_type is ManifestEntryType.SEGMENT_SEALED
    assert entry.disposition is ManifestDisposition.ARCHIVED
    assert entry.segment_id == "2026-11-001"
    assert entry.segment_file == "archive/audit-1-3.jsonl"
    assert entry.first_global_sequence == 1
    assert entry.last_global_sequence == 3
    assert entry.record_count == 3
    assert entry.first_segment_sequence == 1
    assert entry.last_segment_sequence == 3
    assert entry.first_previous_hash == GENESIS
    assert entry.last_record_hash == HASH_A
    assert entry.previous_manifest_hash == GENESIS
    assert len(entry.entry_hash) == 64


def test_create_computes_a_valid_entry_hash() -> None:
    created = ManifestEntry.create(
        manifest_sequence=1,
        entry_type="segment_sealed",
        segment_id="2026-11-001",
        disposition="archived",
        segment_file="archive/audit-1-3.jsonl",
        first_global_sequence=1,
        last_global_sequence=3,
        record_count=3,
        first_previous_hash=GENESIS,
        last_record_hash=HASH_A,
        sealed_at=BASE_TIMESTAMP,
        tool="audit_rotate",
        tool_version="1.0.0",
        previous_manifest_hash=GENESIS,
    )
    assert created.manifest_version == MANIFEST_SCHEMA_VERSION
    assert created.entry_hash == compute_entry_hash(created)
    assert verify_entry_hash(created) == created.entry_hash


def test_to_json_line_is_canonical_and_round_trips() -> None:
    entry = _entry()
    text = entry.to_json_line()
    assert "\n" not in text
    assert ": " not in text and ", " not in text
    assert set(json.loads(text)) == set(entry.as_body())
    assert ManifestEntry.from_dict(json.loads(text)) == entry


def test_create_computes_entry_hash_and_rejects_claimed_one() -> None:
    entry = _entry()
    assert entry.entry_hash == compute_entry_hash(entry)
    body = entry.as_body()
    body["entry_hash"] = HASH_B
    with pytest.raises(ManifestHashMismatchError):
        verify_entry_hash(body)


def test_missing_required_field_is_rejected() -> None:
    body = _entry().as_body()
    del body["record_count"]
    with pytest.raises(ManifestSchemaError, match="missing required field"):
        ManifestEntry.from_dict(body)


def test_unknown_field_is_rejected() -> None:
    body = _entry().as_body()
    body["archive_checksum"] = HASH_A
    with pytest.raises(ManifestSchemaError, match="unknown field"):
        ManifestEntry.from_dict(body)


def test_non_mapping_payload_is_rejected() -> None:
    with pytest.raises(ManifestSchemaError, match="mapping"):
        ManifestEntry.from_dict([1, 2, 3])


def test_non_string_key_is_rejected() -> None:
    body = _entry().as_body()
    body[1] = "x"  # type: ignore[index]
    with pytest.raises(ManifestSchemaError, match="keys must be strings"):
        ManifestEntry.from_dict(body)


@pytest.mark.parametrize("version", [2, 0, -1, "1", True, None, 1.0])
def test_invalid_manifest_version_is_rejected(version: object) -> None:
    with pytest.raises(ManifestSchemaError):
        _entry(manifest_version=version)


@pytest.mark.parametrize("sequence", [0, -1, "1", True, None, 1.5])
def test_invalid_manifest_sequence_is_rejected(sequence: object) -> None:
    with pytest.raises(ManifestSchemaError):
        _entry(manifest_sequence=sequence)


@pytest.mark.parametrize(
    "segment_id",
    ["2026-11-000", "2026-13-001", "2026-00-001", "2026-11-1", "202611-001",
     "2026-11-1000", "2026-11-01a", "2026-11-001 ", ""],
)
def test_invalid_segment_id_is_rejected(segment_id: object) -> None:
    with pytest.raises(ManifestSchemaError):
        _entry(segment_id=segment_id)


@pytest.mark.parametrize(
    "sealed_at",
    [
        "2026-11-16T12:00:00Z",
        "2026-11-16T12:00:00.000000+00:00",
        "2026-11-16 12:00:00.000000Z",
        "2026-02-30T12:00:00.000000Z",
        "not-a-timestamp",
        "",
        1731844800,
        None,
    ],
)
def test_invalid_timestamp_is_rejected(sealed_at: object) -> None:
    with pytest.raises(ManifestTimestampError):
        _entry(sealed_at=sealed_at)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("first_previous_hash", "A" * 64),
        ("last_record_hash", "a" * 63),
        ("previous_manifest_hash", "g" * 64),
        ("entry_hash", ""),
        ("last_record_hash", 123),
        ("first_previous_hash", None),
    ],
)
def test_invalid_hash_format_is_rejected(field: str, value: object) -> None:
    with pytest.raises(ManifestSchemaError):
        _entry(**{field: value})


@pytest.mark.parametrize(
    ("first", "last"),
    [(0, 3), (4, 3), (-1, 0)],
)
def test_invalid_global_sequence_bounds_are_rejected(
    first: int, last: int
) -> None:
    with pytest.raises(ManifestSchemaError):
        _entry(first_global_sequence=first, last_global_sequence=last)


@pytest.mark.parametrize("count", [0, -1, 2, 4, "3", True])
def test_inconsistent_record_count_is_rejected(count: object) -> None:
    with pytest.raises(ManifestSchemaError):
        _entry(first_global_sequence=1, last_global_sequence=3, record_count=count)


@pytest.mark.parametrize(
    "entry_type", ["segment_sealed_v2", "SEGMENT_SEALED", "", None, 1]
)
def test_invalid_entry_type_is_rejected(entry_type: object) -> None:
    with pytest.raises(ManifestSchemaError, match="vocabulary"):
        _entry(entry_type=entry_type)


@pytest.mark.parametrize(
    "disposition", ["archived_offsite_v2", "ARCHIVED", "", None, 2]
)
def test_invalid_disposition_is_rejected(disposition: object) -> None:
    with pytest.raises(ManifestSchemaError, match="vocabulary"):
        _entry(disposition=disposition)


def test_sealed_entry_requires_archived_disposition() -> None:
    with pytest.raises(ManifestSchemaError, match="segment_sealed"):
        _entry(disposition=ManifestDisposition.ARCHIVED_OFFSITE)


def test_sealed_entry_requires_archive_reference() -> None:
    with pytest.raises(ManifestArchivePathError, match="required"):
        _entry(segment_file=None)


def test_retention_entry_requires_offsite_disposition() -> None:
    with pytest.raises(ManifestSchemaError, match="retention_decision"):
        _entry(entry_type="retention_decision", disposition="archived")


def test_retention_entry_must_not_carry_archive_reference() -> None:
    with pytest.raises(ManifestArchivePathError, match="null"):
        _entry(
            entry_type="retention_decision",
            segment_file="archive/audit-1-3.jsonl",
        )


@pytest.mark.parametrize(
    "tool", ["", "Audit_Rotate", "1tool", "-tool", "a" * 65, "tool name", None]
)
def test_invalid_tool_is_rejected(tool: object) -> None:
    with pytest.raises(ManifestSchemaError):
        _entry(tool=tool)


@pytest.mark.parametrize(
    "version", ["", " ", "v" * 33, "1 0", 1, None]
)
def test_invalid_tool_version_is_rejected(version: object) -> None:
    with pytest.raises(ManifestSchemaError):
        _entry(tool_version=version)


def test_oversized_archive_reference_is_rejected() -> None:
    with pytest.raises(ManifestArchivePathError, match="at most"):
        validate_archive_reference("archive/" + "a" * 300 + ".jsonl")


# ---------------------------------------------------------------------------
# Canonicalization and hashing
# ---------------------------------------------------------------------------


def test_key_order_does_not_change_canonical_form_or_digest() -> None:
    entry = _entry()
    body = entry.as_body()
    reversed_body = dict(reversed(list(body.items())))
    assert json.loads(entry.to_json_line()) == reversed_body
    parsed = ManifestEntry.from_dict(reversed_body)
    assert parsed.entry_hash == entry.entry_hash
    assert compute_entry_hash(reversed_body) == entry.entry_hash


def test_whitespace_in_json_input_does_not_change_digest() -> None:
    entry = _entry()
    compact = entry.to_json_line()
    spaced = json.dumps(entry.as_body(), indent=2, sort_keys=True)
    assert ManifestEntry.from_dict(json.loads(spaced)) == entry
    assert json.loads(compact) == json.loads(spaced)


@pytest.mark.parametrize(
    "changes",
    [
        {"segment_id": "2026-11-002"},
        {"record_count": 4, "last_global_sequence": 4},
        {"last_record_hash": HASH_B},
        {"sealed_at": "2026-11-17T00:00:00.000000Z"},
        {"tool_version": "1.0.1"},
        {"manifest_sequence": 2},
    ],
)
def test_changing_a_protected_field_changes_the_digest(
    changes: dict[str, object],
) -> None:
    baseline = _entry()
    # Rebuild through create so the field set stays self-consistent.
    values = baseline.as_body()
    values.update(changes)
    values["entry_hash"] = GENESIS
    if "segment_file" not in changes and "last_global_sequence" in changes:
        values["segment_file"] = (
            "archive/audit-1-%d.jsonl" % values["last_global_sequence"]
        )
    changed = ManifestEntry.from_dict(values)
    assert changed.entry_hash != baseline.entry_hash


def test_claimed_entry_hash_never_influences_the_digest() -> None:
    entry = _entry()
    body = entry.as_body()
    body["entry_hash"] = HASH_B
    assert compute_entry_hash(body) == entry.entry_hash


def test_tampered_entry_hash_is_refused() -> None:
    entry = _entry()
    with pytest.raises(ManifestHashMismatchError) as excinfo:
        verify_entry_hash(_forge(entry, entry_hash=HASH_B))
    assert excinfo.value.position is None


def test_verify_entry_hash_returns_the_recomputed_digest() -> None:
    entry = _entry()
    assert verify_entry_hash(entry) == entry.entry_hash


def test_changing_predecessor_hash_changes_digest_and_breaks_chain() -> None:
    chain = _link([_entry()])
    altered = _rehash(chain[0], previous_manifest_hash=HASH_B)
    assert altered.entry_hash != chain[0].entry_hash
    with pytest.raises(ManifestContinuityError):
        verify_manifest([altered], expected_first_previous_hash=GENESIS)
    with pytest.raises(ManifestHashMismatchError):
        verify_entry_hash(_forge(chain[0], entry_hash=HASH_B))


# ---------------------------------------------------------------------------
# Manifest chain
# ---------------------------------------------------------------------------


def test_empty_manifest_is_valid_and_invents_nothing() -> None:
    summary = verify_manifest([])
    assert summary.entry_count == 0
    assert summary.first_manifest_sequence is None
    assert summary.last_manifest_sequence is None
    assert summary.last_entry_hash is None
    assert summary.segment_ids == ()
    assert summary.archive_files == ()


def test_single_genesis_entry_verifies() -> None:
    entry = _entry()
    summary = verify_manifest([entry])
    assert summary.entry_count == 1
    assert summary.first_manifest_sequence == 1
    assert summary.last_manifest_sequence == 1
    assert summary.last_entry_hash == entry.entry_hash
    assert summary.segment_ids == ("2026-11-001",)
    assert summary.archive_files == ("archive/audit-1-3.jsonl",)


def test_multi_record_chain_verifies_in_manifest_order() -> None:
    chain = _two_segments()
    summary = verify_manifest(chain)
    assert summary.entry_count == 2
    assert summary.last_entry_hash == chain[-1].entry_hash
    assert summary.segment_ids == ("2026-11-001", "2026-11-002")
    assert summary.offsite_segment_ids == ()


def test_mappings_are_accepted_as_chain_entries() -> None:
    chain = _two_segments()
    summary = verify_manifest([entry.as_body() for entry in chain])
    assert summary.segment_ids == ("2026-11-001", "2026-11-002")


def test_non_mapping_entry_is_rejected() -> None:
    with pytest.raises(ManifestSchemaError, match="ManifestEntry"):
        verify_manifest(["not-an-entry"])  # type: ignore[list-item]


def test_segment_labels_are_not_sorted_lexically() -> None:
    # The manifest order is authoritative: 2026-11-002 may precede 2026-11-001.
    first = _entry(
        segment_id="2026-11-002",
        first_global_sequence=1,
        last_global_sequence=2,
        last_record_hash=HASH_A,
    )
    second = _entry(
        segment_id="2026-11-001",
        first_global_sequence=3,
        last_global_sequence=4,
        first_previous_hash=HASH_A,
        last_record_hash=HASH_B,
    )
    summary = verify_manifest(_link([first, second]))
    assert summary.segment_ids == ("2026-11-002", "2026-11-001")


def test_segment_allocation_month_may_differ_from_event_timestamps() -> None:
    entry = _entry(segment_id="2026-12-001")
    assert verify_manifest([entry]).segment_ids == ("2026-12-001",)


def test_manifest_sequence_gap_is_rejected() -> None:
    chain = _two_segments()
    broken = [chain[0], _rehash(chain[1], manifest_sequence=3)]
    with pytest.raises(ManifestSequenceError) as excinfo:
        verify_manifest(broken)
    assert excinfo.value.position == 1


def test_duplicate_manifest_sequence_is_rejected() -> None:
    chain = _two_segments()
    broken = [chain[0], _rehash(chain[1], manifest_sequence=1)]
    with pytest.raises(ManifestSequenceError):
        verify_manifest(broken)


def test_incorrect_predecessor_hash_is_rejected() -> None:
    chain = _two_segments()
    broken = [chain[0], _rehash(chain[1], previous_manifest_hash=HASH_B)]
    with pytest.raises(ManifestContinuityError) as excinfo:
        verify_manifest(broken)
    assert excinfo.value.position == 1


def test_first_entry_must_use_the_genesis_predecessor() -> None:
    entry = _entry(previous_manifest_hash=HASH_A)
    with pytest.raises(ManifestContinuityError):
        verify_manifest([entry])


def test_repeated_segment_identity_is_rejected() -> None:
    first = _entry(
        segment_id="2026-11-001",
        first_global_sequence=1,
        last_global_sequence=2,
        last_record_hash=HASH_A,
    )
    second = _entry(
        segment_id="2026-11-001",
        first_global_sequence=3,
        last_global_sequence=4,
        first_previous_hash=HASH_A,
        last_record_hash=HASH_B,
    )
    with pytest.raises(ManifestDuplicateError, match="sealed only once"):
        verify_manifest(_link([first, second]))


def test_duplicate_archive_reference_is_rejected() -> None:
    chain = _two_segments()
    forged = _forge(chain[1], segment_file=chain[0].segment_file)
    with pytest.raises(ManifestDuplicateError, match="archive file twice"):
        verify_manifest([chain[0], forged])


def test_reordered_entries_are_rejected() -> None:
    chain = _two_segments()
    with pytest.raises(ManifestChainError):
        verify_manifest([chain[1], chain[0]])


def test_tampered_entry_hash_in_chain_is_rejected() -> None:
    chain = _two_segments()
    broken = [chain[0], _forge(chain[1], entry_hash=HASH_B)]
    with pytest.raises(ManifestHashMismatchError) as excinfo:
        verify_manifest(broken)
    assert excinfo.value.position == 1


def test_global_sequence_gap_between_segments_is_rejected() -> None:
    chain = _two_segments()
    broken = [
        chain[0],
        _rehash(
            chain[1],
            first_global_sequence=5,
            last_global_sequence=6,
            record_count=2,
            segment_file="archive/audit-5-6.jsonl",
        ),
    ]
    with pytest.raises(ManifestContinuityError, match="global sequence"):
        verify_manifest(broken)


def test_anchor_mismatch_between_segments_is_rejected() -> None:
    chain = _two_segments()
    broken = [chain[0], _rehash(chain[1], first_previous_hash=HASH_B)]
    with pytest.raises(ManifestContinuityError, match="final record hash"):
        verify_manifest(broken)


def test_first_segment_must_start_at_the_expected_global_sequence() -> None:
    entry = _entry(first_global_sequence=2, last_global_sequence=4)
    with pytest.raises(ManifestContinuityError):
        verify_manifest([entry])


def test_expected_first_previous_hash_seam_is_supported() -> None:
    entry = _entry(first_global_sequence=7, last_global_sequence=9,
                   first_previous_hash=HASH_B)
    summary = verify_manifest(
        [entry],
        expected_first_global_sequence=7,
        expected_first_previous_hash=HASH_B,
    )
    assert summary.segment_ids == ("2026-11-001",)


def test_invalid_verification_seams_are_rejected() -> None:
    with pytest.raises(ManifestSchemaError):
        verify_manifest([], expected_first_global_sequence=0)
    with pytest.raises(ManifestHashFormatError):
        verify_manifest([], expected_first_previous_hash="nope")


def test_retention_decision_after_seal_verifies() -> None:
    sealed = _entry()
    decision = _entry(
        entry_type="retention_decision",
        first_global_sequence=sealed.first_global_sequence,
        last_global_sequence=sealed.last_global_sequence,
        first_previous_hash=sealed.first_previous_hash,
        last_record_hash=sealed.last_record_hash,
        sealed_at="2027-11-20T00:00:00.000000Z",
    )
    summary = verify_manifest(_link([sealed, decision]))
    assert summary.segment_ids == ("2026-11-001",)
    assert summary.offsite_segment_ids == ("2026-11-001",)
    assert summary.archive_files == (sealed.segment_file,)


def test_retention_decision_without_seal_is_rejected() -> None:
    decision = _entry(entry_type="retention_decision")
    with pytest.raises(ManifestReferenceError):
        verify_manifest([decision])


def test_contradicting_retention_decision_is_rejected() -> None:
    sealed = _entry()
    decision = _entry(
        entry_type="retention_decision",
        record_count=2,
        first_global_sequence=1,
        last_global_sequence=2,
        first_previous_hash=sealed.first_previous_hash,
        last_record_hash=sealed.last_record_hash,
    )
    with pytest.raises(ManifestContinuityError, match="contradicts"):
        verify_manifest(_link([sealed, decision]))


def test_duplicate_retention_decision_is_rejected() -> None:
    sealed = _entry()
    decision = _entry(
        entry_type="retention_decision",
        first_previous_hash=sealed.first_previous_hash,
        last_record_hash=sealed.last_record_hash,
    )
    with pytest.raises(ManifestDuplicateError, match="one retention"):
        verify_manifest(_link([sealed, decision, decision]))


# ---------------------------------------------------------------------------
# Archive path safety
# ---------------------------------------------------------------------------


def test_valid_archive_filename_is_accepted() -> None:
    assert (
        validate_archive_reference("archive/audit-1-42.jsonl")
        == "archive/audit-1-42.jsonl"
    )
    assert ARCHIVE_FILENAME_PATTERN.fullmatch("audit-1-42.jsonl") is not None


@pytest.mark.parametrize(
    "reference",
    [
        "/data/audit/archive/audit-1-42.jsonl",
        "/archive/audit-1-42.jsonl",
        "\\\\server\\share\\audit-1-42.jsonl",
        "C:/data/audit/archive/audit-1-42.jsonl",
        "C:\\data\\audit\\archive\\audit-1-42.jsonl",
        "file:///archive/audit-1-42.jsonl",
    ],
)
def test_absolute_archive_reference_is_rejected(reference: str) -> None:
    with pytest.raises(ManifestArchivePathError):
        validate_archive_reference(reference)


@pytest.mark.parametrize(
    "reference",
    [
        "archive/../audit.jsonl",
        "../archive/audit-1-42.jsonl",
        "archive/../../archive/audit-1-42.jsonl",
        "archive\\..\\audit-1-42.jsonl",
        "archive/./audit-1-42.jsonl",
        "archive//audit-1-42.jsonl",
        "archive/sub/audit-1-42.jsonl",
    ],
)
def test_parent_traversal_and_nested_paths_are_rejected(
    reference: str,
) -> None:
    with pytest.raises(ManifestArchivePathError):
        validate_archive_reference(reference)


@pytest.mark.parametrize(
    "reference",
    [
        "audit/audit-1-42.jsonl",
        "other/audit-1-42.jsonl",
        "archive/archive/audit-1-42.jsonl",
        "data/audit/archive/audit-1-42.jsonl",
    ],
)
def test_unexpected_directory_is_rejected(reference: str) -> None:
    with pytest.raises(ManifestArchivePathError):
        validate_archive_reference(reference)


@pytest.mark.parametrize(
    "reference",
    [
        "archive/audit-1-42.log",
        "archive/audit-1-42.jsonl.bak",
        "archive/audit-1-42.JSONL",
        "archive/audit-1-42.json",
        "archive/audit-1-42",
        "archive/audit-1-42.jsonl ",
    ],
)
def test_invalid_extension_is_rejected(reference: str) -> None:
    with pytest.raises(ManifestArchivePathError):
        validate_archive_reference(reference)


@pytest.mark.parametrize(
    "reference",
    [
        "archive\\audit-1-42.jsonl",
        "archive\\audit-1-42.jsonl",
    ],
)
def test_windows_separators_are_rejected(reference: str) -> None:
    with pytest.raises(ManifestArchivePathError, match="separator"):
        validate_archive_reference(reference)


@pytest.mark.parametrize(
    "filename",
    [
        "audit-01-42.jsonl",
        "audit-0-42.jsonl",
        "audit-1-0.jsonl",
        "audit-1-2-3.jsonl",
        "audit--1-2.jsonl",
        "audit-1-.jsonl",
        "audit-1_2.jsonl",
        "segment-1-2.jsonl",
        "audit-1-2.jsonl.tmp",
    ],
)
def test_malformed_archive_naming_is_rejected(filename: str) -> None:
    with pytest.raises(ManifestArchivePathError):
        validate_archive_reference(f"archive/{filename}")


def test_inverted_archive_range_is_rejected() -> None:
    with pytest.raises(ManifestArchivePathError, match="inverted"):
        validate_archive_reference("archive/audit-9-3.jsonl")


def test_archive_name_must_match_the_entry_sequence_bounds() -> None:
    with pytest.raises(ManifestArchivePathError, match="first_global"):
        validate_archive_reference(
            "archive/audit-2-4.jsonl",
            first_global_sequence=1,
            last_global_sequence=4,
        )
    with pytest.raises(ManifestArchivePathError, match="last_global"):
        validate_archive_reference(
            "archive/audit-1-4.jsonl",
            first_global_sequence=1,
            last_global_sequence=3,
        )
    with pytest.raises(ManifestArchivePathError):
        _entry(
            first_global_sequence=1,
            last_global_sequence=3,
            segment_file="archive/audit-2-4.jsonl",
        )


@pytest.mark.parametrize(
    "reference",
    ["", " ", "archive/", "archive/audit-1-42.jsonl\x00", "archive/\x01.jsonl"],
)
def test_empty_or_control_archive_reference_is_rejected(
    reference: str,
) -> None:
    with pytest.raises(ManifestArchivePathError):
        validate_archive_reference(reference)


def test_non_string_archive_reference_is_rejected() -> None:
    with pytest.raises(ManifestArchivePathError):
        validate_archive_reference(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_no_file_is_created_and_real_audit_directory_is_untouched(
    tmp_path: Path,
) -> None:
    chain = _two_segments()
    verify_manifest(chain)
    assert list(tmp_path.iterdir()) == []
    assert not (PROJECT_ROOT / "data" / "audit").exists()


def test_manifest_errors_never_echo_values() -> None:
    secret = "EmployeeAliceSecret"
    body = _entry().as_body()
    body["tool"] = secret
    with pytest.raises(ManifestSchemaError) as excinfo:
        ManifestEntry.from_dict(body)
    assert secret not in str(excinfo.value)


def test_error_hierarchy_is_consistent() -> None:
    assert issubclass(ManifestSchemaError, ManifestError)
    assert issubclass(ManifestTimestampError, ManifestSchemaError)
    assert issubclass(ManifestArchivePathError, ManifestSchemaError)
    assert issubclass(ManifestHashFormatError, ManifestSchemaError)
    assert issubclass(ManifestSequenceError, ManifestChainError)
    assert issubclass(ManifestContinuityError, ManifestChainError)
    assert issubclass(ManifestDuplicateError, ManifestChainError)
    assert issubclass(ManifestReferenceError, ManifestChainError)
