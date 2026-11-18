"""Synthetic audit installations for the P5.5.7 tests.

Everything here is synthetic and lives under ``tmp_path``; the real
``data/audit`` directory is never read, written or referenced.

An "installation" is a directory containing the active journal, an optional
manifest and optional sealed archives, laid out exactly as
``docs/AUDIT_JOURNAL_DESIGN.md`` §9.1 prescribes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Sequence

from src.audit_layout import AuditLayout
from src.audit_manifest import (
    GENESIS_MANIFEST_HASH,
    ManifestDisposition,
    ManifestEntry,
    ManifestEntryType,
)
from src.audit_schema import (
    GENESIS_PREVIOUS_HASH,
    AuditAction,
    AuditEvent,
    AuditOutcome,
    AuditReasonCode,
    AuditSource,
    format_timestamp,
    parse_timestamp,
)

BASE_TIMESTAMP = "2026-11-16T12:00:00.000000Z"
TOOL = "audit_rotate"
TOOL_VERSION = "5.5.7"


def audit_id(sequence: int) -> str:
    """A deterministic canonical UUIDv4-shaped identifier for ``sequence``."""
    return f"{sequence:08d}-1111-4111-8111-111111111111"


def timestamp_for(sequence: int) -> str:
    """A deterministic, strictly increasing UTC timestamp."""
    base = parse_timestamp(BASE_TIMESTAMP)
    return format_timestamp(base + timedelta(seconds=sequence))


def build_events(
    specs: Sequence[tuple[str, int]],
) -> list[AuditEvent]:
    """Build one connected chain from ``(segment_id, record_count)`` specs."""
    events: list[AuditEvent] = []
    previous = GENESIS_PREVIOUS_HASH
    sequence = 1
    for segment_id, count in specs:
        for local in range(1, count + 1):
            event = AuditEvent.create(
                sequence=sequence,
                segment_id=segment_id,
                segment_sequence=local,
                previous_hash=previous,
                timestamp=parse_timestamp(timestamp_for(sequence)),
                audit_id=audit_id(sequence),
                action=AuditAction.AUTH_LOGIN,
                outcome=AuditOutcome.SUCCESS,
                reason_code=AuditReasonCode.OK,
                source=AuditSource.SYSTEM,
            )
            events.append(event)
            previous = event.record_hash
            sequence += 1
    return events


def write_events(path: Path, events: Sequence[AuditEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            event.to_json_line().encode("utf-8") + b"\n" for event in events
        )
    )


def group_by_segment(
    events: Sequence[AuditEvent],
) -> list[tuple[str, list[AuditEvent]]]:
    grouped: list[tuple[str, list[AuditEvent]]] = []
    for event in events:
        if grouped and grouped[-1][0] == event.segment_id:
            grouped[-1][1].append(event)
        else:
            grouped.append((event.segment_id, [event]))
    return grouped


def seal_entry(
    manifest_sequence: int,
    previous_manifest_hash: str,
    events: Sequence[AuditEvent],
    *,
    segment_file: str | None = None,
    sealed_at: str = BASE_TIMESTAMP,
) -> ManifestEntry:
    """Build the ``segment_sealed`` entry declared by a segment's contents."""
    assert events
    first = events[0].sequence
    last = events[-1].sequence
    return ManifestEntry.create(
        manifest_sequence=manifest_sequence,
        entry_type=ManifestEntryType.SEGMENT_SEALED,
        segment_id=events[0].segment_id,
        disposition=ManifestDisposition.ARCHIVED,
        segment_file=segment_file or f"archive/audit-{first}-{last}.jsonl",
        first_global_sequence=first,
        last_global_sequence=last,
        record_count=len(events),
        first_previous_hash=events[0].previous_hash,
        last_record_hash=events[-1].record_hash,
        sealed_at=sealed_at,
        tool=TOOL,
        tool_version=TOOL_VERSION,
        previous_manifest_hash=previous_manifest_hash,
    )


def retention_entry(
    manifest_sequence: int,
    previous_manifest_hash: str,
    seal: ManifestEntry,
    *,
    sealed_at: str = BASE_TIMESTAMP,
) -> ManifestEntry:
    return ManifestEntry.create(
        manifest_sequence=manifest_sequence,
        entry_type=ManifestEntryType.RETENTION_DECISION,
        segment_id=seal.segment_id,
        disposition=ManifestDisposition.ARCHIVED_OFFSITE,
        segment_file=None,
        first_global_sequence=seal.first_global_sequence,
        last_global_sequence=seal.last_global_sequence,
        record_count=seal.record_count,
        first_previous_hash=seal.first_previous_hash,
        last_record_hash=seal.last_record_hash,
        sealed_at=sealed_at,
        tool=TOOL,
        tool_version=TOOL_VERSION,
        previous_manifest_hash=previous_manifest_hash,
    )


def write_manifest(path: Path, entries: Sequence[ManifestEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            entry.to_json_line().encode("utf-8") + b"\n" for entry in entries
        )
    )


@dataclass
class Installation:
    """A synthetic audit installation plus the facts used to build it."""

    layout: AuditLayout
    manifest: tuple[ManifestEntry, ...]
    segments: tuple[tuple[str, tuple[AuditEvent, ...]], ...]
    active: tuple[AuditEvent, ...]

    @property
    def journal(self) -> Path:
        return self.layout.journal

    @property
    def archive_directory(self) -> Path:
        return self.layout.archive_directory

    def archive_path(self, segment_index: int) -> Path:
        _segment_id, events = self.segments[segment_index]
        return self.layout.archive_path(
            events[0].sequence, events[-1].sequence
        )


def install(
    root: Path,
    *,
    sealed: Sequence[int] = (),
    active: int = 0,
    retention: Sequence[int] = (),
    manifest: bool | None = None,
) -> Installation:
    """Installation with generated ``2026-11-NNN`` labels (see below)."""
    specs: list[tuple[str, int]] = [
        (f"2026-11-{index + 1:03d}", count)
        for index, count in enumerate(sealed)
    ]
    active_spec = None
    if active:
        active_spec = (f"2026-11-{len(sealed) + 1:03d}", active)
    include_manifest = bool(sealed or retention) or active > 0
    if manifest is False:
        include_manifest = False
    return install_events(
        root,
        segments=specs,
        active=active_spec,
        retention=retention,
        manifest=include_manifest,
    )


def install_events(
    root: Path,
    *,
    segments: Sequence[tuple[str, int]] = (),
    active: tuple[str, int] | None = None,
    retention: Sequence[int] = (),
    manifest: bool = True,
) -> Installation:
    """Create an installation with ``sealed`` records per archived segment.

    ``sealed`` gives the record count of each archived segment in manifest
    order (each gets an allocation label), ``active`` the record count of the
    appendable segment (0 creates an empty active file), and ``retention`` the
    indices of segments that also carry an ``archived_offsite`` decision.
    ``manifest=False`` omits the manifest file entirely.
    """
    layout = AuditLayout.for_directory(root / "audit")
    layout.archive_directory.mkdir(parents=True, exist_ok=True)

    specs = list(segments)
    if active is not None:
        specs.append(active)
    events = build_events(specs)

    grouped = group_by_segment(events)
    sealed_groups = grouped[: len(segments)]
    active_events: tuple[AuditEvent, ...] = ()
    if active is not None:
        active_events = tuple(grouped[len(segments)][1])

    entries: list[ManifestEntry] = []
    previous_hash = GENESIS_MANIFEST_HASH
    for index, (_segment_id, segment_events) in enumerate(sealed_groups):
        entry = seal_entry(len(entries) + 1, previous_hash, segment_events)
        entries.append(entry)
        previous_hash = entry.entry_hash
        write_events(
            layout.directory / (entry.segment_file or ""), segment_events
        )
        if index in retention:
            decision = retention_entry(len(entries) + 1, previous_hash, entry)
            entries.append(decision)
            previous_hash = decision.entry_hash

    if manifest:
        write_manifest(layout.manifest, entries)
    if active_events:
        write_events(layout.journal, active_events)
    else:
        layout.journal.write_bytes(b"")

    return Installation(
        layout=layout,
        manifest=tuple(entries),
        segments=tuple(
            (segment_id, tuple(segment_events))
            for segment_id, segment_events in sealed_groups
        ),
        active=active_events,
    )
