"""Session value model for the Phase 5 security layer (P5.2).

Data and validation only: no token generation, no hashing, no issuance or
validation policy, no SQL and no HTTP. P5.3/P5.4 own those behaviours.

Contract highlights
-------------------

* **The token itself never enters this model.** Only the SHA-256 hex digest
  of the opaque session token is stored (``session_token_hash``), and only
  that digest is persisted. Nothing here can reconstruct a cookie value, so
  a leaked database row does not become a usable session.
* **Every timestamp is application-authored and timezone-aware.** There are
  no database defaults; the model refuses naive datetimes, so an ambiguous
  local-time deadline cannot exist.
* ``expires_at`` is the sliding idle deadline; ``absolute_expires_at`` is
  the hard cap. A session may not idle-expire after its absolute deadline.
* ``revoked_at`` is optional and idempotent by contract (the repository
  keeps the first revocation instant).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = [
    "SESSION_TOKEN_HASH_LENGTH",
    "UserSession",
    "SessionValidationError",
    "is_valid_session_token_hash",
]

#: SHA-256 hex digest length, matching ``user_session.session_token_hash
#: CHAR(64)`` in ``migrations/V002__security.sql``.
SESSION_TOKEN_HASH_LENGTH: int = 64

_TOKEN_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SessionValidationError(ValueError):
    """Raised when a session field violates its validation rule."""


def is_valid_session_token_hash(value: object) -> bool:
    """Whether ``value`` is a lowercase 64-character SHA-256 hex digest."""
    return isinstance(value, str) and _TOKEN_HASH_PATTERN.fullmatch(value) is not None


def _require_utc_aware(value: datetime, field_name: str) -> datetime:
    """Reject naive datetimes; timezone-aware values pass through as-is."""
    if not isinstance(value, datetime):
        raise SessionValidationError(
            f"{field_name} must be a datetime, got {type(value).__name__}"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise SessionValidationError(
            f"{field_name} must be timezone-aware (e.g. datetime.now(UTC)), "
            f"got naive datetime {value!r}"
        )
    return value


@dataclass(frozen=True, slots=True)
class UserSession:
    """One persisted session record (digest plus deadlines; never a token).

    Attributes
    ----------
    session_token_hash:
        Lowercase SHA-256 hex digest of the opaque token, exactly 64
        characters (the database column is ``CHAR(64)``). Uppercase or
        shorter/longer values are refused rather than normalized, so the
        stored key and the looked-up key cannot diverge.
    user_id:
        Owning ``app_user.user_id`` (the database enforces the foreign key).
    issued_at / last_seen_at:
        Creation and most recent activity instants (application-authored).
    expires_at / absolute_expires_at:
        Sliding idle deadline and hard cap; ``expires_at`` may not exceed
        ``absolute_expires_at``.
    revoked_at:
        When the session was revoked, or ``None`` while it is live. The
        first revocation instant is preserved by the repository.
    """

    session_token_hash: str
    user_id: str
    issued_at: datetime
    expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None = None
    last_seen_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session_token_hash, str):
            raise SessionValidationError(
                "session_token_hash must be a str, got "
                f"{type(self.session_token_hash).__name__}"
            )
        if not is_valid_session_token_hash(self.session_token_hash):
            raise SessionValidationError(
                "session_token_hash must be a lowercase 64-character "
                "SHA-256 hex digest; the raw session token is never stored"
            )

        if not isinstance(self.user_id, str):
            raise SessionValidationError(
                f"user_id must be a str, got {type(self.user_id).__name__}"
            )
        normalized_id = self.user_id.strip()
        if not normalized_id:
            raise SessionValidationError(
                "user_id must be non-empty after trimming whitespace"
            )
        object.__setattr__(self, "user_id", normalized_id)

        object.__setattr__(
            self, "issued_at", _require_utc_aware(self.issued_at, "issued_at")
        )
        object.__setattr__(
            self, "expires_at", _require_utc_aware(self.expires_at, "expires_at")
        )
        object.__setattr__(
            self,
            "absolute_expires_at",
            _require_utc_aware(self.absolute_expires_at, "absolute_expires_at"),
        )

        if self.expires_at < self.issued_at:  # type: ignore[operator]
            raise SessionValidationError(
                "expires_at must not precede issued_at"
            )
        if self.expires_at > self.absolute_expires_at:  # type: ignore[operator]
            raise SessionValidationError(
                "expires_at must not exceed absolute_expires_at (the idle "
                "deadline cannot outlive the hard cap)"
            )

        if self.last_seen_at is None:
            object.__setattr__(self, "last_seen_at", self.issued_at)
        else:
            object.__setattr__(
                self,
                "last_seen_at",
                _require_utc_aware(self.last_seen_at, "last_seen_at"),
            )
            if self.last_seen_at < self.issued_at:  # type: ignore[operator]
                raise SessionValidationError(
                    "last_seen_at must not precede issued_at"
                )

        if self.revoked_at is not None:
            object.__setattr__(
                self,
                "revoked_at",
                _require_utc_aware(self.revoked_at, "revoked_at"),
            )
            if self.revoked_at < self.issued_at:  # type: ignore[operator]
                raise SessionValidationError(
                    "revoked_at must not precede issued_at"
                )
