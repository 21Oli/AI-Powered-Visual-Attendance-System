"""Tests for durable manifest persistence (P5.5.7 stage A).

Every manifest lives in ``tmp_path``; the real ``data/audit`` directory is
never touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import src.audit_manifest_store as store_module
from src.audit_layout import AuditLayout
from src.audit_manifest import (
    GENESIS_MANIFEST_HASH,
    ManifestDisposition,
    ManifestEntry,
    ManifestEntryType,
    compute_entry_hash,
    verify_manifest,
)
from src.audit_manifest_store import (
    MANIFEST_TOOL,
    MANIFEST_TOOL_VERSION,
    ManifestContendedError,
    ManifestCorruptError,
    ManifestIntegrityError,
    ManifestPoisonedError,
    ManifestStore,
    ManifestUnavailableError,
)
from tests.audit_support import BASE_TIMESTAMP, build_events, seal_entry

_HASH_A = "a" * 64


def _layout(tmp_path: Path) -> AuditLayout:
    directory = tmp_path / "audit"
    directory.mkdir(parents=True, exist_ok=True)
    return AuditLayout.for_directory(directory)


def _append(store: ManifestStore, **overrides: object) -> ManifestEntry:
    values: dict[str, object] = {
        "entry_type": ManifestEntryType.SEGMENT_SEALED,
        "segment_id": "2026-11-001",
        "disposition": ManifestDisposition.ARCHIVED,
        "segment_file": "archive/audit-1-3.jsonl",
        "first_global_sequence": 1,
        "last_global_sequence": 3,
        "record_count": 3,
        "first_previous_hash": GENESIS_MANIFEST_HASH,
        "last_record_hash": _HASH_A,
        "sealed_at": BASE_TIMESTAMP,
    }
    values.update(overrides)
    return store.append(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_first_append_creates_the_manifest(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    entry = _append(store)
    assert layout.manifest.is_file()
    raw = layout.manifest.read_bytes()
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    assert raw.decode("utf-8").strip() == entry.to_json_line()
    assert entry.manifest_sequence == 1
    assert entry.previous_manifest_hash == GENESIS_MANIFEST_HASH
    assert entry.tool == MANIFEST_TOOL
    assert entry.tool_version == MANIFEST_TOOL_VERSION


def test_sequential_appends_chain(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    first = _append(store)
    second = _append(
        store,
        segment_id="2026-11-002",
        segment_file="archive/audit-4-6.jsonl",
        first_global_sequence=4,
        last_global_sequence=6,
        first_previous_hash=_HASH_A,
        last_record_hash="b" * 64,
    )
    assert second.manifest_sequence == 2
    assert second.previous_manifest_hash == first.entry_hash
    entries = [
        ManifestEntry.from_dict(json.loads(line))
        for line in layout.manifest.read_text(encoding="utf-8").splitlines()
    ]
    verification = verify_manifest(entries)
    assert verification.entry_count == 2
    assert verification.last_entry_hash == second.entry_hash


def test_reload_sees_the_appended_entries(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _append(ManifestStore(layout))
    reloaded = ManifestStore(layout)
    verification = reloaded.load()
    assert verification.entry_count == 1
    assert reloaded.next_sequence == 2
    assert reloaded.last_hash == verification.last_entry_hash


def test_retention_decision_is_appended_after_its_seal(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    seal = _append(store)
    decision = store.append(
        entry_type=ManifestEntryType.RETENTION_DECISION,
        segment_id=seal.segment_id,
        disposition=ManifestDisposition.ARCHIVED_OFFSITE,
        segment_file=None,
        first_global_sequence=seal.first_global_sequence,
        last_global_sequence=seal.last_global_sequence,
        record_count=seal.record_count,
        first_previous_hash=seal.first_previous_hash,
        last_record_hash=seal.last_record_hash,
        sealed_at=BASE_TIMESTAMP,
    )
    assert decision.segment_file is None
    assert decision.disposition.value == "archived_offsite"
    verification = verify_manifest(store.entries)
    assert verification.offsite_segment_ids == (seal.segment_id,)


# ---------------------------------------------------------------------------
# Refusals before any write
# ---------------------------------------------------------------------------


def test_duplicate_seal_is_refused_without_writing(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    _append(store)
    before = layout.manifest.read_bytes()
    with pytest.raises(ManifestIntegrityError):
        _append(
            store,
            segment_file="archive/audit-4-6.jsonl",
            first_global_sequence=4,
            last_global_sequence=6,
            first_previous_hash=_HASH_A,
            last_record_hash="b" * 64,
        )
    assert layout.manifest.read_bytes() == before


def test_duplicate_retention_decision_is_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    seal = _append(store)
    retention = dict(
        entry_type=ManifestEntryType.RETENTION_DECISION,
        segment_id=seal.segment_id,
        disposition=ManifestDisposition.ARCHIVED_OFFSITE,
        segment_file=None,
        first_global_sequence=seal.first_global_sequence,
        last_global_sequence=seal.last_global_sequence,
        record_count=seal.record_count,
        first_previous_hash=seal.first_previous_hash,
        last_record_hash=seal.last_record_hash,
        sealed_at=BASE_TIMESTAMP,
    )
    store.append(**retention)
    with pytest.raises(ManifestIntegrityError):
        store.append(**retention)


def test_retention_for_unknown_segment_is_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    with pytest.raises(ManifestIntegrityError):
        store.append(
            entry_type=ManifestEntryType.RETENTION_DECISION,
            segment_id="2026-11-009",
            disposition=ManifestDisposition.ARCHIVED_OFFSITE,
            segment_file=None,
            first_global_sequence=1,
            last_global_sequence=1,
            record_count=1,
            first_previous_hash=GENESIS_MANIFEST_HASH,
            last_record_hash=_HASH_A,
            sealed_at=BASE_TIMESTAMP,
        )
    assert not layout.manifest.exists()


def test_continuity_break_is_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    _append(store, segment_file="archive/audit-1-3.jsonl")
    before = layout.manifest.read_bytes()
    with pytest.raises(ManifestIntegrityError):
        _append(
            store,
            segment_id="2026-11-002",
            segment_file="archive/audit-9-11.jsonl",
            first_global_sequence=9,
            last_global_sequence=11,
            first_previous_hash=_HASH_A,
            last_record_hash="b" * 64,
        )
    assert layout.manifest.read_bytes() == before


def test_invalid_schema_is_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    with pytest.raises(Exception):
        _append(store, segment_id="2026-11-000")
    assert not layout.manifest.exists()


# ---------------------------------------------------------------------------
# Reading defects
# ---------------------------------------------------------------------------


def test_torn_tail_is_reported_as_recovery(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _append(ManifestStore(layout))
    raw = layout.manifest.read_bytes()
    layout.manifest.write_bytes(raw[:-2])
    with pytest.raises(ManifestCorruptError) as caught:
        ManifestStore(layout).load()
    assert caught.value.recovery is True
    assert caught.value.line == 1


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.manifest.write_bytes(b"{oops}\n")
    with pytest.raises(ManifestCorruptError):
        ManifestStore(layout).load()


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    entry = _append(store)
    body = entry.as_body()
    body["extra"] = 1
    layout.manifest.write_bytes(json.dumps(body).encode("utf-8") + b"\n")
    with pytest.raises(ManifestCorruptError):
        ManifestStore(layout).load()


def test_incorrect_entry_hash_is_an_integrity_failure(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    entry = _append(store)
    from dataclasses import replace

    forged = replace(entry, entry_hash="f" * 64)
    layout.manifest.write_bytes(forged.to_json_line().encode("utf-8") + b"\n")
    with pytest.raises(ManifestIntegrityError):
        ManifestStore(layout).load()


def test_duplicate_manifest_sequence_is_an_integrity_failure(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    first = _append(store)
    second = _append(
        store,
        segment_id="2026-11-002",
        segment_file="archive/audit-4-6.jsonl",
        first_global_sequence=4,
        last_global_sequence=6,
        first_previous_hash=_HASH_A,
        last_record_hash="b" * 64,
    )
    repeated = first.__class__.from_dict(
        {**first.as_body(), "manifest_sequence": 2}
    )
    from dataclasses import replace

    repeated = replace(
        repeated,
        previous_manifest_hash=second.previous_manifest_hash,
    )
    body = repeated.as_body()
    body["entry_hash"] = GENESIS_MANIFEST_HASH
    object.__setattr__(repeated, "entry_hash", compute_entry_hash(body))
    layout.manifest.write_bytes(
        first.to_json_line().encode("utf-8")
        + b"\n"
        + repeated.to_json_line().encode("utf-8")
        + b"\n"
    )
    with pytest.raises(ManifestIntegrityError):
        ManifestStore(layout).load()


def test_poisoned_store_refuses_every_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)

    def broken_write(handle, payload):  # noqa: ANN001
        raise OSError("disk full")

    monkeypatch.setattr(store_module, "_write_manifest_line", broken_write)
    with pytest.raises(ManifestUnavailableError):
        _append(store)
    assert store.is_poisoned is True
    monkeypatch.undo()
    with pytest.raises(ManifestPoisonedError):
        _append(store)
    assert layout.manifest.read_bytes() == b""


# ---------------------------------------------------------------------------
# Fault injection at the filesystem boundary
# ---------------------------------------------------------------------------


def test_partial_write_does_not_advance_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)

    def short_write(handle, payload):  # noqa: ANN001
        return len(payload) - 1

    monkeypatch.setattr(store_module, "_write_manifest_line", short_write)
    with pytest.raises(ManifestUnavailableError):
        _append(store)
    assert store.is_poisoned is True
    assert store.entries == ()
    assert store.next_sequence == 1


def test_flush_failure_poisons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)

    def broken_flush(handle):  # noqa: ANN001
        raise OSError("flush failed")

    monkeypatch.setattr(store_module, "_flush_manifest", broken_flush)
    with pytest.raises(ManifestUnavailableError):
        _append(store)
    assert store.is_poisoned is True


def test_fsync_failure_poisons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)

    def broken_fsync(handle):  # noqa: ANN001
        raise OSError("fsync failed")

    monkeypatch.setattr(store_module, "_fsync_manifest", broken_fsync)
    with pytest.raises(ManifestUnavailableError):
        _append(store)
    assert store.is_poisoned is True


def test_fsync_failure_leaves_no_state_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    monkeypatch.setattr(
        store_module,
        "_fsync_manifest",
        lambda handle: (_ for _ in ()).throw(OSError("fsync failed")),
    )
    with pytest.raises(ManifestUnavailableError):
        _append(store)
    assert store.entries == ()
    assert store.next_sequence == 1


def test_unexpected_write_exception_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)

    def exploding_write(handle, payload):  # noqa: ANN001
        raise RuntimeError("secret path C:/somewhere")

    monkeypatch.setattr(store_module, "_write_manifest_line", exploding_write)
    with pytest.raises(ManifestUnavailableError) as caught:
        _append(store)
    assert "secret" not in str(caught.value)
    assert "C:/somewhere" not in str(caught.value)


def test_file_replacement_during_append_is_contended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)

    def replaced(path: Path):
        real = os.stat(path)
        return os.stat_result(
            (real.st_mode, real.st_ino, real.st_dev, real.st_nlink, real.st_uid,
             real.st_gid, real.st_size, real.st_atime, real.st_mtime, real.st_ctime)
        )

    def swap_after_write(handle, payload):  # noqa: ANN001
        written = handle.write(payload)
        return written

    monkeypatch.setattr(store_module, "_write_manifest_line", swap_after_write)

    real_stat = store_module._stat_manifest_handle
    calls = {"count": 0}

    def flaky_stat(handle):  # noqa: ANN001
        calls["count"] += 1
        stat_result = real_stat(handle)
        if calls["count"] == 2:
            from types import SimpleNamespace

            return SimpleNamespace(
                st_size=stat_result.st_size,
                st_dev=stat_result.st_dev,
                st_ino=stat_result.st_ino + 1,
            )
        return stat_result

    monkeypatch.setattr(store_module, "_stat_manifest_handle", flaky_stat)
    with pytest.raises(ManifestContendedError):
        _append(store)
    assert store.is_poisoned is True


def test_unexpected_size_after_append_is_contended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    real_stat = store_module._stat_manifest_handle
    calls = {"count": 0}

    def flaky_stat(handle):  # noqa: ANN001
        calls["count"] += 1
        stat_result = real_stat(handle)
        if calls["count"] == 2:
            from types import SimpleNamespace

            return SimpleNamespace(
                st_size=stat_result.st_size + 7,
                st_dev=stat_result.st_dev,
                st_ino=stat_result.st_ino,
            )
        return stat_result

    monkeypatch.setattr(store_module, "_stat_manifest_handle", flaky_stat)
    with pytest.raises(ManifestContendedError):
        _append(store)
    assert store.is_poisoned is True


def test_seal_helper_entry_matches_store_output(tmp_path: Path) -> None:
    events = build_events([("2026-11-001", 2)])
    expected = seal_entry(1, GENESIS_MANIFEST_HASH, events)
    layout = _layout(tmp_path)
    store = ManifestStore(layout)
    stored = store.append(
        entry_type=ManifestEntryType.SEGMENT_SEALED,
        segment_id=events[0].segment_id,
        disposition=ManifestDisposition.ARCHIVED,
        segment_file=expected.segment_file,
        first_global_sequence=events[0].sequence,
        last_global_sequence=events[-1].sequence,
        record_count=len(events),
        first_previous_hash=events[0].previous_hash,
        last_record_hash=events[-1].record_hash,
        sealed_at=BASE_TIMESTAMP,
    )
    assert stored.entry_hash == expected.entry_hash
