"""Operator-controlled audit rotation, recovery and retention (P5.5.7).

Rotation seals the current active journal into an immutable archive and opens a
fresh empty active file. It never runs automatically: there is no scheduler, no
background thread, no startup hook and no API route that can trigger it. Only
:mod:`tools.audit_rotate` calls this module.

Rotation order (design §9.3)
============================

1. verify the whole available history (manifest + every declared archive) and
   the active journal, and require the active file to be newline-terminated;
2. ``os.replace`` the active journal to ``archive/audit-<first>-<last>.jsonl``;
3. best-effort ``fsync`` of the archive directory;
4. durably append exactly one ``segment_sealed`` manifest entry;
5. mark the archive read-only where the OS allows it;
6. create the next empty active journal;
7. the next segment id is the deterministic successor of the sealed segment,
   so a restart derives the same id (see :mod:`src.audit_layout`).

No ``audit.rotate`` event is written to the event journal. No record is
rewritten and no historical byte is modified.

Recovery (design §9.4)
======================

* crash before the rename — nothing changed, re-running is safe;
* archive present, seal absent — :meth:`RotationOperations.register_existing`
  (``--register-existing``) re-verifies the archive and appends exactly one
  correct seal, refusing anything ambiguous;
* seal present, active journal absent —
  :meth:`RotationOperations.create_active` (``--create-active``) creates an
  empty active journal after verifying history, never overwriting a file;
* empty active file after rotation — valid; the id is derived deterministically
  and the first append links to the manifest's final record hash;
* torn manifest append — no automatic truncation or repair; the verifier
  reports the recovery state and the writer refuses to start.

Every operation fails closed, and no operation deletes, moves or rewrites
anything except the documented single ``os.replace`` of the rotation itself.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from src.audit_archive import (
    ACTIVE_SEGMENT_MISSING,
    HistoryState,
    IntegrityProblem,
    LayoutProblem,
    RecoveryProblem,
    SegmentSummary,
    UnavailableProblem,
    parse_event_lines,
    read_archive_bytes,
    summarise_segment,
    verify_active_journal,
    verify_declared_archive,
    verify_history,
)
from src.audit_layout import (
    AuditLayout,
    SegmentAllocationError,
    next_segment_id,
    successor_segment_id,
)
from src.audit_locking import (
    JournalLease,
    LayoutLock,
    LockContendedError,
    LockUnavailableError,
)
from src.audit_manifest import (
    ARCHIVE_FILENAME_PATTERN,
    GENESIS_MANIFEST_HASH,
    ManifestDisposition,
    ManifestEntry,
    ManifestEntryType,
    ManifestError,
    validate_archive_reference,
)
from src.audit_manifest_store import (
    MANIFEST_TOOL,
    MANIFEST_TOOL_VERSION,
    ManifestContendedError,
    ManifestStore,
    ManifestStoreError,
)
from src.audit_schema import format_timestamp
from src.clock import Clock, utc_now

__all__ = [
    "ROTATION_TOOL",
    "ROTATION_TOOL_VERSION",
    "RotationContendedError",
    "RotationError",
    "RotationIntegrityError",
    "RotationOperations",
    "RotationPlan",
    "RotationRecoveryRequiredError",
    "RotationUnavailableError",
]

ROTATION_TOOL: Final[str] = MANIFEST_TOOL
ROTATION_TOOL_VERSION: Final[str] = MANIFEST_TOOL_VERSION

_MSG_UNAVAILABLE = "the audit installation is unavailable"
_MSG_INTEGRITY = "the audit installation failed integrity verification"
_MSG_RECOVERY = "the audit installation requires explicit operator recovery"
_MSG_CONTENDED = (
    "another audit writer or rotation holds the lock; stop the application and "
    "retry"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RotationError(Exception):
    """Base class for rotation failures; messages are fixed and sanitized."""

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        self.category = category


class RotationUnavailableError(RotationError):
    """A required file could not be read; nothing was changed."""


class RotationIntegrityError(RotationError):
    """History, manifest or journal integrity failed; nothing was changed."""


class RotationRecoveryRequiredError(RotationError):
    """The installation is in a documented recovery state (see the runbook)."""


class RotationContendedError(RotationError):
    """A writer or another rotation holds a lock; nothing was changed."""


# ---------------------------------------------------------------------------
# Filesystem seams (module level so tests can inject failures)
# ---------------------------------------------------------------------------


def _replace_file(source: Path, target: Path) -> None:
    os.replace(source, target)


def _fsync_directory(directory: Path) -> bool:
    """Best-effort directory fsync; unsupported platforms return ``False``."""
    if os.name == "nt":
        return False
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(descriptor)
        return True
    except OSError:
        return False
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _mark_read_only(path: Path) -> bool:
    """Mark a sealed archive read-only where the OS allows it."""
    mode = stat.S_IREAD if os.name == "nt" else stat.S_IRUSR | stat.S_IRGRP
    try:
        os.chmod(path, mode)
        return True
    except OSError:
        return False


def _create_empty_file(path: Path) -> None:
    with open(path, "xb"):
        pass


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RotationPlan:
    """Everything rotation would do, computed without changing anything."""

    segment_id: str
    record_count: int
    first_global_sequence: int
    last_global_sequence: int
    first_previous_hash: str
    last_record_hash: str
    first_timestamp: str
    last_timestamp: str
    archive_reference: str
    next_segment_id: str
    journal_path: Path
    archive_path: Path
    journal_size: int = 0
    journal_mtime_ns: int = 0
    journal_identity: tuple[int, int] | None = None
    directory_fsync: bool = False
    read_only_marked: bool = False


def _wrap(problem: LayoutProblem) -> RotationError:
    """Translate a layout problem into a rotation error without leaking text."""
    if isinstance(problem, RecoveryProblem):
        return RotationRecoveryRequiredError(
            _MSG_RECOVERY, category=problem.category
        )
    if isinstance(problem, IntegrityProblem):
        return RotationIntegrityError(_MSG_INTEGRITY, category=problem.category)
    return RotationUnavailableError(
        _MSG_UNAVAILABLE, category=problem.category
    )


def _wrap_lock(error: LockUnavailableError) -> RotationError:
    if isinstance(error, LockContendedError):
        return RotationContendedError(_MSG_CONTENDED)
    return RotationUnavailableError(_MSG_UNAVAILABLE)


class RotationOperations:
    """Verify, rotate, register, recreate and record retention."""

    def __init__(
        self, layout: AuditLayout, *, clock: Clock = utc_now
    ) -> None:
        self._layout = layout
        self._clock = clock

    @property
    def layout(self) -> AuditLayout:
        return self._layout

    # -- inspection ---------------------------------------------------------

    def inspect(self) -> HistoryState:
        """Verify the sealed history only (read-only, no lock)."""
        try:
            return verify_history(self._layout)
        except LayoutProblem as problem:
            raise _wrap(problem) from None

    def plan(self) -> RotationPlan:
        """Compute the rotation plan read-only; never creates or changes a file.

        No lock is taken, so a dry run is a point-in-time report rather than a
        reservation; the real rotation re-computes and re-verifies the plan
        under the layout lock.
        """
        return self._plan()

    # -- rotation -----------------------------------------------------------

    def rotate(self, *, dry_run: bool = False) -> RotationPlan:
        """Seal the active journal; with ``dry_run`` only verify and report."""
        if dry_run:
            return self._plan()

        try:
            lock = LayoutLock(self._layout)
            lock.acquire()
        except LockUnavailableError as error:
            raise _wrap_lock(error) from None
        try:
            plan = self._plan()

            # Proving that no writer is appending: the writer's own byte-0
            # lock is taken and the journal is re-checked against the plan.
            # The lease is released before the rename because Windows cannot
            # replace a file that another handle holds open.
            lease = JournalLease(self._layout.journal)
            try:
                lease.acquire()
            except LockUnavailableError as error:
                raise _wrap_lock(error) from None
            try:
                self._assert_plan_current(plan)
            finally:
                lease.release()

            try:
                _replace_file(self._layout.journal, plan.archive_path)
            except OSError:
                # Nothing was renamed: the active journal is still in place.
                raise RotationUnavailableError(_MSG_UNAVAILABLE) from None

            directory_fsync = _fsync_directory(self._layout.archive_directory)

            store = ManifestStore(self._layout)
            try:
                store.append(
                    entry_type=ManifestEntryType.SEGMENT_SEALED,
                    segment_id=plan.segment_id,
                    disposition=ManifestDisposition.ARCHIVED,
                    segment_file=plan.archive_reference,
                    first_global_sequence=plan.first_global_sequence,
                    last_global_sequence=plan.last_global_sequence,
                    record_count=plan.record_count,
                    first_previous_hash=plan.first_previous_hash,
                    last_record_hash=plan.last_record_hash,
                    sealed_at=self._timestamp(),
                    tool=ROTATION_TOOL,
                    tool_version=ROTATION_TOOL_VERSION,
                    assume_locked=True,
                )
            except ManifestStoreError as error:
                raise RotationRecoveryRequiredError(
                    "the active journal was moved into the archive but its "
                    "manifest seal could not be recorded; run "
                    "--register-existing to complete the seal",
                    category="SEAL_NOT_RECORDED",
                ) from None

            read_only = _mark_read_only(plan.archive_path)

            try:
                _create_empty_file(self._layout.journal)
            except FileExistsError:
                raise RotationRecoveryRequiredError(
                    "the segment is sealed but the active journal already "
                    "exists; operator investigation is required",
                    category="ACTIVE_ALREADY_EXISTS",
                ) from None
            except OSError:
                raise RotationRecoveryRequiredError(
                    "the segment is sealed but the new active journal could "
                    "not be created; run --create-active to complete recovery",
                    category="ACTIVE_NOT_CREATED",
                ) from None

            return replace(
                plan,
                directory_fsync=directory_fsync,
                read_only_marked=read_only,
            )
        finally:
            lock.release()

    def _assert_plan_current(self, plan: RotationPlan) -> None:
        """Refuse to rename a journal that changed since it was verified."""
        try:
            stat_result = os.stat(plan.journal_path)
        except OSError:
            raise RotationContendedError(_MSG_CONTENDED) from None
        identity = None
        if getattr(stat_result, "st_ino", 0):
            identity = (stat_result.st_dev, stat_result.st_ino)
        if (
            stat_result.st_size != plan.journal_size
            or stat_result.st_mtime_ns != plan.journal_mtime_ns
            or (
                plan.journal_identity is not None
                and identity is not None
                and identity != plan.journal_identity
            )
        ):
            raise RotationContendedError(_MSG_CONTENDED)

    def _plan(self) -> RotationPlan:
        layout = self._layout
        if not layout.directory.is_dir():
            raise RotationUnavailableError(_MSG_UNAVAILABLE)

        history = self.inspect()

        try:
            active = verify_active_journal(layout, history)
        except LayoutProblem as problem:
            raise _wrap(problem) from None

        if not active.events:
            raise RotationIntegrityError(
                "the active journal contains no records to seal; rotation "
                "would create an empty archive",
                category="NOTHING_TO_SEAL",
            )
        summary = active.summary
        assert summary is not None  # non-empty events always summarise

        if summary.segment_id in history.sealed_segment_ids:
            raise RotationIntegrityError(
                "the active segment is already sealed",
                category="SEGMENT_ALREADY_SEALED",
            )

        archive_reference = layout.archive_reference(
            summary.first_global_sequence, summary.last_global_sequence
        )
        validate_archive_reference(
            archive_reference,
            first_global_sequence=summary.first_global_sequence,
            last_global_sequence=summary.last_global_sequence,
        )
        archive_path = layout.archive_path_for_reference(archive_reference)
        if os.path.lexists(archive_path):
            raise RotationRecoveryRequiredError(
                "the archive destination already exists; operator "
                "investigation is required",
                category="ARCHIVE_EXISTS",
            )

        try:
            next_id = successor_segment_id(summary.segment_id)
        except SegmentAllocationError:
            raise RotationIntegrityError(
                "segment ordinal 999 is exhausted for this allocation month; "
                "an operator decision is required",
                category="ORDINAL_EXHAUSTED",
            ) from None
        if next_id in history.sealed_segment_ids:
            raise RotationIntegrityError(
                "the next segment identifier is already sealed",
                category="SEGMENT_ID_COLLISION",
            )

        try:
            journal_stat = os.stat(layout.journal)
        except OSError:
            raise RotationUnavailableError(_MSG_UNAVAILABLE) from None
        journal_identity = None
        if getattr(journal_stat, "st_ino", 0):
            journal_identity = (journal_stat.st_dev, journal_stat.st_ino)

        return RotationPlan(
            segment_id=summary.segment_id,
            record_count=summary.record_count,
            first_global_sequence=summary.first_global_sequence,
            last_global_sequence=summary.last_global_sequence,
            first_previous_hash=summary.first_previous_hash,
            last_record_hash=summary.last_record_hash,
            first_timestamp=summary.first_timestamp,
            last_timestamp=summary.last_timestamp,
            archive_reference=archive_reference,
            next_segment_id=next_id,
            journal_path=layout.journal,
            archive_path=archive_path,
            journal_size=journal_stat.st_size,
            journal_mtime_ns=journal_stat.st_mtime_ns,
            journal_identity=journal_identity,
        )

    # -- recovery: register an interrupted archive --------------------------

    def register_existing(self, reference: str | None = None) -> ManifestEntry:
        """Append the missing seal for an archive produced by a crash.

        The archive is re-read and fully verified (schema, digests, sequence
        continuity, segment identity, bounds, anchors) and its position is
        checked against the existing manifest and the remaining active journal
        before exactly one seal is appended. Ambiguity is refused; a segment is
        never sealed twice.
        """
        try:
            lock = LayoutLock(self._layout)
            lock.acquire()
        except LockUnavailableError as error:
            raise _wrap_lock(error) from None
        try:
            try:
                history = verify_history(
                    self._layout, allow_unregistered=True
                )
            except LayoutProblem as problem:
                raise _wrap(problem) from None
            archive_path, name = self._resolve_candidate(reference, history)

            try:
                raw = read_archive_bytes(archive_path)
                events = parse_event_lines(raw)
            except LayoutProblem as problem:
                raise _wrap(problem) from None
            if not events:
                raise RotationIntegrityError(
                    "the archive is empty and cannot be registered",
                    category="ARCHIVE_EMPTY",
                )
            segment_ids = {event.segment_id for event in events}
            if len(segment_ids) != 1:
                raise RotationIntegrityError(
                    "the archive contains more than one segment identity",
                    category="ARCHIVE_AMBIGUOUS",
                )
            segment_id = events[0].segment_id
            if segment_id in history.sealed_segment_ids:
                raise RotationIntegrityError(
                    "the segment is already sealed; a second seal is never "
                    "generated",
                    category="SEGMENT_ALREADY_SEALED",
                )

            try:
                summary = summarise_segment(
                    events,
                    expected_segment_id=segment_id,
                    expected_first_sequence=history.expected_next_sequence,
                    expected_previous_hash=history.expected_previous_hash,
                )
            except LayoutProblem as problem:
                raise _wrap(problem) from None

            if name != (
                f"audit-{summary.first_global_sequence}"
                f"-{summary.last_global_sequence}.jsonl"
            ):
                raise RotationIntegrityError(
                    "the archive file name does not match its own sequence "
                    "bounds",
                    category="ARCHIVE_NAME_MISMATCH",
                )

            sealed_at = self._timestamp()
            entry = ManifestEntry.create(
                manifest_sequence=len(history.manifest_entries) + 1,
                entry_type=ManifestEntryType.SEGMENT_SEALED,
                segment_id=segment_id,
                disposition=ManifestDisposition.ARCHIVED,
                segment_file=f"archive/{name}",
                first_global_sequence=summary.first_global_sequence,
                last_global_sequence=summary.last_global_sequence,
                record_count=summary.record_count,
                first_previous_hash=summary.first_previous_hash,
                last_record_hash=summary.last_record_hash,
                sealed_at=sealed_at,
                tool=ROTATION_TOOL,
                tool_version=ROTATION_TOOL_VERSION,
                previous_manifest_hash=history.last_manifest_hash
                or GENESIS_MANIFEST_HASH,
            )

            # Check the rest of the installation against the candidate history
            # *before* writing, so a seal is never appended into a state that
            # contradicts the remaining active journal.
            candidate = replace(
                history,
                manifest_entries=history.manifest_entries + (entry,),
                segments=history.segments + (summary,),
                audit_ids=history.audit_ids | summary.audit_ids,
            )
            try:
                verify_active_journal(self._layout, candidate)
            except RecoveryProblem as problem:
                # A missing active journal is the normal state after an
                # interrupted rotation; there is simply nothing left to check
                # against the new seal.
                if problem.category != ACTIVE_SEGMENT_MISSING:
                    raise _wrap(problem) from None
            except LayoutProblem as problem:
                raise _wrap(problem) from None

            store = ManifestStore(self._layout)
            return store.append(
                entry_type=ManifestEntryType.SEGMENT_SEALED,
                segment_id=segment_id,
                disposition=ManifestDisposition.ARCHIVED,
                segment_file=f"archive/{name}",
                first_global_sequence=summary.first_global_sequence,
                last_global_sequence=summary.last_global_sequence,
                record_count=summary.record_count,
                first_previous_hash=summary.first_previous_hash,
                last_record_hash=summary.last_record_hash,
                sealed_at=sealed_at,
                tool=ROTATION_TOOL,
                tool_version=ROTATION_TOOL_VERSION,
                assume_locked=True,
            )
        except ManifestStoreError as error:
            if isinstance(error, ManifestContendedError):
                raise RotationContendedError(_MSG_CONTENDED) from None
            raise RotationIntegrityError(
                _MSG_INTEGRITY, category="MANIFEST_WRITE_FAILED"
            ) from None
        except ManifestError:
            raise RotationIntegrityError(
                _MSG_INTEGRITY, category="MANIFEST_ENTRY_INVALID"
            ) from None
        finally:
            lock.release()

    def _resolve_candidate(
        self, reference: str | None, history: HistoryState
    ) -> tuple[Path, str]:
        layout = self._layout
        archive_directory = layout.archive_directory
        declared = {
            Path(entry.segment_file or "").name
            for entry in history.seals
            if entry.segment_file is not None
        }
        try:
            present = sorted(os.listdir(archive_directory))
        except OSError:
            raise RotationUnavailableError(_MSG_UNAVAILABLE) from None
        candidates = [
            name
            for name in present
            if name not in declared and ARCHIVE_FILENAME_PATTERN.fullmatch(name)
        ]

        if reference is None:
            if len(candidates) != 1:
                raise RotationRecoveryRequiredError(
                    "the archive to register is ambiguous; name it explicitly "
                    "with --register-existing PATH",
                    category="AMBIGUOUS_ARCHIVE",
                )
            name = candidates[0]
        else:
            name = reference.replace("\\", "/").rsplit("/", 1)[-1]
            if ARCHIVE_FILENAME_PATTERN.fullmatch(name) is None:
                raise RotationIntegrityError(
                    "the named archive is not an approved archive file name",
                    category="INVALID_ARCHIVE_NAME",
                )
            if name in declared:
                raise RotationIntegrityError(
                    "the named archive is already registered",
                    category="ARCHIVE_ALREADY_REGISTERED",
                )

        return archive_directory / name, name

    # -- recovery: recreate a missing active journal ------------------------

    def create_active(self) -> tuple[Path, str]:
        """Create the empty active journal after a seal, never overwriting."""
        try:
            lock = LayoutLock(self._layout)
            lock.acquire()
        except LockUnavailableError as error:
            raise _wrap_lock(error) from None
        try:
            history = self.inspect()
            if not history.seals:
                raise RotationIntegrityError(
                    "no sealed segment exists; a missing active journal for a "
                    "new installation is handled by the writer, not here",
                    category="NO_SEALED_HISTORY",
                )
            if os.path.lexists(self._layout.journal):
                raise RotationIntegrityError(
                    "the active journal already exists; it is never replaced",
                    category="ACTIVE_EXISTS",
                )
            try:
                _create_empty_file(self._layout.journal)
            except OSError:
                raise RotationUnavailableError(_MSG_UNAVAILABLE) from None
            try:
                size = self._layout.journal.stat().st_size
            except OSError:
                raise RotationUnavailableError(_MSG_UNAVAILABLE) from None
            if size != 0:
                raise RotationIntegrityError(
                    "the created active journal is not empty",
                    category="ACTIVE_NOT_EMPTY",
                )
            try:
                next_id = next_segment_id(
                    history.sealed_segment_ids,
                    moment=self._clock(),
                    reserved=(),
                )
            except SegmentAllocationError:
                raise RotationIntegrityError(
                    "segment ordinal 999 is exhausted for this allocation "
                    "month; an operator decision is required",
                    category="ORDINAL_EXHAUSTED",
                ) from None
            return self._layout.journal, next_id
        finally:
            lock.release()

    # -- retention ----------------------------------------------------------

    def record_retention(self, segment_id: str) -> ManifestEntry:
        """Record an owner-approved offsite retention decision.

        The archive must still be present and content-verified: the decision is
        recorded *before* the operator moves the media offsite, so it can never
        claim that a removed segment was verified afterwards. Nothing is moved
        or deleted here.
        """
        try:
            lock = LayoutLock(self._layout)
            lock.acquire()
        except LockUnavailableError as error:
            raise _wrap_lock(error) from None
        try:
            history = self.inspect()
            seal = next(
                (entry for entry in history.seals if entry.segment_id == segment_id),
                None,
            )
            if seal is None:
                raise RotationIntegrityError(
                    "the segment is not sealed; a retention decision must "
                    "reference an existing seal",
                    category="UNKNOWN_SEGMENT",
                )
            if segment_id in history.offsite_segment_ids:
                raise RotationIntegrityError(
                    "the segment already carries an offsite retention decision",
                    category="DUPLICATE_RETENTION_DECISION",
                )
            # The contents must be verifiable now: the decision is recorded
            # before any removal, never after it.
            try:
                verify_declared_archive(seal, self._layout)
            except LayoutProblem as problem:
                raise _wrap(problem) from None

            store = ManifestStore(self._layout)
            return store.append(
                entry_type=ManifestEntryType.RETENTION_DECISION,
                segment_id=seal.segment_id,
                disposition=ManifestDisposition.ARCHIVED_OFFSITE,
                segment_file=None,
                first_global_sequence=seal.first_global_sequence,
                last_global_sequence=seal.last_global_sequence,
                record_count=seal.record_count,
                first_previous_hash=seal.first_previous_hash,
                last_record_hash=seal.last_record_hash,
                sealed_at=self._timestamp(),
                tool=ROTATION_TOOL,
                tool_version=ROTATION_TOOL_VERSION,
                assume_locked=True,
            )
        except ManifestStoreError:
            raise RotationIntegrityError(
                _MSG_INTEGRITY, category="MANIFEST_WRITE_FAILED"
            ) from None
        finally:
            lock.release()

    # -- helpers ------------------------------------------------------------

    def _timestamp(self) -> str:
        moment = self._clock()
        try:
            return format_timestamp(moment)
        except Exception:
            raise RotationUnavailableError(
                "the operator clock returned an invalid instant"
            ) from None
