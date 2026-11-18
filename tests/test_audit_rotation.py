"""Tests for operator rotation, crash recovery and retention (P5.5.7 C/D/G).

Every installation is synthetic and lives under ``tmp_path``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import src.audit_rotation as rotation_module
from src.audit_archive import (
    ACTIVE_SEGMENT_MISSING,
    ARCHIVE_MISSING,
    HistoryState,
    verify_active_journal,
    verify_history,
)
from src.audit_layout import AuditLayout, next_segment_id
from src.audit_locking import JournalLease, LayoutLock, LockContendedError
from src.audit_manifest import GENESIS_MANIFEST_HASH
from src.audit_rotation import (
    RotationContendedError,
    RotationIntegrityError,
    RotationOperations,
    RotationRecoveryRequiredError,
    RotationUnavailableError,
)
from src.audit_schema import (
    AuditAction,
    AuditEvent,
    AuditOutcome,
    AuditReasonCode,
    AuditSource,
    parse_timestamp,
)
from src.audit_writer import AuditJournalWriter
from tests.audit_support import (
    audit_id,
    build_events,
    install,
    install_events,
    seal_entry,
    write_events,
    write_manifest,
)

FIXED = parse_timestamp("2026-11-20T09:00:00.000000Z")


def _clock():
    return FIXED


def _operations(root: Path) -> RotationOperations:
    return RotationOperations(
        AuditLayout.for_directory(root / "audit"), clock=_clock
    )


def _journal_only(root: Path, count: int = 3) -> None:
    install_events(
        root,
        segments=[],
        active=(f"2026-11-001", count),
        manifest=False,
    )


def _fingerprint(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".rotation.lock"
    }


# ---------------------------------------------------------------------------
# Successful rotation
# ---------------------------------------------------------------------------


def test_rotation_seals_the_active_journal(tmp_path: Path) -> None:
    _journal_only(tmp_path, 3)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    original = layout.journal.read_bytes()

    plan = _operations(tmp_path).rotate()

    assert plan.segment_id == "2026-11-001"
    assert plan.record_count == 3
    assert plan.first_global_sequence == 1
    assert plan.last_global_sequence == 3
    assert plan.archive_reference == "archive/audit-1-3.jsonl"
    assert plan.next_segment_id == "2026-11-002"

    assert not layout.journal.read_bytes()
    archive = layout.archive_path(1, 3)
    assert archive.read_bytes() == original
    assert layout.manifest.is_file()

    history = verify_history(layout)
    assert len(history.manifest_entries) == 1
    assert history.segments[0].record_count == 3
    active = verify_active_journal(layout, history)
    assert active.is_empty


def test_rotation_records_the_exact_anchors(tmp_path: Path) -> None:
    _journal_only(tmp_path, 2)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    events = build_events([("2026-11-001", 2)])
    history = verify_history(layout)
    _operations(tmp_path).rotate()
    history = verify_history(layout)
    entry = history.manifest_entries[0]
    assert entry.first_previous_hash == "0" * 64
    assert entry.last_record_hash == events[-1].record_hash
    assert entry.segment_file == "archive/audit-1-2.jsonl"


def test_writer_continues_the_chain_after_rotation(tmp_path: Path) -> None:
    _journal_only(tmp_path, 2)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    plan = _operations(tmp_path).rotate()
    writer = AuditJournalWriter(layout.journal, clock=_clock)
    event = writer.append(
        action=AuditAction.AUTH_LOGIN,
        outcome=AuditOutcome.SUCCESS,
        reason_code=AuditReasonCode.OK,
        source=AuditSource.SYSTEM,
        timestamp=FIXED,
        audit_id=audit_id(99),
    )
    assert event.sequence == 3
    assert event.previous_hash == plan.last_record_hash
    assert event.segment_id == "2026-11-002"
    assert event.segment_sequence == 1


def test_two_rotations_in_one_month(tmp_path: Path) -> None:
    _journal_only(tmp_path, 1)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    operations = _operations(tmp_path)
    first = operations.rotate()
    assert first.next_segment_id == "2026-11-002"

    writer = AuditJournalWriter(layout.journal, clock=_clock)
    writer.append(
        action=AuditAction.AUTH_LOGIN,
        outcome=AuditOutcome.SUCCESS,
        reason_code=AuditReasonCode.OK,
        source=AuditSource.SYSTEM,
        timestamp=FIXED,
        audit_id=audit_id(77),
    )
    second = operations.rotate(dry_run=True)
    assert second.segment_id == "2026-11-002"
    assert second.next_segment_id == "2026-11-003"

    operations.rotate()
    history = verify_history(layout)
    assert history.sealed_segment_ids == ("2026-11-001", "2026-11-002")


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    _journal_only(tmp_path, 2)
    before = _fingerprint(tmp_path)
    plan = _operations(tmp_path).rotate(dry_run=True)
    assert plan.record_count == 2
    assert _fingerprint(tmp_path) == before
    layout = AuditLayout.for_directory(tmp_path / "audit")
    assert not layout.manifest.exists()
    assert not layout.archive_directory.joinpath("audit-1-2.jsonl").exists()


def test_dry_run_does_not_create_the_lock_file(tmp_path: Path) -> None:
    _journal_only(tmp_path, 1)
    _operations(tmp_path).rotate(dry_run=True)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    assert not layout.rotation_lock.exists()


def test_segment_allocation_is_deterministic(tmp_path: Path) -> None:
    _journal_only(tmp_path, 1)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    _operations(tmp_path).rotate()
    history = verify_history(layout)
    first = next_segment_id(history.sealed_segment_ids, moment=FIXED)
    second = next_segment_id(history.sealed_segment_ids, moment=FIXED)
    assert first == second == "2026-11-002"


# ---------------------------------------------------------------------------
# Refusals before any change
# ---------------------------------------------------------------------------


def test_empty_active_journal_refuses_rotation(tmp_path: Path) -> None:
    install_events(tmp_path, segments=[], active=None, manifest=False)
    with pytest.raises(RotationIntegrityError) as caught:
        _operations(tmp_path).rotate()
    assert caught.value.category == "NOTHING_TO_SEAL"


def test_multiple_segments_in_the_active_journal_refuse(tmp_path: Path) -> None:
    install_events(
        tmp_path,
        segments=[],
        active=("2026-11-001", 2),
        manifest=False,
    )
    layout = AuditLayout.for_directory(tmp_path / "audit")
    events = build_events([("2026-11-001", 1), ("2026-11-002", 1)])
    write_events(layout.journal, events)
    with pytest.raises(RotationIntegrityError):
        _operations(tmp_path).rotate()


def test_existing_archive_destination_refuses(tmp_path: Path) -> None:
    _journal_only(tmp_path, 2)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    layout.archive_directory.mkdir(exist_ok=True)
    placeholder = layout.archive_path(1, 2)
    placeholder.write_bytes(b"placeholder")
    with pytest.raises(RotationRecoveryRequiredError) as caught:
        _operations(tmp_path).rotate()
    assert caught.value.category in {"ARCHIVE_EXISTS", "ARCHIVE_UNREGISTERED"}
    assert layout.journal.exists()
    assert placeholder.read_bytes() == b"placeholder"


def test_ordinal_exhaustion_refuses(tmp_path: Path) -> None:
    install_events(
        tmp_path,
        segments=[("2026-11-001", 1)],
        active=("2026-11-999", 1),
    )
    with pytest.raises(RotationIntegrityError) as caught:
        _operations(tmp_path).rotate()
    assert caught.value.category == "ORDINAL_EXHAUSTED"


def test_unregistered_archive_refuses_rotation(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1], active=1)
    write_events(
        installation.layout.archive_path(3, 4),
        build_events([("2026-11-004", 2)]),
    )
    with pytest.raises(RotationRecoveryRequiredError):
        _operations(tmp_path).rotate()


def test_corrupt_active_journal_refuses_rotation(tmp_path: Path) -> None:
    _journal_only(tmp_path, 2)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    layout.journal.write_bytes(layout.journal.read_bytes().rstrip(b"\n"))
    with pytest.raises((RotationIntegrityError, RotationRecoveryRequiredError)):
        _operations(tmp_path).rotate()
    assert not layout.manifest.exists()


def test_layout_lock_contention_refuses_rotation(tmp_path: Path) -> None:
    _journal_only(tmp_path, 1)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    lock = LayoutLock(layout)
    lock.acquire()
    try:
        with pytest.raises(RotationContendedError):
            _operations(tmp_path).rotate()
    finally:
        lock.release()


def test_live_writer_lock_refuses_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_only(tmp_path, 1)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    before = _fingerprint(tmp_path)

    def held(self) -> None:  # noqa: ANN001
        raise LockContendedError("a writer holds the journal lock")

    monkeypatch.setattr(rotation_module.JournalLease, "acquire", held)
    with pytest.raises(RotationContendedError):
        _operations(tmp_path).rotate()
    assert _fingerprint(tmp_path) == before
    assert layout.journal.exists()
    assert not layout.manifest.exists()


def test_held_lease_blocks_rotation_without_changes(tmp_path: Path) -> None:
    """A real held byte lock is mandatory on Windows, so reads fail closed."""
    _journal_only(tmp_path, 1)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    before = _fingerprint(tmp_path)
    lease = JournalLease(layout.journal)
    lease.acquire()
    try:
        with pytest.raises(
            (RotationContendedError, RotationUnavailableError)
        ):
            _operations(tmp_path).rotate()
    finally:
        lease.release()
    assert _fingerprint(tmp_path) == before
    assert not layout.manifest.exists()


def test_missing_installation_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(RotationUnavailableError):
        RotationOperations(
            AuditLayout.for_directory(tmp_path / "missing"), clock=_clock
        ).rotate(dry_run=True)


# ---------------------------------------------------------------------------
# Fault injection during rotation
# ---------------------------------------------------------------------------


def test_rename_failure_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_only(tmp_path, 2)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    before = _fingerprint(tmp_path)

    def boom(source: Path, target: Path) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(rotation_module, "_replace_file", boom)
    with pytest.raises(RotationUnavailableError):
        _operations(tmp_path).rotate()
    assert _fingerprint(tmp_path) == before


def test_directory_fsync_unsupported_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_only(tmp_path, 1)
    monkeypatch.setattr(rotation_module, "_fsync_directory", lambda path: False)
    plan = _operations(tmp_path).rotate()
    assert plan.directory_fsync is False


def test_read_only_marking_failure_does_not_fail_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_only(tmp_path, 1)
    monkeypatch.setattr(rotation_module, "_mark_read_only", lambda path: False)
    plan = _operations(tmp_path).rotate()
    assert plan.read_only_marked is False


def test_seal_failure_requires_explicit_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_only(tmp_path, 2)
    layout = AuditLayout.for_directory(tmp_path / "audit")

    def boom(self, **kwargs):  # noqa: ANN001
        raise rotation_module.ManifestStoreError("write failed")

    monkeypatch.setattr(rotation_module.ManifestStore, "append", boom)
    with pytest.raises(RotationRecoveryRequiredError) as caught:
        _operations(tmp_path).rotate()
    assert caught.value.category == "SEAL_NOT_RECORDED"
    assert layout.archive_path(1, 2).exists()
    assert not layout.journal.exists()
    assert not layout.manifest.exists()


def test_active_creation_failure_requires_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_only(tmp_path, 1)
    layout = AuditLayout.for_directory(tmp_path / "audit")

    def boom(path: Path) -> None:
        raise OSError("cannot create")

    monkeypatch.setattr(rotation_module, "_create_empty_file", boom)
    with pytest.raises(RotationRecoveryRequiredError) as caught:
        _operations(tmp_path).rotate()
    assert caught.value.category == "ACTIVE_NOT_CREATED"
    assert layout.archive_path(1, 1).exists()
    with pytest.raises(Exception):
        verify_active_journal(layout, verify_history(layout))


# ---------------------------------------------------------------------------
# Recovery: interrupted rotation (archive present, seal missing)
# ---------------------------------------------------------------------------


def _interrupted(tmp_path: Path, count: int = 2) -> AuditLayout:
    """Rotate with the manifest append failing: the archive is left behind."""
    _journal_only(tmp_path, count)
    layout = AuditLayout.for_directory(tmp_path / "audit")

    original = rotation_module.ManifestStore.append

    def failing(self, **kwargs):  # noqa: ANN001
        raise rotation_module.ManifestStoreError("write failed")

    rotation_module.ManifestStore.append = failing
    try:
        with pytest.raises(RotationRecoveryRequiredError):
            _operations(tmp_path).rotate()
    finally:
        rotation_module.ManifestStore.append = original
    return layout


def test_register_existing_completes_an_interrupted_rotation(
    tmp_path: Path,
) -> None:
    layout = _interrupted(tmp_path, 2)
    entry = _operations(tmp_path).register_existing()
    assert entry.segment_id == "2026-11-001"
    assert entry.record_count == 2
    assert entry.manifest_sequence == 1
    history = verify_history(layout)
    assert history.sealed_segment_ids == ("2026-11-001",)


def test_register_existing_accepts_an_explicit_path(tmp_path: Path) -> None:
    layout = _interrupted(tmp_path, 1)
    entry = _operations(tmp_path).register_existing("archive/audit-1-1.jsonl")
    assert entry.segment_file == "archive/audit-1-1.jsonl"


def test_register_existing_is_ambiguous_with_two_candidates(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[1], active=1)
    write_events(
        installation.layout.archive_path(3, 3),
        build_events([("2026-11-003", 1)]),
    )
    write_events(
        installation.layout.archive_path(4, 4),
        build_events([("2026-11-004", 1)]),
    )
    with pytest.raises(RotationRecoveryRequiredError) as caught:
        _operations(tmp_path).register_existing()
    assert caught.value.category == "AMBIGUOUS_ARCHIVE"


def test_register_existing_refuses_a_torn_archive(tmp_path: Path) -> None:
    layout = _interrupted(tmp_path, 2)
    path = layout.archive_path(1, 2)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    with pytest.raises(RotationIntegrityError):
        _operations(tmp_path).register_existing()
    assert not layout.manifest.exists()


def test_register_existing_refuses_a_tampered_archive(tmp_path: Path) -> None:
    layout = _interrupted(tmp_path, 2)
    path = layout.archive_path(1, 2)
    events = build_events([("2026-11-001", 2)])
    tampered = replace(events[0], record_hash="c" * 64)
    write_events(path, [tampered, events[1]])
    with pytest.raises(RotationIntegrityError):
        _operations(tmp_path).register_existing()


def test_register_existing_refuses_a_second_registration(
    tmp_path: Path,
) -> None:
    layout = _interrupted(tmp_path, 1)
    _operations(tmp_path).register_existing()
    before = layout.manifest.read_bytes()
    with pytest.raises(RotationIntegrityError) as caught:
        _operations(tmp_path).register_existing("audit-1-1.jsonl")
    assert caught.value.category == "ARCHIVE_ALREADY_REGISTERED"
    assert layout.manifest.read_bytes() == before


def test_register_existing_refuses_an_already_sealed_segment(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[2], active=1)
    layout = installation.layout
    sealed_events = list(installation.segments[0][1])
    duplicate = []
    previous = sealed_events[-1].record_hash
    sequence = 4
    for local in range(1, 3):
        event = AuditEvent.create(
            sequence=sequence,
            segment_id=sealed_events[0].segment_id,
            segment_sequence=local,
            previous_hash=previous,
            timestamp=parse_timestamp("2026-11-17T00:00:00.000000Z"),
            audit_id=audit_id(100 + local),
            action=AuditAction.AUTH_LOGIN,
            outcome=AuditOutcome.SUCCESS,
            reason_code=AuditReasonCode.OK,
            source=AuditSource.SYSTEM,
        )
        duplicate.append(event)
        previous = event.record_hash
        sequence += 1
    write_events(layout.archive_path(4, 5), duplicate)
    with pytest.raises(RotationIntegrityError):
        _operations(tmp_path).register_existing()


def test_register_existing_refuses_an_inconsistent_active_journal(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[2], active=1)
    layout = installation.layout
    events = build_events([("2026-11-003", 2)])
    rebuilt = []
    prior = installation.manifest[0].last_record_hash
    sequence = 4
    for local, event in enumerate(events, start=1):
        replacement = AuditEvent.create(
            sequence=sequence,
            segment_id="2026-11-003",
            segment_sequence=local,
            previous_hash=prior,
            timestamp=parse_timestamp(event.timestamp),
            audit_id=audit_id(200 + local),
            action=AuditAction.AUTH_LOGIN,
            outcome=AuditOutcome.SUCCESS,
            reason_code=AuditReasonCode.OK,
            source=AuditSource.SYSTEM,
        )
        rebuilt.append(replacement)
        prior = replacement.record_hash
        sequence += 1
    write_events(layout.archive_path(4, 5), rebuilt)
    # The existing active journal already owns sequence 4, so registering an
    # archive that also claims 4 is contradictory.
    before = layout.manifest.read_bytes()
    with pytest.raises(RotationIntegrityError):
        _operations(tmp_path).register_existing()
    assert layout.manifest.read_bytes() == before
    assert len(
        verify_history(layout, allow_unregistered=True).manifest_entries
    ) == 1


# ---------------------------------------------------------------------------
# Recovery: sealed segment with a missing active journal
# ---------------------------------------------------------------------------


def test_create_active_after_a_seal(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2], active=0)
    layout = installation.layout
    layout.journal.unlink()
    path, next_id = _operations(tmp_path).create_active()
    assert path == layout.journal
    assert path.read_bytes() == b""
    assert next_id == "2026-11-002"
    history = verify_history(layout)
    assert verify_active_journal(layout, history).is_empty


def test_create_active_refuses_to_replace_an_existing_journal(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[2], active=1)
    with pytest.raises(RotationIntegrityError) as caught:
        _operations(tmp_path).create_active()
    assert caught.value.category == "ACTIVE_EXISTS"
    assert installation.layout.journal.read_bytes()


def test_create_active_requires_sealed_history(tmp_path: Path) -> None:
    install_events(tmp_path, segments=[], active=None, manifest=False)
    with pytest.raises(RotationIntegrityError) as caught:
        _operations(tmp_path).create_active()
    assert caught.value.category == "NO_SEALED_HISTORY"


def test_writer_derives_the_same_pending_segment_after_recovery(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[1], active=0)
    layout = installation.layout
    layout.journal.unlink()
    _operations(tmp_path).create_active()
    first = AuditJournalWriter(layout.journal, clock=_clock)
    from src.audit_schema import (
        AuditAction,
        AuditOutcome,
        AuditReasonCode,
        AuditSource,
    )

    event = first.append(
        action=AuditAction.AUTH_LOGIN,
        outcome=AuditOutcome.SUCCESS,
        reason_code=AuditReasonCode.OK,
        source=AuditSource.SYSTEM,
        timestamp=FIXED,
        audit_id=audit_id(11),
    )
    assert event.segment_id == "2026-11-002"
    assert event.sequence == 2


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_record_retention_appends_a_decision(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2])
    layout = installation.layout
    archive_bytes = installation.archive_path(0).read_bytes()

    entry = _operations(tmp_path).record_retention("2026-11-001")

    assert entry.disposition.value == "archived_offsite"
    assert entry.segment_file is None
    assert entry.record_count == 2
    assert entry.first_global_sequence == 1
    assert installation.archive_path(0).read_bytes() == archive_bytes
    history = verify_history(layout)
    assert history.offsite_segment_ids == ("2026-11-001",)
    assert len(history.manifest_entries) == 2


def test_record_retention_refuses_unknown_segment(tmp_path: Path) -> None:
    install(tmp_path, sealed=[1])
    with pytest.raises(RotationIntegrityError) as caught:
        _operations(tmp_path).record_retention("2026-11-002")
    assert caught.value.category == "UNKNOWN_SEGMENT"


def test_record_retention_refuses_a_duplicate(tmp_path: Path) -> None:
    install(tmp_path, sealed=[1], retention=[0])
    with pytest.raises(RotationIntegrityError) as caught:
        _operations(tmp_path).record_retention("2026-11-001")
    assert caught.value.category == "DUPLICATE_RETENTION_DECISION"


def test_record_retention_refuses_when_the_archive_is_gone(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[1])
    installation.archive_path(0).unlink()
    with pytest.raises(RotationIntegrityError) as caught:
        _operations(tmp_path).record_retention("2026-11-001")
    assert caught.value.category == ARCHIVE_MISSING


def test_retention_does_not_move_or_delete(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[1])
    before = sorted(path.name for path in installation.archive_directory.iterdir())
    _operations(tmp_path).record_retention("2026-11-001")
    after = sorted(path.name for path in installation.archive_directory.iterdir())
    assert before == after


def test_missing_active_after_seal_is_reported_as_recovery(
    tmp_path: Path,
) -> None:
    installation = install(tmp_path, sealed=[1])
    installation.layout.journal.unlink()
    history = verify_history(installation.layout)
    with pytest.raises(Exception) as caught:
        verify_active_journal(installation.layout, history)
    assert getattr(caught.value, "category", None) == ACTIVE_SEGMENT_MISSING


def test_history_state_helpers_are_consistent(tmp_path: Path) -> None:
    installation = install(tmp_path, sealed=[2, 1])
    history: HistoryState = verify_history(installation.layout)
    assert history.expected_next_sequence == 4
    assert history.expected_previous_hash == history.segments[-1].last_record_hash
    assert history.total_records == 3
    assert history.anchor_only is False


def test_seal_entry_helper_matches_rotation_output(tmp_path: Path) -> None:
    _journal_only(tmp_path, 2)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    _operations(tmp_path).rotate()
    recorded = verify_history(layout).manifest_entries[0]
    events = build_events([("2026-11-001", 2)])
    expected = seal_entry(
        1,
        GENESIS_MANIFEST_HASH,
        events,
        sealed_at="2026-11-20T09:00:00.000000Z",
    )
    assert recorded.entry_hash == expected.entry_hash


def test_manifest_written_by_rotation_is_canonical(tmp_path: Path) -> None:
    _journal_only(tmp_path, 1)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    _operations(tmp_path).rotate()
    raw = layout.manifest.read_bytes()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    entry = verify_history(layout).manifest_entries[0]
    assert raw == entry.to_json_line().encode("utf-8") + b"\n"


def test_rotation_does_not_write_an_event(tmp_path: Path) -> None:
    _journal_only(tmp_path, 2)
    layout = AuditLayout.for_directory(tmp_path / "audit")
    _operations(tmp_path).rotate()
    history = verify_history(layout)
    assert history.total_records == 2
    assert not layout.journal.read_bytes()
