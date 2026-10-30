"""Windows single-process startup guard for the Phase 3 development API.

Step 1.4 deliverable: an OS-enforced exclusive lock (``msvcrt.locking``
byte-range lock on one byte at offset 0 of ``DATA_DIR/api_guard.lock``)
held for the guarded server's entire process lifetime. A second API
process cannot begin serving while the first holds the guard.

Approved contract (Phase 0 sign-off, finalized before implementation):

* **Windows-only.** Implemented with the stdlib ``msvcrt`` module on
  ``win32``. POSIX behavior is deliberately out of scope: non-Windows
  platforms raise :class:`StartupGuardUnavailableError` deterministically
  instead of getting a silent advisory/Python-only fallback (which could
  never guarantee true exclusivity).
* **Lock file:** ``AppSettings.data_dir / "api_guard.lock"``. The path is
  derived from the already-validated settings; no environment parsing is
  duplicated here.
* **Open mode:** ``a+b`` — creates the file if missing, never truncates.
* **Byte zero:** exists before locking. The initial byte is written only
  when the file is empty, so repeated startups (and a guard file left
  behind by an abnormal termination) do not grow the file.
* **Acquisition:** nonblocking exclusive lock (``LK_NBLCK``) on one byte
  at offset 0. Contention raises :class:`StartupGuardError`; Uvicorn is
  never started in that case and no request is ever served.
* **Lifetime:** the file handle is kept open (locked) for as long as the
  caller holds the returned :class:`StartupGuard` — normally the whole
  server lifetime inside :func:`src.api.run_server`.
* **Release:** :meth:`StartupGuard.release` performs ``LK_UNLCK`` and
  closes the handle; use the context-manager form so orderly shutdown
  always releases. If the process terminates abnormally, the Windows OS
  releases the lock with the process handle (verified by tests); the
  stale file itself is harmless at the next startup.
* **Sanitization:** raised exception messages are fixed constants —
  never absolute paths, environment values, or raw OS error text. The
  full path exists only on the object (``lock_path``) for trusted,
  server-side diagnostics.

This is a **local single-process development safeguard**, not
authentication and not a production security boundary. It prevents one
class of misconfiguration (two API processes serving concurrently on
this machine), nothing more.
"""

from __future__ import annotations

import os
import sys

from src.config import AppSettings

__all__ = [
    "GUARD_FILE_NAME",
    "StartupGuard",
    "StartupGuardError",
    "StartupGuardUnavailableError",
    "acquire_startup_guard",
]

#: File name of the guard lock, resolved under ``AppSettings.data_dir``.
GUARD_FILE_NAME: str = "api_guard.lock"

#: Fixed, sanitized messages (no paths, no environment values, no raw
#: OS error text may enter client-facing console output through these).
_MSG_CONTENTION = (
    "the single-instance startup guard is already held by another "
    "process; refusing to start the API"
)
_MSG_CREATE_FAILED = "the single-instance startup guard could not be created"
_MSG_ACQUIRE_FAILED = "the single-instance startup guard could not be acquired"
_MSG_PLATFORM = (
    "the single-instance startup guard is Windows-only (msvcrt); "
    "POSIX platforms are out of scope for Phase 3"
)

#: Windows error numbers that mean "byte range already locked".
_EACCES = 13  # access denied (lock taken by another process/handle)
_ERRDEADLK = 36  # "Resource deadlock avoided" — also a contention result


class StartupGuardError(Exception):
    """Startup-guard failure (contention or guard-file creation failure).

    The message is a fixed sanitized constant; trusted detail (path,
    driver-level) stays with the ``__cause__``/logging for server-side
    diagnostics only.
    """


class StartupGuardUnavailableError(StartupGuardError):
    """The ``msvcrt`` guard mechanism is not available on this platform.

    Deterministic explicit failure — never a silent advisory fallback:
    exclusivity cannot be guaranteed by a Python-level flag, so
    non-Windows platforms must not *appear* to be guarded.
    """


class StartupGuard:
    """A held Windows byte-range lock; context manager for the server run.

    Instantiate via :func:`acquire_startup_guard`. The context-manager
    form is the intended usage in :func:`src.api.run_server`::

        with acquire_startup_guard(settings) as _:
            import uvicorn  # only after successful acquisition
            uvicorn.Server(config).run()
        # guard released here (orderly shutdown)

    Lock rules (see the module docstring for the full contract): the
    guard file is created in ``a+b`` mode, byte 0 is ensured only when
    the file is empty, the nonblocking exclusive lock is taken on byte 0
    at the file pointer's offset 0 (hence the ``seek(0)`` before
    ``locking``), and the handle stays open until :meth:`release`.
    """

    def __init__(self, settings: AppSettings) -> None:
        import msvcrt  # Windows-only stdlib; POSIX is out of scope

        if sys.platform != "win32" or not hasattr(msvcrt, "locking"):
            raise StartupGuardUnavailableError(_MSG_PLATFORM)

        self._lock_path = settings.data_dir / GUARD_FILE_NAME
        # Only the guard's own parent directory is created (an explicit,
        # narrow side effect of starting the server; ``ensure_directories``
        # belongs to the wider application startup contract, not here).
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StartupGuardError(_MSG_CREATE_FAILED) from exc

        try:
            handle = open(self._lock_path, "a+b")
        except OSError as exc:
            raise StartupGuardError(_MSG_CREATE_FAILED) from exc

        try:
            # Ensure byte zero exists — write only when empty so repeated
            # startups (and a guard file left behind by an abnormal
            # termination) never grow the file.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\x01")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                handle.close()
                if exc.errno in (_EACCES, _ERRDEADLK):
                    raise StartupGuardError(_MSG_CONTENTION) from None
                raise StartupGuardError(_MSG_ACQUIRE_FAILED) from exc
        except StartupGuardError:
            handle.close()
            raise
        except OSError as exc:
            handle.close()
            raise StartupGuardError(_MSG_CREATE_FAILED) from exc

        self._handle = handle
        self._msvcrt = msvcrt
        self._released = False

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def lock_path(self):
        """The guard file path (trusted diagnostics; not for client output)."""
        return self._lock_path

    def release(self) -> None:
        """Unlock byte 0 and close the handle (idempotent).

        Idempotency matters for reliable ``finally``-style shutdown:
        releasing twice is a no-op rather than an error.
        """
        if self._released or self._handle is None:
            return
        handle, self._handle = self._handle, None
        try:
            handle.seek(0)
            try:
                self._msvcrt.locking(handle.fileno(), self._msvcrt.LK_UNLCK, 1)
            finally:
                # Close in every path: the OS keeps the byte-range lock
                # only while the handle is open, so an unlock failure
                # must not leave a closed-but-still-locked state unclear;
                # an abnormal process exit releases it regardless.
                handle.close()
        finally:
            self._released = True

    def __enter__(self) -> "StartupGuard":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False  # never suppress caller exceptions


def acquire_startup_guard(settings: AppSettings) -> StartupGuard:
    """Acquire the Windows single-process startup guard for ``settings``.

    Raises :class:`StartupGuardError` (contention or guard-file failure)
    or :class:`StartupGuardUnavailableError` (non-Windows platform) —
    callers must abort serving on either; Uvicorn must never start.
    """
    return StartupGuard(settings)
