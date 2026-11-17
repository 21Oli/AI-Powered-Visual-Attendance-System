"""Application-scoped audit subsystem for the FastAPI app (P5.5.5).

This module owns the process's single :class:`~src.audit_writer.AuditJournalWriter`
and the single :class:`~src.audit_service.AuditService` built over it, and it
turns initialization failures into an explicit, sanitized **restricted** state
instead of raising into a request.

Design rules
------------

* **One writer, one service, one journal.** The writer and service are built
  once per application instance (lazily, on the first audited operation) and
  reused for every request. Routes never see the writer or the JSONL path; the
  integration helpers own the service access.
* **Initialization is fail-closed.** The writer performs its own full
  verification (P5.5.3) before the service exists. Any
  :class:`~src.audit_writer.AuditJournalError` — unavailable, corrupt,
  integrity, contended — puts the subsystem in restricted mode permanently for
  this process: no append is ever attempted, nothing is created, repaired,
  truncated or replaced, and no second journal is opened.
* **No false readiness.** :attr:`AuditSubsystem.state` is ``uninitialized``
  until an audited operation forces initialization, then ``ready`` or
  ``restricted``. The state is server-internal; ``/health`` never reports it.
* **Sanitized diagnostics.** The only failure fact retained is the writer error
  class name. One ``CRITICAL`` log line records that class; no path, OS text,
  record contents, identifier or digest is logged or returned.
* **Offline compositions.** ``create_app`` may be built without an audit
  subsystem (existing offline endpoint tests). That state is explicit
  (:attr:`AuditSubsystem` absent) rather than a hidden "off" switch in the
  production path: :func:`src.api.build_server_config` and the default
  ``create_app(settings)`` composition always install one.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Final

from src.audit_schema import AuditAction, AuditEvent
from src.audit_service import AuditService
from src.audit_writer import (
    AUDIT_DIRECTORY_NAME,
    JOURNAL_FILENAME,
    AuditJournalError,
    AuditJournalWriter,
)
from src.config import AppSettings

__all__ = [
    "AUDIT_READY",
    "AUDIT_RESTRICTED",
    "AUDIT_UNINITIALIZED",
    "AuditSubsystem",
    "default_audit_path",
    "production_audit_builder",
]

#: Internal sanitized subsystem states (never exposed over HTTP).
AUDIT_UNINITIALIZED: Final[str] = "uninitialized"
AUDIT_READY: Final[str] = "ready"
AUDIT_RESTRICTED: Final[str] = "restricted"

_logger = logging.getLogger(__name__)


def default_audit_path(settings: AppSettings) -> Path:
    """The active journal for this settings object: ``<data_dir>/audit``."""
    return Path(settings.data_dir) / AUDIT_DIRECTORY_NAME / JOURNAL_FILENAME


def production_audit_builder(settings: AppSettings) -> Callable[[], AuditService]:
    """Return the production builder: one verified writer, one service.

    The writer is constructed on first use (never at app construction), so
    importing/building the application still performs no filesystem work. It
    uses this settings object's ``data_dir`` rather than the writer's project
    default, so tests and alternative deployments stay isolated.
    """

    def build() -> AuditService:
        writer = AuditJournalWriter(default_audit_path(settings))
        return AuditService(writer)

    return build


class AuditSubsystem:
    """Lazy, thread-safe holder for the application's audit service.

    ``builder`` constructs the service (and therefore initializes/verifies the
    journal); ``service`` injects an already-built one for tests. Exactly one of
    them is used. After a failure the subsystem is permanently restricted for
    this instance.
    """

    def __init__(
        self,
        *,
        builder: Callable[[], AuditService] | None = None,
        service: AuditService | None = None,
    ) -> None:
        if (builder is None) == (service is None):
            raise TypeError(
                "AuditSubsystem requires exactly one of builder or service"
            )
        self._builder = builder
        self._service = service
        self._state = AUDIT_READY if service is not None else AUDIT_UNINITIALIZED
        self._failure: str | None = None
        self._lock = threading.Lock()

    # -- state --------------------------------------------------------------

    @property
    def state(self) -> str:
        """``uninitialized``, ``ready`` or ``restricted`` (internal only)."""
        return self._state

    @property
    def failure(self) -> str | None:
        """Sanitized writer error class name when restricted, else ``None``."""
        return self._failure

    def service(self) -> AuditService | None:
        """The audit service, or ``None`` when the subsystem is restricted.

        The first call performs initialization/verification; failure is
        converted into restricted mode and logged once. Never raises.
        """
        if self._service is not None:
            return self._service
        with self._lock:
            if self._state == AUDIT_RESTRICTED:
                return None
            if self._service is None:
                try:
                    self._service = self._builder()  # type: ignore[misc]
                except AuditJournalError as exc:
                    self._state = AUDIT_RESTRICTED
                    self._failure = type(exc).__name__
                    _logger.critical(
                        "audit subsystem restricted: %s; audited mutations "
                        "will fail closed (no journal was created, repaired "
                        "or replaced)",
                        self._failure,
                    )
                    return None
                self._state = AUDIT_READY
            return self._service

    # -- best-effort helper -------------------------------------------------

    def best_effort(
        self,
        emit: Callable[[AuditService], AuditEvent],
        *,
        action: AuditAction,
        request_id: str | None,
    ) -> AuditEvent | None:
        """Run a standalone emission without ever raising or changing a response.

        ``emit`` receives the ready service, so callers never re-fetch it or
        handle the restricted state themselves. When the subsystem is restricted
        there is no service to call: the event is skipped and one sanitized
        ``WARNING`` records the action, request ID and failure class. No event
        contents are logged.
        """
        service = self.service()
        if service is None:
            _logger.warning(
                "audit best-effort event skipped (action=%s, request_id=%s): %s",
                action.value,
                request_id if request_id is not None else "-",
                self._failure or "unavailable",
            )
            return None
        return service.record_best_effort(
            lambda: emit(service), action=action, request_id=request_id
        )
