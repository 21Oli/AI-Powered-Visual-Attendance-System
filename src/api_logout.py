"""Logout API: current-session revocation and cookie clearing (P5.4.6).

``POST /api/v1/auth/logout`` ends **the session the request presents** and
removes the browser cookie. It composes the existing primitives rather than
adding a second mechanism:

* :func:`src.api_login.require_trusted_origin_dependency` — the exact same
  Origin guard login uses (logout is state-changing, and a cross-site page must
  not be able to sign a user out);
* :meth:`src.session_service.SessionService.revoke_session` — one session, by
  token digest; ``revoke_all_for_user`` is deliberately never called;
* :func:`src.api_cookies.clear_session_cookie` — the approved deletion cookie
  (same name/path/HttpOnly/SameSite/Secure policy, no ``Domain``).

Token handling
--------------

The raw token is read **only** from the configured ``va_session`` cookie. No
token is accepted from the query string or the request body, none is returned
or logged, and the response has no body at all (``204 No Content``).

Idempotency
-----------

Logout must succeed for a client that is already signed out, otherwise an
expired or revoked session would leave a stale cookie behind forever:

* absent or empty cookie -> nothing to revoke, cookie cleared;
* malformed token -> :class:`~src.session_service.InvalidSessionToken` is
  caught **before** any lookup, cookie cleared;
* unknown token -> :class:`~src.session_repository.SessionNotFound` caught,
  cookie cleared;
* already-revoked or expired session -> the repository's ``COALESCE`` revoke
  keeps the first revocation instant and returns the row, cookie cleared.

All of those are the same ``204`` with the same headers, so the response never
reveals whether the token corresponded to a real session.

Failure policy
--------------

Only *expected invalid-session* outcomes are treated as already-signed-out.
Infrastructure and programming failures are **not** swallowed: a
``DatabaseUnavailableError`` stays the sanitized ``503 DATABASE_UNAVAILABLE``
and an unexpected exception stays the sanitized ``500 INTERNAL_ERROR``. In both
cases the deletion cookie is **not** sent, because the server cannot confirm
that the session was actually revoked; the caller keeps its cookie and can
retry. Nothing in those responses carries a token, a digest or driver detail.
"""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, FastAPI, Request
from starlette.responses import Response

from src.api_cookies import clear_session_cookie
from src.api_login import require_trusted_origin_dependency
from src.config import SESSION_COOKIE, AppSettings
from src.session_repository import SessionNotFound
from src.session_service import AuthenticationError, SessionService

__all__ = [
    "LOGOUT_PATH",
    "perform_logout",
    "register_logout_route",
]

#: Exact route this milestone adds (the only logout surface).
LOGOUT_PATH: str = "/api/v1/auth/logout"


def perform_logout(
    sessions: SessionService,
    *,
    token: str | None,
    settings: AppSettings,
) -> Response:
    """Revoke ``token``'s session when present, then clear the cookie.

    ``token`` is the raw ``va_session`` cookie value (or ``None`` when the
    cookie is absent). Expected invalid-session outcomes are already-signed-out
    and still return the ``204``; anything else propagates to the existing
    sanitized handlers before a cookie could be cleared.
    """
    if token:
        try:
            sessions.revoke_session(token)
        except (AuthenticationError, SessionNotFound):
            # Malformed, unknown or already-revoked/expired: the client is
            # signing out of a session that is not live. Same answer, no hint.
            pass

    response = Response(status_code=204)
    clear_session_cookie(response, settings=settings)
    return response


def register_logout_route(
    app: FastAPI,
    *,
    path: str,
    get_session_service: Callable[[], SessionService],
    settings: AppSettings,
) -> None:
    """Register ``POST {path}`` on ``app`` (the only logout surface)."""
    origin_guard = Depends(require_trusted_origin_dependency(settings))

    @app.post(path, status_code=204, dependencies=[origin_guard])
    def logout(request: Request) -> Response:
        """Sign out the session carried by the ``va_session`` cookie.

        The body is never read and no query parameter is consulted, so a
        client cannot nominate a different session to revoke.
        """
        return perform_logout(
            get_session_service(),
            token=request.cookies.get(SESSION_COOKIE.name),
            settings=settings,
        )
