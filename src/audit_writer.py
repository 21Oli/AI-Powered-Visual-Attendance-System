"""Durable, append-only audit journal writer (Phase 5 — P5.5.3).

Implements the writer half of ``docs/AUDIT_JOURNAL_DESIGN.md`` §5–§7 for the
single active journal ``data/audit/audit.jsonl``, on top of the pure
schema/hash primitives in :mod:`src.audit_schema`.

What this module is
-------------------

* A **reusable infrastructure component** with no HTTP, business, database or
  manifest coupling. P5.5.4/P5.5.5 will call :meth:`AuditJournalWriter.append`;
  nothing else in the application is imported here.
* **Startup verification.** Construction reads the active journal, parses every
  complete record, validates it as an :class:`~src.audit_schema.AuditEvent`,
  verifies the whole chain (genesis, predecessor hashes, global and
  segment-local sequences, unique segment ids, digests) and only then becomes
  ``ready``. There is no repair, truncation, rewriting or history
  reconstruction: a torn final line, malformed record or chain defect raises a
  typed error and no writer is returned.
* **Durable append.** Each append writes exactly one canonical JSON line
  (``AuditEvent.to_json_line()`` + ``\\n``), flushes, ``os.fsync``s, and only
  then advances the in-memory sequence/hash state. A successful return is the
  durability boundary.
* **Poisoning.** Any exception after writing may have begun — write, flush,
  ``fsync``, a short write, an unexpected (non-OS) exception from any of them,
  a failed post-``fsync`` confirmation, or a file-size/inode/path-identity
  change since verification — is ambiguous. The instance is poisoned
  permanently, refuses every later append, and never retries, truncates or
  rolls back. Exceptions before writing begins (open, lock, pre-write stat) are
  sanitized and leave the writer usable, because no bytes could have been
  changed. Recovery requires a new writer, which performs full verification
  again (and will report the torn/duplicated tail honestly).
* **Path identity.** Before writing and after ``fsync`` the writer compares the
  opened handle's size/dev/inode with the verified state and re-stats the
  journal path, refusing and poisoning when the path no longer names the
  handle's file. On platforms that expose no file identity (``st_ino`` is 0)
  the check degrades to size equality; the writer makes no identity claim
  beyond what ``os.stat`` exposes (not a cryptographic or filesystem-level
  guarantee, and not protection against a privileged adversary).
* **Locking.** A module-level ``threading.Lock`` serializes append steps inside
  the process, and a non-blocking OS advisory lock (``msvcrt.locking`` on
  Windows, ``fcntl.flock`` on POSIX) is taken on the journal for the duration of
  each append (and briefly during startup verification) as defence in depth
  against an accidental second process. This is **not** multi-process support:
  contention fails closed with :class:`AuditJournalContendedError`, and a
  file-size/inode mismatch poisons a stale writer instead of guessing.

Storage contract
----------------

* UTF-8 JSON Lines, one canonical record per line, ``\\n`` terminated, appended
  only. Previously written bytes are never modified.
* ``data/audit/audit.jsonl`` by default (``PROJECT_ROOT`` based; an explicit
  path or ``DATA_DIR``-derived path can be supplied).
* **Initialization** (missing journal) is allowed only when the audit directory
  holds no ``manifest.jsonl`` and no non-empty ``archive/``: historical evidence
  without an active journal means the history cannot be verified, so the writer
  refuses (``AuditJournalIntegrityError``) rather than creating a new genesis.
* **Existing journal** → mandatory verification. When a manifest and/or
  archived segments are present (P5.5.7), the writer verifies the manifest
  chain, every declared archive and the active journal's boundary against the
  sealed history before becoming ``ready``. It refuses readiness for a missing
  declared archive, an unregistered archive, a duplicate segment, manifest
  corruption, a broken link or an ambiguous recovery state, and it never
  creates or repairs historical data.
* **Rotation continuity.** After a rotation the journal is an empty file with no
  identifier of its own: the next segment id is derived deterministically from
  the last sealed segment (same allocation month, ordinal + 1) and the first
  append links to the manifest's final record hash and continues the global
  sequence. Repeated restarts derive the same id.
* An **empty existing journal** is a valid empty chain (schema §15.3): the next
  append is the genesis record (sequence 1, ``previous_hash`` all zeroes) in a
  newly allocated ``YYYY-MM-NNN`` segment. A file truncated to zero is
  indistinguishable from a fresh empty journal without a manifest; that is the
  documented tamper-evidence limit, not silent repair.

Segment and timestamp rules
---------------------------

* The writer never rotates. It continues the verified journal's last segment
  (``segment_id`` unchanged, ``segment_sequence + 1``) regardless of the
  calendar month. Only a fresh/empty journal allocates a segment id, using the
  UTC allocation month at construction and ordinal ``001``.
* Segment ids are labels, never ordering proof; no timestamp-to-segment month
  comparison is performed.
* Timestamps must be timezone-aware UTC (formatted through the schema's fixed
  microsecond form) and **non-decreasing** within the active segment; a
  backwards timestamp is rejected before any write (``AuditSchemaError``).
* A caller-supplied ``audit_id`` must be unique against every record already in
  the verified journal; duplicates are rejected before any write.

Diagnostics and safety
----------------------

Exception messages are fixed and sanitized: no journal path, no OS error text,
no record contents, no identifiers, no hashes beyond the fact of a mismatch.
The journal path is available as a property for trusted server-side
diagnostics, and poisoning records one sanitized ``ERROR`` log line with the
journal's file name and a reason class.

Deferred (not implemented here): segment sealing, archives, rotation,
retention recording and the read-only verifier CLI live in
:mod:`src.audit_rotation`, :mod:`src.audit_archive` and :mod:`tools.audit_rotate`
(P5.5.7). The writer never rotates and never writes a manifest entry.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from enum import Enum
from pathlib import Path
from typing import IO, Final, Mapping

from src.audit_archive import (
    LayoutProblem,
    RecoveryProblem,
    UnavailableProblem,
    verify_active_journal,
    verify_history,
)
from src.audit_layout import AuditLayout, SegmentAllocationError, next_segment_id
from src.audit_schema import (
    AuditAction,
    AuditError,
    AuditEvent,
    AuditOutcome,
    AuditReasonCode,
    AuditSchemaError,
    AuditSource,
    AuditTargetType,
    GENESIS_PREVIOUS_HASH,
    canonical_json_bytes,
    format_timestamp,
    verify_chain,
)
from src.clock import Clock, utc_now
from src.config import PROJECT_ROOT
from src.user import Role

__all__ = [
    "ARCHIVE_DIRECTORY_NAME",
    "JOURNAL_FILENAME",
    "MANIFEST_FILENAME",
    "AuditJournalContendedError",
    "AuditJournalCorruptError",
    "AuditJournalError",
    "AuditJournalIntegrityError",
    "AuditJournalPoisonedError",
    "AuditJournalUnavailableError",
    "AuditJournalWriter",
    "WriterState",
]

_logger = logging.getLogger(__name__)

#: Directory under the data root that holds the audit journal.
AUDIT_DIRECTORY_NAME: Final[str] = "audit"
#: Active journal file name.
JOURNAL_FILENAME: Final[str] = "audit.jsonl"
#: Manifest file name (created and verified by P5.5.7, not here).
MANIFEST_FILENAME: Final[str] = "manifest.jsonl"
#: Archive directory name (used by P5.5.7, not here).
ARCHIVE_DIRECTORY_NAME: Final[str] = "archive"

#: Serializes append steps for every writer instance in this process. The
#: design specifies one process-local lock; sharing it globally also prevents
#: two same-process instances from interleaving appends or racing their state
#: checks.
_PROCESS_LOCK: Final[threading.Lock] = threading.Lock()

_MSG_UNAVAILABLE = "the audit journal is unavailable"
_MSG_POISONED = (
    "the audit journal writer is poisoned; a restart with full verification "
    "is required"
)
_MSG_CONTENDED = "the audit journal is locked or was modified by another writer"


class AuditJournalError(Exception):
    """Base class for every audit writer failure.

    Messages are fixed, sanitized strings: they never contain the journal path,
    raw OS error text, record contents, identifiers or secrets.
    """


class AuditJournalUnavailableError(AuditJournalError):
    """The journal could not be created, read, locked or durably written."""


class AuditJournalCorruptError(AuditJournalError):
    """A record is torn, malformed, unreadable or schema-invalid.

    ``line`` is the 1-based journal line (``None`` when not line-specific).
    """

    def __init__(self, message: str, *, line: int | None = None) -> None:
        super().__init__(message)
        self.line = line


class AuditJournalIntegrityError(AuditJournalError):
    """Full-chain verification failed (link, sequence, segment or digest)."""

    def __init__(
        self,
        message: str,
        *,
        position: int | None = None,
        sequence: int | None = None,
    ) -> None:
        super().__init__(message)
        self.position = position
        self.sequence = sequence


class AuditJournalPoisonedError(AuditJournalError):
    """The instance observed an ambiguous persistence outcome and is poisoned."""


class AuditJournalContendedError(AuditJournalError):
    """Another writer holds the journal lock or changed the file underneath."""


class WriterState(str, Enum):
    """Explicit writer lifecycle (design §5)."""

    NOT_INITIALIZED = "not_initialized"
    READY = "ready"
    POISONED = "poisoned"
    UNAVAILABLE = "unavailable"


class _ShortWrite(Exception):
    """Internal marker: the underlying write accepted fewer bytes than asked."""


# ---------------------------------------------------------------------------
# Filesystem / lock seams (module-level so tests can inject failures)
# ---------------------------------------------------------------------------


def _ensure_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)


def _open_append_handle(path: Path) -> IO[bytes]:
    """Open the journal for binary append (creates it when absent)."""
    return open(path, "ab")


def _open_for_verification(path: Path) -> IO[bytes]:
    """Open the existing journal read/write so the OS lock can be applied."""
    return open(path, "r+b")


def _write_line(handle: IO[bytes], payload: bytes) -> int:
    return handle.write(payload)


def _stat_handle(handle: IO[bytes]) -> os.stat_result:
    return os.fstat(handle.fileno())


def _stat_path(path: Path) -> os.stat_result:
    return os.stat(path)


def _flush_handle(handle: IO[bytes]) -> None:
    handle.flush()


def _fsync_file(handle: IO[bytes]) -> None:
    os.fsync(handle.fileno())


def _lock_handle(handle: IO[bytes]) -> None:
    """Take the non-blocking OS advisory lock on byte 0 of the journal."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle: IO[bytes]) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_unlock(handle: IO[bytes]) -> None:
    """Release the OS lock; closing the handle releases it anyway."""
    try:
        _unlock_handle(handle)
    except OSError:
        pass


def _default_journal_path() -> Path:
    return PROJECT_ROOT / "data" / AUDIT_DIRECTORY_NAME / JOURNAL_FILENAME


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class AuditJournalWriter:
    """The single active audit journal, verified before use.

    Construction performs full verification and raises on any defect:

    * :class:`AuditJournalUnavailableError` — directory/file/permission/clock
      or other I/O failure.
    * :class:`AuditJournalCorruptError` — a torn, malformed or schema-invalid
      record (``line`` is set).
    * :class:`AuditJournalIntegrityError` — the chain, sequences, links,
      segment ids, digests or timestamp monotonicity are invalid, or historical
      artifacts exist without a verifiable active journal.
    * :class:`AuditJournalContendedError` — the journal lock is held.

    A constructed writer is ``READY`` and has exclusive logical use of the file
    for its lifetime; creating a second writer on the same path is detected at
    append time (the size/inode change poisons the stale one).
    """

    def __init__(
        self,
        journal_path: Path | str | None = None,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._state: WriterState = WriterState.NOT_INITIALIZED
        self._journal_path: Path = (
            Path(journal_path) if journal_path is not None else _default_journal_path()
        )
        self._clock: Clock = clock
        self._last_sequence: int = 0
        self._last_hash: str = GENESIS_PREVIOUS_HASH
        self._active_segment_id: str | None = None
        self._segment_sequence: int = 0
        self._last_timestamp: str | None = None
        self._audit_ids: set[str] = set()
        self._expected_size: int = 0
        self._expected_identity: tuple[int, int] | None = None
        self._pending_segment_id: str | None = None
        try:
            self._initialize()
        except AuditJournalError:
            # No usable writer exists; the typed error is the report.
            self._state = WriterState.UNAVAILABLE
            raise
        self._state = WriterState.READY

    # -- public state -------------------------------------------------------

    @property
    def state(self) -> WriterState:
        return self._state

    @property
    def is_ready(self) -> bool:
        return self._state is WriterState.READY

    @property
    def journal_path(self) -> Path:
        """Absolute journal path (trusted server-side diagnostics only)."""
        return self._journal_path

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    @property
    def last_record_hash(self) -> str:
        return self._last_hash

    @property
    def active_segment_id(self) -> str | None:
        return self._active_segment_id

    # -- initialization -----------------------------------------------------

    def _initialize(self) -> None:
        path = self._journal_path
        directory = path.parent
        try:
            if directory.exists() and not directory.is_dir():
                raise AuditJournalUnavailableError(_MSG_UNAVAILABLE)
            if not directory.exists():
                _ensure_directory(directory)
        except AuditJournalError:
            raise
        except OSError:
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None

        manifest = directory / MANIFEST_FILENAME
        archive = directory / ARCHIVE_DIRECTORY_NAME
        archive_has_files = False
        if archive.exists():
            if not archive.is_dir():
                raise AuditJournalIntegrityError(
                    "audit archive path is not a directory; refusing to start"
                )
            try:
                archive_has_files = any(archive.iterdir())
            except OSError:
                raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None

        try:
            journal_exists = path.exists()
        except OSError:
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None

        if not journal_exists:
            if manifest.exists() or archive_has_files:
                # Historical evidence without an active journal: the history
                # cannot be verified, so a new genesis journal is forbidden.
                raise AuditJournalIntegrityError(
                    "audit history exists but the active journal is missing; "
                    "refusing to initialize a new chain (use "
                    "tools.audit_rotate --create-active after verifying the "
                    "installation)"
                )
            try:
                with _open_append_handle(path):
                    pass
                created_stat = os.stat(path)
            except OSError:
                raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None
            if created_stat.st_size:
                # Another writer created and populated the journal between the
                # existence check and our open: verify it instead of assuming
                # genesis (never overwrite or append blindly).
                self._load_and_verify()
                return
            self._prepare_empty()
            return

        if path.is_dir():
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE)
        if manifest.exists() or archive_has_files:
            self._adopt_sealed_history()
            return
        self._load_and_verify()

    def _adopt_sealed_history(self) -> None:
        """Verify sealed history and the active journal's boundary against it.

        Raises a typed, sanitized error and leaves no ``ready`` writer when any
        part of the history is missing, unregistered, duplicated or broken.
        Nothing is created or repaired here.
        """
        layout = AuditLayout.from_journal(self._journal_path)
        try:
            history = verify_history(layout)
            active = verify_active_journal(layout, history)
        except LayoutProblem as problem:
            raise self._map_layout_problem(problem) from None

        try:
            stat_result = os.stat(self._journal_path)
        except OSError:
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None
        self._expected_size = stat_result.st_size
        self._expected_identity = self._identity(stat_result)
        self._audit_ids = set(history.audit_ids)
        if active.summary is not None:
            self._audit_ids |= set(active.summary.audit_ids)

        if active.events:
            last = active.events[-1]
            self._last_sequence = last.sequence
            self._last_hash = last.record_hash
            self._active_segment_id = last.segment_id
            self._segment_sequence = last.segment_sequence
            self._last_timestamp = last.timestamp
            self._pending_segment_id = None
            return

        # Empty active journal after a rotation: derive the pending segment id
        # from persisted state so every restart agrees.
        seal = history.last_seal
        if seal is not None:
            self._last_sequence = seal.last_global_sequence
            self._last_hash = seal.last_record_hash
        try:
            self._pending_segment_id = next_segment_id(
                history.sealed_segment_ids, moment=self._clock()
            )
        except SegmentAllocationError:
            raise AuditJournalIntegrityError(
                "no further segment identifier can be allocated; an operator "
                "decision is required",
            ) from None
        self._active_segment_id = None
        self._segment_sequence = 0
        self._last_timestamp = None

    @staticmethod
    def _map_layout_problem(problem: LayoutProblem) -> AuditJournalError:
        if isinstance(problem, UnavailableProblem):
            return AuditJournalUnavailableError(_MSG_UNAVAILABLE)
        if isinstance(problem, RecoveryProblem):
            return AuditJournalIntegrityError(
                "sealed audit history requires explicit operator recovery "
                f"({problem.category}); refusing to append",
                sequence=None,
            )
        return AuditJournalIntegrityError(
            "sealed audit history verification failed "
            f"({problem.category}); refusing to append"
        )

    def _prepare_empty(self) -> None:
        """State for an empty (new or zero-length) journal."""
        stat = self._stat_journal()
        self._expected_size = stat.st_size
        self._expected_identity = self._identity(stat)
        self._allocate_segment_id()

    def _allocate_segment_id(self) -> None:
        moment = self._clock()
        try:
            formatted = format_timestamp(moment)
        except AuditError:
            raise AuditJournalUnavailableError(
                "the audit clock returned an invalid instant"
            ) from None
        self._pending_segment_id = f"{formatted[:7]}-001"
        self._active_segment_id = None
        self._segment_sequence = 0

    def _load_and_verify(self) -> None:
        handle = self._open_verification_handle()
        try:
            try:
                _lock_handle(handle)
            except OSError as exc:
                raise self._map_lock_error(exc) from None
            try:
                raw = self._read_all(handle)
                try:
                    stat = _stat_handle(handle)
                except OSError:
                    raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None
            finally:
                _safe_unlock(handle)
        finally:
            try:
                handle.close()
            except OSError:
                pass

        events = self._parse_records(raw)
        self._verify_records(events)
        self._expected_size = stat.st_size
        self._expected_identity = self._identity(stat)
        if not events:
            self._allocate_segment_id()
            return
        last = events[-1]
        self._last_sequence = last.sequence
        self._last_hash = last.record_hash
        self._active_segment_id = last.segment_id
        self._segment_sequence = last.segment_sequence
        self._last_timestamp = last.timestamp
        self._pending_segment_id = None

    def _open_verification_handle(self) -> IO[bytes]:
        try:
            return _open_for_verification(self._journal_path)
        except OSError:
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None

    def _read_all(self, handle: IO[bytes]) -> bytes:
        try:
            handle.seek(0)
            return handle.read()
        except OSError:
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None

    def _parse_records(self, raw: bytes) -> list[AuditEvent]:
        if raw and not raw.endswith(b"\n"):
            raise AuditJournalCorruptError(
                "audit journal has an incomplete final record",
                line=raw.count(b"\n") + 1,
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise AuditJournalCorruptError(
                "audit journal is not valid UTF-8"
            ) from None
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        events: list[AuditEvent] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                raise AuditJournalCorruptError(
                    "audit journal contains an empty record", line=index
                )
            try:
                payload = json.loads(line)
            except ValueError:
                raise AuditJournalCorruptError(
                    "audit journal contains malformed JSON", line=index
                ) from None
            try:
                events.append(AuditEvent.from_dict(payload))
            except AuditError:
                raise AuditJournalCorruptError(
                    "audit journal record violates the approved schema",
                    line=index,
                ) from None
        return events

    def _verify_records(self, events: list[AuditEvent]) -> None:
        try:
            verify_chain(events)
        except AuditError as exc:
            raise AuditJournalIntegrityError(
                "audit journal chain verification failed: "
                f"{type(exc).__name__}",
                position=getattr(exc, "position", None),
                sequence=getattr(exc, "sequence", None),
            ) from None

        seen_ids: set[str] = set()
        previous_timestamp: str | None = None
        for index, event in enumerate(events):
            if event.audit_id in seen_ids:
                raise AuditJournalIntegrityError(
                    "audit journal contains a duplicate audit id",
                    position=index,
                    sequence=event.sequence,
                )
            seen_ids.add(event.audit_id)
            if previous_timestamp is not None and event.timestamp < previous_timestamp:
                raise AuditJournalIntegrityError(
                    "audit journal timestamps are not non-decreasing",
                    position=index,
                    sequence=event.sequence,
                )
            previous_timestamp = event.timestamp
        self._audit_ids = seen_ids

    def _stat_journal(self) -> os.stat_result:
        try:
            return _stat_path(self._journal_path)
        except OSError:
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None

    @staticmethod
    def _identity(stat: os.stat_result) -> tuple[int, int] | None:
        if not getattr(stat, "st_ino", 0):
            return None
        return (stat.st_dev, stat.st_ino)

    # -- append -------------------------------------------------------------

    def append(
        self,
        *,
        action: AuditAction,
        outcome: AuditOutcome,
        reason_code: AuditReasonCode,
        source: AuditSource,
        actor_user_id: str | None = None,
        actor_role: Role | None = None,
        target_type: AuditTargetType | None = None,
        target_id: str | None = None,
        correlation_id: str | None = None,
        request_id: str | None = None,
        metadata: Mapping[str, str | int | bool] | None = None,
        timestamp: object | None = None,
        audit_id: str | None = None,
    ) -> AuditEvent:
        """Durably append one validated record and return it.

        The writer supplies ``sequence``, ``segment_id``, ``segment_sequence``
        and ``previous_hash`` from verified state; the caller supplies the
        event's meaning. ``timestamp`` defaults to the injected clock.

        Raises :class:`AuditJournalPoisonedError` once the instance is
        poisoned, :class:`AuditJournalContendedError` on lock contention or a
        file-size/inode change, :class:`AuditJournalUnavailableError` for I/O
        failure, and :class:`~src.audit_schema.AuditSchemaError` /
        :class:`~src.audit_schema.AuditTimestampError` for invalid input (no
        write occurs for input errors).
        """
        with _PROCESS_LOCK:
            self._require_ready()

            timestamp_value = self._clock() if timestamp is None else timestamp
            formatted = format_timestamp(timestamp_value)  # type: ignore[arg-type]
            if (
                self._last_timestamp is not None
                and formatted < self._last_timestamp
            ):
                raise AuditSchemaError(
                    "timestamp must not precede the active segment's last "
                    "record"
                )

            segment_id = self._active_segment_id or self._pending_segment_id
            if segment_id is None:  # pragma: no cover - defensive
                raise AuditJournalUnavailableError(_MSG_UNAVAILABLE)
            segment_sequence = self._segment_sequence + 1

            chosen_audit_id = (
                audit_id if audit_id is not None else str(uuid.uuid4())
            )

            event = AuditEvent.create(
                sequence=self._last_sequence + 1,
                segment_id=segment_id,
                segment_sequence=segment_sequence,
                action=action,
                outcome=outcome,
                reason_code=reason_code,
                source=source,
                previous_hash=self._last_hash,
                timestamp=timestamp_value,  # type: ignore[arg-type]
                actor_user_id=actor_user_id,
                actor_role=actor_role,
                target_type=target_type,
                target_id=target_id,
                correlation_id=correlation_id,
                request_id=request_id,
                metadata=metadata,
                audit_id=chosen_audit_id,
            )
            if event.audit_id in self._audit_ids:
                raise AuditSchemaError(
                    "audit_id already exists in the verified journal"
                )
            payload = canonical_json_bytes(event.as_body()) + b"\n"

            self._write_and_sync(payload, event)
            return event

    def _write_and_sync(self, payload: bytes, event: AuditEvent) -> None:
        try:
            handle = _open_append_handle(self._journal_path)
        except OSError:
            # No write was attempted; the writer stays usable.
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None

        try:
            try:
                _lock_handle(handle)
            except OSError as exc:
                raise self._map_lock_error(exc) from None
            try:
                self._assert_unchanged(handle)
                self._write_durably(handle, payload)
                stat = self._confirm_durable(handle, len(payload))
            finally:
                _safe_unlock(handle)
        finally:
            self._close_quietly(handle)

        # Durability and identity confirmed: advance state only now.
        self._expected_size += len(payload)
        confirmed_identity = self._identity(stat)
        if confirmed_identity is not None:
            self._expected_identity = confirmed_identity
        self._last_sequence = event.sequence
        self._last_hash = event.record_hash
        self._active_segment_id = event.segment_id
        self._segment_sequence = event.segment_sequence
        self._last_timestamp = event.timestamp
        self._audit_ids.add(event.audit_id)

    def _write_durably(self, handle: IO[bytes], payload: bytes) -> None:
        """Write, flush and fsync one record; any failure here is ambiguous."""
        try:
            written = _write_line(handle, payload)
            if written != len(payload):
                raise _ShortWrite()
            _flush_handle(handle)
            _fsync_file(handle)
        except _ShortWrite:
            self._poison("short write")
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None
        except OSError:
            self._poison("write/flush/fsync failure")
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None
        except Exception:
            # An unexpected (non-OS) exception cannot prove what reached the
            # file; treat it exactly like an I/O failure. Messages are never
            # propagated, so injected exception text cannot leak.
            self._poison("unexpected write/flush/fsync failure")
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None

    def _confirm_durable(
        self, handle: IO[bytes], payload_length: int
    ) -> os.stat_result:
        """Verify size, identity and path after fsync; poison on any mismatch."""
        try:
            stat = _stat_handle(handle)
        except OSError:
            self._poison("could not confirm the journal after fsync")
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None
        except Exception:
            self._poison("could not confirm the journal after fsync")
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None

        expected_size = self._expected_size + payload_length
        if stat.st_size != expected_size:
            # Another writer appended between our fsync and the size check.
            self._poison("file size does not match the appended record")
            raise AuditJournalContendedError(_MSG_CONTENDED) from None
        identity = self._identity(stat)
        if (
            self._expected_identity is not None
            and identity is not None
            and identity != self._expected_identity
        ):
            self._poison("journal file was replaced during append")
            raise AuditJournalContendedError(_MSG_CONTENDED) from None
        if not self._path_matches(stat):
            self._poison("journal path no longer refers to the appended file")
            raise AuditJournalContendedError(_MSG_CONTENDED) from None
        return stat

    def _assert_unchanged(self, handle: IO[bytes]) -> None:
        """Pre-write checks; I/O failure here is sanitized and not poisoning."""
        try:
            stat = _stat_handle(handle)
        except OSError:
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None
        except Exception:
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE) from None
        if stat.st_size != self._expected_size:
            self._poison("file size changed since verification")
            raise AuditJournalContendedError(_MSG_CONTENDED)
        identity = self._identity(stat)
        if (
            self._expected_identity is not None
            and identity is not None
            and identity != self._expected_identity
        ):
            self._poison("journal file was replaced since verification")
            raise AuditJournalContendedError(_MSG_CONTENDED)
        if not self._path_matches(stat):
            self._poison("journal path no longer refers to the verified file")
            raise AuditJournalContendedError(_MSG_CONTENDED)

    def _path_matches(self, handle_stat: os.stat_result) -> bool:
        """True when the journal path still names the opened handle's file.

        Compares device/inode when the platform exposes them and falls back to
        size equality otherwise; a missing or unreadable path is a mismatch.
        """
        try:
            path_stat = _stat_path(self._journal_path)
        except OSError:
            return False
        except Exception:
            return False
        identity = self._identity(handle_stat)
        path_identity = self._identity(path_stat)
        if identity is not None and path_identity is not None:
            return (
                identity == path_identity
                and path_stat.st_size == handle_stat.st_size
            )
        return path_stat.st_size == handle_stat.st_size

    @staticmethod
    def _close_quietly(handle: IO[bytes]) -> None:
        try:
            handle.close()
        except OSError:
            pass
        except Exception:
            pass

    def _map_lock_error(self, exc: OSError) -> AuditJournalError:
        if isinstance(exc, BlockingIOError):
            return AuditJournalContendedError(_MSG_CONTENDED)
        if exc.errno in {13, 36}:  # EACCES / EDEADLK on Windows byte locks
            return AuditJournalContendedError(_MSG_CONTENDED)
        return AuditJournalUnavailableError(_MSG_UNAVAILABLE)

    def _require_ready(self) -> None:
        if self._state is WriterState.POISONED:
            raise AuditJournalPoisonedError(_MSG_POISONED)
        if self._state is not WriterState.READY:
            raise AuditJournalUnavailableError(_MSG_UNAVAILABLE)

    def _poison(self, reason: str) -> None:
        if self._state is WriterState.POISONED:
            return
        self._state = WriterState.POISONED
        _logger.error(
            "audit journal writer poisoned (%s): %s",
            self._journal_path.name,
            reason,
        )
