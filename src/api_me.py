"""Current-user API: the authenticated principal's safe profile (P5.4.7).

``GET /api/v1/auth/me`` answers one question — *who is this session's user?* —
and adds no policy of its own:

* authentication is the existing P5.4.4 dependency
  (:func:`src.api_auth.make_session_dependency`), which reads **only** the
  ``va_session`` cookie and validates it through the existing
  :meth:`src.session_service.SessionService.validate_session`;
* the identity is the existing credential-free
  :class:`src.api_auth.AuthenticatedPrincipal` projection (no ``User``, no
  ``UserSession``), so no password hash, token or digest can reach this module;
* the response is an explicit four-field model serialized with
  ``model_dump(mode="json")``, not an arbitrary object.

Contract
--------

``200`` with exactly ``user_id``, ``username``, ``role`` and ``is_active``
(``role`` uses the project's existing role serialization, e.g. ``"Admin"``),
and ``Cache-Control: no-store`` so a shared or intermediary cache can never
replay one user's profile to another. Any missing, empty, malformed, unknown,
revoked, expired or inactive-account session is the existing sanitized
``401 AUTHENTICATION_REQUIRED``; a storage failure stays ``503
DATABASE_UNAVAILABLE`` and an unexpected fault ``500 INTERNAL_ERROR``. This
route never sets or clears a cookie.

Method and origin
-----------------

``GET`` only — no ``POST``/``PATCH`` variant exists. Reading the current
identity is not state-changing, so it carries **no** Origin/CSRF requirement;
login and logout keep their mandatory trusted-Origin guard untouched, and no
CORS is introduced. The client cannot choose whose profile it receives: no
request body, query parameter or custom header is consulted.
"""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api_auth import AuthenticatedPrincipal, make_session_dependency
from src.session_service import SessionService
from src.user import Role

__all__ = [
    "ME_PATH",
    "MeResponse",
    "current_principal_dependency",
    "register_me_route",
]

#: Exact route this milestone adds (the only current-user surface).
ME_PATH: str = "/api/v1/auth/me"


class MeResponse(BaseModel):
    """The authenticated user's public profile — exactly four fields.

    ``extra="forbid"`` means the model cannot be extended accidentally, and
    the type set is deliberately limited to identity facts the principal
    already holds. Credential material is not a field, so it cannot be added
    by a future edit without changing this model on purpose.
    """

    user_id: str
    username: str
    role: Role
    is_active: bool

    model_config = {"extra": "forbid"}


def current_principal_dependency(
    get_session_service: Callable[[], SessionService],
) -> Callable[[Request], AuthenticatedPrincipal]:
    """Resolve the principal through the existing session dependency.

    The session service is built lazily on first use (never during
    ``create_app``), matching the login/logout composition, and the returned
    callable is exactly :func:`make_session_dependency`'s contract — one
    sanitized ``AuthenticationRequired`` for every unusable session, storage
    failures propagating untouched.
    """

    def resolve_principal(request: Request) -> AuthenticatedPrincipal:
        return make_session_dependency(get_session_service())(request)

    return resolve_principal


def register_me_route(
    app: FastAPI,
    *,
    path: str,
    get_session_service: Callable[[], SessionService],
) -> None:
    """Register ``GET {path}`` on ``app`` (the only current-user surface)."""
    dependency = Depends(current_principal_dependency(get_session_service))

    @app.get(path, response_model=MeResponse)
    def me(principal: AuthenticatedPrincipal = dependency) -> JSONResponse:
        """Return the identity of the session's user; never a credential."""
        body = MeResponse(
            user_id=principal.user_id,
            username=principal.username,
            role=principal.role,
            is_active=principal.is_active,
        ).model_dump(mode="json")
        return JSONResponse(
            status_code=200,
            content=body,
            headers={"cache-control": "no-store"},
        )
