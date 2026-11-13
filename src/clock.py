"""The application clock seam for the Phase 5 authentication layer (P5.3).

A single definition of "now" so that session deadlines and password policy
are testable deterministically without freezing the process clock.

Contract
--------

:func:`utc_now` returns a **timezone-aware UTC** ``datetime``. Every service
that needs the current instant takes a zero-argument callable instead of
calling :func:`datetime.now` itself, so a test can inject a fixed clock and
advance it explicitly.

A clock that returns a naive datetime, a non-datetime, or (for the session
service) an instant earlier than a session's issuance is treated as a
failure and fails closed — an ambiguous local time must never be able to
extend a session.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable

__all__ = ["Clock", "utc_now"]

#: Signature of an injectable clock.
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Return the current timezone-aware UTC instant."""
    return datetime.now(UTC)
