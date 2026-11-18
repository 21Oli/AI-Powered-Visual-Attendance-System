"""Advisory locks for the mutating audit tools (P5.5.7).

Two locks exist, both **advisory** and both non-blocking:

* the layout lock ``data/audit/.rotation.lock`` serialises every operation that
  changes the installation layout or the manifest (rotation, recovery
  registration, missing-active recreation, retention decisions, manifest
  appends). It is an ordinary file in the audit directory and holds no secrets;
* the journal lease takes the writer's own byte-0 exclusive lock on
  ``audit.jsonl`` so rotation can prove that no writer is currently appending.

Neither is a multi-process guarantee: they fail closed on contention, and the
rotation documentation requires the application to be stopped. A process that
does not honour advisory locks is not stopped by them, and the tools make no
claim to protect against a privileged adversary or a non-cooperating process.

Nothing here is imported by the read-only verifier.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final

from src.audit_layout import AuditLayout

__all__ = [
    "JournalLease",
    "LayoutLock",
    "LockContendedError",
    "LockUnavailableError",
]

_MSG_CONTENDED: Final[str] = (
    "another audit operation holds the lock; stop the application and retry"
)
_MSG_UNAVAILABLE: Final[str] = "the audit lock could not be taken"


class LockUnavailableError(Exception):
    """The lock file or target could not be opened or locked."""


class LockContendedError(LockUnavailableError):
    """Another process holds the lock."""


def _lock(handle: IO[bytes]) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: IO[bytes]) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _map_error(exc: OSError) -> LockUnavailableError:
    if isinstance(exc, BlockingIOError):
        return LockContendedError(_MSG_CONTENDED)
    if exc.errno in {13, 36, 11}:  # EACCES / EDEADLK (Windows) / EAGAIN
        return LockContendedError(_MSG_CONTENDED)
    return LockUnavailableError(_MSG_UNAVAILABLE)


@dataclass
class LayoutLock:
    """Exclusive advisory lock on the installation's ``.rotation.lock``."""

    layout: AuditLayout
    _handle: IO[bytes] | None = field(default=None, repr=False)

    def acquire(self) -> None:
        if self._handle is not None:  # pragma: no cover - defensive
            return
        directory = self.layout.directory
        if not directory.is_dir():
            raise LockUnavailableError(_MSG_UNAVAILABLE)
        path: Path = self.layout.rotation_lock
        try:
            handle = open(path, "a+b")
        except OSError:
            raise LockUnavailableError(_MSG_UNAVAILABLE) from None
        try:
            _lock(handle)
        except OSError as exc:
            try:
                handle.close()
            except OSError:
                pass
            raise _map_error(exc) from None
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            _unlock(handle)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass

    def __enter__(self) -> "LayoutLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


@dataclass
class JournalLease:
    """The writer's own exclusive byte-0 lock, used to detect a live writer."""

    path: Path
    _handle: IO[bytes] | None = field(default=None, repr=False)

    def acquire(self) -> None:
        if self._handle is not None:  # pragma: no cover - defensive
            return
        try:
            handle = open(self.path, "r+b")
        except OSError:
            raise LockUnavailableError(_MSG_UNAVAILABLE) from None
        try:
            _lock(handle)
        except OSError as exc:
            try:
                handle.close()
            except OSError:
                pass
            raise _map_error(exc) from None
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            _unlock(handle)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass

    def __enter__(self) -> "JournalLease":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
