"""User administration service layer (P5.6).

This module is the **only** place that decides user-management business rules:
create, list, role change, deactivate, and first-Admin provisioning. It
composes the existing P5.2 repositories, the P5.3 password service, and the
P5.5 audit service — it owns no SQL, no HTTP, no cookie handling and no
password hashing.

Fail-closed rules
-----------------
* Every mutation is audited through the intent/result protocol before it runs.
* The last-active-Admin invariant is enforced atomically in the repository
  (one enforced-READ COMMITTED advisory-lock transaction), never by counting
  Admins in the service.
* Self-role-change and self-deactivation are refused by the service, not only
  by the route.
* Only Admin users may call these operations; the route enforces
  ``users:manage``, and the service enforces the remaining business invariants.
* Passwords are hashed with the existing Argon2id implementation and never
  stored or returned in plaintext.
* Sensitive values (passwords, hashes, tokens) never enter audit metadata.

What it does not do
-------------------
No HTTP route, no request parsing, no cookie handling, no frontend, no
database migration, no login/logout/me and no attendance work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from src.api_request_context import current_request_id
from src.audit_schema import AuditSource, AuditReasonCode
from src.audit_service import (
    AuditService,
    AuditUnavailableError,
    AuditActor,
    AuditContext,
    MutationIntent,
)
from src.clock import utc_now
from src.password import PasswordService
from src.user import Role, User, UserValidationError
from src.user_repository import (
    LastAdminProtectionError,
    UserAlreadyExists,
    UserNotFound,
    UserRepository,
)

#: Roles that may be explicitly assigned by an Admin through the API.
_ASSIGNABLE_ROLES: Final[frozenset[Role]] = frozenset(Role)

#: Fixed, sanitized error messages (never leak db detail or identifier).
_MSG_LAST_ADMIN_PROTECTED = (
    "The last active administrator account cannot be changed or deactivated."
)
_MSG_SELF_ROLE_CHANGE = "An administrator cannot change their own role."
_MSG_SELF_DEACTIVATION = "An administrator cannot deactivate their own account."
_MSG_NOT_ADMIN = "Only an administrator may manage user accounts."
_MSG_ADMIN_ALREADY_EXISTS = (
    "An active administrator already exists; first-Admin provisioning is refused."
)
_MSG_SESSION_REVOCATION_FAILED = (
    "The account was deactivated, but its sessions could not be revoked. "
    "Treat the account as deactivated and do not retry blindly."
)
_MSG_USER_EXISTS = "A user with that username already exists."
_MSG_USER_NOT_FOUND = "The requested user was not found."
_MSG_INVALID_ROLE = "The requested role is not valid."
_MSG_PASSWORD_TOO_SHORT = (
    "The password must be at least 8 characters."
)


class UserAdministrationError(Exception):
    """Base class for user-administration service failures."""
    code: Final[str]


class LastAdminProtectedError(UserAdministrationError):
    """The operation would violate the last-active-Admin invariant."""
    code: Final[str] = "LAST_ADMIN_PROTECTED"
    def __str__(self) -> str:
        return _MSG_LAST_ADMIN_PROTECTED


class SelfRoleChangeError(UserAdministrationError):
    """An Admin attempted to change their own role."""
    code: Final[str] = "SELF_ROLE_CHANGE"
    def __str__(self) -> str:
        return _MSG_SELF_ROLE_CHANGE


class SelfDeactivationError(UserAdministrationError):
    """An Admin attempted to deactivate their own account."""
    code: Final[str] = "SELF_DEACTIVATION"
    def __str__(self) -> str:
        return _MSG_SELF_DEACTIVATION


class ActorNotAdminError(UserAdministrationError):
    """A non-Admin principal attempted a user-management operation."""
    code: Final[str] = "FORBIDDEN"
    def __str__(self) -> str:
        return _MSG_NOT_ADMIN


class AdminAlreadyExistsError(UserAdministrationError):
    """First-Admin provisioning was refused because an active Admin exists."""
    code: Final[str] = "ADMIN_ALREADY_EXISTS"
    def __str__(self) -> str:
        return _MSG_ADMIN_ALREADY_EXISTS


class SessionRevocationFailedError(UserAdministrationError):
    """Deactivation applied but revoking the target's sessions failed."""
    code: Final[str] = "SESSION_REVOCATION_FAILED"
    def __str__(self) -> str:
        return _MSG_SESSION_REVOCATION_FAILED


class UserAlreadyExistsError(UserAdministrationError):
    """The requested username is already taken."""
    code: Final[str] = "USER_ALREADY_EXISTS"
    def __str__(self) -> str:
        return _MSG_USER_EXISTS


class UserNotFoundError(UserAdministrationError):
    """The requested user does not exist."""
    code: Final[str] = "USER_NOT_FOUND"
    def __str__(self) -> str:
        return _MSG_USER_NOT_FOUND


class InvalidRoleError(UserAdministrationError):
    """The requested role is not one of the approved roles."""
    code: Final[str] = "INVALID_ROLE"
    def __str__(self) -> str:
        return _MSG_INVALID_ROLE


class PasswordTooShortError(UserAdministrationError):
    """The supplied password is below the minimum length."""
    code: Final[str] = "PASSWORD_TOO_SHORT"
    def __str__(self) -> str:
        return _MSG_PASSWORD_TOO_SHORT


@dataclass(frozen=True, slots=True)
class UserListItem:
    """Safe, public metadata for one user (never a hash or credential)."""
    user_id: str
    username: str
    role: Role
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UserListPage:
    """One page of user listing results."""
    users: list[UserListItem]
    limit: int
    next_after: str | None


@dataclass(frozen=True, slots=True)
class CreatedUser:
    """The public result of a successful user creation."""
    user_id: str
    username: str
    role: Role
    is_active: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RoleChangeResult:
    """The public result of a successful role change."""
    user_id: str
    username: str
    new_role: Role
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DeactivationResult:
    """The public result of a successful deactivation."""
    user_id: str
    username: str
    is_active: bool
    updated_at: datetime


class UserService:
    """User administration business logic (P5.6).

    Parameters
    ----------
    users:
        The P5.2 user repository (one connection per operation).
    passwords:
        The P5.3 password service (Argon2id hashing/verification).
    audit:
        The P5.5 audit service (intent/result protocol).
    sessions:
        The P5.3 session service (for session revocation on deactivation).
        Required for production use; tests may inject a fake.
    """

    def __init__(
        self,
        *,
        users: UserRepository,
        passwords: PasswordService,
        audit: AuditService,
        sessions: Any,
    ) -> None:
        self._users = users
        self._passwords = passwords
        self._audit = audit
        self._sessions = sessions

    # -- helpers ------------------------------------------------------------

    def _now(self) -> datetime:
        return utc_now()

    @staticmethod
    def _audit_request_id(request_id: str | None) -> str | None:
        """Resolve the server-generated request ID used for audit correlation.

        An explicit value from the HTTP layer always wins. When the caller is
        inside a request (the middleware publishes its UUIDv4 in a context
        variable) that ID is reused, so the audit record matches the response's
        ``X-Request-ID`` and its structured logs. A CLI invocation or a direct
        service call has no request, and fabricating a random UUID there would
        break correlation instead of providing it, so ``None`` is returned.
        """
        if request_id is not None:
            return request_id
        return current_request_id()

    def _require_admin(self, principal: object) -> None:
        """Refuse non-Admin principals (defence in depth behind the route)."""
        role = getattr(principal, "role", None)
        if role is not Role.ADMIN:
            raise ActorNotAdminError()

    def _record_unknown_outcome(self, intent: MutationIntent) -> None:
        """Record an unknown mutation outcome before re-raising.

        Any failure that is not one of the typed, provably-not-applied errors
        may have reached PostgreSQL (for example a dropped connection during
        commit), so it must never be audited as a definite failure.
        """
        self._audit.record_unknown(
            intent, reason_code=AuditReasonCode.DATABASE_OUTCOME_UNKNOWN
        )

    def _assert_not_self_role_change(
        self, target_user_id: str, actor_user_id: str
    ) -> None:
        """Refuse self-role-change."""
        if target_user_id == actor_user_id:
            raise SelfRoleChangeError()

    def _assert_not_self_deactivation(
        self, target_user_id: str, actor_user_id: str
    ) -> None:
        """Refuse self-deactivation."""
        if target_user_id == actor_user_id:
            raise SelfDeactivationError()

    def _require_assignable_role(self, role: object) -> Role:
        """Refuse non-Role values and unapproved roles."""
        try:
            role_member = Role(role) if not isinstance(role, Role) else role
        except (ValueError, TypeError):
            raise InvalidRoleError()
        if role_member not in _ASSIGNABLE_ROLES:
            raise InvalidRoleError()
        return role_member

    def _require_existing_user(self, user_id: str) -> User:
        """Fetch a user or raise UserNotFoundError."""
        user = self._users.get(user_id)
        if user is None:
            raise UserNotFoundError()
        return user

    # -- list ---------------------------------------------------------------

    def list_users(
        self,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> UserListPage:
        """Return a bounded page of users (safe metadata only).

        Password hashes, tokens and other credentials are never returned.
        """
        from src.config import USER_PAGINATION

        resolved_limit = (
            USER_PAGINATION.default_page_size if limit is None else limit
        )
        effective_limit = min(resolved_limit, USER_PAGINATION.max_page_size)

        users = self._users.list(after=after, limit=limit)
        return UserListPage(
            users=[
                UserListItem(
                    user_id=u.user_id,
                    username=u.username,
                    role=u.role,
                    is_active=u.is_active,
                    created_at=u.created_at,
                    updated_at=u.updated_at,
                )
                for u in users
            ],
            # Echo the effective page size, not the row count: a short page
            # means there is nothing after it.
            limit=effective_limit,
            next_after=(
                users[-1].user_id if len(users) == effective_limit else None
            ),
        )

    # -- create -------------------------------------------------------------

    def create_user(
        self,
        *,
        actor: object,
        user_id: str,
        username: str,
        password: str,
        role: Role,
        request_id: str | None = None,
    ) -> CreatedUser:
        """Create a new application user as an Admin.

        Passwords are hashed with the existing Argon2id implementation and never
        stored or returned in plaintext.
        """
        self._require_admin(actor)

        role = self._require_assignable_role(role)

        if not isinstance(password, str) or len(password) < 8:
            raise PasswordTooShortError()

        now = self._now()

        # Hash the password first, then validate the user model.
        password_hash = self._passwords.hash_password(password)

        # Validate the user model (normalization, key derivation, etc.)
        try:
            new_user = User(
                user_id=user_id,
                username=username,
                password_hash=password_hash,
                role=role,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        except UserValidationError as exc:
            raise UserAdministrationError(str(exc)) from exc

        # Begin audit intent before the mutation.
        actor_obj = AuditActor(user_id=getattr(actor, "user_id", ""), role=getattr(actor, "role", Role.ADMIN))
        context = AuditContext(
            actor=actor_obj, request_id=self._audit_request_id(request_id)
        )
        
        try:
            intent = self._audit.begin_user_create(
                target_id=user_id,
                context=context,
            )
        except AuditUnavailableError:
            raise

        # Create the user.
        try:
            created = self._users.create(new_user)
        except UserAlreadyExists as exc:
            self._audit.record_not_applied(intent, AuditReasonCode.EMPLOYEE_EXISTS)
            raise UserAlreadyExistsError() from exc
        except Exception:
            self._record_unknown_outcome(intent)
            raise

        # Record successful result (a failed result write becomes the
        # AUDIT_RESULT_UNCERTAIN contract, never a silent success).
        self._audit.record_applied(intent)

        return CreatedUser(
            user_id=created.user_id,
            username=created.username,
            role=created.role,
            is_active=created.is_active,
            created_at=created.created_at,
        )

    # -- role change --------------------------------------------------------

    def change_user_role(
        self,
        *,
        actor: object,
        target_user_id: str,
        new_role: Role,
        request_id: str | None = None,
    ) -> RoleChangeResult:
        """Change another user's role as an Admin.

        Self-role-change, non-Admin actors, and invalid roles are refused.
        Admin demotion is protected by the last-admin invariant.
        """
        self._require_admin(actor)
        actor_user_id = getattr(actor, "user_id", "")
        self._assert_not_self_role_change(target_user_id, actor_user_id)

        new_role = self._require_assignable_role(new_role)
        target = self._require_existing_user(target_user_id)

        # The last-admin invariant for demotion is enforced atomically by the
        # repository (advisory-lock-guarded check + update). No count is made
        # here: a service-side count would read one paginated page and miscount.
        now = self._now()

        # Begin audit intent before the mutation.
        actor_obj = AuditActor(user_id=actor_user_id, role=Role.ADMIN)
        context = AuditContext(
            actor=actor_obj, request_id=self._audit_request_id(request_id)
        )
        
        try:
            intent = self._audit.begin_user_role_change(
                target_id=target_user_id,
                context=context,
            )
        except AuditUnavailableError:
            raise

        # Perform the role change with admin check if needed.
        try:
            if target.role is Role.ADMIN and new_role is not Role.ADMIN:
                updated = self._users.update_role_with_admin_check(
                    target_user_id=target_user_id,
                    new_role=new_role,
                    updated_at=now,
                )
            else:
                updated = self._users.update_role(
                    target_user_id, new_role, updated_at=now
                )
        except LastAdminProtectionError as exc:
            self._audit.record_not_applied(intent, AuditReasonCode.VALIDATION_FAILED)
            raise LastAdminProtectedError() from exc
        except UserNotFound as exc:
            self._audit.record_not_applied(intent, AuditReasonCode.EMPLOYEE_NOT_FOUND)
            raise UserNotFoundError() from exc
        except Exception:
            self._record_unknown_outcome(intent)
            raise

        # Record successful result.
        self._audit.record_applied(intent)

        return RoleChangeResult(
            user_id=updated.user_id,
            username=updated.username,
            new_role=updated.role,
            updated_at=updated.updated_at,
        )

    # -- deactivate ----------------------------------------------------------

    def deactivate_user(
        self,
        *,
        actor: object,
        target_user_id: str,
        request_id: str | None = None,
    ) -> DeactivationResult:
        """Deactivate another user as an Admin.

        Self-deactivation, non-Admin actors, and deactivation of the last
        active Admin are refused. The last-admin check is performed atomically
        with the update to prevent TOCTOU races. Session revocation happens
        after successful deactivation.
        """
        self._require_admin(actor)
        actor_user_id = getattr(actor, "user_id", "")
        self._assert_not_self_deactivation(target_user_id, actor_user_id)

        target = self._require_existing_user(target_user_id)

        if not target.is_active:
            # Already inactive: no mutation and no audit, but still revoke. A
            # caller retrying after a failed revocation must not be told
            # "success" while live sessions survive; the account's inactive
            # state already blocks authentication, and this repairs the rows.
            try:
                self._sessions.revoke_all_for_user(target_user_id)
            except Exception:
                raise SessionRevocationFailedError() from None
            return DeactivationResult(
                user_id=target.user_id,
                username=target.username,
                is_active=False,
                updated_at=target.updated_at,
            )

        now = self._now()

        # Begin audit intent before the mutation.
        actor_obj = AuditActor(user_id=actor_user_id, role=Role.ADMIN)
        context = AuditContext(
            actor=actor_obj, request_id=self._audit_request_id(request_id)
        )
        
        try:
            intent = self._audit.begin_user_deactivate(
                target_id=target_user_id,
                context=context,
            )
        except AuditUnavailableError:
            raise

        try:
            updated = self._users.update_active_with_admin_check(
                target_user_id=target_user_id,
                is_active=False,
                updated_at=now,
            )
        except UserNotFound as exc:
            self._audit.record_not_applied(intent, AuditReasonCode.EMPLOYEE_NOT_FOUND)
            raise UserNotFoundError() from exc
        except LastAdminProtectionError as exc:
            self._audit.record_not_applied(intent, AuditReasonCode.VALIDATION_FAILED)
            raise LastAdminProtectedError() from exc
        except Exception:
            self._record_unknown_outcome(intent)
            raise

        # Revoke every session for the deactivated user. The database row is
        # already committed, so this cannot be rolled back: if revocation fails
        # the overall security outcome is unconfirmed and the caller must not
        # receive a success response.
        try:
            self._sessions.revoke_all_for_user(target_user_id)
        except Exception:
            self._record_unknown_outcome(intent)
            raise SessionRevocationFailedError() from None

        # Record successful result.
        self._audit.record_applied(intent)

        return DeactivationResult(
            user_id=updated.user_id,
            username=updated.username,
            is_active=updated.is_active,
            updated_at=updated.updated_at,
        )

    # -- first-admin provisioning -------------------------------------------

    def provision_first_admin(
        self,
        *,
        user_id: str,
        username: str,
        password: str,
        request_id: str | None = None,
    ) -> CreatedUser:
        """Create the first Admin account (operator-controlled, CLI).

        Refuses if any active Admin already exists. Safe against concurrent
        attempts: the repository checks and inserts inside the shared
        enforced-READ COMMITTED advisory-lock transaction, so exactly one
        attempt succeeds and the loser is refused.
        """
        role = Role.ADMIN

        if not isinstance(password, str) or len(password) < 8:
            raise PasswordTooShortError()

        now = self._now()

        # Hash the password first, then validate the user model.
        password_hash = self._passwords.hash_password(password)

        try:
            new_user = User(
                user_id=user_id,
                username=username,
                password_hash=password_hash,
                role=role,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        except UserValidationError as exc:
            raise UserAdministrationError(str(exc)) from exc

        # Begin audit intent before the mutation (CLI source).
        actor_obj = AuditActor(user_id="cli", role=Role.ADMIN)
        context = AuditContext(
            actor=actor_obj, request_id=self._audit_request_id(request_id)
        )
        
        try:
            intent = self._audit.begin_first_admin_provision(
                target_id=user_id,
                context=context,
                source=AuditSource.CLI,
            )
        except AuditUnavailableError:
            raise

        # Attempt to create the first Admin atomically.
        try:
            created = self._users.provision_first_admin(
                new_user,
                updated_at=now,
            )
        except UserAlreadyExists as exc:
            self._audit.record_not_applied(intent, AuditReasonCode.EMPLOYEE_EXISTS)
            raise UserAlreadyExistsError() from exc
        except LastAdminProtectionError as exc:
            self._audit.record_not_applied(intent, AuditReasonCode.VALIDATION_FAILED)
            raise AdminAlreadyExistsError() from exc
        except Exception:
            self._record_unknown_outcome(intent)
            raise

        # Record successful result.
        self._audit.record_applied(intent)

        return CreatedUser(
            user_id=created.user_id,
            username=created.username,
            role=created.role,
            is_active=created.is_active,
            created_at=created.created_at,
        )
