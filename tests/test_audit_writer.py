"""P5.5.3 audit journal writer tests (offline, temporary directories only).

Every test uses ``tmp_path``; the real ``data/audit`` directory is never
touched. Filesystem failure points are injected through the writer's
module-level seams (``_write_line``, ``_flush_handle``, ``_fsync_file``,
``_lock_handle``, ``_open_append_handle``), so no test damages real data or
depends on timing sleeps.

Nothing here connects to PostgreSQL, reads ``.env``, or handles employee,
camera, embedding or biometric data.
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import src.audit_writer as writer_module
from src.audit_schema import (
    GENESIS_PREVIOUS_HASH,
    AuditAction,
    AuditEvent,
    AuditHashMismatchError,
    AuditOutcome,
    AuditReasonCode,
    AuditSchemaError,
    AuditSource,
    AuditTargetType,
    canonical_json_bytes,
    compute_record_hash,
    verify_chain,
)
from src.audit_writer import (
    AuditJournalContendedError,
    AuditJournalCorruptError,
    AuditJournalIntegrityError,
    AuditJournalPoisonedError,
    AuditJournalUnavailableError,
    AuditJournalWriter,
    WriterState,
)
from src.user import Role

FIXED_TS = datetime(2026, 11, 16, 9, 30, 0, 123456, tzinfo=UTC)
LATER_TS = FIXED_TS + timedelta(seconds=1)
INTENT_ID = "0a0e1f2a-3b4c-4d5e-8f6a-7b8c9d0e1f20"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "audit.jsonl"


def open_writer(tmp_path: Path, *, clock=None) -> AuditJournalWriter:
    return AuditJournalWriter(
        journal_path(tmp_path),
        clock=clock if clock is not None else (lambda: FIXED_TS),
    )


def append_mutation(writer: AuditJournalWriter, **overrides: object) -> AuditEvent:
    fields: dict[str, object] = {
        "action": AuditAction.EMPLOYEE_CREATE,
        "outcome": AuditOutcome.SUCCESS,
        "reason_code": AuditReasonCode.OK,
        "source": AuditSource.API,
        "actor_user_id": "user-1",
        "actor_role": Role.ADMIN,
        "target_type": AuditTargetType.EMPLOYEE,
        "target_id": "E1",
        "correlation_id": INTENT_ID,
        "timestamp": FIXED_TS,
        "metadata": {"endpoint": "/api/v1/employees", "method": "POST"},
    }
    fields.update(overrides)
    return writer.append(**fields)  # type: ignore[arg-type]


def append_denied_login(writer: AuditJournalWriter, **overrides: object) -> AuditEvent:
    fields: dict[str, object] = {
        "action": AuditAction.AUTH_LOGIN,
        "outcome": AuditOutcome.DENIED,
        "reason_code": AuditReasonCode.INVALID_CREDENTIALS,
        "source": AuditSource.API,
        "target_type": AuditTargetType.ACCOUNT,
        "timestamp": FIXED_TS,
    }
    fields.update(overrides)
    return writer.append(**fields)  # type: ignore[arg-type]


def build_events(segment_ids: list[str], *, start: datetime = FIXED_TS) -> list[AuditEvent]:
    """Build a valid event chain whose records use ``segment_ids`` in order."""
    events: list[AuditEvent] = []
    previous = GENESIS_PREVIOUS_HASH
    counts: dict[str, int] = {}
    for position, segment_id in enumerate(segment_ids, start=1):
        counts[segment_id] = counts.get(segment_id, 0) + 1
        event = AuditEvent.create(
            sequence=position,
            segment_id=segment_id,
            segment_sequence=counts[segment_id],
            action=AuditAction.AUTH_LOGIN,
            outcome=AuditOutcome.DENIED,
            reason_code=AuditReasonCode.INVALID_CREDENTIALS,
            source=AuditSource.API,
            previous_hash=previous,
            timestamp=start + timedelta(microseconds=position),
            target_type=AuditTargetType.ACCOUNT,
        )
        events.append(event)
        previous = event.record_hash
    return events


def write_journal(path: Path, events: list[AuditEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            canonical_json_bytes(event.as_body()) + b"\n" for event in events
        )
    )


def tampered_body(event: AuditEvent) -> dict[str, object]:
    body = event.as_body()
    body["target_id"] = "E999"
    return body


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def test_missing_journal_with_empty_directory_is_initialized(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    assert not path.exists()
    writer = open_writer(tmp_path)
    assert writer.state is WriterState.READY
    assert path.exists()
    assert path.read_bytes() == b""
    assert writer.last_sequence == 0
    assert writer.last_record_hash == GENESIS_PREVIOUS_HASH
    assert writer.active_segment_id is None


def test_missing_directory_is_created(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    assert writer.journal_path.parent.is_dir()


def test_journal_created_between_check_and_open_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A racing writer must be verified, never treated as genesis."""
    path = journal_path(tmp_path)
    events = build_events(["2026-11-001"])
    real_open = writer_module._open_append_handle

    def racing_open(handle_path: Path):
        write_journal(handle_path, events)
        return real_open(handle_path)

    monkeypatch.setattr(writer_module, "_open_append_handle", racing_open)
    writer = open_writer(tmp_path)
    assert writer.state is WriterState.READY
    assert writer.last_sequence == 1
    assert writer.active_segment_id == "2026-11-001"


def test_existing_valid_journal_loads(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    events = build_events(["2026-11-001"])
    write_journal(path, events)
    writer = open_writer(tmp_path)
    assert writer.state is WriterState.READY
    assert writer.last_sequence == 1
    assert writer.last_record_hash == events[0].record_hash
    assert writer.active_segment_id == "2026-11-001"


def test_missing_journal_with_manifest_artifact_refuses(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    path.parent.mkdir(parents=True)
    (path.parent / "manifest.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(AuditJournalIntegrityError):
        open_writer(tmp_path)
    assert not path.exists()


def test_missing_journal_with_archived_segment_refuses(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    archive = path.parent / "archive"
    archive.mkdir(parents=True)
    (archive / "audit-1-10.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(AuditJournalIntegrityError):
        open_writer(tmp_path)
    assert not path.exists()


def test_missing_journal_with_empty_archive_directory_is_allowed(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    (path.parent / "archive").mkdir(parents=True)
    writer = open_writer(tmp_path)
    assert writer.state is WriterState.READY


def test_archive_path_that_is_not_a_directory_refuses(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    path.parent.mkdir(parents=True)
    (path.parent / "archive").write_text("not a directory", encoding="utf-8")
    with pytest.raises(AuditJournalIntegrityError):
        open_writer(tmp_path)


def test_existing_journal_with_sealed_manifest_refuses(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    write_journal(path, build_events(["2026-11-001"]))
    (path.parent / "manifest.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(AuditJournalIntegrityError):
        open_writer(tmp_path)


def test_audit_directory_path_that_is_a_file_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "audit").write_text("not a directory", encoding="utf-8")
    with pytest.raises(AuditJournalUnavailableError):
        open_writer(tmp_path)


def test_journal_path_that_is_a_directory_fails_closed(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    path.mkdir(parents=True)
    with pytest.raises(AuditJournalUnavailableError):
        open_writer(tmp_path)


def test_directory_creation_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(directory: Path) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(writer_module, "_ensure_directory", boom)
    with pytest.raises(AuditJournalUnavailableError):
        open_writer(tmp_path)


def test_verification_open_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = journal_path(tmp_path)
    write_journal(path, build_events(["2026-11-001"]))

    def boom(handle_path: Path):
        raise PermissionError("denied")

    monkeypatch.setattr(writer_module, "_open_for_verification", boom)
    with pytest.raises(AuditJournalUnavailableError):
        open_writer(tmp_path)


def test_empty_existing_journal_is_a_valid_genesis_state(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    writer = open_writer(tmp_path)
    assert writer.state is WriterState.READY
    assert writer.last_sequence == 0
    event = append_denied_login(writer)
    assert event.sequence == 1
    assert event.previous_hash == GENESIS_PREVIOUS_HASH


# ---------------------------------------------------------------------------
# Startup verification
# ---------------------------------------------------------------------------


def test_valid_multi_record_journal_loads(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    events = build_events(["2026-11-001", "2026-11-001", "2026-11-001"])
    write_journal(path, events)
    writer = open_writer(tmp_path)
    assert writer.last_sequence == 3
    assert writer.last_record_hash == events[-1].record_hash


def test_valid_chain_across_segments_loads(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    events = build_events(
        ["2026-11-001", "2026-11-001", "2026-12-001", "2026-12-001"]
    )
    write_journal(path, events)
    writer = open_writer(tmp_path)
    assert writer.active_segment_id == "2026-12-001"
    event = append_denied_login(writer, timestamp=FIXED_TS + timedelta(days=30))
    assert event.segment_id == "2026-12-001"
    assert event.segment_sequence == 3
    assert event.sequence == 5


def test_invalid_json_is_reported_with_line(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    write_journal(path, build_events(["2026-11-001"]))
    path.write_bytes(path.read_bytes() + b"{not json}\n")
    with pytest.raises(AuditJournalCorruptError) as caught:
        open_writer(tmp_path)
    assert caught.value.line == 2


def test_missing_required_field_is_corrupt(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    body = build_events(["2026-11-001"])[0].as_body()
    del body["outcome"]
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json_bytes(body) + b"\n")
    with pytest.raises(AuditJournalCorruptError):
        open_writer(tmp_path)


def test_invalid_schema_version_is_corrupt(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    body = build_events(["2026-11-001"])[0].as_body()
    body["schema_version"] = 2
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json_bytes(body) + b"\n")
    with pytest.raises(AuditJournalCorruptError):
        open_writer(tmp_path)


def test_invalid_utf8_is_corrupt(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00\n")
    with pytest.raises(AuditJournalCorruptError):
        open_writer(tmp_path)


def test_empty_line_is_corrupt(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\n")
    with pytest.raises(AuditJournalCorruptError):
        open_writer(tmp_path)


def test_tampered_event_content_is_detected(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    event = build_events(["2026-11-001"])[0]
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json_bytes(tampered_body(event)) + b"\n")
    with pytest.raises(AuditJournalIntegrityError):
        open_writer(tmp_path)


def test_incorrect_record_hash_is_detected(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    event = build_events(["2026-11-001"])[0]
    body = event.as_body()
    body["record_hash"] = "0" * 64
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json_bytes(body) + b"\n")
    with pytest.raises(AuditJournalIntegrityError):
        open_writer(tmp_path)


def test_incorrect_previous_hash_is_detected(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    events = build_events(["2026-11-001", "2026-11-001"])
    body = events[1].as_body()
    body["previous_hash"] = "a" * 64
    path.parent.mkdir(parents=True)
    path.write_bytes(
        canonical_json_bytes(events[0].as_body())
        + b"\n"
        + canonical_json_bytes(body)
        + b"\n"
    )
    with pytest.raises(AuditJournalIntegrityError):
        open_writer(tmp_path)


def test_global_sequence_gap_is_detected(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    events = build_events(["2026-11-001", "2026-11-001"])
    body = events[1].as_body()
    body["sequence"] = 3
    path.parent.mkdir(parents=True)
    path.write_bytes(
        canonical_json_bytes(events[0].as_body())
        + b"\n"
        + canonical_json_bytes(body)
        + b"\n"
    )
    with pytest.raises(AuditJournalIntegrityError):
        open_writer(tmp_path)


def test_segment_sequence_gap_is_detected(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    events = build_events(["2026-11-001", "2026-11-001", "2026-11-001"])
    body = events[2].as_body()
    body["segment_sequence"] = 2
    path.parent.mkdir(parents=True)
    lines = [
        canonical_json_bytes(events[0].as_body()),
        canonical_json_bytes(events[1].as_body()),
        canonical_json_bytes(body),
    ]
    path.write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(AuditJournalIntegrityError):
        open_writer(tmp_path)


def test_repeated_segment_id_is_detected(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    events = build_events(["2026-11-001", "2026-11-002", "2026-11-001"])
    write_journal(path, events)
    with pytest.raises(AuditJournalIntegrityError):
        open_writer(tmp_path)


def test_incomplete_final_line_is_corrupt(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    write_journal(path, build_events(["2026-11-001"]))
    path.write_bytes(path.read_bytes() + b'{"schema_version":1')
    with pytest.raises(AuditJournalCorruptError) as caught:
        open_writer(tmp_path)
    assert caught.value.line == 2


def test_duplicate_audit_id_is_detected(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    events = build_events(["2026-11-001", "2026-11-001"])
    second = events[1].as_body()
    second["audit_id"] = events[0].audit_id
    # Recompute the digest so only the duplicate identity is wrong.
    second["record_hash"] = compute_record_hash(second)
    path.parent.mkdir(parents=True)
    path.write_bytes(
        canonical_json_bytes(events[0].as_body())
        + b"\n"
        + canonical_json_bytes(second)
        + b"\n"
    )
    with pytest.raises(AuditJournalIntegrityError, match="duplicate audit id"):
        open_writer(tmp_path)


def test_decreasing_timestamps_in_history_are_detected(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    events = build_events(["2026-11-001", "2026-11-001"])
    body = events[1].as_body()
    body["timestamp"] = "2026-11-16T09:29:00.000000Z"
    body["record_hash"] = compute_record_hash(body)
    path.parent.mkdir(parents=True)
    path.write_bytes(
        canonical_json_bytes(events[0].as_body())
        + b"\n"
        + canonical_json_bytes(body)
        + b"\n"
    )
    with pytest.raises(AuditJournalIntegrityError, match="non-decreasing"):
        open_writer(tmp_path)


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


def test_first_append_is_genesis_and_allocates_segment(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    event = append_mutation(writer)
    assert event.sequence == 1
    assert event.segment_id == "2026-11-001"
    assert event.segment_sequence == 1
    assert event.previous_hash == GENESIS_PREVIOUS_HASH
    assert event.record_hash == compute_record_hash(event)
    assert writer.last_sequence == 1
    assert writer.last_record_hash == event.record_hash
    assert writer.active_segment_id == "2026-11-001"


def test_subsequent_append_continues_sequence_and_segment(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    first = append_mutation(writer)
    second = append_denied_login(writer, timestamp=LATER_TS)
    assert second.sequence == 2
    assert second.segment_sequence == 2
    assert second.segment_id == first.segment_id
    assert second.previous_hash == first.record_hash
    assert writer.last_sequence == 2


def test_appended_line_is_canonical_and_newline_terminated(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    event = append_mutation(writer)
    raw = journal_path(tmp_path).read_bytes()
    assert raw == event.to_json_line().encode("utf-8") + b"\n"
    assert raw.count(b"\n") == 1
    assert b", " not in raw and b": " not in raw
    assert json.loads(raw.decode("utf-8")) == event.as_body()


def test_one_newline_per_record(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    for index in range(3):
        append_denied_login(writer, timestamp=FIXED_TS + timedelta(seconds=index))
    raw = journal_path(tmp_path).read_bytes()
    assert raw.count(b"\n") == 3
    assert raw.endswith(b"\n")


def test_state_is_updated_only_after_success(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    first = append_mutation(writer)
    assert writer.last_sequence == 1
    assert writer.last_record_hash == first.record_hash


def test_reload_continues_the_chain(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    first = append_mutation(writer)
    reopened = open_writer(tmp_path)
    second = append_denied_login(reopened, timestamp=LATER_TS)
    assert second.sequence == 2
    assert second.previous_hash == first.record_hash


def test_duplicate_audit_id_is_rejected(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    first = append_mutation(writer)
    before = journal_path(tmp_path).read_bytes()
    with pytest.raises(AuditSchemaError, match="audit_id"):
        append_mutation(
            writer,
            timestamp=LATER_TS,
            audit_id=first.audit_id,
        )
    assert journal_path(tmp_path).read_bytes() == before
    assert writer.state is WriterState.READY
    assert writer.last_sequence == 1


def test_backwards_timestamp_is_rejected(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    append_mutation(writer, timestamp=LATER_TS)
    before = journal_path(tmp_path).read_bytes()
    with pytest.raises(AuditSchemaError, match="timestamp"):
        append_denied_login(writer, timestamp=FIXED_TS)
    assert journal_path(tmp_path).read_bytes() == before
    assert writer.state is WriterState.READY


def test_equal_timestamp_is_allowed(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    append_mutation(writer)
    second = append_denied_login(writer, timestamp=FIXED_TS)
    assert second.sequence == 2


def test_invalid_event_is_rejected_before_writing(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    with pytest.raises(AuditSchemaError):
        append_mutation(writer, outcome=AuditOutcome.ERROR)
    assert journal_path(tmp_path).read_bytes() == b""
    assert writer.state is WriterState.READY


def test_timestamp_month_may_differ_from_segment_month(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    write_journal(path, build_events(["2026-11-001"]))
    writer = open_writer(tmp_path, clock=lambda: datetime(2026, 12, 2, tzinfo=UTC))
    later = datetime(2026, 12, 2, 8, 0, tzinfo=UTC)
    event = append_denied_login(writer, timestamp=later)
    assert event.segment_id == "2026-11-001"
    assert event.timestamp.startswith("2026-12-02")


def test_new_journal_segment_uses_allocation_month(tmp_path: Path) -> None:
    writer = open_writer(
        tmp_path, clock=lambda: datetime(2027, 1, 3, 10, 0, tzinfo=UTC)
    )
    event = append_denied_login(
        writer, timestamp=datetime(2027, 1, 3, 10, 0, tzinfo=UTC)
    )
    assert event.segment_id == "2027-01-001"


def test_invalid_audit_id_is_rejected_before_writing(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    with pytest.raises(AuditSchemaError):
        append_denied_login(writer, audit_id="not-a-uuid")
    assert journal_path(tmp_path).read_bytes() == b""


# ---------------------------------------------------------------------------
# Failure and poisoning
# ---------------------------------------------------------------------------


def test_open_failure_does_not_poison(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = open_writer(tmp_path)
    monkeypatch.setattr(
        writer_module,
        "_open_append_handle",
        lambda path: (_ for _ in ()).throw(PermissionError("denied")),
    )
    with pytest.raises(AuditJournalUnavailableError):
        append_mutation(writer)
    assert writer.state is WriterState.READY
    assert journal_path(tmp_path).read_bytes() == b""


def test_lock_contention_fails_closed_without_poisoning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)

    def busy(handle) -> None:
        raise BlockingIOError("locked")

    monkeypatch.setattr(writer_module, "_lock_handle", busy)
    with pytest.raises(AuditJournalContendedError):
        append_mutation(writer)
    assert writer.state is WriterState.READY
    assert journal_path(tmp_path).read_bytes() == b""


def test_partial_write_poisons_the_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)

    def broken_write(handle, payload: bytes) -> int:
        handle.write(payload[:10])
        raise OSError("disk full")

    monkeypatch.setattr(writer_module, "_write_line", broken_write)
    with pytest.raises(AuditJournalUnavailableError):
        append_mutation(writer)
    assert writer.state is WriterState.POISONED
    assert writer.last_sequence == 0

    monkeypatch.undo()
    with pytest.raises(AuditJournalPoisonedError):
        append_mutation(writer)
    # No retry, no truncation: the partial bytes are still there.
    assert 0 < journal_path(tmp_path).stat().st_size < 100


def test_short_write_poisons_the_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)

    def short_write(handle, payload: bytes) -> int:
        handle.write(payload[:5])
        return 5

    monkeypatch.setattr(writer_module, "_write_line", short_write)
    with pytest.raises(AuditJournalUnavailableError):
        append_mutation(writer)
    assert writer.state is WriterState.POISONED


def test_flush_failure_poisons_the_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)

    def broken_flush(handle) -> None:
        raise OSError("flush failed")

    monkeypatch.setattr(writer_module, "_flush_handle", broken_flush)
    with pytest.raises(AuditJournalUnavailableError):
        append_mutation(writer)
    assert writer.state is WriterState.POISONED
    assert writer.last_sequence == 0


def test_fsync_failure_poisons_and_never_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)

    def broken_fsync(handle) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(writer_module, "_fsync_file", broken_fsync)
    with pytest.raises(AuditJournalUnavailableError):
        append_mutation(writer)
    assert writer.state is WriterState.POISONED
    assert writer.last_sequence == 0

    monkeypatch.undo()
    # The line may or may not be durable; it is never removed or rewritten.
    raw = journal_path(tmp_path).read_bytes()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    # A fresh writer verifies whatever is actually there (one record).
    reopened = open_writer(tmp_path)
    assert reopened.last_sequence == 1


def test_poisoned_writer_refuses_every_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)
    append_mutation(writer)

    def broken_fsync(handle) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(writer_module, "_fsync_file", broken_fsync)
    with pytest.raises(AuditJournalUnavailableError):
        append_denied_login(writer, timestamp=LATER_TS)
    monkeypatch.undo()
    assert writer.state is WriterState.POISONED

    size_after_failure = journal_path(tmp_path).stat().st_size
    for _ in range(3):
        with pytest.raises(AuditJournalPoisonedError):
            append_denied_login(writer, timestamp=LATER_TS)
    assert journal_path(tmp_path).stat().st_size == size_after_failure


def test_recovery_requires_a_new_verified_writer(tmp_path: Path) -> None:
    path = journal_path(tmp_path)
    first = build_events(["2026-11-001"])[0]
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json_bytes(first.as_body()))
    with pytest.raises(AuditJournalCorruptError):
        open_writer(tmp_path)


def test_stale_writer_is_poisoned_by_an_external_append(tmp_path: Path) -> None:
    first = open_writer(tmp_path)
    append_mutation(first)
    second = open_writer(tmp_path)
    append_denied_login(second, timestamp=LATER_TS)
    with pytest.raises(AuditJournalContendedError):
        append_denied_login(first, timestamp=LATER_TS)
    assert first.state is WriterState.POISONED
    assert second.state is WriterState.READY
    # Exactly two records; no duplicate sequence was written.
    raw = journal_path(tmp_path).read_bytes()
    assert raw.count(b"\n") == 2


def test_lock_is_released_after_success(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    append_mutation(writer)
    handle = open(journal_path(tmp_path), "r+b")
    try:
        writer_module._lock_handle(handle)
        writer_module._unlock_handle(handle)
    finally:
        handle.close()


def test_runtime_error_after_partial_write_poisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)
    size_before = journal_path(tmp_path).stat().st_size

    def exploding_write(handle, payload: bytes) -> int:
        handle.write(payload[:10])
        raise RuntimeError("injected partial-write failure")

    monkeypatch.setattr(writer_module, "_write_line", exploding_write)
    with pytest.raises(AuditJournalUnavailableError) as excinfo:
        append_mutation(writer)
    assert "injected" not in str(excinfo.value)
    assert writer.state is WriterState.POISONED
    assert writer.last_sequence == 0
    assert journal_path(tmp_path).stat().st_size > size_before

    monkeypatch.undo()
    with pytest.raises(AuditJournalPoisonedError):
        append_mutation(writer)
    assert journal_path(tmp_path).stat().st_size > 0


def test_runtime_error_during_flush_poisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)

    def broken_flush(handle) -> None:
        raise RuntimeError("injected flush failure")

    monkeypatch.setattr(writer_module, "_flush_handle", broken_flush)
    with pytest.raises(AuditJournalUnavailableError) as excinfo:
        append_mutation(writer)
    assert "injected" not in str(excinfo.value)
    assert writer.state is WriterState.POISONED
    assert writer.last_sequence == 0

    monkeypatch.undo()
    with pytest.raises(AuditJournalPoisonedError):
        append_mutation(writer)


def test_runtime_error_during_fsync_poisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)

    def broken_fsync(handle) -> None:
        raise RuntimeError("injected fsync failure")

    monkeypatch.setattr(writer_module, "_fsync_file", broken_fsync)
    with pytest.raises(AuditJournalUnavailableError) as excinfo:
        append_mutation(writer)
    assert "injected" not in str(excinfo.value)
    assert writer.state is WriterState.POISONED
    assert writer.last_sequence == 0

    monkeypatch.undo()
    # The bytes are never removed or rewritten by the writer.
    raw = journal_path(tmp_path).read_bytes()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1


def test_unexpected_post_write_confirmation_failure_poisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)
    real_stat_handle = writer_module._stat_handle
    calls = {"count": 0}

    def flaky_stat(handle):
        calls["count"] += 1
        if calls["count"] == 1:  # the pre-write check succeeds
            return real_stat_handle(handle)
        raise RuntimeError("injected stat failure after fsync")

    monkeypatch.setattr(writer_module, "_stat_handle", flaky_stat)
    with pytest.raises(AuditJournalUnavailableError) as excinfo:
        append_mutation(writer)
    assert "injected" not in str(excinfo.value)
    assert calls["count"] >= 2
    assert writer.state is WriterState.POISONED
    assert writer.last_sequence == 0

    monkeypatch.undo()
    with pytest.raises(AuditJournalPoisonedError):
        append_mutation(writer)


def test_journal_replacement_before_append_poisons(
    tmp_path: Path,
) -> None:
    writer = open_writer(tmp_path)
    path = journal_path(tmp_path)
    replacement = tmp_path / "audit" / "replacement.jsonl"
    replacement.write_bytes(b"")
    # Same size, different file: only identity can detect this replacement.
    os.replace(replacement, path)

    with pytest.raises(AuditJournalContendedError):
        append_mutation(writer)
    assert writer.state is WriterState.POISONED
    assert writer.last_sequence == 0
    assert path.read_bytes() == b""


def test_journal_replacement_during_append_poisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the path naming another file after fsync.

    Windows holds the append handle without FILE_SHARE_DELETE, so a real
    ``os.replace`` during the append is refused by the OS; the check is
    exercised with a real second file's stat supplied through the ``_stat_path``
    seam, which is exactly what the writer compares.
    """
    writer = open_writer(tmp_path)
    replacement = tmp_path / "audit" / "other.jsonl"
    replacement.write_bytes(b"")
    real_stat_path = writer_module._stat_path
    real_fsync = writer_module._fsync_file
    swapped = {"active": False}

    def fsync_then_swap(handle) -> None:
        real_fsync(handle)
        swapped["active"] = True

    def swapped_path(path: Path):
        if swapped["active"]:
            return real_stat_path(replacement)
        return real_stat_path(path)

    monkeypatch.setattr(writer_module, "_fsync_file", fsync_then_swap)
    monkeypatch.setattr(writer_module, "_stat_path", swapped_path)
    with pytest.raises(AuditJournalContendedError):
        append_mutation(writer)
    assert writer.state is WriterState.POISONED
    assert writer.last_sequence == 0


def test_pre_write_stat_failure_is_sanitized_and_does_not_poison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)

    def failing_stat(handle):
        raise OSError("injected stat failure")

    monkeypatch.setattr(writer_module, "_stat_handle", failing_stat)
    with pytest.raises(AuditJournalUnavailableError) as excinfo:
        append_mutation(writer)
    assert "injected" not in str(excinfo.value)
    assert writer.state is WriterState.READY
    assert journal_path(tmp_path).read_bytes() == b""

    def exploding_stat(handle):
        raise RuntimeError("injected stat failure")

    monkeypatch.setattr(writer_module, "_stat_handle", exploding_stat)
    with pytest.raises(AuditJournalUnavailableError) as excinfo:
        append_mutation(writer)
    assert "injected" not in str(excinfo.value)
    assert writer.state is WriterState.READY
    assert journal_path(tmp_path).read_bytes() == b""

    # Still usable once the I/O condition clears: no bytes were changed.
    monkeypatch.undo()
    assert append_mutation(writer).sequence == 1


def test_ambiguous_failure_never_advances_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)
    first = append_mutation(writer)

    def broken_fsync(handle) -> None:
        raise RuntimeError("injected fsync failure")

    monkeypatch.setattr(writer_module, "_fsync_file", broken_fsync)
    with pytest.raises(AuditJournalUnavailableError):
        append_denied_login(writer, timestamp=LATER_TS)
    monkeypatch.undo()

    assert writer.state is WriterState.POISONED
    assert writer.last_sequence == first.sequence
    assert writer.last_record_hash == first.record_hash
    assert writer.active_segment_id == first.segment_id

    size_after_failure = journal_path(tmp_path).stat().st_size
    for _ in range(3):
        with pytest.raises(AuditJournalPoisonedError):
            append_denied_login(writer, timestamp=LATER_TS)
    assert journal_path(tmp_path).stat().st_size == size_after_failure


def test_lock_is_released_after_unexpected_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)

    def broken_flush(handle) -> None:
        raise RuntimeError("injected flush failure")

    monkeypatch.setattr(writer_module, "_flush_handle", broken_flush)
    with pytest.raises(AuditJournalUnavailableError):
        append_mutation(writer)
    monkeypatch.undo()

    handle = open(journal_path(tmp_path), "r+b")
    try:
        writer_module._lock_handle(handle)
        writer_module._unlock_handle(handle)
    finally:
        handle.close()


def test_lock_is_released_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = open_writer(tmp_path)

    def broken_fsync(handle) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(writer_module, "_fsync_file", broken_fsync)
    with pytest.raises(AuditJournalUnavailableError):
        append_mutation(writer)
    monkeypatch.undo()
    handle = open(journal_path(tmp_path), "r+b")
    try:
        writer_module._lock_handle(handle)
        writer_module._unlock_handle(handle)
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_threads_produce_one_contiguous_chain(tmp_path: Path) -> None:
    writer = open_writer(tmp_path)
    total = 40
    errors: list[BaseException] = []

    def work(count: int) -> None:
        try:
            for _ in range(count):
                append_denied_login(writer)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(work, 5) for _ in range(8)]
        for future in futures:
            future.result()

    assert errors == []
    events = [
        AuditEvent.from_dict(json.loads(line))
        for line in journal_path(tmp_path).read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == total
    assert [event.sequence for event in events] == list(range(1, total + 1))
    assert len({event.audit_id for event in events}) == total
    verify_chain(events)


def test_two_writers_never_duplicate_a_sequence(tmp_path: Path) -> None:
    first = open_writer(tmp_path)
    second = open_writer(tmp_path)
    append_mutation(first)
    with pytest.raises(AuditJournalContendedError):
        append_denied_login(second, timestamp=LATER_TS)
    assert second.state is WriterState.POISONED
    assert first.state is WriterState.READY
    events = [
        AuditEvent.from_dict(json.loads(line))
        for line in journal_path(tmp_path).read_text(encoding="utf-8").splitlines()
    ]
    assert [event.sequence for event in events] == [1]


def test_concurrent_appends_keep_hashes_and_sequences_consistent(
    tmp_path: Path,
) -> None:
    writer = open_writer(tmp_path)
    barrier = threading.Barrier(4)

    def work() -> None:
        barrier.wait()
        append_denied_login(writer)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = [
        AuditEvent.from_dict(json.loads(line))
        for line in journal_path(tmp_path).read_text(encoding="utf-8").splitlines()
    ]
    verify_chain(events)
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert all(
        current.previous_hash == previous.record_hash
        for previous, current in zip(events, events[1:])
    )
