"""Tests for the audit storage layout and segment allocation (P5.5.7).

Pure path and label arithmetic: nothing here touches the filesystem.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.audit_layout import (
    JOURNAL_FILENAME,
    MAX_SEGMENT_ORDINAL,
    ROTATION_LOCK_FILENAME,
    AuditLayout,
    SegmentAllocationError,
    allocation_month,
    next_segment_id,
    successor_segment_id,
)
from src.audit_manifest import ARCHIVE_DIRECTORY_NAME, MANIFEST_FILENAME

BASE = datetime(2026, 11, 20, 9, 0, 0, tzinfo=UTC)


def test_layout_paths_are_derived_from_the_audit_directory() -> None:
    layout = AuditLayout.for_directory(Path("data") / "audit")
    assert layout.journal == Path("data") / "audit" / JOURNAL_FILENAME
    assert layout.manifest == Path("data") / "audit" / MANIFEST_FILENAME
    assert layout.archive_directory == (
        Path("data") / "audit" / ARCHIVE_DIRECTORY_NAME
    )
    assert layout.rotation_lock == (
        Path("data") / "audit" / ROTATION_LOCK_FILENAME
    )


def test_layout_from_journal_uses_the_parent_directory() -> None:
    layout = AuditLayout.from_journal(Path("tmp") / "audit" / "audit.jsonl")
    assert layout.directory == Path("tmp") / "audit"
    assert layout.journal == Path("tmp") / "audit" / "audit.jsonl"


def test_archive_path_and_reference_are_derived_from_the_bounds() -> None:
    layout = AuditLayout.for_directory(Path("audit"))
    assert layout.archive_path(1, 3).name == "audit-1-3.jsonl"
    assert layout.archive_reference(1, 3) == "archive/audit-1-3.jsonl"
    assert layout.archive_path_for_reference("archive/audit-4-9.jsonl") == (
        Path("audit") / "archive" / "audit-4-9.jsonl"
    )


def test_inverted_archive_bounds_are_refused() -> None:
    with pytest.raises(SegmentAllocationError):
        AuditLayout.archive_filename(5, 4)
    with pytest.raises(SegmentAllocationError):
        AuditLayout.archive_filename(0, 0)


def test_allocation_month_is_utc() -> None:
    assert allocation_month(BASE) == "2026-11"
    other = datetime(
        2026, 12, 1, 0, 30, tzinfo=timezone(timedelta(hours=2))
    )
    assert allocation_month(other) == "2026-11"


def test_allocation_month_requires_an_aware_datetime() -> None:
    with pytest.raises(SegmentAllocationError):
        allocation_month(datetime(2026, 11, 20))  # noqa: DTZ001 - deliberate


def test_successor_keeps_the_month_and_increments_the_ordinal() -> None:
    assert successor_segment_id("2026-11-001") == "2026-11-002"
    assert successor_segment_id("2026-11-099") == "2026-11-100"
    assert successor_segment_id("2026-12-001") == "2026-12-002"


def test_successor_refuses_ordinal_999() -> None:
    with pytest.raises(SegmentAllocationError):
        successor_segment_id(f"2026-11-{MAX_SEGMENT_ORDINAL:03d}")


def test_successor_refuses_malformed_labels() -> None:
    for label in ("2026-11", "2026-13-001", "2026-11-000", "2026-11-1", ""):
        with pytest.raises(SegmentAllocationError):
            successor_segment_id(label)


def test_new_installation_allocates_the_current_month_at_001() -> None:
    assert next_segment_id([], moment=BASE) == "2026-11-001"
    assert (
        next_segment_id([], moment=datetime(2027, 1, 1, tzinfo=UTC))
        == "2027-01-001"
    )


def test_later_segments_follow_the_last_sealed_segment() -> None:
    assert next_segment_id(["2026-11-001"], moment=BASE) == "2026-11-002"
    assert (
        next_segment_id(["2026-11-001", "2026-11-002"], moment=BASE)
        == "2026-11-003"
    )


def test_successor_is_restart_stable_even_across_a_month_boundary() -> None:
    # The allocation month follows the sealed predecessor, not "today", so a
    # restart in a later month derives the same next identifier.
    sealed = ["2026-11-001"]
    november = next_segment_id(sealed, moment=BASE)
    january = next_segment_id(
        sealed, moment=datetime(2027, 1, 15, tzinfo=UTC)
    )
    assert november == january == "2026-11-002"


def test_duplicate_sealed_ids_are_refused() -> None:
    with pytest.raises(SegmentAllocationError):
        next_segment_id(["2026-11-001", "2026-11-001"], moment=BASE)


def test_reserved_identifiers_are_refused_instead_of_reused() -> None:
    with pytest.raises(SegmentAllocationError):
        next_segment_id(
            ["2026-11-001"], moment=BASE, reserved=["2026-11-002"]
        )
