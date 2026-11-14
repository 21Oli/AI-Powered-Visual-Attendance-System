"""Centralized backend authorization policy (P5.4.10).

This module is the *only* place that decides what a role may do. It is
deliberately small: an explicit permission enum, one immutable role-to-permission
table, one pure decision function, and one FastAPI dependency factory. There is
no policy language, no inheritance, no wildcard and no default grant.

Identity source
---------------

Authorization never reads identity from the request. The dependency wraps the
existing P5.4.4 authentication dependency
(:func:`src.api_auth.make_session_dependency` through
:func:`src.api_me.current_principal_dependency`), so the only credential is the
``va_session`` cookie and the only identity is the validated server-side
:class:`~src.api_auth.AuthenticatedPrincipal`. A body, query parameter or
header claiming a role, user ID or permission is ignored — there is no code
path here that reads one.

Fail-closed rules
-----------------

* Unknown, non-:class:`~src.user.Role` or missing roles get **no** permissions;
  a raw string such as ``"Admin"`` is not a ``Role`` and is denied.
* A permission that is not a :class:`Permission` member is denied (and refuses
  route construction, so a typo cannot silently unprotect an endpoint).
* ``is_active=False`` is denied even if the role would otherwise be allowed:
  session validation already refuses inactive accounts, and this check means
  authorization does not depend on that being true forever.
* A role absent from the table grants nothing — there is no fallback branch
  that allows access.
* Missing attribute definitions raise at import time? No: the table is built
  from the enum members, and a test asserts every ``Permission`` is granted to
  at least one role and every ``Role`` has an entry.

HTTP behaviour
--------------

:class:`PermissionDenied` carries no user data. The application maps it to the
sanitized ``403 PERMISSION_DENIED`` envelope with the existing ``X-Request-ID``
header; an unauthenticated request still fails earlier with the existing
``401 AUTHENTICATION_REQUIRED``. Database failures during session validation
propagate unchanged and stay the existing sanitized 503; unexpected faults stay
500. Nothing here logs a username, role, token or permission.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Callable, Final, Mapping

from fastapi import Request

from src.api_auth import AuthenticatedPrincipal
from src.user import Role

__all__ = [
    "ALL_PERMISSIONS",
    "PERMISSION_DENIED_CODE",
    "PERMISSION_DENIED_MESSAGE",
    "Permission",
    "PermissionDenied",
    "ROLE_PERMISSIONS",
    "enforce_permission",
    "has_permission",
    "permission_dependency",
    "permissions_for",
]


class Permission(str, Enum):
    """The approved Phase 5 permissions (owner matrix, P5.4.10).

    Values are the stable machine names used in policy, tests and
    documentation. They are **not** accepted from a client: nothing in the
    request can name a permission.
    """

    EMPLOYEES_READ = "employees:read"
    EMPLOYEES_CREATE = "employees:create"
    EMPLOYEES_UPDATE = "employees:update"
    EMPLOYEES_DEACTIVATE = "employees:deactivate"
    ATTENDANCE_READ = "attendance:read"
    ATTENDANCE_CHECKOUT = "attendance:checkout"
    AUDIT_READ = "audit:read"
    USERS_MANAGE = "users:manage"


#: Every permission that exists. Used to assert completeness in tests.
ALL_PERMISSIONS: Final[frozenset[Permission]] = frozenset(Permission)

#: Stable machine code for the sanitized 403 envelope.
PERMISSION_DENIED_CODE: str = "PERMISSION_DENIED"
#: Fixed human message; never names the role, permission or account.
PERMISSION_DENIED_MESSAGE: str = (
    "You do not have permission to perform this action."
)


class PermissionDenied(Exception):
    """The authenticated principal lacks the required permission.

    Deliberately carries no request identity: the client learns only that the
    action is not permitted. The required permission is kept for server-side
    tests and never rendered into the response body.
    """

    def __init__(self, permission: Permission) -> None:
        super().__init__("permission denied")
        self.permission = permission


#: The approved matrix. Exactly four roles, exactly the granted permissions —
#: no inheritance, no hierarchy, no implicit administrative power.
#: Values are ``frozenset`` and the mapping itself is read-only, so a caller
#: cannot widen a role at runtime.
ROLE_PERMISSIONS: Final[Mapping[Role, frozenset[Permission]]] = MappingProxyType(
    {
        Role.ADMIN: frozenset(Permission),
        Role.HR_MANAGER: frozenset(
            {
                Permission.EMPLOYEES_READ,
                Permission.EMPLOYEES_CREATE,
                Permission.EMPLOYEES_UPDATE,
                Permission.ATTENDANCE_READ,
                Permission.ATTENDANCE_CHECKOUT,
            }
        ),
        Role.OPERATOR: frozenset(
            {
                Permission.EMPLOYEES_READ,
                Permission.ATTENDANCE_READ,
                Permission.ATTENDANCE_CHECKOUT,
            }
        ),
        Role.AUDITOR: frozenset(
            {
                Permission.EMPLOYEES_READ,
                Permission.ATTENDANCE_READ,
                Permission.AUDIT_READ,
            }
        ),
    }
)


def permissions_for(role: object) -> frozenset[Permission]:
    """Return the granted permissions for ``role`` — empty for anything unknown.

    Only a real :class:`~src.user.Role` member can be granted anything. Strings,
    ``None``, other enums and lookalike objects get the empty set, so a
    client-supplied ``"Admin"`` can never be coerced into the Admin policy.
    """
    if not isinstance(role, Role):
        return frozenset()
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(
    principal: AuthenticatedPrincipal, permission: Permission
) -> bool:
    """Whether this validated principal holds ``permission`` (fail closed)."""
    if not isinstance(permission, Permission):
        return False
    if not isinstance(principal, AuthenticatedPrincipal):
        return False
    if not principal.is_active:
        # Session validation also refuses inactive accounts; this check makes
        # authorization independent of that remaining true.
        return False
    return permission in permissions_for(principal.role)


def enforce_permission(
    principal: AuthenticatedPrincipal, permission: Permission
) -> None:
    """Raise :class:`PermissionDenied` unless the principal may proceed."""
    if not has_permission(principal, permission):
        raise PermissionDenied(permission)


def permission_dependency(
    permission: Permission,
    get_principal: Callable[[Request], AuthenticatedPrincipal],
) -> Callable[[Request], AuthenticatedPrincipal]:
    """Build the FastAPI dependency that enforces one permission.

    ``get_principal`` is the existing authentication dependency (already
    resolved by the application); this function never parses a cookie, validates
    a token or looks up a session itself. The permission is validated at
    construction so a mistyped value fails loudly instead of silently leaving a
    route unprotected, and each dependency instance closes over exactly one
    permission.
    """
    if not isinstance(permission, Permission):
        raise TypeError(
            "permission must be a Permission member, got "
            f"{type(permission).__name__}"
        )
    if not callable(get_principal):
        raise TypeError("get_principal must be a callable")

    def require_permission(request: Request) -> AuthenticatedPrincipal:
        principal = get_principal(request)
        enforce_permission(principal, permission)
        return principal

    return require_permission
