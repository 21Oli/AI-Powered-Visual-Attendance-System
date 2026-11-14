"""Login API: password verification, session issuance and Origin enforcement (P5.4.5).

``POST /api/v1/auth/login`` is the first authenticated surface of the system.
It composes the existing primitives rather than restating them:

* :class:`src.password.PasswordService` — Argon2id verification, rehash
  detection and rehashing;
* :class:`src.user_repository.UserRepository` — ``find_by_username`` (lookup
  key derived by ``username_key_for``) and ``update_password_hash``;
* :class:`src.session_service.SessionService` — session issuance (only the
  token digest is persisted);
* :func:`src.api_cookies.set_session_cookie` — the approved cookie;
* :class:`src.api_auth.AuthenticatedPrincipal` — the credential-free identity.

Credential handling
-------------------

* Passwords are never trimmed, normalised, logged or echoed. They exist as the
  ``password`` argument and as input to Argon2id.
* The success response carries identity only. The raw session token leaves the
  process solely as the ``Set-Cookie`` value.
* Unknown usernames are verified against :data:`DUMMY_PASSWORD_HASH`, a
  precomputed Argon2id hash of a random value that was discarded at generation
  time. The dummy plaintext is not stored, logged or derivable, and the hash
  is a module constant — one verification per attempt, never regenerated.
* Unknown username, wrong password and an inactive account all raise the same
  :class:`src.api_auth.AuthenticationRequired`, so the public 401 is identical
  and says nothing about account existence or status.

Origin enforcement
------------------

Login is cookie-authenticated, so it must not be reachable cross-site. The
route requires exactly one ``Origin`` header that is an exact member of the
owner-approved allowlist (:data:`src.config.TRUSTED_ORIGINS`, injected through
``AppSettings.login_trusted_origins``, supplied in deployments through
``ATTENDANCE_TRUSTED_ORIGINS`` and injected in tests). Missing, empty, ``null``,
malformed, duplicated or untrusted origins raise :class:`OriginNotAllowed`,
rendered as a sanitized ``403 ORIGIN_NOT_ALLOWED``. An empty allowlist (the
staging/production default until exact URLs are supplied) rejects everything.

The back-end does **not** compare ``Origin`` to ``Host`` and never consults
``X-Forwarded-Host``/``X-Forwarded-Proto``: the browser's origin is the trusted
value, and the same-origin Next.js proxy forwards it unchanged for this route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.responses import Response

from src.api_auth import AuthenticatedPrincipal, AuthenticationRequired
from src.api_cookies import set_session_cookie
from src.config import PASSWORD_HASHING, AppSettings
from src.origin import OriginSyntaxError, normalize_origin, origin_is_allowed
from src.password import PasswordService
from src.session_service import AuthenticatedSession, SessionService
from src.user import MAX_USERNAME_LENGTH, Role, UserValidationError
from src.user_repository import UserRepository

__all__ = [
    "DUMMY_PASSWORD_HASH",
    "LOGIN_PATH",
    "LoginRequest",
    "LoginResponse",
    "LoginResult",
    "LoginService",
    "LoginUser",
    "ORIGIN_NOT_ALLOWED_CODE",
    "ORIGIN_NOT_ALLOWED_MESSAGE",
    "OriginNotAllowed",
    "build_login_service",
    "register_login_route",
    "require_trusted_origin_dependency",
]

#: Exact route this milestone adds (the only login surface).
LOGIN_PATH: str = "/api/v1/auth/login"

#: Stable machine code for the sanitized Origin refusal.
ORIGIN_NOT_ALLOWED_CODE: str = "ORIGIN_NOT_ALLOWED"
#: Fixed human message; never names the offending value.
ORIGIN_NOT_ALLOWED_MESSAGE: str = "The request origin is not allowed."

#: Non-secret Argon2id hash used to equalise the unknown-account path.
#:
#: Generated **once** with the approved parameters (m=19456, t=2, p=1, 16-byte
#: salt, 32-byte hash) from a random value that was discarded immediately. No
#: password matches it, the plaintext is not recorded anywhere, and it is
#: never returned by an endpoint or written to a log. It is a constant so the
#: unknown-user path performs one real verification without hashing per
#: request, and so its parameters can be pinned by a test.
DUMMY_PASSWORD_HASH: str = (
    "$argon2id$v=19$m=19456,t=2,p=1$zvGe7uE2bqnKNGhNakd7bw"
    "$3Xhkhp9j3YLf58DZBtZcbb7BjTLuEm9AJrgGWnFySJY"
)


class OriginNotAllowed(Exception):
    """The request did not carry exactly one trusted ``Origin``.

    Carries no detail: the client learns that the origin is unacceptable, not
    which header was sent, whether the allowlist is empty, or which entry
    would have matched.
    """


class LoginRequest(BaseModel):
    """The only accepted login input: a username and a password.

    ``extra="forbid"`` rejects client-supplied ``user_id``/``role``/
    ``is_active`` or any other field, so identity can only come from the
    database row. Length bounds mirror the existing user/password
    configuration limits; the password is passed through untouched.
    """

    username: str = Field(min_length=1, max_length=MAX_USERNAME_LENGTH)
    password: str = Field(min_length=1, max_length=PASSWORD_HASHING.max_password_length)

    model_config = {"extra": "forbid"}


class LoginUser(BaseModel):
    """The public identity returned by a successful login."""

    user_id: str
    username: str
    role: Role
    is_active: bool


class LoginResponse(BaseModel):
    """Envelope for the success body: identity only, never a credential."""

    user: LoginUser


@dataclass(frozen=True, slots=True)
class LoginResult:
    """A successful authentication: public identity plus the one-time token.

    ``token`` is excluded from ``repr`` so an accidental log of this object
    cannot leak the credential; only ``set_session_cookie`` consumes it.
    """

    principal: AuthenticatedPrincipal
    token: str = field(repr=False)


class LoginService:
    """Authenticate a username/password pair and issue a server-side session.

    Parameters
    ----------
    users, passwords, sessions:
        Existing P5.2/P5.3/P5.3 implementations. Nothing here re-implements
        verification, hashing or session storage.
    """

    def __init__(
        self,
        *,
        users: UserRepository,
        passwords: PasswordService,
        sessions: SessionService,
    ) -> None:
        self._users = users
        self._passwords = passwords
        self._sessions = sessions

    def authenticate(self, username: str, password: str) -> LoginResult:
        """Return the identity and token, or raise the generic refusal.

        Order guarantees the public response is identical for every failure:

        1. ``find_by_username`` (normalization is the repository's contract),
           falling back to :data:`DUMMY_PASSWORD_HASH` for an unknown account;
        2. one real Argon2id verification in every case — including inactive
           accounts, whose stored hash is verified before the refusal;
        3. ``needs_rehash`` and persistence *before* issuance, so a failed
           rehash cannot produce a session;
        4. ``issue_session`` — the only place a token exists;
        5. the credential-free principal projection.
        """
        try:
            user = self._users.find_by_username(username)
        except UserValidationError:
            # A username that cannot even form a lookup key (too short, control
            # characters) is just another failed credential. The dummy
            # verification still runs so the response is indistinguishable from
            # an unknown account.
            self._passwords.verify_password(password, DUMMY_PASSWORD_HASH)
            raise AuthenticationRequired() from None
        stored_hash = DUMMY_PASSWORD_HASH if user is None else user.password_hash
        verified = self._passwords.verify_password(password, stored_hash)

        if user is None or not verified:
            raise AuthenticationRequired()
        if not user.is_active:
            # Verified above (real hash) so timing does not distinguish an
            # inactive account; no rehash and no session for an account that
            # cannot authenticate.
            raise AuthenticationRequired()

        if self._passwords.needs_rehash(user.password_hash):
            replacement = self._passwords.hash_password(password)
            self._users.update_password_hash(user.user_id, replacement)

        issued = self._sessions.issue_session(user.user_id)
        principal = AuthenticatedPrincipal.from_session(
            AuthenticatedSession(user=user, session=issued.session)
        )
        return LoginResult(principal=principal, token=issued.token)


def require_trusted_origin_dependency(
    settings: AppSettings,
) -> Callable[[Request], None]:
    """Build the Origin guard for the login route.

    Exactly one ``Origin`` header is required and must equal (after
    canonicalisation) an allowlist entry. Duplicate headers are rejected
    because Starlette keeps them separately — a proxy that appends rather than
    replaces must not turn into "first one wins".
    """
    # Settings validate every entry at construction, so an invalid value here
    # means the settings object never came from validated configuration: raise
    # rather than silently dropping the entry (fail closed, loudly).
    trusted = tuple(
        normalize_origin(entry) for entry in settings.login_trusted_origins or ()
    )

    def require_trusted_origin(request: Request) -> None:
        values = request.headers.getlist("origin")
        if len(values) != 1:
            raise OriginNotAllowed()
        try:
            origin = normalize_origin(values[0])
        except OriginSyntaxError:
            raise OriginNotAllowed() from None
        if not origin_is_allowed(origin, trusted):
            raise OriginNotAllowed()

    return require_trusted_origin


def register_login_route(
    app: FastAPI,
    *,
    path: str,
    get_login_service: Callable[[], LoginService],
    settings: AppSettings,
) -> None:
    """Register ``POST {path}`` on ``app`` (the only login surface)."""
    origin_guard = Depends(require_trusted_origin_dependency(settings))

    @app.post(path, dependencies=[origin_guard])
    def login(payload: LoginRequest, request: Request) -> Response:
        """Authenticate, issue a session, and set the approved cookie.

        The success body contains identity only. The raw token is written to
        ``Set-Cookie`` and nowhere else; failures propagate as the existing
        sanitized 401/503/500 responses.
        """
        result = get_login_service().authenticate(payload.username, payload.password)
        body = LoginResponse(
            user=LoginUser(
                user_id=result.principal.user_id,
                username=result.principal.username,
                role=result.principal.role,
                is_active=result.principal.is_active,
            )
        ).model_dump(mode="json")
        response = JSONResponse(content=body)
        set_session_cookie(response, token=result.token, settings=settings)
        return response


def build_login_service(settings: AppSettings) -> LoginService:
    """Build the production login service lazily (no connection, no hashing).

    Mirrors ``build_session_service``: the DSN availability check happens
    before the repository modules (and psycopg) are imported, and constructing
    the service opens nothing and hashes nothing.
    """
    from src.db import DatabaseUnavailableError

    # Validate the configured DSN *before* importing the repository layer, so a
    # missing/blank database fails closed (sanitized 503) instead of reaching
    # psycopg's implicit "local default target" behaviour. The message carries
    # no DSN, host, user or password.
    if not isinstance(settings.database_url, str) or not settings.database_url.strip():
        raise DatabaseUnavailableError(
            "No application database is configured for the API."
        )

    from src.db_security_repository import (
        open_session_repository,
        open_user_repository,
    )

    # One user repository serves both the credential lookup and the session
    # service's revocation lookups; nothing connects or hashes here.
    users = open_user_repository()
    return LoginService(
        users=users,
        passwords=PasswordService(),
        sessions=SessionService(users=users, sessions=open_session_repository()),
    )
