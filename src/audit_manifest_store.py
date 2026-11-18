"""Durable append-only persistence for the audit manifest (P5.5.7 stage A).

The manifest is ``data/audit/manifest.jsonl``: one canonical JSON entry per
line, UTF-8, ``\\n`` terminated, append only. This module is the only writer of
that file.

Durability contract
===================

* the whole existing manifest is re-read and its chain verified before every
  append; a torn tail, malformed line, schema breach, broken link, duplicate
  seal or invalid retention reference refuses the append;
* the candidate entry is validated **in memory** together with the whole
  existing chain before a single byte is written, so a rejected entry never
  reaches the file;
* the written bytes are ``canonical_json_text(entry) + "\\n"`` through the
  accepted :mod:`src.audit_manifest` model, followed by ``flush`` and
  ``os.fsync``; the file size and identity are re-confirmed afterwards and the
  in-memory state advances only then;
* there is no truncate, no rewrite, no repair, no normalisation and no
  automatic recovery anywhere in this module.

A failed or ambiguous persistence attempt poisons the instance permanently: it
refuses further appends and reports a sanitized error, exactly like the journal
writer. Messages never contain the manifest path, OS error text or entry
values.

The append takes the installation's ``.rotation.lock`` so rotation and manifest
appends can never interleave. A caller that already holds that lock (the
rotation tool) passes ``assume_locked=True``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final, Sequence

from src.audit_archive import (
    IntegrityProblem,
    LayoutProblem,
    RecoveryProblem,
    UnavailableProblem,
    read_manifest_entries,
)
from src.audit_layout import AuditLayout
from src.audit_locking import LayoutLock, LockContendedError, LockUnavailableError
from src.audit_manifest import (
    GENESIS_MANIFEST_HASH,
    ManifestChainError,
    ManifestDisposition,
    ManifestEntry,
    ManifestEntryType,
    ManifestError,
    ManifestVerification,
    compute_entry_hash,
    verify_manifest,
)

__all__ = [
    "MANIFEST_TOOL",
    "MANIFEST_TOOL_VERSION",
    "ManifestContendedError",
    "ManifestCorruptError",
    "ManifestIntegrityError",
    "ManifestPoisonedError",
    "ManifestStore",
    "ManifestStoreError",
    "ManifestUnavailableError",
]

#: Stable tool provenance recorded in every manifest entry written by this
#: milestone. It is a label, not a security control.
MANIFEST_TOOL: Final[str] = "audit_rotate"
MANIFEST_TOOL_VERSION: Final[str] = "5.5.7"

_MSG_UNAVAILABLE = "the audit manifest is unavailable"
_MSG_INTEGRITY = "the audit manifest failed integrity verification"
_MSG_CONTENDED = "the audit manifest is locked or was changed by another writer"
_MSG_POISONED = (
    "the audit manifest writer observed an ambiguous result and is poisoned; "
    "operator investigation is required"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ManifestStoreError(Exception):
    """Base class for manifest persistence failures (sanitized messages)."""


class ManifestUnavailableError(ManifestStoreError):
    """The manifest could not be created, read or durably appended."""


class ManifestCorruptError(ManifestStoreError):
    """A manifest line is torn, malformed or schema-invalid.

    ``line`` is the 1-based manifest line and ``recovery`` is true when the
    state is a documented crash-recovery state (a torn final line) rather than
    arbitrary corruption.
    """

    def __init__(
        self,
        message: str,
        *,
        line: int | None = None,
        recovery: bool = False,
    ) -> None:
        super().__init__(message)
        self.line = line
        self.recovery = recovery


class ManifestIntegrityError(ManifestStoreError):
    """The manifest chain, continuity or uniqueness rule failed."""

    def __init__(
        self,
        message: str,
        *,
        position: int | None = None,
        manifest_sequence: int | None = None,
    ) -> None:
        super().__init__(message)
        self.position = position
        self.manifest_sequence = manifest_sequence


class ManifestContendedError(ManifestStoreError):
    """Another process holds the lock or changed the file underneath."""


class ManifestPoisonedError(ManifestStoreError):
    """The instance observed an ambiguous outcome and refuses further writes."""


# ---------------------------------------------------------------------------
# Filesystem seams (module level so tests can inject failures)
# ---------------------------------------------------------------------------


def _open_manifest_append(path: Path) -> IO[bytes]:
    return open(path, "ab")


def _write_manifest_line(handle: IO[bytes], payload: bytes) -> int:
    return handle.write(payload)


def _flush_manifest(handle: IO[bytes]) -> None:
    handle.flush()


def _fsync_manifest(handle: IO[bytes]) -> None:
    os.fsync(handle.fileno())


def _stat_manifest_handle(handle: IO[bytes]) -> os.stat_result:
    return os.fstat(handle.fileno())


def _stat_manifest_path(path: Path) -> os.stat_result:
    return os.stat(path)


def _identity(stat: os.stat_result) -> tuple[int, int] | None:
    if not getattr(stat, "st_ino", 0):
        return None
    return (stat.st_dev, stat.st_ino)


class _ShortWrite(Exception):
    """Internal marker: fewer bytes were accepted than requested."""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class ManifestStore:
    """An append-only manifest with verified in-memory state."""

    layout: AuditLayout
    lock: LayoutLock | None = None
    _entries: list[ManifestEntry] | None = None
    _verification: ManifestVerification | None = None
    _expected_size: int = 0
    _expected_identity: tuple[int, int] | None = None
    _poisoned: bool = False

    # -- state --------------------------------------------------------------

    @property
    def entries(self) -> tuple[ManifestEntry, ...]:
        return tuple(self._entries or ())

    @property
    def verification(self) -> ManifestVerification | None:
        return self._verification

    @property
    def last_hash(self) -> str:
        entries = self._entries or []
        return entries[-1].entry_hash if entries else GENESIS_MANIFEST_HASH

    @property
    def next_sequence(self) -> int:
        return len(self._entries or ()) + 1

    @property
    def is_poisoned(self) -> bool:
        return self._poisoned

    # -- loading ------------------------------------------------------------

    def load(self) -> ManifestVerification:
        """Read and verify the manifest; cache its state for later appends."""
        path = self.layout.manifest
        if not os.path.lexists(path):
            self._entries = []
            self._verification = verify_manifest(())
            self._expected_size = 0
            self._expected_identity = None
            return self._verification

        try:
            entries = read_manifest_entries(path)
        except RecoveryProblem as problem:
            raise ManifestCorruptError(
                "the manifest ends with an incomplete entry; operator "
                "investigation is required",
                line=problem.line,
                recovery=True,
            ) from None
        except UnavailableProblem:
            raise ManifestUnavailableError(_MSG_UNAVAILABLE) from None
        except IntegrityProblem as problem:
            raise ManifestCorruptError(
                "the manifest is not readable JSONL of approved entries",
                line=problem.line,
            ) from None

        try:
            verification = verify_manifest(entries)
        except ManifestChainError as error:
            raise ManifestIntegrityError(
                _MSG_INTEGRITY,
                position=getattr(error, "position", None),
                manifest_sequence=getattr(error, "manifest_sequence", None),
            ) from None
        except ManifestError:
            raise ManifestIntegrityError(_MSG_INTEGRITY) from None

        try:
            stat_result = _stat_manifest_path(path)
        except OSError:
            raise ManifestUnavailableError(_MSG_UNAVAILABLE) from None

        self._entries = list(entries)
        self._verification = verification
        self._expected_size = stat_result.st_size
        self._expected_identity = _identity(stat_result)
        return verification

    # -- appending ----------------------------------------------------------

    def append(
        self,
        *,
        entry_type: ManifestEntryType | str,
        segment_id: str,
        disposition: ManifestDisposition | str,
        segment_file: str | None,
        first_global_sequence: int,
        last_global_sequence: int,
        record_count: int,
        first_previous_hash: str,
        last_record_hash: str,
        sealed_at: str,
        tool: str = MANIFEST_TOOL,
        tool_version: str = MANIFEST_TOOL_VERSION,
        assume_locked: bool = False,
    ) -> ManifestEntry:
        """Build, validate and durably append exactly one manifest entry."""
        if self._poisoned:
            raise ManifestPoisonedError(_MSG_POISONED)
        if self._entries is None:
            self.load()

        lock = None
        if not assume_locked:
            lock = self.lock or LayoutLock(self.layout)
            try:
                lock.acquire()
            except LockContendedError:
                raise ManifestContendedError(_MSG_CONTENDED) from None
            except LockUnavailableError:
                raise ManifestUnavailableError(_MSG_UNAVAILABLE) from None

        try:
            # Re-read on every append: another process may have appended while
            # this instance was idle, and appending on stale state could create
            # a fork in the chain.
            try:
                self.load()
            except (ManifestCorruptError, ManifestIntegrityError):
                raise
            except ManifestStoreError:
                raise
            except Exception:  # pragma: no cover - defensive
                raise ManifestUnavailableError(_MSG_UNAVAILABLE) from None

            entry = ManifestEntry.create(
                manifest_sequence=self.next_sequence,
                entry_type=entry_type,
                segment_id=segment_id,
                disposition=disposition,
                segment_file=segment_file,
                first_global_sequence=first_global_sequence,
                last_global_sequence=last_global_sequence,
                record_count=record_count,
                first_previous_hash=first_previous_hash,
                last_record_hash=last_record_hash,
                sealed_at=sealed_at,
                tool=tool,
                tool_version=tool_version,
                previous_manifest_hash=self.last_hash,
            )

            candidate = list(self._entries or ()) + [entry]
            try:
                verify_manifest(candidate)
            except ManifestChainError as error:
                raise ManifestIntegrityError(
                    _MSG_INTEGRITY,
                    position=getattr(error, "position", None),
                    manifest_sequence=getattr(error, "manifest_sequence", None),
                ) from None
            except ManifestError:
                raise ManifestIntegrityError(_MSG_INTEGRITY) from None

            self._append_durably(entry)
            return entry
        finally:
            if lock is not None:
                lock.release()

    def _append_durably(self, entry: ManifestEntry) -> None:
        payload = entry.to_json_line().encode("utf-8") + b"\n"
        path = self.layout.manifest
        try:
            handle = _open_manifest_append(path)
        except OSError:
            raise ManifestUnavailableError(_MSG_UNAVAILABLE) from None

        try:
            self._assert_unchanged(handle)
            try:
                written = _write_manifest_line(handle, payload)
                if written != len(payload):
                    raise _ShortWrite()
                _flush_manifest(handle)
                _fsync_manifest(handle)
            except _ShortWrite:
                self._poison()
                raise ManifestUnavailableError(_MSG_UNAVAILABLE) from None
            except OSError:
                self._poison()
                raise ManifestUnavailableError(_MSG_UNAVAILABLE) from None
            except Exception:
                self._poison()
                raise ManifestUnavailableError(_MSG_UNAVAILABLE) from None
            stat_result = self._confirm(handle, len(payload))
        finally:
            try:
                handle.close()
            except OSError:
                pass

        self._expected_size += len(payload)
        confirmed = _identity(stat_result)
        if confirmed is not None:
            self._expected_identity = confirmed
        if self._entries is None:  # pragma: no cover - defensive
            self._entries = []
        self._entries.append(entry)
        try:
            self._verification = verify_manifest(self._entries)
        except ManifestError:  # pragma: no cover - just verified in memory
            self._poison()
            raise ManifestIntegrityError(_MSG_INTEGRITY) from None

    def _assert_unchanged(self, handle: IO[bytes]) -> None:
        try:
            stat_result = _stat_manifest_handle(handle)
        except OSError:
            raise ManifestUnavailableError(_MSG_UNAVAILABLE) from None
        except Exception:
            raise ManifestUnavailableError(_MSG_UNAVAILABLE) from None
        if stat_result.st_size != self._expected_size:
            self._poison()
            raise ManifestContendedError(_MSG_CONTENDED)
        identity = _identity(stat_result)
        if (
            self._expected_identity is not None
            and identity is not None
            and identity != self._expected_identity
        ):
            self._poison()
            raise ManifestContendedError(_MSG_CONTENDED)

    def _confirm(self, handle: IO[bytes], payload_length: int) -> os.stat_result:
        try:
            stat_result = _stat_manifest_handle(handle)
        except Exception:
            self._poison()
            raise ManifestUnavailableError(_MSG_UNAVAILABLE) from None
        if stat_result.st_size != self._expected_size + payload_length:
            self._poison()
            raise ManifestContendedError(_MSG_CONTENDED)
        try:
            path_stat = _stat_manifest_path(self.layout.manifest)
        except OSError:
            self._poison()
            raise ManifestContendedError(_MSG_CONTENDED) from None
        identity = _identity(stat_result)
        path_identity = _identity(path_stat)
        if identity is not None and path_identity is not None:
            if identity != path_identity or path_stat.st_size != stat_result.st_size:
                self._poison()
                raise ManifestContendedError(_MSG_CONTENDED)
        elif path_stat.st_size != stat_result.st_size:
            self._poison()
            raise ManifestContendedError(_MSG_CONTENDED)
        return stat_result

    def _poison(self) -> None:
        self._poisoned = True
