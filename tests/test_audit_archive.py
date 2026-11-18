"""Tests for archive and historical chain verification (P5.5.7 stage B/G).

Every installation is synthetic and lives under ``tmp_path``; the real
``data/audit`` directory is never touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.audit_archive as archive_module
from src.audit_archive import (
    ACTIVE_SEGMENT_MISSING,
    ARCHIVE_ANCHOR_MISMATCH,
    ARCHIVE_CHANGED,
    ARCHIVE_COUNT_MISMATCH,
    ARCHIVE_DUPLICATE_AUDIT_ID,
    ARCHIVE_HASH_MISMATCH,
    ARCHIVE_MALFORMED_JSON,
    ARCHIVE_MISSING,
    ARCHIVE_NOT_REGULAR_FILE,
    ARCHIVE_SCHEMA_INVALID,
    ARCHIVE_SEGMENT_INVALID,
    ARCHIVE_TORN_TAIL,
    ARCHIVE_UNEXPECTED_FILE,
    ARCHIVE_UNREGISTERED,
    BROKEN_PREDECESSOR,
    DUPLICATE_AUDIT_ID,
    IntegrityProblem,
    MANIFEST_BROKEN_CHAIN,
    MANIFEST_CORRUPT,
    MANIFEST_DUPLICATE,
    MANIFEST_HASH_MISMATCH,
    MANIFEST_REFERENCE_INVALID,
    MANIFEST_SCHEMA_INVALID,
    MANIFEST_SEQUENCE_INVALID,
    MANIFEST_TORN_TAIL,
    RecoveryProblem,
    SEGMENT_INVALID,
    SEGMENT_INVALID as ACTIVE_SEGMENT_INVALID,
    UnavailableProblem,
    read_manifest_entries,
    verify_active_journal,
    verify_declared_archive,
    verify_history,
)
from src.audit_manifest import (
    GENESIS_MANIFEST_HASH,
    ManifestEntry,
    compute_entry_hash,
)
from src.audit_schema import (
    AuditAction,
    AuditEvent,
    AuditOutcome,
    AuditReasonCode,
    AuditSource,
    parse_timestamp,
)
from tests.audit_support import (
    BASE_TIMESTAMP,
    build_events,
    install,
    install_events,
    seal_entry,
    timestamp_for,
    write_events,
    write_manifest,
)

_OTHER_HASH = "a" * 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rebuild(
    events: list[AuditEvent],
    *,
    previous_hash: str | None = None,
    segment_id: str | None = None,
    first_sequence: int | None = None,
    audit_ids: dict[int, str] | None = None,
) -> list[AuditEvent]:
    """Recompute a segment's chain with optional deliberate changes."""
    rebuilt: list[AuditEvent] = []
    previous = previous_hash or events[0].previous_hash
    sequence = first_sequence or events[0].sequence
    for index, event in enumerate(events, start=1):
        replacement = AuditEvent.create(
            sequence=sequence,
            segment_id=segment_id or event.segment_id,
            segment_sequence=index,
            previous_hash=previous,
            timestamp=parse_timestamp(event.timestamp),
            audit_id=(audit_ids or {}).get(index, event.audit_id),
            action=event.action,
            outcome=event.outcome,
            reason_code=event.reason_code,
            source=event.source,
        )
        rebuilt.append(replacement)
        previous = replacement.record_hash
        sequence += 1
    return rebuilt


def _rehash_entry(entry: ManifestEntry, **changes: object) -> ManifestEntry:
    from dataclasses import replace

    clone = replace(entry)
    for name, value in changes.items():
        object.__setattr__(clone, name, value)
    body = clone.as_body()
    body["entry_hash"] = GENESIS_MANIFEST_HASH
    object.__setattr__(clone, "entry_hash", compute_entry_hash(body))
    return clone


def _relink(entries: list[ManifestEntry]) -> list[ManifestEntry]:
    linked: list[ManifestEntry] = []
    previous = GENESIS_MANIFEST_HASH
    for index, entry in enumerate(entries, start=1):
        linked.append(
            _rehash_entry(
                entry,
                manifest_sequence=index,
                previous_manifest_hash=previous,
            )
        )
        previous = linked[-1].entry_hash
    return linked


def _expect(installation, category: str) -> None:
    with pytest.raises(IntegrityProblem) as caught:
        verify_history(installation.layout)
    assert caught.value.category == category


# ---------------------------------------------------------------------------
# Valid installations
# ---------------------------------------------------------------------------


def test_single_sealed_segment_verifies(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[3])
    history = verify_history(installation.layout)
    assert len(history.segments) == 1
    assert history.segments[0].record_count == 3
    assert history.segments[0].first_global_sequence == 1
    assert history.segments[0].last_global_sequence == 3
    assert history.total_records == 3
    assert history.anchor_only is False
    assert history.last_seal is not None


def test_multiple_sealed_segments_verify(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2, 3, 1])
    history = verify_history(installation.layout)
    assert [summary.record_count for summary in history.segments] == [2, 3, 1]
    assert history.total_records == 6
    assert history.expected_next_sequence == 7
    assert history.sealed_segment_ids == (
        "2026-11-001",
        "2026-11-002",
        "2026-11-003",
    )


def test_active_segment_continues_sealed_history(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2], active=2)
    history = verify_history(installation.layout)
    active = verify_active_journal(installation.layout, history)
    assert active.segment_id == "2026-11-002"
    assert active.last_sequence == 4
    assert active.summary is not None
    assert active.summary.first_previous_hash == history.expected_previous_hash


def test_empty_active_journal_after_seal_is_valid(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2], active=0)
    history = verify_history(installation.layout)
    active = verify_active_journal(installation.layout, history)
    assert active.exists
    assert active.is_empty
    assert history.expected_next_sequence == 3


def test_manifest_order_not_file_name_order(tmp_path: Path) -> None:
    # Ranges 1-8, 9-10, 11-11: lexical file order is 1-8, 11-11, 9-10, which
    # must not influence verification order.
    installation = install_events(
        tmp_path, segments=[("2026-11-001", 8), ("2026-11-002", 2), ("2026-11-003", 1)]
    )
    history = verify_history(installation.layout)
    assert [summary.segment_id for summary in history.segments] == [
        "2026-11-001",
        "2026-11-002",
        "2026-11-003",
    ]
    names = sorted(path.name for path in installation.archive_directory.iterdir())
    assert names[1].startswith("audit-11-"), names


def test_non_lexical_segment_labels_verify(tmp_path: Path) -> None:
    installation = install_events(
        tmp_path, segments=[("2026-11-002", 1), ("2026-11-001", 1)]
    )
    history = verify_history(installation.layout)
    assert [summary.segment_id for summary in history.segments] == [
        "2026-11-002",
        "2026-11-001",
    ]


def test_history_verification_is_read_only(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2], active=1)
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in installation.layout.directory.rglob("*")
        if path.is_file()
    }
    verify_history(installation.layout)
    after = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in installation.layout.directory.rglob("*")
        if path.is_file()
    }
    assert before == after


def test_empty_installation_has_empty_history(tmp_path: Path) -> None:
    layout_directory = tmp_path / "audit"
    layout_directory.mkdir()
    from src.audit_layout import AuditLayout

    history = verify_history(AuditLayout.for_directory(layout_directory))
    assert history.manifest_entries == ()
    assert history.segments == ()
    assert history.expected_next_sequence == 1


# ---------------------------------------------------------------------------
# Archive content defects
# ---------------------------------------------------------------------------


def test_empty_archive_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2])
    installation.archive_path(0).write_bytes(b"")
    _expect(installation, "ARCHIVE_SEGMENT_INVALID")


def test_torn_archive_tail_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2])
    path = installation.archive_path(0)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    _expect(installation, ARCHIVE_TORN_TAIL)


def test_archive_malformed_json_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    installation.archive_path(0).write_bytes(b"{broken\n")
    _expect(installation, ARCHIVE_MALFORMED_JSON)


def test_archive_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    installation.archive_path(0).write_bytes(b"\xff\xfe\n")
    _expect(installation, "ARCHIVE_INVALID_UTF8")


def test_archive_broken_digest_is_rejected(tmp_path: Path) -> None:
    from dataclasses import replace

    installation = install(tmp_path, sealed=[2])
    events = list(installation.segments[0][1])
    events[0] = replace(events[0], record_hash=_OTHER_HASH)
    write_events(installation.archive_path(0), events)
    _expect(installation, ARCHIVE_HASH_MISMATCH)


def test_archive_broken_predecessor_is_rejected(tmp_path: Path) -> None:
    from dataclasses import replace

    installation = install(tmp_path, sealed=[2])
    events = list(installation.segments[0][1])
    events[1] = replace(events[1], previous_hash=_OTHER_HASH)
    write_events(installation.archive_path(0), events)
    _expect(installation, "ARCHIVE_BROKEN_PREDECESSOR")


def test_archive_sequence_gap_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2])
    events = installation.segments[0][1]
    rebuilt = _rebuild(list(events), first_sequence=2)
    write_events(installation.archive_path(0), rebuilt)
    _expect(installation, "ARCHIVE_SEQUENCE_INVALID")


def test_archive_record_count_mismatch_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[3])
    events = installation.segments[0][1]
    write_events(installation.archive_path(0), events[:2])
    _expect(installation, ARCHIVE_COUNT_MISMATCH)


def test_archive_wrong_segment_identity_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2])
    events = installation.segments[0][1]
    rebuilt = _rebuild(list(events), segment_id="2026-11-999")
    write_events(installation.archive_path(0), rebuilt)
    _expect(installation, ARCHIVE_SEGMENT_INVALID)


def test_archive_anchor_mismatch_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2], active=1)
    entry = installation.manifest[0]
    forged = _rehash_entry(entry, last_record_hash=_OTHER_HASH)
    write_manifest(installation.layout.manifest, [forged])
    _expect(installation, ARCHIVE_ANCHOR_MISMATCH)


def test_duplicate_audit_id_across_segments_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1, 1])
    first = installation.segments[0][1]
    second = installation.segments[1][1]
    rebuilt = _rebuild(
        list(second), audit_ids={1: first[0].audit_id}
    )
    write_events(installation.archive_path(1), rebuilt)
    entries = list(installation.manifest)
    entries[1] = seal_entry(
        2,
        entries[0].entry_hash,
        rebuilt,
        segment_file=entries[1].segment_file,
    )
    write_manifest(installation.layout.manifest, entries)
    _expect(installation, ARCHIVE_DUPLICATE_AUDIT_ID)


def test_archive_with_unknown_field_is_a_schema_failure(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    body = installation.segments[0][1][0].as_body()
    body["surprise"] = 1
    installation.archive_path(0).write_bytes(
        json.dumps(body).encode("utf-8") + b"\n"
    )
    _expect(installation, ARCHIVE_SCHEMA_INVALID)


def test_archive_with_blank_line_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2])
    raw = installation.archive_path(0).read_bytes()
    lines = raw.split(b"\n")
    installation.archive_path(0).write_bytes(lines[0] + b"\n\n" + b"\n".join(lines[1:]))
    _expect(installation, "ARCHIVE_EMPTY_LINE")


# ---------------------------------------------------------------------------
# Archive availability, registration and path safety
# ---------------------------------------------------------------------------


def test_missing_archive_without_retention_is_an_integrity_failure(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[2])
    installation.archive_path(0).unlink()
    _expect(installation, ARCHIVE_MISSING)


def test_missing_archive_with_retention_is_anchor_only(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2], retention=[0])
    installation.archive_path(0).unlink()
    history = verify_history(installation.layout)
    assert history.anchor_only_segment_ids == ("2026-11-001",)
    assert history.segments[0].anchor_only is True
    assert history.offsite_segment_ids == ("2026-11-001",)


def test_retention_with_archive_present_still_content_verifies(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[2], retention=[0])
    history = verify_history(installation.layout)
    assert history.anchor_only_segment_ids == ()
    assert history.segments[0].anchor_only is False


def test_restored_archive_under_offsite_decision_is_content_verified(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[2], retention=[0])
    installation.archive_path(0).unlink()
    verify_history(installation.layout)
    events = installation.segments[0][1]
    write_events(installation.archive_path(0), events)
    history = verify_history(installation.layout)
    assert history.segments[0].anchor_only is False


def test_unregistered_archive_requires_recovery(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    write_events(installation.archive_path(0), installation.segments[0][1])
    extra = installation.layout.archive_path(2, 4)
    write_events(extra, build_events([("2026-11-004", 3)]))
    with pytest.raises(RecoveryProblem) as caught:
        verify_history(installation.layout)
    assert caught.value.category == ARCHIVE_UNREGISTERED
    assert caught.value.recovery is True


def test_archives_without_manifest_require_recovery(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1], manifest=False)
    with pytest.raises(RecoveryProblem) as caught:
        verify_history(installation.layout)
    assert caught.value.category == ARCHIVE_UNREGISTERED


def test_unexpected_archive_directory_entry_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    (installation.archive_directory / "notes.txt").write_text("x", encoding="utf-8")
    _expect(installation, ARCHIVE_UNEXPECTED_FILE)


def test_archive_symlink_is_refused(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    path = installation.archive_path(0)
    target = tmp_path / "elsewhere.jsonl"
    target.write_bytes(path.read_bytes())
    path.unlink()
    try:
        os.symlink(target, path)
    except (OSError, NotImplementedError):  # pragma: no cover - platform
        pytest.skip("symlinks are not permitted on this platform")
    _expect(installation, ARCHIVE_NOT_REGULAR_FILE)


def test_manifest_path_traversal_is_a_schema_failure(tmp_path: Path) -> None:
    import hashlib

    from dataclasses import replace

    from src.audit_schema import canonical_json_bytes

    installation = install(tmp_path, sealed=[1])
    entry = installation.manifest[0]
    forged = replace(entry)
    object.__setattr__(forged, "segment_file", "../audit-1-1.jsonl")
    body = forged.as_body()
    body["entry_hash"] = GENESIS_MANIFEST_HASH
    object.__setattr__(
        forged,
        "entry_hash",
        hashlib.sha256(canonical_json_bytes(body)).hexdigest(),
    )
    write_manifest(installation.layout.manifest, [forged])
    _expect(installation, MANIFEST_SCHEMA_INVALID)


def test_archive_changed_during_read_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installation = install(tmp_path, sealed=[1])
    real_fstat = os.fstat
    calls = {"count": 0}

    def flaky_fstat(descriptor: int):
        calls["count"] += 1
        stat_result = real_fstat(descriptor)
        if calls["count"] == 1:
            return SimpleNamespace(
                st_dev=stat_result.st_dev,
                st_ino=stat_result.st_ino,
                st_size=stat_result.st_size,
                st_mtime_ns=stat_result.st_mtime_ns,
            )
        return SimpleNamespace(
            st_dev=stat_result.st_dev,
            st_ino=stat_result.st_ino,
            st_size=stat_result.st_size + 1,
            st_mtime_ns=stat_result.st_mtime_ns,
        )

    verify_history(installation.layout)
    monkeypatch.setattr(archive_module.os, "fstat", flaky_fstat)
    with pytest.raises(RecoveryProblem) as caught:
        verify_declared_archive(installation.manifest[0], installation.layout)
    assert caught.value.category == ARCHIVE_CHANGED


# ---------------------------------------------------------------------------
# Manifest defects
# ---------------------------------------------------------------------------


def test_manifest_torn_tail_requires_recovery(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    raw = installation.layout.manifest.read_bytes()
    installation.layout.manifest.write_bytes(raw.rstrip(b"\n")[:-1])
    with pytest.raises(RecoveryProblem) as caught:
        verify_history(installation.layout)
    assert caught.value.category == MANIFEST_TORN_TAIL


def test_manifest_malformed_json_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    installation.layout.manifest.write_bytes(b"{not-json}\n")
    _expect(installation, MANIFEST_CORRUPT)


def test_manifest_empty_line_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    raw = installation.layout.manifest.read_bytes()
    installation.layout.manifest.write_bytes(raw + b"\n")
    _expect(installation, MANIFEST_CORRUPT)


def test_manifest_entry_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    entry = installation.manifest[0]
    from dataclasses import replace

    forged = replace(entry, entry_hash=_OTHER_HASH)
    write_manifest(installation.layout.manifest, [forged])
    _expect(installation, MANIFEST_HASH_MISMATCH)


def test_manifest_sequence_gap_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    entry = installation.manifest[0]
    gap = _rehash_entry(
        entry,
        manifest_sequence=2,
        previous_manifest_hash=GENESIS_MANIFEST_HASH,
    )
    write_manifest(installation.layout.manifest, [gap])
    _expect(installation, MANIFEST_SEQUENCE_INVALID)


def test_manifest_duplicate_seal_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    entry = installation.manifest[0]
    duplicate = _rehash_entry(
        entry,
        manifest_sequence=2,
        previous_manifest_hash=entry.entry_hash,
    )
    write_manifest(installation.layout.manifest, [entry, duplicate])
    _expect(installation, MANIFEST_DUPLICATE)


def test_manifest_retention_before_seal_is_rejected(tmp_path: Path) -> None:
    from tests.audit_support import retention_entry

    installation = install(tmp_path, sealed=[1])
    entry = installation.manifest[0]
    decision = retention_entry(1, GENESIS_MANIFEST_HASH, entry)
    seal = _rehash_entry(
        entry,
        manifest_sequence=2,
        previous_manifest_hash=decision.entry_hash,
    )
    write_manifest(installation.layout.manifest, [decision, seal])
    _expect(installation, MANIFEST_REFERENCE_INVALID)


def test_manifest_retention_bounds_contradiction_is_rejected(
    tmp_path: Path,
) -> None:
    from tests.audit_support import retention_entry

    installation = install(tmp_path, sealed=[2], retention=[0])
    seal, decision = installation.manifest
    broken = _rehash_entry(
        decision, record_count=1, last_global_sequence=1
    )
    write_manifest(
        installation.layout.manifest,
        _relink([seal, broken]),
    )
    _expect(installation, MANIFEST_BROKEN_CHAIN)


def test_manifest_duplicate_retention_decision_is_rejected(
    tmp_path: Path,
) -> None:
    from tests.audit_support import retention_entry

    installation = install(tmp_path, sealed=[1], retention=[0])
    seal, decision = installation.manifest
    again = _rehash_entry(
        decision,
        manifest_sequence=3,
        previous_manifest_hash=decision.entry_hash,
    )
    write_manifest(installation.layout.manifest, [seal, decision, again])
    _expect(installation, MANIFEST_DUPLICATE)


def test_manifest_unknown_field_is_a_schema_failure(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    body = installation.manifest[0].as_body()
    body["extra"] = "x"
    installation.layout.manifest.write_bytes(
        json.dumps(body).encode("utf-8") + b"\n"
    )
    _expect(installation, MANIFEST_SCHEMA_INVALID)


def test_manifest_segment_continuity_break_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1, 1])
    first, second = installation.manifest
    broken = _rehash_entry(second, first_previous_hash=_OTHER_HASH)
    write_manifest(
        installation.layout.manifest, _relink([first, broken])
    )
    _expect(installation, MANIFEST_BROKEN_CHAIN)


def test_read_manifest_entries_returns_approved_entries(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2], retention=[0])
    entries = read_manifest_entries(installation.layout.manifest)
    assert len(entries) == 2
    assert entries[0].segment_id == entries[1].segment_id


# ---------------------------------------------------------------------------
# Active journal boundary
# ---------------------------------------------------------------------------


def test_missing_active_journal_after_seal_requires_recovery(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[2])
    installation.layout.journal.unlink()
    history = verify_history(installation.layout)
    with pytest.raises(RecoveryProblem) as caught:
        verify_active_journal(installation.layout, history)
    assert caught.value.category == ACTIVE_SEGMENT_MISSING


def test_missing_journal_without_history_is_unavailable(tmp_path: Path) -> None:
    from src.audit_layout import AuditLayout

    layout = AuditLayout.for_directory(tmp_path / "audit")
    layout.directory.mkdir()
    history = verify_history(layout)
    with pytest.raises(UnavailableProblem):
        verify_active_journal(layout, history)


def test_active_journal_that_restarts_the_chain_is_rejected(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[2], active=1)
    fresh = build_events([("2026-11-002", 1)])
    write_events(installation.layout.journal, fresh)
    history = verify_history(installation.layout)
    with pytest.raises(IntegrityProblem) as caught:
        verify_active_journal(installation.layout, history)
    assert caught.value.category == BROKEN_PREDECESSOR


def test_active_journal_reusing_a_sealed_segment_id_is_rejected(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[2], active=1)
    events = installation.active
    reseeded = _rebuild(
        list(events), previous_hash=installation.manifest[0].last_record_hash
    )
    from dataclasses import replace

    reseeded = [
        replace(event, segment_id="2026-11-001") for event in reseeded
    ]
    write_events(installation.layout.journal, reseeded)
    history = verify_history(installation.layout)
    with pytest.raises(IntegrityProblem) as caught:
        verify_active_journal(installation.layout, history)
    assert caught.value.category in {ACTIVE_SEGMENT_INVALID, SEGMENT_INVALID}


def test_duplicate_audit_id_between_archive_and_active_is_rejected(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[1], active=1)
    sealed_event = installation.segments[0][1][0]
    active_event = installation.active[0]
    rebuilt = AuditEvent.create(
        sequence=active_event.sequence,
        segment_id=active_event.segment_id,
        segment_sequence=1,
        previous_hash=sealed_event.record_hash,
        timestamp=parse_timestamp(active_event.timestamp),
        audit_id=sealed_event.audit_id,
        action=AuditAction.AUTH_LOGIN,
        outcome=AuditOutcome.SUCCESS,
        reason_code=AuditReasonCode.OK,
        source=AuditSource.SYSTEM,
    )
    write_events(installation.layout.journal, [rebuilt])
    history = verify_history(installation.layout)
    with pytest.raises(IntegrityProblem) as caught:
        verify_active_journal(installation.layout, history)
    assert caught.value.category == DUPLICATE_AUDIT_ID


def test_active_journal_with_two_segments_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1], active=0)
    first = installation.manifest[0].last_record_hash
    events = build_events([("2026-11-002", 1), ("2026-11-003", 1)])
    rebuilt = _rebuild(
        events, previous_hash=first, first_sequence=2
    )
    write_events(installation.layout.journal, rebuilt)
    history = verify_history(installation.layout)
    with pytest.raises(IntegrityProblem) as caught:
        verify_active_journal(installation.layout, history)
    assert caught.value.category == SEGMENT_INVALID


def test_active_journal_torn_tail_is_rejected(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1], active=2)
    raw = installation.layout.journal.read_bytes().rstrip(b"\n")
    installation.layout.journal.write_bytes(raw)
    history = verify_history(installation.layout)
    with pytest.raises(IntegrityProblem) as caught:
        verify_active_journal(installation.layout, history)
    assert caught.value.category == "TORN_TAIL"


def test_verify_declared_archive_rejects_a_retention_entry(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[1], retention=[0])
    seal, decision = installation.manifest
    with pytest.raises(IntegrityProblem):
        verify_declared_archive(decision, installation.layout)


def test_timestamp_month_is_never_compared_with_segment_month(
    tmp_path: Path,
) -> None:
    events = build_events([("2026-11-001", 1)])
    installation = install_events(tmp_path, segments=[("2026-11-001", 1)])
    assert installation.segments[0][1][0].timestamp.startswith("2026-11-16")
    verify_history(installation.layout)
    assert events[0].segment_id.startswith("2026-11")
    assert timestamp_for(1) == events[0].timestamp


def test_base_timestamp_constant_is_the_documented_form() -> None:
    assert BASE_TIMESTAMP.endswith("Z")
    assert len(BASE_TIMESTAMP) == 27
