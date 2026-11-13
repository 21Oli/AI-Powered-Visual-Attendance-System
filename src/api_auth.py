"""Authentication dependency infrastructure (P5.4.4).

The dependency in this module turns an incoming request into a *server-side*
:class:`AuthenticatedPrincipal`, or refuses it. It is infrastructure only: no
route uses it yet, so the eight business endpoints are exactly as unauthenticated
as they were in P5.4.3. Login, logout, ``/me``, throttling, CSRF, role-based
authorization and audit logging are later milestones.

Resolution flow
---------------

1. Read the cookie named by :data:`src.config.SESSION_COOKIE` (``va_session``).
2. No cookie -> :class:`AuthenticationRequired` (a sanitized HTTP 401).
3. ``SessionService.validate_session(token)`` — the existing P5.3
   implementation, not a second validator. It performs the structural check,
   loads the row by token *digest*, and enforces revocation, the absolute
   lifetime, the idle deadline and account activity.
4. Project the returned :class:`~src.session_service.AuthenticatedSession` onto
   a frozen, credential-free :class:`AuthenticatedPrincipal` for later
   authorization dependencies. The ``User`` and ``UserSession`` rows stay
   inside this call: their ``password_hash`` and ``session_token_hash`` never
   enter the principal.

Source of truth
---------------

Only the cookie is a credential source. A session token is never read from the
query string, the request body, an arbitrary header or a client-supplied JSON
field, and no user ID, role or activity flag is ever taken from the client: the
user object comes from the ``app_user`` row the session points at. A browser
that sends ``?user_id=...``, ``X-User-Id`` or a role header changes nothing.

Failure behaviour
-----------------

* Missing, malformed, unknown, expired, revoked or inactive-account sessions all
  become the *same* :class:`AuthenticationRequired`, which the application maps
  to ``401 AUTHENTICATION_REQUIRED`` with the existing error envelope and the
  P5.4.3 ``X-Request-ID`` header. The reason is never disclosed.
* Database/repository failures are **not** caught here. ``DatabaseUnavailableError``
  propagates to the existing handler and stays ``503 DATABASE_UNAVAILABLE``;
  unexpected repository or clock failures stay 500. A storage outage is never
  reported as "not signed in".
* No token is issued, refreshed or rotated here, and nothing is logged.

Concurrency
-----------

The dependency is a pure function of the request and the injected service; it
holds no per-user state. The returned principal is immutable and request-scoped,
so concurrent requests cannot observe one another's identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import Request

from src.config import SESSION_COOKIE, AppSettings
from src.session_service import (
    AuthenticatedSession,
    AuthenticationError,
    SessionService,
)
from src.user import Role

__all__ = [
    "AUTHENTICATION_REQUIRED_CODE",
    "AUTHENTICATION_REQUIRED_MESSAGE",
    "AuthenticatedPrincipal",
    "AuthenticationRequired",
    "build_session_service",
    "make_session_dependency",
]

#: Stable machine code for the sanitized 401 envelope.
AUTHENTICATION_REQUIRED_CODE: str = "AUTHENTICATION_REQUIRED"
#: Fixed human message; never varies with the reason for refusal.
AUTHENTICATION_REQUIRED_MESSAGE: str = "Authentication is required."


class AuthenticationRequired(Exception):
    """No usable session was presented.

    Deliberately carries no detail: the client learns only that authentication
    is required — not whether the cookie was absent, malformed, unknown,
    expired, revoked or attached to an inactive account.
    """


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Minimal, credential-free server-side identity for one request.

    Only the four fields below are retained, all sourced from the validated
    ``app_user`` row. The ``User`` and ``UserSession`` objects are deliberately
    **not** stored: they carry ``password_hash`` and ``session_token_hash``,
    which FastAPI's response encoder would happily serialize and which
    ``UserSession``'s ``repr`` would print. Dropping the objects — rather than
    relying on ``repr=False`` — means neither ``repr(principal)`` nor
    ``jsonable_encoder(principal)`` can expose credential material.

    The value is immutable (frozen + slots), request-scoped, and safe to return
    from a route or include in a response model.
    """

    user_id: str
    username: str
    role: Role
    is_active: bool

    @classmethod
    def from_session(
        cls, authenticated: AuthenticatedSession
    ) -> "AuthenticatedPrincipal":
        """Project a validated session onto the identity fields only.

        This is the single boundary where server-side rows are reduced to
        public identity; the credential-bearing objects go out of scope here.
        """
        user = authenticated.user
        return cls(
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            is_active=user.is_active,
        )


def make_session_dependency(
    sessions: SessionService,
) -> Callable[[Request], AuthenticatedPrincipal]:
    """Build the FastAPI dependency that resolves the authenticated principal.

    ``sessions`` is injected (production composition or a test double), so the
    dependency stays a plain callable that ``Depends`` can use and a test can
    call directly. Nothing is cached between requests.
    """
    if not callable(getattr(sessions, "validate_session", None)):
        raise TypeError(
            "make_session_dependency expects a SessionService-like object "
            "with validate_session()"
        )

    def require_authenticated_principal(request: Request) -> AuthenticatedPrincipal:
        token = request.cookies.get(SESSION_COOKIE.name)
        if not token:
            raise AuthenticationRequired()
        try:
            authenticated = sessions.validate_session(token)
        except AuthenticationError:
            # One refusal for every authentication outcome; the underlying
            # exception message stays server-side (and is not even logged here).
            raise AuthenticationRequired() from None
        return AuthenticatedPrincipal.from_session(authenticated)

    return require_authenticated_principal


def build_session_service(settings: AppSettings) -> SessionService:
    """Build the production :class:`SessionService` lazily (no connection).

    Mirrors ``build_default_services``: availability is checked *before* the
    repository modules (and therefore psycopg) are imported, so
    ``import src.api_auth`` and app construction stay driver-free. The
    repositories open a connection per operation on first use, never here.
    """
    from src.db import DatabaseUnavailableError

    if not isinstance(settings.database_url, str) or not settings.database_url.strip():
        raise DatabaseUnavailableError(
            "No application database is configured for the API."
        )

    from src.db_security_repository import (
        open_session_repository,
        open_user_repository,
    )

    return SessionService(
        users=open_user_repository(),
        sessions=open_session_repository(),
    )
