"""P5.4.10 authorization-policy unit tests (offline, no app, no database).

Locks the approved owner matrix exactly: four roles, eight permissions, no
inheritance, no wildcard, no default grant, and an immutable mapping.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType

import pytest

from src.api_auth import AuthenticatedPrincipal
from src.authz import (
    ALL_PERMISSIONS,
    PERMISSION_DENIED_CODE,
    PERMISSION_DENIED_MESSAGE,
    ROLE_PERMISSIONS,
    Permission,
    PermissionDenied,
    enforce_permission,
    has_permission,
    permission_dependency,
    permissions_for,
)
from src.user import Role

#: The approved matrix, written out literally so a change to the policy (or a
#: change to the enum) fails here rather than silently widening access.
EXPECTED = {
    Role.ADMIN: {
        "employees:read",
        "employees:create",
        "employees:update",
        "employees:deactivate",
        "attendance:read",
        "attendance:checkout",
        "audit:read",
        "users:manage",
    },
    Role.HR_MANAGER: {
        "employees:read",
        "employees:create",
        "employees:update",
        "attendance:read",
        "attendance:checkout",
    },
    Role.OPERATOR: {
        "employees:read",
        "attendance:read",
        "attendance:checkout",
    },
    Role.AUDITOR: {
        "employees:read",
        "attendance:read",
        "audit:read",
    },
}


def principal(role: Role = Role.ADMIN, *, is_active: bool = True):
    return AuthenticatedPrincipal(
        user_id="user-1",
        username="ada",
        role=role,
        is_active=is_active,
    )


# ---------------------------------------------------------------------------
# A. The complete matrix — every role against every permission
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("permission", list(Permission))
def test_every_role_permission_pair(role: Role, permission: Permission) -> None:
    expected = permission.value in EXPECTED[role]
    assert has_permission(principal(role), permission) is expected


@pytest.mark.parametrize("role", list(Role))
def test_role_permission_sets_are_exactly_the_approved_matrix(role: Role) -> None:
    assert {permission.value for permission in permissions_for(role)} == EXPECTED[role]


def test_all_four_roles_and_no_others_are_covered() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)
    assert len(Role) == 4
    assert len(ALL_PERMISSIONS) == 8


def test_every_permission_is_granted_to_at_least_one_role() -> None:
    granted = set().union(*ROLE_PERMISSIONS.values())
    assert granted == ALL_PERMISSIONS, "an unreachable permission would be dead policy"


def test_admin_is_not_a_superset_by_inheritance_but_by_explicit_listing() -> None:
    # Admin holds every permission because the matrix grants all eight, not
    # because another role inherits upward.
    assert ROLE_PERMISSIONS[Role.ADMIN] == ALL_PERMISSIONS
    for role in (Role.HR_MANAGER, Role.OPERATOR, Role.AUDITOR):
        assert not ROLE_PERMISSIONS[role] >= ROLE_PERMISSIONS[Role.ADMIN]
        assert ROLE_PERMISSIONS[role] < ROLE_PERMISSIONS[Role.ADMIN]


@pytest.mark.parametrize(
    ("role", "forbidden"),
    [
        (Role.HR_MANAGER, Permission.EMPLOYEES_DEACTIVATE),
        (Role.HR_MANAGER, Permission.AUDIT_READ),
        (Role.HR_MANAGER, Permission.USERS_MANAGE),
        (Role.OPERATOR, Permission.EMPLOYEES_CREATE),
        (Role.OPERATOR, Permission.EMPLOYEES_UPDATE),
        (Role.OPERATOR, Permission.EMPLOYEES_DEACTIVATE),
        (Role.OPERATOR, Permission.AUDIT_READ),
        (Role.OPERATOR, Permission.USERS_MANAGE),
        (Role.AUDITOR, Permission.EMPLOYEES_CREATE),
        (Role.AUDITOR, Permission.EMPLOYEES_UPDATE),
        (Role.AUDITOR, Permission.EMPLOYEES_DEACTIVATE),
        (Role.AUDITOR, Permission.ATTENDANCE_CHECKOUT),
        (Role.AUDITOR, Permission.USERS_MANAGE),
    ],
)
def test_negative_cases_are_explicitly_denied(
    role: Role, forbidden: Permission
) -> None:
    assert has_permission(principal(role), forbidden) is False
    with pytest.raises(PermissionDenied):
        enforce_permission(principal(role), forbidden)


# ---------------------------------------------------------------------------
# B. Fail-closed handling of unknown roles and permissions
# ---------------------------------------------------------------------------


class LookalikeRole(str, Enum):
    """A different enum with the same values must never be accepted."""

    ADMIN = "Admin"


@pytest.mark.parametrize(
    "unknown_role",
    ["Admin", "admin", "", None, 0, object(), LookalikeRole.ADMIN],
)
def test_unknown_roles_receive_no_permissions(unknown_role: object) -> None:
    assert permissions_for(unknown_role) == frozenset()
    fake = AuthenticatedPrincipal(
        user_id="user-1",
        username="ada",
        role=unknown_role,  # type: ignore[arg-type]
        is_active=True,
    )
    for permission in Permission:
        assert has_permission(fake, permission) is False
    with pytest.raises(PermissionDenied):
        enforce_permission(fake, Permission.EMPLOYEES_READ)


@pytest.mark.parametrize(
    "unknown_permission",
    ["employees:read", "employees:delete", "", None, 1, LookalikeRole.ADMIN],
)
def test_unknown_permissions_are_denied(unknown_permission: object) -> None:
    assert has_permission(principal(Role.ADMIN), unknown_permission) is False  # type: ignore[arg-type]
    with pytest.raises(PermissionDenied):
        enforce_permission(principal(Role.ADMIN), unknown_permission)  # type: ignore[arg-type]


def test_inactive_principal_is_never_authorized() -> None:
    inactive_admin = principal(Role.ADMIN, is_active=False)
    for permission in Permission:
        assert has_permission(inactive_admin, permission) is False
    with pytest.raises(PermissionDenied):
        enforce_permission(inactive_admin, Permission.EMPLOYEES_READ)


def test_non_principal_objects_are_denied() -> None:
    for value in (None, "Admin", object(), {"role": "Admin"}):
        assert has_permission(value, Permission.EMPLOYEES_READ) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# C. The policy is immutable
# ---------------------------------------------------------------------------


def test_role_permissions_mapping_cannot_be_mutated() -> None:
    assert isinstance(ROLE_PERMISSIONS, MappingProxyType)
    with pytest.raises(TypeError):
        ROLE_PERMISSIONS[Role.OPERATOR] = ALL_PERMISSIONS  # type: ignore[index]
    with pytest.raises(TypeError):
        del ROLE_PERMISSIONS[Role.OPERATOR]  # type: ignore[attr-defined]


def test_granted_permission_sets_cannot_be_mutated() -> None:
    granted = ROLE_PERMISSIONS[Role.OPERATOR]
    assert isinstance(granted, frozenset)
    with pytest.raises(AttributeError):
        granted.add(Permission.USERS_MANAGE)  # type: ignore[attr-defined]


def test_policy_mutation_attempts_do_not_change_decisions() -> None:
    for role in Role:
        permissions_for(role)  # no-op reads
    assert has_permission(principal(Role.OPERATOR), Permission.USERS_MANAGE) is False
    assert has_permission(principal(Role.ADMIN), Permission.USERS_MANAGE) is True


# ---------------------------------------------------------------------------
# D. Reusable dependency
# ---------------------------------------------------------------------------


def test_dependency_returns_the_resolved_principal_when_permitted() -> None:
    active_admin = principal(Role.ADMIN)
    dependency = permission_dependency(
        Permission.EMPLOYEES_DEACTIVATE, lambda request: active_admin
    )
    assert dependency(object()) is active_admin  # type: ignore[arg-type]


def test_dependency_raises_permission_denied_without_the_permission() -> None:
    operator = principal(Role.OPERATOR)
    dependency = permission_dependency(
        Permission.EMPLOYEES_DEACTIVATE, lambda request: operator
    )
    with pytest.raises(PermissionDenied) as raised:
        dependency(object())  # type: ignore[arg-type]
    assert raised.value.permission is Permission.EMPLOYEES_DEACTIVATE


def test_dependency_propagates_authentication_failures_unchanged() -> None:
    from src.api_auth import AuthenticationRequired

    def unauthenticated(request):
        raise AuthenticationRequired()

    dependency = permission_dependency(Permission.EMPLOYEES_READ, unauthenticated)
    with pytest.raises(AuthenticationRequired):
        dependency(object())  # type: ignore[arg-type]


def test_dependency_propagates_infrastructure_failures_unchanged() -> None:
    from src.db import DatabaseUnavailableError

    def broken(request):
        raise DatabaseUnavailableError("offline")

    dependency = permission_dependency(Permission.EMPLOYEES_READ, broken)
    with pytest.raises(DatabaseUnavailableError):
        dependency(object())  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["employees:read", None, object(), 3])
def test_dependency_refuses_a_non_permission_at_construction(bad: object) -> None:
    with pytest.raises(TypeError):
        permission_dependency(bad, lambda request: principal())  # type: ignore[arg-type]


def test_dependency_refuses_a_non_callable_resolver() -> None:
    with pytest.raises(TypeError):
        permission_dependency(Permission.EMPLOYEES_READ, object())  # type: ignore[arg-type]


def test_denial_carries_no_account_or_session_data() -> None:
    error = PermissionDenied(Permission.USERS_MANAGE)
    assert str(error) == "permission denied"
    assert "ada" not in repr(error)
    assert "user-1" not in repr(error)
    assert PERMISSION_DENIED_CODE == "PERMISSION_DENIED"
    assert "permission" in PERMISSION_DENIED_MESSAGE.lower()
    assert "users:manage" not in PERMISSION_DENIED_MESSAGE
