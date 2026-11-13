"""Session persistence for the Phase 5 security layer (P5.2).

Same conventions as :mod:`src.user_repository`: static parameterized SQL, one
connection per operation from an injected factory, typed persistence-state
errors, no catch-all, no driver import at module level.

Scope discipline
----------------

This module **only stores, finds, touches and revokes** session rows. It does
not generate tokens, hash tokens, decide lifetimes, authenticate requests, or
expose HTTP (P5.3/P5.4). It never accepts or returns a raw session token: the
caller supplies a SHA-256 hex digest, and the model keeps it that way.

Revocation semantics
--------------------

* :meth:`SessionRepository.revoke` is **idempotent**: a second call keeps the
  first ``revoked_at`` (``COALESCE``), so an authentic logout and a repeated
  logout are indistinguishable in the record.
* :meth:`SessionRepository.revoke_all_for_user` revokes every currently live
  session for a user and reports how many it changed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Final

from src.user_session import (
    SessionValidationError,
    UserSession,
    is_valid_session_token_hash,
)

__all__ = [
    "SessionAlreadyExists",
    "SessionNotFound",
    "SessionRepository",
    "SessionUserNotFound",
]


class SessionAlreadyExists(RuntimeError):
    """A session insert collided on ``session_token_hash``."""


class SessionNotFound(RuntimeError):
    """A touch/revoke found no session for the given token hash."""


class SessionUserNotFound(RuntimeError):
    """A session insert referenced a ``user_id`` with no ``app_user`` row."""


# ---------------------------------------------------------------------------
# Static SQL (bound parameters, named columns, no interpolation)
# ---------------------------------------------------------------------------

_SELECT_COLUMNS: Final[str] = (
    "session_token_hash, user_id, issued_at, expires_at, "
    "absolute_expires_at, revoked_at, last_seen_at"
)

_INSERT_SQL: Final[str] = (
    "INSERT INTO user_session "
    "(session_token_hash, user_id, issued_at, expires_at, "
    "absolute_expires_at, revoked_at, last_seen_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)

_GET_SQL: Final[str] = (
    f"SELECT {_SELECT_COLUMNS} FROM user_session "
    "WHERE session_token_hash = %s"
)

_TOUCH_SQL: Final[str] = (
    "UPDATE user_session SET last_seen_at = %s, expires_at = %s "
    "WHERE session_token_hash = %s "
    f"RETURNING {_SELECT_COLUMNS}"
)

_REVOKE_SQL: Final[str] = (
    "UPDATE user_session SET revoked_at = COALESCE(revoked_at, %s) "
    "WHERE session_token_hash = %s "
    f"RETURNING {_SELECT_COLUMNS}"
)

_REVOKE_ALL_SQL: Final[str] = (
    "UPDATE user_session SET revoked_at = %s "
    "WHERE user_id = %s AND revoked_at IS NULL "
    "RETURNING session_token_hash"
)

#: PostgreSQL SQLSTATEs this layer translates (psycopg 3 attribute,
#: duck-typed; no driver import).
_UNIQUE_VIOLATION_SQLSTATE: Final[str] = "23505"
_FOREIGN_KEY_VIOLATION_SQLSTATE: Final[str] = "23503"

_ROW_COLUMNS: Final[tuple[str, ...]] = (
    "session_token_hash",
    "user_id",
    "issued_at",
    "expires_at",
    "absolute_expires_at",
    "revoked_at",
    "last_seen_at",
)


def _row_to_session(row: Iterable[object]) -> UserSession:
    """Rebuild a validated :class:`UserSession` from one result row."""
    values = tuple(row)
    if len(values) != len(_ROW_COLUMNS):
        raise RuntimeError(
            "session row shape mismatch: expected exactly the seven "
            "user_session columns"
        )
    return UserSession(
        session_token_hash=values[0],  # type: ignore[arg-type]
        user_id=values[1],  # type: ignore[arg-type]
        issued_at=values[2],  # type: ignore[arg-type]
        expires_at=values[3],  # type: ignore[arg-type]
        absolute_expires_at=values[4],  # type: ignore[arg-type]
        revoked_at=values[5],  # type: ignore[arg-type]
        last_seen_at=values[6],  # type: ignore[arg-type]
    )


def _require_hash(value: object, field_name: str) -> str:
    """Validate a token hash argument before any DB call."""
    if not is_valid_session_token_hash(value):
        raise SessionValidationError(
            f"{field_name} must be a lowercase 64-character SHA-256 hex "
            "digest; the raw session token is never stored"
        )
    return value  # type: ignore[return-value]


def _require_aware(value: object, field_name: str) -> datetime:
    """Validate an application-authored timestamp before any DB call."""
    if not isinstance(value, datetime):
        raise SessionValidationError(
            f"{field_name} must be a datetime, got {type(value).__name__}"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise SessionValidationError(
            f"{field_name} must be timezone-aware (e.g. datetime.now(UTC))"
        )
    return value


class SessionRepository:
    """Persistence facade for ``user_session`` (digests and deadlines only)."""

    def __init__(
        self,
        connection_factory: Callable[..., AbstractContextManager[object]],
    ) -> None:
        self._connection_factory = connection_factory

    # -- helpers ------------------------------------------------------------

    def _run(self, sql: str, params: tuple[object, ...]) -> object:
        """Run one statement on a fresh connection; return its result."""
        with self._connection_factory() as connection:
            return connection.execute(sql, params)  # type: ignore[attr-defined]

    # -- approved operations ------------------------------------------------

    def insert(self, session: UserSession) -> UserSession:
        """Persist a new session row; returns the same model on success.

        A duplicate token hash raises :class:`SessionAlreadyExists`; a
        missing owning user raises :class:`SessionUserNotFound` (the
        database's foreign key is the authority). Any other failure
        propagates unchanged.
        """
        if not isinstance(session, UserSession):
            raise SessionValidationError(
                "insert expects an already-validated UserSession instance, "
                f"got {type(session).__name__}"
            )
        with self._connection_factory() as connection:
            try:
                connection.execute(  # type: ignore[attr-defined]
                    _INSERT_SQL,
                    (
                        session.session_token_hash,
                        session.user_id,
                        session.issued_at,
                        session.expires_at,
                        session.absolute_expires_at,
                        session.revoked_at,
                        session.last_seen_at,
                    ),
                )
            except (SessionAlreadyExists, SessionUserNotFound):
                raise
            except Exception as exc:
                sqlstate = getattr(exc, "sqlstate", None)
                if sqlstate == _UNIQUE_VIOLATION_SQLSTATE:
                    raise SessionAlreadyExists(
                        "a session already exists for the requested token hash"
                    ) from exc
                if sqlstate == _FOREIGN_KEY_VIOLATION_SQLSTATE:
                    raise SessionUserNotFound(
                        "the session's user does not exist"
                    ) from exc
                raise
        return session

    def find_by_token_hash(self, session_token_hash: str) -> UserSession | None:
        """Exact-match fetch by digest; ``None`` when absent.

        Expiry and revocation are **not** interpreted here: this layer reports
        the stored facts, and P5.4 decides whether a row is still usable.
        """
        digest = _require_hash(session_token_hash, "session_token_hash")
        row = self._run(_GET_SQL, (digest,)).fetchone()  # type: ignore[attr-defined]
        return None if row is None else _row_to_session(row)

    def touch(
        self,
        session_token_hash: str,
        *,
        last_seen_at: datetime,
        expires_at: datetime,
    ) -> UserSession:
        """Slide the idle deadline and record activity.

        Missing session → :class:`SessionNotFound`. Callers must not raise
        ``expires_at`` beyond the row's absolute cap: the value is written
        verbatim, and the caller owns that policy (P5.4).
        """
        digest = _require_hash(session_token_hash, "session_token_hash")
        seen = _require_aware(last_seen_at, "last_seen_at")
        deadline = _require_aware(expires_at, "expires_at")
        row = self._run(
            _TOUCH_SQL, (seen, deadline, digest)
        ).fetchone()  # type: ignore[attr-defined]
        if row is None:
            raise SessionNotFound(
                "no session was updated because the requested session does "
                "not exist"
            )
        return _row_to_session(row)

    def revoke(self, session_token_hash: str, *, revoked_at: datetime) -> UserSession:
        """Revoke one session, preserving an earlier revocation instant.

        Missing session → :class:`SessionNotFound`. Idempotent for a
        already-revoked row (the first timestamp is kept).
        """
        digest = _require_hash(session_token_hash, "session_token_hash")
        revoked = _require_aware(revoked_at, "revoked_at")
        row = self._run(
            _REVOKE_SQL, (revoked, digest)
        ).fetchone()  # type: ignore[attr-defined]
        if row is None:
            raise SessionNotFound(
                "no session was revoked because the requested session does "
                "not exist"
            )
        return _row_to_session(row)

    def revoke_all_for_user(self, user_id: str, *, revoked_at: datetime) -> int:
        """Revoke every live session for ``user_id``; returns the count changed.

        Already-revoked rows are left untouched (their original instant is
        preserved), so the count is the number of sessions this call ended.
        """
        if not isinstance(user_id, str) or not user_id.strip():
            raise SessionValidationError("user_id must be a non-empty string")
        revoked = _require_aware(revoked_at, "revoked_at")
        rows: Iterable[object] = self._run(
            _REVOKE_ALL_SQL, (revoked, user_id.strip())
        ).fetchall()  # type: ignore[attr-defined]
        return sum(1 for _ in rows)
