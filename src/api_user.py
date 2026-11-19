"""User administration API (P5.6).

Admin-only user management under ``/api/v1/users``:

* ``GET /api/v1/users`` — list users (paginated, safe metadata only)
* ``POST /api/v1/users`` — create a user
* ``PATCH /api/v1/users/{user_id}/role`` — change a user's role
* ``POST /api/v1/users/{user_id}/deactivate`` — deactivate a user

All routes require ``users:manage`` (Admin only). The service layer enforces
the remaining business invariants (last-Admin protection, no self-change, valid
roles, hashed passwords, no credential leakage).

Composition
-----------

This module owns route definitions only. The application injects

* ``get_user_service`` — a **cached, application-scoped** getter for the one
  :class:`~src.user_service.UserService` instance (no service, writer or
  repository is re-created per request), and
* ``get_session_service`` — the existing session-service getter used by
  :func:`src.api_auth.make_session_dependency` to authenticate the caller.

Neither the session service nor any repository is reached through a private
attribute: the service dependency is supplied by the application factory, which
builds it through public constructors (:func:`build_user_service`).

No public registration endpoint exists. First-Admin provisioning is a CLI
operation (:mod:`src.user_provision`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api_auth import AuthenticatedPrincipal, make_session_dependency
from src.audit_service import AuditService
from src.authz import Permission, permission_dependency
from src.config import AppSettings
from src.password import PasswordService
from src.user import Role, UserValidationError
from src.user_service import (
    ActorNotAdminError,
    AdminAlreadyExistsError,
    CreatedUser,
    DeactivationResult,
    InvalidRoleError,
    LastAdminProtectedError,
    PasswordTooShortError,
    RoleChangeResult,
    SelfDeactivationError,
    SelfRoleChangeError,
    SessionRevocationFailedError,
    UserAdministrationError,
    UserAlreadyExistsError,
    UserNotFoundError,
    UserService,
)

__all__ = [
    "USERS_PATH",
    "USER_ROLE_PATH",
    "USER_DEACTIVATE_PATH",
    "UserCreateRequest",
    "RoleChangeRequest",
    "build_user_service",
    "register_user_routes",
]

USERS_PATH: str = "/users"
USER_ROLE_PATH: str = "/users/{user_id}/role"
USER_DEACTIVATE_PATH: str = "/users/{user_id}/deactivate"

#: Fixed, sanitized (status, code, message) for user administration.
_USER_ADMIN_ERROR_RESPONSES: dict[
    type[UserAdministrationError], tuple[int, str, str]
] = {
    ActorNotAdminError: (403, "FORBIDDEN", "Only an administrator may manage user accounts."),
    SessionRevocationFailedError: (
        503,
        "SESSION_REVOCATION_FAILED",
        "The account was deactivated, but its sessions could not be revoked. "
        "Do not retry blindly.",
    ),
    LastAdminProtectedError: (400, "LAST_ADMIN_PROTECTED", "The last active administrator cannot be modified."),
    SelfRoleChangeError: (400, "SELF_ROLE_CHANGE", "An administrator cannot change their own role."),
    SelfDeactivationError: (400, "SELF_DEACTIVATION", "An administrator cannot deactivate their own account."),
    AdminAlreadyExistsError: (
        400,
        "ADMIN_ALREADY_EXISTS",
        "An active administrator already exists; provisioning is refused.",
    ),
    UserAlreadyExistsError: (400, "USER_ALREADY_EXISTS", "A user with that username already exists."),
    UserNotFoundError: (400, "USER_NOT_FOUND", "The requested user was not found."),
    InvalidRoleError: (400, "INVALID_ROLE", "The requested role is not valid."),
    PasswordTooShortError: (400, "PASSWORD_TOO_SHORT", "The password must be at least 8 characters."),
}


def _error_response(exc: UserAdministrationError) -> JSONResponse:
    """Fixed, sanitized error envelope for a user-administration failure."""
    status, code, message = _USER_ADMIN_ERROR_RESPONSES.get(
        type(exc), (400, "USER_ADMIN_ERROR", "User administration error.")
    )
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message}}
    )


def _role_or_none(value: str) -> Role | None:
    """Parse a validated request string into a Role, or ``None``."""
    try:
        return Role(value)
    except ValueError:
        return None


class UserCreateRequest(BaseModel):
    """The public input for user creation (P5.6)."""
    user_id: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=1024)
    role: str = Field(pattern=r"^(Admin|HR Manager|Operator|Auditor)$")
    model_config = {"extra": "forbid"}


class RoleChangeRequest(BaseModel):
    """The public input for a role change (P5.6)."""
    new_role: str = Field(pattern=r"^(Admin|HR Manager|Operator|Auditor)$")
    model_config = {"extra": "forbid"}


class UserListItem(BaseModel):
    """Safe, public metadata for one user (never a hash or credential)."""
    user_id: str
    username: str
    role: str
    is_active: bool
    created_at: str
    updated_at: str
    model_config = {"extra": "forbid"}


class UserListResponse(BaseModel):
    """Paginated list of users (safe metadata only)."""
    users: list[UserListItem]
    limit: int
    next_after: str | None
    model_config = {"extra": "forbid"}


class CreatedUserResponse(BaseModel):
    """The public result of a successful user creation."""
    user_id: str
    username: str
    role: str
    is_active: bool
    created_at: str
    model_config = {"extra": "forbid"}


class RoleChangeResponse(BaseModel):
    """The public result of a successful role change."""
    user_id: str
    username: str
    new_role: str
    updated_at: str
    model_config = {"extra": "forbid"}


class DeactivationResponse(BaseModel):
    """The public result of a successful deactivation."""
    user_id: str
    username: str
    is_active: bool
    updated_at: str
    model_config = {"extra": "forbid"}


def _role_to_str(role: Role) -> str:
    """Serialize a Role for JSON responses."""
    return role.value


def _datetime_to_str(dt: object) -> str:
    """Serialize a datetime for JSON responses (ISO 8601 UTC)."""
    if not isinstance(dt, datetime):
        raise TypeError(f"expected datetime, got {type(dt).__name__}")
    return dt.isoformat()


def build_user_service(
    settings: AppSettings,
    *,
    sessions: Any,
    audit: AuditService,
) -> UserService:
    """Build the production :class:`UserService` (no connection on build).

    Mirrors :func:`src.api_login.build_login_service`: the DSN availability
    check happens *before* the repository layer (and psycopg) is imported, so a
    missing database fails closed instead of falling back to an implicit local
    target. The repositories themselves open one connection per operation.

    The caller owns the lifecycle: the application factory calls this once and
    caches the result, so routes never construct a service, a writer or an
    audit journal per request.
    """
    from src.db import DatabaseUnavailableError

    if not isinstance(settings.database_url, str) or not settings.database_url.strip():
        raise DatabaseUnavailableError(
            "No application database is configured for the API."
        )

    from src.db_security_repository import open_user_repository

    return UserService(
        users=open_user_repository(),
        passwords=PasswordService(),
        audit=audit,
        sessions=sessions,
    )


def register_user_routes(
    app: FastAPI,
    *,
    path: str,
    get_user_service: Callable[[], UserService],
    get_session_service: Callable[[], Any],
) -> None:
    """Register the four user-administration routes on ``app`` (P5.6).

    Parameters
    ----------
    app:
        The FastAPI application.
    path:
        The URL prefix (e.g. ``/api/v1``).
    get_user_service:
        An application-scoped getter for the single ``UserService``. It is
        called per request but must return the same cached instance.
    get_session_service:
        The application's session-service getter. It authenticates the caller
        through the existing :func:`make_session_dependency`; no repository or
        private attribute is read here.
    """
    def _get_principal(request: Request) -> AuthenticatedPrincipal:
        return make_session_dependency(get_session_service())(request)

    users_guard = permission_dependency(Permission.USERS_MANAGE, _get_principal)

    @app.get(f"{path}{USERS_PATH}")
    def list_users(
        request: Request,
        principal: AuthenticatedPrincipal = Depends(users_guard),
    ) -> JSONResponse:
        """List users (Admin only, paginated, safe metadata only)."""
        service = get_user_service()

        after = request.query_params.get("after")
        limit_str = request.query_params.get("limit")
        limit: int | None = None
        if limit_str is not None:
            try:
                limit = int(limit_str)
                if limit < 1:
                    raise ValueError()
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"error": {"code": "INVALID_LIMIT", "message": "limit must be a positive integer."}},
                )

        try:
            page = service.list_users(after=after, limit=limit)
        except UserValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "VALIDATION_FAILED", "message": str(exc)}},
            )

        items = [
            UserListItem(
                user_id=u.user_id,
                username=u.username,
                role=_role_to_str(u.role),
                is_active=u.is_active,
                created_at=_datetime_to_str(u.created_at),
                updated_at=_datetime_to_str(u.updated_at),
            )
            for u in page.users
        ]

        return JSONResponse(
            status_code=200,
            content=UserListResponse(
                users=items,
                limit=page.limit,
                next_after=page.next_after,
            ).model_dump(mode="json"),
        )

    @app.post(f"{path}{USERS_PATH}")
    def create_user(
        payload: UserCreateRequest,
        principal: AuthenticatedPrincipal = Depends(users_guard),
    ) -> JSONResponse:
        """Create a new user (Admin only)."""
        service = get_user_service()
        role = _role_or_none(payload.role)
        if role is None:
            return _error_response(InvalidRoleError())

        try:
            created: CreatedUser = service.create_user(
                actor=principal,
                user_id=payload.user_id,
                username=payload.username,
                password=payload.password,
                role=role,
            )
        except UserAdministrationError as exc:
            return _error_response(exc)
        except UserValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "VALIDATION_FAILED", "message": str(exc)}},
            )

        return JSONResponse(
            status_code=201,
            content=CreatedUserResponse(
                user_id=created.user_id,
                username=created.username,
                role=_role_to_str(created.role),
                is_active=created.is_active,
                created_at=_datetime_to_str(created.created_at),
            ).model_dump(mode="json"),
        )

    @app.patch(f"{path}{USER_ROLE_PATH}")
    def change_role(
        payload: RoleChangeRequest,
        user_id: str,
        principal: AuthenticatedPrincipal = Depends(users_guard),
    ) -> JSONResponse:
        """Change another user's role (Admin only)."""
        service = get_user_service()
        new_role = _role_or_none(payload.new_role)
        if new_role is None:
            return _error_response(InvalidRoleError())

        try:
            result: RoleChangeResult = service.change_user_role(
                actor=principal,
                target_user_id=user_id,
                new_role=new_role,
            )
        except UserAdministrationError as exc:
            return _error_response(exc)
        except UserValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "VALIDATION_FAILED", "message": str(exc)}},
            )

        return JSONResponse(
            status_code=200,
            content=RoleChangeResponse(
                user_id=result.user_id,
                username=result.username,
                new_role=_role_to_str(result.new_role),
                updated_at=_datetime_to_str(result.updated_at),
            ).model_dump(mode="json"),
        )

    @app.post(f"{path}{USER_DEACTIVATE_PATH}")
    def deactivate_user(
        user_id: str,
        principal: AuthenticatedPrincipal = Depends(users_guard),
    ) -> JSONResponse:
        """Deactivate another user (Admin only)."""
        service = get_user_service()

        try:
            result: DeactivationResult = service.deactivate_user(
                actor=principal,
                target_user_id=user_id,
            )
        except UserAdministrationError as exc:
            return _error_response(exc)
        except UserValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "VALIDATION_FAILED", "message": str(exc)}},
            )

        return JSONResponse(
            status_code=200,
            content=DeactivationResponse(
                user_id=result.user_id,
                username=result.username,
                is_active=result.is_active,
                updated_at=_datetime_to_str(result.updated_at),
            ).model_dump(mode="json"),
        )
