"""Session issuance, validation, expiry and revocation (P5.3).

This is the authentication **primitive** layer: it turns an approved account
into a persisted session, and a presented token into an authenticated
account — or a typed refusal. It does not read cookies, parse headers,
authenticate a request, or expose HTTP (P5.4/P5.5), and it makes no role
decision (P5.6).

Lifecycle
---------

* **Issue** — an active user gets a 256-bit token (returned once), a SHA-256
  digest (the only persisted form), ``expires_at = now + 8h`` and
  ``absolute_expires_at = now + 12h``.
* **Validate** — the token is structurally checked before any lookup, then
  the row must be live (not revoked, not idle-expired, not past the absolute
  cap) and its owner must still exist and be active. On success the idle
  deadline slides to ``min(now + 8h, absolute_expires_at)`` and
  ``last_seen_at`` is recorded. **Activity can never extend the absolute
  cap.**
* **Revoke** — one session (digest preserved as the key) or every live
  session for a user.

Fail-closed rules
-----------------

* Every rejection is a distinct :class:`AuthenticationError` subtype so a
  later milestone can map it without inspecting strings. Messages are
  generic and never contain a token, digest, username or hash.
* A clock that returns a naive datetime, a non-datetime, or an instant
  earlier than a session's issuance is refused (:class:`ClockError` /
  :class:`InvalidSessionToken`); an ambiguous local time can never extend a
  session.
* Unknown tokens and malformed tokens fail through the same public path and
  cost no more than one indexed lookup — no user enumeration.
* Repository/driver failures propagate untouched after the guards. Nothing
  is swallowed and no partially-authenticated result is returned.
* Concurrent revocation between the read and the touch fails closed
  (``SessionNotFound`` is translated to :class:`InvalidSessionToken`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.clock import Clock, utc_now
from src.config import SESSION_POLICY, SessionPolicyConfig
from src.session_repository import (
    SessionAlreadyExists,
    SessionNotFound,
    SessionRepository,
    SessionUserNotFound,
)
from src.session_token import (
    SessionTokenError,
    generate_session_token,
    is_valid_session_token,
    session_token_digest,
)
from src.user import User
from src.user_repository import UserRepository
from src.user_session import SessionValidationError, UserSession

__all__ = [
    "AuthenticationError",
    "AuthenticatedSession",
    "ClockError",
    "InvalidSessionToken",
    "IssuedSession",
    "SessionExpired",
    "SessionIssueError",
    "SessionRevoked",
    "SessionService",
    "UserInactive",
    "UserUnknown",
]


class SessionServiceError(RuntimeError):
    """Base class for session-service failures."""


class AuthenticationError(SessionServiceError):
    """A presented credential must not be accepted (fail closed)."""


class InvalidSessionToken(AuthenticationError):
    """The token is malformed or matches no stored session."""


class SessionExpired(AuthenticationError):
    """The session is past its idle deadline or its absolute cap."""


class SessionRevoked(AuthenticationError):
    """The session was explicitly revoked."""


class UserInactive(AuthenticationError):
    """The account exists but is deactivated."""


class UserUnknown(AuthenticationError):
    """The account does not exist (or vanished after issuance)."""


class SessionIssueError(SessionServiceError):
    """A new session could not be created safely."""


class ClockError(SessionServiceError):
    """The injected clock did not return a timezone-aware datetime."""


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A freshly created session and its one-time raw token.

    ``token`` is excluded from ``repr`` so an accidental log of this object
    cannot leak the credential. The caller must hand it to the client once and
    then forget it; only ``session.session_token_hash`` is persisted.
    """

    token: str = field(repr=False)
    session: UserSession


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """A validated session and the user it belongs to."""

    user: User
    session: UserSession


class SessionService:
    """Issue, validate and revoke sessions for the approved accounts.

    Parameters
    ----------
    users, sessions:
        The P5.2 repositories (one connection per operation, parameterized
        SQL). No second persistence path exists.
    clock:
        Injectable zero-argument callable returning an aware ``datetime``;
        defaults to :func:`src.clock.utc_now`.
    policy:
        Approved lifetimes/token entropy; defaults to
        :data:`src.config.SESSION_POLICY`.
    """

    def __init__(
        self,
        *,
        users: UserRepository,
        sessions: SessionRepository,
        clock: Clock = utc_now,
        policy: SessionPolicyConfig = SESSION_POLICY,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._clock = clock
        self._policy = policy

    # -- internals ----------------------------------------------------------

    def _now(self) -> datetime:
        """Read the injected clock, refusing an unusable instant."""
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ClockError(
                "the session clock must return a timezone-aware datetime"
            )
        return now

    def _require_user_id(self, user_id: object) -> str:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id must be a non-empty string")
        return user_id.strip()

    def _require_token(self, token: object) -> str:
        """Structural token check before any repository call."""
        if not is_valid_session_token(token):
            raise InvalidSessionToken(
                "the presented session token is not valid"
            )
        return token  # type: ignore[return-value]

    def _active_user(self, user_id: str) -> User:
        """Load an account and require that it exists and is active."""
        user = self._users.get(user_id)
        if user is None:
            raise UserUnknown("no account matches the session")
        if not user.is_active:
            raise UserInactive("the account is not active")
        return user

    # -- approved operations ------------------------------------------------

    def issue_session(self, user_id: str) -> IssuedSession:
        """Create a session for an active user and return its raw token once.

        ``UserUnknown`` / ``UserInactive`` refuse before any row is written.
        A collision on the digest (cryptographically improbable) or any other
        storage failure fails closed as :class:`SessionIssueError` — no retry,
        no second token, no partially created session.
        """
        identifier = self._require_user_id(user_id)
        self._active_user(identifier)
        now = self._now()

        token = generate_session_token()
        digest = session_token_digest(token)
        session = UserSession(
            session_token_hash=digest,
            user_id=identifier,
            issued_at=now,
            expires_at=now + self._policy.idle_timeout,
            absolute_expires_at=now + self._policy.absolute_lifetime,
            revoked_at=None,
            last_seen_at=now,
        )
        try:
            stored = self._sessions.insert(session)
        except SessionAlreadyExists:
            raise SessionIssueError(
                "a session token collision prevented issuance"
            ) from None
        except SessionUserNotFound:
            raise UserUnknown("no account matches the session") from None
        except (SessionValidationError, SessionTokenError):  # pragma: no cover
            raise SessionIssueError("the session could not be issued") from None
        return IssuedSession(token=token, session=stored)

    def validate_session(self, token: str) -> AuthenticatedSession:
        """Return the authenticated user for a live session token.

        Order of checks: structural validity (no I/O), existence, clock
        sanity, revocation, absolute cap, idle deadline, account existence and
        activity. On success the idle deadline slides but is clamped to
        ``absolute_expires_at``.
        """
        presented = self._require_token(token)
        digest = session_token_digest(presented)
        stored = self._sessions.find_by_token_hash(digest)
        if stored is None or stored.session_token_hash != digest:
            raise InvalidSessionToken("the presented session token is not valid")

        now = self._now()
        if now < stored.issued_at:
            raise InvalidSessionToken("the presented session token is not valid")
        if stored.revoked_at is not None:
            raise SessionRevoked("the session was revoked")
        if now >= stored.absolute_expires_at:
            raise SessionExpired("the session has reached its absolute lifetime")
        if now >= stored.expires_at:
            raise SessionExpired("the session has expired from inactivity")

        user = self._active_user(stored.user_id)

        # Activity slides the idle deadline, never past the hard cap.
        deadline = min(
            now + self._policy.idle_timeout, stored.absolute_expires_at
        )
        try:
            updated = self._sessions.touch(
                digest, last_seen_at=now, expires_at=deadline
            )
        except SessionNotFound:
            raise InvalidSessionToken(
                "the presented session token is not valid"
            ) from None
        return AuthenticatedSession(user=user, session=updated)

    def revoke_session(self, token: str) -> UserSession:
        """Revoke one session by token; returns the revoked row.

        The token is validated before any lookup, and only its digest is sent
        to the repository. Repository errors (including ``SessionNotFound``)
        propagate unchanged; whether a repeated revoke is an error or a no-op
        is a P5.4 decision.
        """
        presented = self._require_token(token)
        digest = session_token_digest(presented)
        return self._sessions.revoke(digest, revoked_at=self._now())

    def revoke_all_for_user(self, user_id: str) -> int:
        """Revoke every live session for ``user_id``; returns the count."""
        identifier = self._require_user_id(user_id)
        return self._sessions.revoke_all_for_user(
            identifier, revoked_at=self._now()
        )
