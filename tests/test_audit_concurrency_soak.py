"""P5.5.8.4 bounded concurrency and rotation soak (offline, ``tmp_path``).

Concurrency model under test (from ``src/audit_writer.py``):

* a module-level ``threading.Lock`` serialises every append inside one process,
  so threads queue rather than contend;
* a non-blocking OS advisory lock on byte 0 of the journal protects against a
  *second process*, which is refused and poisoned — the writer is a strict
  single-writer design, not a multi-writer one;
* rotation is an exclusive operator operation: it takes the layout lock, then
  the writer's byte-0 lease, and must not run while an application is appending.

These tests therefore assert bounded, in-process behaviour only. They make **no**
claim about multi-process, distributed, network-filesystem or production-scale
concurrency. Workloads are small and deterministic: no sleeps, no timing
assertions, injected clocks, synthetic records only.
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.audit_writer as writer_module
import tools.audit_rotate as rotate_cli
import tools.audit_verify as verify_cli
from src.audit_layout import AuditLayout
from src.audit_manifest_store import ManifestStore, ManifestStoreError
from src.audit_rotation import (
    RotationContendedError,
    RotationOperations,
    RotationRecoveryRequiredError,
    RotationUnavailableError,
)
from src.audit_schema import (
    AuditEvent,
    AuditOutcome,
    AuditReasonCode,
    canonical_json_bytes,
    verify_chain,
)
from src.audit_service import AuditService, AuditUnavailableError
from src.audit_writer import AuditJournalWriter
from src.user import Role
from tests.audit_support import install_events

THREADS = 20
APPENDS_PER_THREAD = 50
TOTAL_APPENDS = THREADS * APPENDS_PER_THREAD
ROTATION_CYCLES = 12
RECORDS_PER_CYCLE = 2

MONTH_EDGE = datetime(2026, 11, 30, 22, 0, 0, 123456, tzinfo=UTC)


class TickClock:
    """Thread-safe strictly increasing clock (no real time involved)."""

    def __init__(self, start: datetime = MONTH_EDGE, step: timedelta = timedelta(hours=1)) -> None:
        self._lock = threading.Lock()
        self._value = start
        self._step = step

    def __call__(self) -> datetime:
        with self._lock:
            value = self._value
            self._value = value + self._step
            return value


def audit_ids(start: int = 1) -> object:
    counter = count(start)
    return lambda: str(uuid.UUID(int=next(counter), version=4))


def layout_of(root: Path) -> AuditLayout:
    return AuditLayout.for_directory(root / "audit")


def actor() -> object:
    return SimpleNamespace(user_id="user-1", role=Role.ADMIN)


def build_service(
    root: Path, clock: TickClock, *, first_id: int = 1
) -> AuditService:
    return AuditService(
        AuditJournalWriter(layout_of(root).journal, clock=clock),
        clock=clock,
        audit_id_factory=audit_ids(first_id),
    )


def append_login(service: AuditService) -> AuditEvent:
    context = AuditService.context(actor())
    return service.record_login(
        outcome=AuditOutcome.SUCCESS,
        reason_code=AuditReasonCode.OK,
        context=context,
    )


def read_events(root: Path) -> list[AuditEvent]:
    return [
        AuditEvent.from_dict(json.loads(line))
        for line in layout_of(root).journal.read_text(encoding="utf-8").splitlines()
    ]


def verify(root: Path, capsys) -> int:
    code = verify_cli.main(["--journal", str(layout_of(root).journal)])
    capsys.readouterr()
    return code


# ---------------------------------------------------------------------------
# Bounded concurrent appends: 20 threads x 50 appends
# ---------------------------------------------------------------------------


def test_twenty_threads_append_one_thousand_ordered_records(
    tmp_path: Path, capsys
) -> None:
    clock = TickClock()
    service = build_service(tmp_path, clock)
    barrier = threading.Barrier(THREADS)
    errors: list[BaseException] = []
    returned: list[AuditEvent] = []
    returned_lock = threading.Lock()

    def work() -> None:
        try:
            barrier.wait()
            for _ in range(APPENDS_PER_THREAD):
                event = append_login(service)
                with returned_lock:
                    returned.append(event)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(work) for _ in range(THREADS)]
        for future in futures:
            future.result()

    assert errors == []
    assert len(returned) == TOTAL_APPENDS

    lines = layout_of(tmp_path).journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == TOTAL_APPENDS
    events = read_events(tmp_path)

    # Global sequence: unique, contiguous, in file order.
    assert [event.sequence for event in events] == list(range(1, TOTAL_APPENDS + 1))
    assert len({event.audit_id for event in events}) == TOTAL_APPENDS
    assert len({event.record_hash for event in events}) == TOTAL_APPENDS

    # Every record is canonical and links to its predecessor.
    for line, event in zip(lines, events):
        assert line == canonical_json_bytes(event.as_body()).decode("utf-8")
    for previous, current in zip(events, events[1:]):
        assert current.previous_hash == previous.record_hash
    verify_chain(events)

    # The returned events are exactly the stored ones (no false success).
    assert sorted(event.sequence for event in returned) == list(
        range(1, TOTAL_APPENDS + 1)
    )
    assert verify(tmp_path, capsys) == verify_cli.EXIT_OK

    # Writer state is still correct, and the chain continues.
    later = append_login(build_service(tmp_path, clock, first_id=10_000))
    assert later.sequence == TOTAL_APPENDS + 1
    assert verify(tmp_path, capsys) == verify_cli.EXIT_OK


def test_concurrent_appends_poison_the_writer_once_and_never_duplicate(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-run fsync failure stops every writer; the prefix stays verifiable."""
    clock = TickClock()
    service = build_service(tmp_path, clock)
    real = writer_module._fsync_file
    limit = 20
    calls = {"n": 0}
    lock = threading.Lock()

    def flaky(handle) -> None:  # noqa: ANN001
        with lock:
            calls["n"] += 1
            position = calls["n"]
        if position > limit:
            raise OSError("injected fsync failure")
        real(handle)

    monkeypatch.setattr(writer_module, "_fsync_file", flaky)
    barrier = threading.Barrier(THREADS)
    successes: list[AuditEvent] = []
    failures: list[Exception] = []
    results_lock = threading.Lock()

    def work() -> None:
        barrier.wait()
        for _ in range(APPENDS_PER_THREAD):
            try:
                event = append_login(service)
            except BaseException as exc:
                with results_lock:
                    failures.append(exc)
            else:
                with results_lock:
                    successes.append(event)

    try:
        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            futures = [pool.submit(work) for _ in range(THREADS)]
            for future in futures:
                future.result()
    finally:
        monkeypatch.undo()

    # Exactly the confirmed appends succeeded; everything after is refused.
    assert len(successes) == limit
    assert len(failures) == TOTAL_APPENDS - limit
    # The service maps every writer failure onto the fail-closed contract.
    assert all(isinstance(exc, AuditUnavailableError) for exc in failures)
    assert all(
        exc.writer_error
        in {"AuditJournalUnavailableError", "AuditJournalPoisonedError"}
        for exc in failures
    )

    # The failing append is ambiguous: it flushed before fsync raised, so it may
    # also be on disk. It was never reported as a success.
    events = read_events(tmp_path)
    assert len(events) in (limit, limit + 1)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert len({event.audit_id for event in events}) == len(events)
    verify_chain(events)
    assert max(event.sequence for event in successes) <= limit

    # No truncation, no rollback, no duplicate retry.
    before = layout_of(tmp_path).journal.read_bytes()
    with pytest.raises(AuditUnavailableError):
        append_login(service)
    assert layout_of(tmp_path).journal.read_bytes() == before
    assert verify(tmp_path, capsys) == verify_cli.EXIT_OK


# ---------------------------------------------------------------------------
# Rotation soak: 12 cycles across a month boundary
# ---------------------------------------------------------------------------


def test_twelve_rotations_across_a_month_boundary(
    tmp_path: Path, capsys
) -> None:
    clock = TickClock()
    install_events(
        tmp_path, segments=[], active=("2026-11-001", 2), manifest=False
    )
    layout = layout_of(tmp_path)
    archives: dict[str, bytes] = {}

    next_sequence = 1
    for cycle in range(1, ROTATION_CYCLES + 1):
        service = build_service(tmp_path, clock, first_id=cycle * 100)
        # The installation starts with two records, so the first cycle seals four.
        expected_count = RECORDS_PER_CYCLE + (2 if cycle == 1 else 0)
        for _ in range(RECORDS_PER_CYCLE):
            append_login(service)
        assert verify(tmp_path, capsys) == verify_cli.EXIT_OK

        plan = RotationOperations(layout, clock=clock).rotate()

        # Each rotation seals exactly the active segment with its own bounds.
        assert plan.segment_id == f"2026-11-{cycle:03d}"
        assert plan.record_count == expected_count
        assert plan.first_global_sequence == next_sequence
        assert plan.last_global_sequence == next_sequence + expected_count - 1
        assert plan.next_segment_id == f"2026-11-{cycle + 1:03d}"
        next_sequence += expected_count

        # Active journal created empty by rotation only.
        assert layout.journal.read_bytes() == b""
        assert verify(tmp_path, capsys) == verify_cli.EXIT_OK

        # Previously sealed archives never change; no archive is overwritten.
        for name, payload in archives.items():
            assert (layout.archive_directory / name).read_bytes() == payload
        for path in sorted(layout.archive_directory.glob("*.jsonl")):
            archives.setdefault(path.name, path.read_bytes())
        assert len(archives) == cycle

    # Manifest: monotonic sequence, chained entries, correct bounds.
    entries = [
        json.loads(line)
        for line in layout.manifest.read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["manifest_sequence"] for entry in entries] == list(
        range(1, ROTATION_CYCLES + 1)
    )
    for previous, current in zip(entries, entries[1:]):
        assert current["previous_manifest_hash"] == previous["entry_hash"]
        assert current["first_global_sequence"] == (
            previous["last_global_sequence"] + 1
        )
    assert entries[-1]["last_global_sequence"] == next_sequence - 1
    assert [entry["segment_id"] for entry in entries] == [
        f"2026-11-{cycle:03d}" for cycle in range(1, ROTATION_CYCLES + 1)
    ]

    # Record timestamps crossed 2026-11-30 -> 2026-12-01 while the segment
    # labels remained successors of the first allocation (design §9.7).
    timestamps = [
        json.loads(line)["timestamp"]
        for path in sorted(layout.archive_directory.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert min(timestamps).startswith("2026-11")
    assert max(timestamps).startswith("2026-12")
    assert entries[0]["sealed_at"] < entries[-1]["sealed_at"]
    assert all(entry["segment_id"].startswith("2026-11-") for entry in entries)

    code = verify_cli.main(["--journal", str(layout.journal)])
    output = capsys.readouterr().out
    assert code == verify_cli.EXIT_OK
    assert f"Records verified: {next_sequence - 1}" in output
    assert "Archived segments: 12" in output


def test_rotation_soak_continues_after_a_documented_recovery(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted seal is recovered with the runbook, then the soak continues."""
    clock = TickClock()
    install_events(
        tmp_path, segments=[], active=("2026-11-001", 2), manifest=False
    )
    layout = layout_of(tmp_path)
    append_login(build_service(tmp_path, clock))

    def failing_append(self, **kwargs):  # noqa: ANN001
        raise ManifestStoreError("injected")

    monkeypatch.setattr(ManifestStore, "append", failing_append)
    with pytest.raises(RotationRecoveryRequiredError):
        RotationOperations(layout, clock=clock).rotate()
    monkeypatch.undo()

    assert not layout.journal.exists()
    assert not layout.manifest.exists()
    archived = sorted(layout.archive_directory.glob("*.jsonl"))
    assert len(archived) == 1
    payload = archived[0].read_bytes()

    assert rotate_cli.main(["--audit-dir", str(layout.directory)]) == (
        rotate_cli.EXIT_RECOVERY_REQUIRED
    )
    capsys.readouterr()
    assert rotate_cli.main(
        ["--register-existing", "--audit-dir", str(layout.directory)]
    ) == rotate_cli.EXIT_OK
    capsys.readouterr()
    assert rotate_cli.main(
        ["--create-active", "--audit-dir", str(layout.directory)]
    ) == rotate_cli.EXIT_OK
    capsys.readouterr()
    assert archived[0].read_bytes() == payload
    assert verify(tmp_path, capsys) == verify_cli.EXIT_OK

    # The soak continues from the recovered state with an unbroken chain.
    append_login(build_service(tmp_path, clock, first_id=500))
    plan = RotationOperations(layout, clock=clock).rotate()
    assert plan.segment_id == "2026-11-002"
    assert plan.first_global_sequence == 4
    assert verify(tmp_path, capsys) == verify_cli.EXIT_OK


def test_rotation_refuses_while_a_thread_is_appending(
    tmp_path: Path, capsys
) -> None:
    """Rotation is exclusive: a live writer's lease blocks it, nothing changes."""
    clock = TickClock()
    install_events(
        tmp_path, segments=[], active=("2026-11-001", 2), manifest=False
    )
    layout = layout_of(tmp_path)
    before = {
        path.name: path.read_bytes()
        for path in layout.directory.iterdir()
        if path.is_file() and path.name != ".rotation.lock"
    }

    handle = (layout.journal).open("r+b")
    try:
        writer_module._lock_handle(handle)
        with pytest.raises(Exception) as caught:
            RotationOperations(layout, clock=clock).rotate()
    finally:
        writer_module._unlock_handle(handle)
        handle.close()

    assert isinstance(
        caught.value, (RotationContendedError, RotationUnavailableError)
    )
    after = {
        path.name: path.read_bytes()
        for path in layout.directory.iterdir()
        if path.is_file() and path.name != ".rotation.lock"
    }
    assert after == before
    assert verify(tmp_path, capsys) == verify_cli.EXIT_OK
