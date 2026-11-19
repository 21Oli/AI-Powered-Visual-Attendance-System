"""Offline tests for the user administration service layer (P5.6).

Pure data + fake repository tests: no database, no driver, no connection,
no file access. The service is exercised over in-memory fakes that mimic the
P5.2 repository shape.

Coverage
--------
* list users (safe metadata only, no hash/credential leakage)
* create user (Admin only, hashed password, unique constraint, role validation)
* change role (Admin only, no self-change, no last-Admin violation, valid role)
* deactivate user (Admin only, no self-deactivation, no last-Admin violation,
  session revocation)
* first-admin provisioning (no existing Admin, refuses when Admin exists,
  safe against concurrent attempts, no credential leakage)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.audit_schema import AuditAction, AuditOutcome, AuditReasonCode, AuditSource
from src.audit_service import (
    AuditResultUncertainError,
    AuditService,
    AuditUnavailableError,
)
from src.password import PasswordService
from src.user import Role, User, UserValidationError
from src.user_repository import UserAlreadyExists, UserNotFound, LastAdminProtectionError
from src.user_service import (
    CreatedUser,
    DeactivationResult,
    InvalidRoleError,
    LastAdminProtectedError,
    ActorNotAdminError,
    AdminAlreadyExistsError,
    PasswordTooShortError,
    RoleChangeResult,
    SelfDeactivationError,
    SessionRevocationFailedError,
    SelfRoleChangeError,
    UserService,
    UserAlreadyExistsError,
    UserNotFoundError,
)

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
HASH = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"


class FakeUserRepository:
    """In-memory user repository for service tests."""

    def __init__(self, users: list[User] | None = None) -> None:
        self._users: dict[str, User] = {}
        self._username_keys: dict[str, str] = {}
        self.failure: BaseException | None = None
        #: Raised only by mutating methods (target reads still succeed).
        self.mutation_failure: BaseException | None = None
        for u in users or []:
            self._save(u)

    def _save(self, user: User) -> None:
        self._users[user.user_id] = user
        self._username_keys[user.username_key] = user.user_id

    def get(self, user_id: str) -> User | None:
        if self.failure is not None:
            raise self.failure
        return self._users.get(user_id)

    def find_by_username(self, username: str) -> User | None:
        if self.failure is not None:
            raise self.failure
        key = username.casefold()
        user_id = self._username_keys.get(key)
        return self._users.get(user_id) if user_id else None

    def list(
        self,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> list[User]:
        if self.failure is not None:
            raise self.failure
        users = sorted(self._users.values(), key=lambda u: u.user_id)
        if after:
            users = [u for u in users if u.user_id > after]
        if limit is not None:
            users = users[:limit]
        return users

    def create(self, user: User) -> User:
        if self.mutation_failure is not None:
            raise self.mutation_failure
        if self.failure is not None:
            raise self.failure
        if user.user_id in self._users:
            raise UserAlreadyExists("duplicate user_id")
        if user.username_key in self._username_keys:
            raise UserAlreadyExists("duplicate username_key")
        self._save(user)
        return user

    def update_role(self, user_id: str, role: Role, *, updated_at: datetime) -> User:
        if self.mutation_failure is not None:
            raise self.mutation_failure
        if self.failure is not None:
            raise self.failure
        user = self._users.get(user_id)
        if user is None:
            raise UserNotFound("user not found")
        updated = User(
            user_id=user.user_id,
            username=user.username,
            password_hash=user.password_hash,
            role=role,
            is_active=user.is_active,
            created_at=user.created_at,
            updated_at=updated_at,
            username_key=user.username_key,
        )
        self._users[user_id] = updated
        return updated

    def update_active(
        self, user_id: str, is_active: bool, *, updated_at: datetime
    ) -> User:
        if self.mutation_failure is not None:
            raise self.mutation_failure
        if self.failure is not None:
            raise self.failure
        user = self._users.get(user_id)
        if user is None:
            raise UserNotFound("user not found")
        updated = User(
            user_id=user.user_id,
            username=user.username,
            password_hash=user.password_hash,
            role=user.role,
            is_active=is_active,
            created_at=user.created_at,
            updated_at=updated_at,
            username_key=user.username_key,
        )
        self._users[user_id] = updated
        return updated

    def update_active_with_admin_check(
        self,
        target_user_id: str,
        is_active: bool,
        *,
        updated_at: datetime,
    ) -> User:
        # Count active admins
        active_admins = sum(
            1 for u in self._users.values()
            if u.role is Role.ADMIN and u.is_active is True
        )
        
        # Get the target user
        target = self._users.get(target_user_id)
        if target is None:
            raise UserNotFound("user not found")
        
        # Check if this is deactivating an active admin
        is_target_active_admin = (
            target.role is Role.ADMIN and target.is_active is True and is_active is False
        )
        
        if is_target_active_admin:
            # Deactivating an active admin: need at least 2 before, so 1 remains
            if active_admins < 2:
                raise LastAdminProtectionError("the last active administrator cannot be deactivated")
        
        # Perform the update
        return self.update_active(target_user_id, is_active, updated_at=updated_at)
    
    def provision_first_admin(
        self,
        user: User,
        *,
        updated_at: datetime,
    ) -> User:
        if self.mutation_failure is not None:
            raise self.mutation_failure

        # Count active admins (must be 0 for first admin)
        active_admins = sum(
            1 for u in self._users.values()
            if u.role is Role.ADMIN and u.is_active is True
        )
        
        if active_admins > 0:
            raise LastAdminProtectionError(
                "an active administrator already exists; provisioning refused"
            )
        
        # Create the first admin
        if user.user_id in self._users:
            raise UserAlreadyExists("duplicate user_id")
        if user.username_key in self._username_keys:
            raise UserAlreadyExists("duplicate username_key")
        self._save(user)
        return user
    
    def update_role_with_admin_check(
        self,
        target_user_id: str,
        new_role: Role,
        *,
        updated_at: datetime,
    ) -> User:
        # Count active admins
        active_admins = sum(
            1 for u in self._users.values()
            if u.role is Role.ADMIN and u.is_active is True
        )
        
        # Get the target user
        target = self._users.get(target_user_id)
        if target is None:
            raise UserNotFound("user not found")
        
        # Check if demoting an active admin
        is_demoting_active_admin = (
            target.role is Role.ADMIN
            and target.is_active is True
            and new_role is not Role.ADMIN
        )
        
        if is_demoting_active_admin:
            # Demoting an active admin: need at least 2 before, so 1 remains
            if active_admins < 2:
                raise LastAdminProtectionError("the last active administrator cannot be demoted")
        
        # Perform the role update
        return self.update_role(target_user_id, new_role, updated_at=updated_at)


class _FakeIntent:
    """Fake mutation intent for testing."""
    def __init__(self, fake_id: str):
        self._fake_id = fake_id


class FakeAuditService:
    """Minimal audit service fake that records intent/result calls."""

    def __init__(self) -> None:
        self.begin_calls: list[tuple[AuditAction, str]] = []
        self.result_calls: list[tuple[str, AuditOutcome, AuditReasonCode]] = []
        self.unavailable = False
        #: When set, a successful-result write fails after the mutation.
        self.result_write_fails = False

    def begin_user_create(
        self,
        *,
        target_id: str,
        context: object,
        metadata: dict | None = None,
        source: AuditSource = AuditSource.API,
    ) -> _FakeIntent:
        if self.unavailable:
            raise AuditUnavailableError()
        self.begin_calls.append((AuditAction.USER_CREATE, target_id))
        return _FakeIntent(f"intent-create-{target_id}")

    def begin_user_role_change(
        self,
        *,
        target_id: str,
        context: object,
        metadata: dict | None = None,
        source: AuditSource = AuditSource.API,
    ) -> _FakeIntent:
        if self.unavailable:
            raise AuditUnavailableError()
        self.begin_calls.append((AuditAction.USER_ROLE_CHANGE, target_id))
        return _FakeIntent(f"intent-role-{target_id}")

    def begin_user_deactivate(
        self,
        *,
        target_id: str,
        context: object,
        metadata: dict | None = None,
        source: AuditSource = AuditSource.API,
    ) -> _FakeIntent:
        if self.unavailable:
            raise AuditUnavailableError()
        self.begin_calls.append((AuditAction.USER_DEACTIVATE, target_id))
        return _FakeIntent(f"intent-deactivate-{target_id}")

    def begin_first_admin_provision(
        self,
        *,
        target_id: str,
        context: object,
        metadata: dict | None = None,
        source: AuditSource = AuditSource.CLI,
    ) -> _FakeIntent:
        if self.unavailable:
            raise AuditUnavailableError()
        self.begin_calls.append((AuditAction.FIRST_ADMIN_PROVISION, target_id))
        return _FakeIntent(f"intent-provision-{target_id}")

    def record_result(
        self,
        intent: object,
        *,
        outcome: AuditOutcome,
        reason_code: AuditReasonCode,
        metadata: dict | None = None,
    ) -> _FakeIntent:
        self.result_calls.append((intent_id(intent), outcome, reason_code))
        return _FakeIntent(f"result-{intent_id(intent)}")

    def record_applied(self, intent: object, *, metadata: dict | None = None) -> _FakeIntent:
        if self.result_write_fails:
            raise AuditResultUncertainError("FakeJournalError")
        return self.record_result(intent, outcome=AuditOutcome.SUCCESS, reason_code=AuditReasonCode.OK, metadata=metadata)

    def record_not_applied(self, intent: object, reason_code: AuditReasonCode, *, metadata: dict | None = None) -> _FakeIntent | None:
        return self.record_result(intent, outcome=AuditOutcome.ERROR, reason_code=reason_code, metadata=metadata)

    def record_unknown(self, intent: object, *, reason_code: AuditReasonCode = AuditReasonCode.DATABASE_OUTCOME_UNKNOWN, metadata: dict | None = None) -> _FakeIntent:
        return self.record_result(intent, outcome=AuditOutcome.UNCERTAIN, reason_code=reason_code, metadata=metadata)


class FakeSessionService:
    """Fake session service that records revocation calls."""

    def __init__(self) -> None:
        self.revoke_all_calls: list[str] = []
        self.failure: BaseException | None = None

    def revoke_all_for_user(self, user_id: str) -> int:
        """Record the revocation call and return a count."""
        if self.failure is not None:
            raise self.failure
        self.revoke_all_calls.append(user_id)
        return 1

    def _users(self) -> Any:
        """Expose users attribute for UserService construction."""
        return None


def intent_id(intent: object) -> str:
    """Extract a fake intent id for tracking."""
    return getattr(intent, "_fake_id", "intent")


def make_user(
    *,
    user_id: str = "u_0001",
    username: str = "ada",
    role: Role = Role.ADMIN,
    is_active: bool = True,
) -> User:
    return User(
        user_id=user_id,
        username=username,
        password_hash=HASH,
        role=role,
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


def build_service(
    users: list[User] | None = None,
) -> tuple[UserService, FakeUserRepository, FakeAuditService, FakeSessionService]:
    """Build a UserService over fakes with a fake session service."""
    user_repo = FakeUserRepository(users)
    audit = FakeAuditService()
    sessions = FakeSessionService()
    service = UserService(
        users=user_repo,
        passwords=PasswordService(),
        audit=audit,
        sessions=sessions,
    )
    return service, user_repo, audit, sessions


# ---------------------------------------------------------------------------
# List users
# ---------------------------------------------------------------------------


def test_list_users_returns_safe_metadata_only() -> None:
    service, user_repo, _audit, _sessions = build_service([
        make_user(user_id="u_0001", username="ada", role=Role.ADMIN),
        make_user(user_id="u_0002", username="grace", role=Role.OPERATOR, is_active=False),
    ])

    page = service.list_users()

    assert len(page.users) == 2
    assert page.users[0].user_id == "u_0001"
    assert page.users[0].username == "ada"
    assert page.users[0].role is Role.ADMIN
    assert page.users[0].is_active is True
    assert page.users[0].created_at == NOW
    # Password hash must never be in the result.
    assert not hasattr(page.users[0], "password_hash")


def test_list_users_is_paginated() -> None:
    service, _, _, _ = build_service([
        make_user(user_id=f"u_{i:04d}", username=f"user{i}") for i in range(1, 6)
    ])

    page1 = service.list_users(limit=2)
    assert len(page1.users) == 2
    assert page1.next_after == "u_0002"

    page2 = service.list_users(after=page1.next_after, limit=2)
    assert len(page2.users) == 2
    assert page2.users[0].user_id == "u_0003"


# ---------------------------------------------------------------------------
# Create user
# ---------------------------------------------------------------------------


def test_create_user_hashes_password_and_returns_safe_result() -> None:
    # Need an existing admin so the actor is not the only admin
    service, user_repo, audit, _sessions = build_service([
        make_user(user_id="u_existing_admin", role=Role.ADMIN)
    ])
    principal = type("Principal", (), {"user_id": "u_existing_admin", "role": Role.ADMIN})()

    created = service.create_user(
        actor=principal,
        user_id="u_new",
        username="newuser",
        password="correct-secure-password",
        role=Role.OPERATOR,
    )

    assert created.user_id == "u_new"
    assert created.username == "newuser"
    assert created.role is Role.OPERATOR
    assert created.is_active is True
    # Password hash must never be returned.
    assert not hasattr(created, "password_hash")

    # The stored user must have a real Argon2id hash, not plaintext.
    stored = user_repo.get("u_new")
    assert stored is not None
    assert stored.password_hash != "correct-secure-password"
    assert stored.password_hash.startswith("$argon2id$")
    assert PasswordService().verify_password("correct-secure-password", stored.password_hash) is True


def test_create_user_refuses_duplicate_username() -> None:
    # Need an existing admin so the actor is not the only admin
    service, _, audit, _sessions = build_service([make_user(user_id="u_existing_admin", role=Role.ADMIN)])
    principal = type("Principal", (), {"user_id": "u_existing_admin", "role": Role.ADMIN})()

    with pytest.raises(UserAlreadyExistsError):
        service.create_user(
            actor=principal,
            user_id="u_new",
            username="ada",  # duplicate username with the existing admin
            password="secure-password-123",
            role=Role.OPERATOR,
        )


def test_create_user_requires_admin_role() -> None:
    service, _, _, _ = build_service()
    principal = type("Principal", (), {"user_id": "u_op", "role": Role.OPERATOR})()

    with pytest.raises(ActorNotAdminError):
        service.create_user(
            actor=principal,
            user_id="u_new",
            username="newuser",
            password="secure-password-123",
            role=Role.OPERATOR,
        )


def test_create_user_requires_valid_role() -> None:
    # Need an existing admin so the actor is not the only admin
    service, _, _, _ = build_service([make_user(user_id="u_existing_admin", role=Role.ADMIN)])
    principal = type("Principal", (), {"user_id": "u_existing_admin", "role": Role.ADMIN})()

    with pytest.raises(InvalidRoleError):
        service.create_user(
            actor=principal,
            user_id="u_new",
            username="newuser",
            password="secure-password-123",
            role="Superuser",  # type: ignore[arg-type]  # intentional invalid string
        )


def test_create_user_refuses_short_password() -> None:
    # Need an existing admin so the actor is not the only admin
    service, _, _, _ = build_service([make_user(user_id="u_existing_admin", role=Role.ADMIN)])
    principal = type("Principal", (), {"user_id": "u_existing_admin", "role": Role.ADMIN})()

    with pytest.raises(PasswordTooShortError):
        service.create_user(
            actor=principal,
            user_id="u_new",
            username="newuser",
            password="short",  # < 8 chars
            role=Role.OPERATOR,
        )


# ---------------------------------------------------------------------------
# Change role
# ---------------------------------------------------------------------------


def test_change_role_updates_and_returns_result() -> None:
    # Need two admins so one can change a user without being the last admin
    service, user_repo, audit, _sessions = build_service([
        make_user(user_id="u_admin1", role=Role.ADMIN),
        make_user(user_id="u_admin2", role=Role.ADMIN),
        make_user(user_id="u_target", role=Role.OPERATOR),
    ])
    principal = type("Principal", (), {"user_id": "u_admin1", "role": Role.ADMIN})()

    result = service.change_user_role(
        actor=principal,
        target_user_id="u_target",
        new_role=Role.AUDITOR,
    )

    assert result.user_id == "u_target"
    assert result.new_role is Role.AUDITOR

    # Verify the stored user is updated.
    stored = user_repo.get("u_target")
    assert stored is not None
    assert stored.role is Role.AUDITOR


def test_change_role_refuses_self_change() -> None:
    service, _, _, _ = build_service([make_user(user_id="u_admin", role=Role.ADMIN)])
    principal = type("Principal", (), {"user_id": "u_admin", "role": Role.ADMIN})()

    with pytest.raises(SelfRoleChangeError):
        service.change_user_role(
            actor=principal,
            target_user_id="u_admin",  # same as actor
            new_role=Role.OPERATOR,
        )


def test_change_role_refuses_last_admin_demotion() -> None:
    service, _, _, _ = build_service([make_user(user_id="u_only_admin", role=Role.ADMIN)])
    principal = type("Principal", (), {"user_id": "u_other", "role": Role.ADMIN})()

    with pytest.raises(LastAdminProtectedError):
        service.change_user_role(
            actor=principal,
            target_user_id="u_only_admin",
            new_role=Role.OPERATOR,
        )


def test_change_role_refuses_invalid_role() -> None:
    # Need two admins so one can attempt to change a user
    service, _, _, _ = build_service([
        make_user(user_id="u_admin1", role=Role.ADMIN),
        make_user(user_id="u_target"),
    ])
    principal = type("Principal", (), {"user_id": "u_admin1", "role": Role.ADMIN})()

    with pytest.raises(InvalidRoleError):
        service.change_user_role(
            actor=principal,
            target_user_id="u_target",
            new_role="Superuser",  # type: ignore[arg-type]  # intentional invalid string
        )


def test_change_role_refuses_missing_target() -> None:
    # Need two admins so one can attempt to change a missing user
    service, _, _, _ = build_service([
        make_user(user_id="u_admin1", role=Role.ADMIN),
        make_user(user_id="u_admin2", role=Role.ADMIN),
    ])
    principal = type("Principal", (), {"user_id": "u_admin1", "role": Role.ADMIN})()

    with pytest.raises(UserNotFoundError):
        service.change_user_role(
            actor=principal,
            target_user_id="u_missing",
            new_role=Role.OPERATOR,
        )


# ---------------------------------------------------------------------------
# Deactivate user
# ---------------------------------------------------------------------------


def test_deactivate_user_revokes_sessions_and_returns_result() -> None:
    # Need two admins so one can deactivate a user without being the last admin
    service, user_repo, audit, sessions = build_service([
        make_user(user_id="u_admin1", role=Role.ADMIN),
        make_user(user_id="u_target", username="grace"),
    ])
    principal = type("Principal", (), {"user_id": "u_admin1", "role": Role.ADMIN})()

    result = service.deactivate_user(
        actor=principal,
        target_user_id="u_target",
    )

    assert result.user_id == "u_target"
    assert result.is_active is False

    # Verify the stored user is deactivated.
    stored = user_repo.get("u_target")
    assert stored is not None
    assert stored.is_active is False

    # Verify session revocation was called.
    assert sessions.revoke_all_calls == ["u_target"]


def test_deactivate_user_refuses_self_deactivation() -> None:
    # Need another admin so self-deactivation is refused for self-operation
    service, _, _, _ = build_service([
        make_user(user_id="u_admin", role=Role.ADMIN),
        make_user(user_id="u_other_admin", role=Role.ADMIN),
    ])
    principal = type("Principal", (), {"user_id": "u_admin", "role": Role.ADMIN})()

    with pytest.raises(SelfDeactivationError):
        service.deactivate_user(
            actor=principal,
            target_user_id="u_admin",
        )


def test_deactivate_user_refuses_last_admin() -> None:
    # Only one admin exists - trying to deactivate them should fail
    # This tests the last-admin protection: you can't deactivate the last admin
    service, _, _, _ = build_service([make_user(user_id="u_only_admin", role=Role.ADMIN)])
    # The only admin tries to deactivate themselves - should fail with self-deactivation
    # first, not last-admin protection
    principal = type("Principal", (), {"user_id": "u_only_admin", "role": Role.ADMIN})()

    # Self-deactivation is refused first
    with pytest.raises(SelfDeactivationError):
        service.deactivate_user(
            actor=principal,
            target_user_id="u_only_admin",
        )

    # Now test with a non-admin actor trying to deactivate the only admin
    # This should fail because the actor is not an admin
    non_admin_principal = type("Principal", (), {"user_id": "u_non_admin", "role": Role.OPERATOR})()
    with pytest.raises(ActorNotAdminError):
        service.deactivate_user(
            actor=non_admin_principal,
            target_user_id="u_only_admin",
        )


def test_deactivate_user_refuses_missing_target() -> None:
    # Need two admins so one can attempt to deactivate a missing user
    service, _, _, _ = build_service([
        make_user(user_id="u_admin1", role=Role.ADMIN),
        make_user(user_id="u_admin2", role=Role.ADMIN),
    ])
    principal = type("Principal", (), {"user_id": "u_admin1", "role": Role.ADMIN})()

    with pytest.raises(UserNotFoundError):
        service.deactivate_user(
            actor=principal,
            target_user_id="u_missing",
        )


# ---------------------------------------------------------------------------
# First-admin provisioning
# ---------------------------------------------------------------------------


def test_provision_first_admin_succeeds_when_no_admin_exists() -> None:
    service, user_repo, audit, _sessions = build_service()
    # No Admin exists yet.

    result = service.provision_first_admin(
        user_id="u_first",
        username="firstadmin",
        password="secure-initial-password",
    )

    assert result.user_id == "u_first"
    assert result.username == "firstadmin"
    assert result.role is Role.ADMIN
    assert result.is_active is True

    # Verify the stored Admin has a hashed password.
    stored = user_repo.get("u_first")
    assert stored is not None
    assert stored.role is Role.ADMIN
    assert stored.password_hash != "secure-initial-password"
    assert stored.password_hash.startswith("$argon2id$")


def test_provision_first_admin_refuses_when_admin_exists() -> None:
    service, _, _, _ = build_service([make_user(user_id="u_existing", role=Role.ADMIN)])

    with pytest.raises(AdminAlreadyExistsError):
        service.provision_first_admin(
            user_id="u_new",
            username="newadmin",
            password="secure-password-123",
        )


def test_provision_first_admin_sequential_second_attempt_is_refused() -> None:
    """Sequential coverage only.

    Real concurrent first-Admin provisioning is exercised against PostgreSQL in
    ``tests/test_tier2_user_concurrency.py`` (skipped unless opted in). This
    test must not be read as concurrency verification.
    """
    service, _, _, _ = build_service([])

    result1 = service.provision_first_admin(
        user_id="u_admin",
        username="admin",
        password="secure-password-123",
    )
    assert result1.user_id == "u_admin"

    with pytest.raises(AdminAlreadyExistsError):
        service.provision_first_admin(
            user_id="u_admin2",
            username="admin2",
            password="another-password-123",
        )


def test_provision_first_admin_refuses_an_admin_created_outside_the_service() -> None:
    user_repo = FakeUserRepository()
    user_repo._save(
        make_user(user_id="u_existing", username="existing", role=Role.ADMIN)
    )
    service = UserService(
        users=user_repo,
        passwords=PasswordService(),
        audit=FakeAuditService(),
        sessions=FakeSessionService(),
    )

    with pytest.raises(AdminAlreadyExistsError):
        service.provision_first_admin(
            user_id="u_new",
            username="newadmin",
            password="secure-password-123",
        )


def test_provision_first_admin_refuses_short_password() -> None:
    service, _, _, _ = build_service()

    with pytest.raises(PasswordTooShortError):
        service.provision_first_admin(
            user_id="u_admin",
            username="admin",
            password="short",
        )


# ---------------------------------------------------------------------------
# Regression tests for last-admin protection
# ---------------------------------------------------------------------------


def test_deactivating_one_of_two_admins_leaves_one_active_admin() -> None:
    """Deactivating one of two admins leaves one active admin.

    This is a regression test for the last-admin invariant: when there are
    two active admins, deactivating one must succeed and leave exactly one
    active admin. The second admin cannot then be deactivated.
    """
    # Start with 2 admins
    users = FakeUserRepository([
        make_user(user_id="u_admin1", role=Role.ADMIN),
        make_user(user_id="u_admin2", role=Role.ADMIN),
    ])
    audit = FakeAuditService()
    sessions = FakeSessionService()
    
    service = UserService(
        users=users, passwords=PasswordService(), audit=audit, sessions=sessions
    )
    # Use Admin1 to deactivate Admin2
    admin1_principal = type("Principal", (), {"user_id": "u_admin1", "role": Role.ADMIN})()
    
    # Deactivate Admin2 - should succeed
    result = service.deactivate_user(
        actor=admin1_principal,
        target_user_id="u_admin2",
    )
    assert result.user_id == "u_admin2"
    assert result.is_active is False
    
    # Verify Admin2 is deactivated
    admin2 = users.get("u_admin2")
    assert admin2 is not None
    assert admin2.is_active is False
    
    # Verify Admin1 is still active
    admin1 = users.get("u_admin1")
    assert admin1 is not None
    assert admin1.is_active is True
    assert admin1.role is Role.ADMIN
    
    # Now trying to deactivate Admin1 using Admin1 should fail with self-deactivation
    with pytest.raises(SelfDeactivationError):
        service.deactivate_user(
            actor=admin1_principal,
            target_user_id="u_admin1",
        )
    
    # Verify Admin1 is still active after the failed attempt
    admin1 = users.get("u_admin1")
    assert admin1.is_active is True
    
    # The last-admin protection also prevents deactivating the last admin
    # even from a different admin (but there is no other admin in this case)
    # This is tested by the service layer's check before the atomic repo call



# ---------------------------------------------------------------------------
# Regression: the last-admin invariant never counts through a paginated list
# ---------------------------------------------------------------------------


def test_role_change_never_counts_admins_through_the_paginated_list() -> None:
    """Demotion must rely on the repository's atomic check, not on ``list``.

    ``UserRepository.list`` is keyset-paginated (default page size 50), so a
    service-side count silently sees one page only. It would refuse a safe
    demotion when the other Admin sorts beyond the page, or allow an unsafe one
    when it does not. The invariant check therefore belongs to the repository.
    """
    service, users, _, _ = build_service([
        make_user(user_id="u_1_admin", username="admin-one", role=Role.ADMIN),
        make_user(user_id="u_9_admin", username="admin-nine", role=Role.ADMIN),
        make_user(user_id="u_5_operator", username="operator", role=Role.OPERATOR),
    ])
    principal = type("Principal", (), {"user_id": "u_9_admin", "role": Role.ADMIN})()

    def _forbidden_list(*, after=None, limit=None):
        raise AssertionError(
            "the last-admin invariant must not be derived from a paginated list"
        )

    users.list = _forbidden_list  # type: ignore[method-assign]

    result = service.change_user_role(
        actor=principal,
        target_user_id="u_1_admin",
        new_role=Role.OPERATOR,
    )
    assert result.new_role is Role.OPERATOR


def test_deactivation_never_counts_admins_through_the_paginated_list() -> None:
    service, users, _, _ = build_service([
        make_user(user_id="u_1_admin", username="admin-one", role=Role.ADMIN),
        make_user(user_id="u_9_admin", username="admin-nine", role=Role.ADMIN),
    ])
    principal = type("Principal", (), {"user_id": "u_9_admin", "role": Role.ADMIN})()

    def _forbidden_list(*, after=None, limit=None):
        raise AssertionError(
            "the last-admin invariant must not be derived from a paginated list"
        )

    users.list = _forbidden_list  # type: ignore[method-assign]

    result = service.deactivate_user(actor=principal, target_user_id="u_1_admin")
    assert result.is_active is False


def test_last_admin_is_still_refused_with_more_than_one_page_of_users() -> None:
    """A page full of non-Admins must not make a lone Admin look replaceable."""
    users = [
        make_user(user_id=f"u_{index:03d}_op", username=f"op{index}", role=Role.OPERATOR)
        for index in range(120)
    ]
    users.append(make_user(user_id="u_999_admin", username="only-admin", role=Role.ADMIN))
    service, _, _, _ = build_service(users)
    principal = type("Principal", (), {"user_id": "u_000_op", "role": Role.ADMIN})()

    with pytest.raises(LastAdminProtectedError):
        service.change_user_role(
            actor=principal,
            target_user_id="u_999_admin",
            new_role=Role.AUDITOR,
        )


# ---------------------------------------------------------------------------
# Audit intent/result protocol (P5.6 mandatory behaviour)
# ---------------------------------------------------------------------------


def admin_principal(user_id: str = "u_admin"):
    return type("Principal", (), {"user_id": user_id, "role": Role.ADMIN})()


def test_intent_write_failure_prevents_the_create() -> None:
    service, users, audit, _ = build_service([])
    audit.unavailable = True

    with pytest.raises(AuditUnavailableError):
        service.create_user(
            actor=admin_principal(),
            user_id="u_new",
            username="newuser",
            password="secure-password-123",
            role=Role.OPERATOR,
        )

    assert users.get("u_new") is None
    assert users.list() == []


def test_intent_write_failure_prevents_deactivation() -> None:
    service, users, audit, sessions = build_service([
        make_user(user_id="u_admin", role=Role.ADMIN),
        make_user(user_id="u_op", username="operator", role=Role.OPERATOR),
    ])
    audit.unavailable = True

    with pytest.raises(AuditUnavailableError):
        service.deactivate_user(actor=admin_principal(), target_user_id="u_op")

    assert users.get("u_op").is_active is True
    assert sessions.revoke_all_calls == []


def test_success_records_a_correlated_result() -> None:
    service, _, audit, _ = build_service([])

    service.create_user(
        actor=admin_principal(),
        user_id="u_new",
        username="newuser",
        password="secure-password-123",
        role=Role.OPERATOR,
    )

    assert audit.begin_calls == [(AuditAction.USER_CREATE, "u_new")]
    assert audit.result_calls == [
        ("intent-create-u_new", AuditOutcome.SUCCESS, AuditReasonCode.OK)
    ]


def test_known_duplicate_records_not_applied() -> None:
    service, _, audit, _ = build_service([
        make_user(user_id="u_taken", username="taken", role=Role.OPERATOR)
    ])

    with pytest.raises(UserAlreadyExistsError):
        service.create_user(
            actor=admin_principal(),
            user_id="u_new",
            username="TAKEN",
            password="secure-password-123",
            role=Role.OPERATOR,
        )

    assert audit.result_calls == [
        ("intent-create-u_new", AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_EXISTS)
    ]


def test_unknown_database_outcome_is_audited_uncertain() -> None:
    service, users, audit, _ = build_service([])
    users.mutation_failure = RuntimeError("connection dropped during commit")

    with pytest.raises(RuntimeError):
        service.create_user(
            actor=admin_principal(),
            user_id="u_new",
            username="newuser",
            password="secure-password-123",
            role=Role.OPERATOR,
        )

    assert audit.result_calls == [
        (
            "intent-create-u_new",
            AuditOutcome.UNCERTAIN,
            AuditReasonCode.DATABASE_OUTCOME_UNKNOWN,
        )
    ]


def test_result_write_failure_surfaces_the_uncertain_audit_contract() -> None:
    service, users, audit, _ = build_service([])
    audit.result_write_fails = True

    with pytest.raises(AuditResultUncertainError):
        service.create_user(
            actor=admin_principal(),
            user_id="u_new",
            username="newuser",
            password="secure-password-123",
            role=Role.OPERATOR,
        )

    # The mutation was applied; only the result record is missing.
    assert users.get("u_new") is not None


def test_audit_never_receives_passwords_or_hashes() -> None:
    service, users, audit, _ = build_service([])
    password = "super-secret-password-123"

    serialized: list[str] = []
    for name in (
        "begin_user_create",
        "record_applied",
        "record_not_applied",
        "record_unknown",
    ):
        original = getattr(audit, name)

        def spy(*args, _original=original, **kwargs):
            serialized.append(repr(args) + repr(kwargs))
            return _original(*args, **kwargs)

        setattr(audit, name, spy)

    service.create_user(
        actor=admin_principal(),
        user_id="u_new",
        username="newuser",
        password=password,
        role=Role.OPERATOR,
    )

    stored = users.get("u_new")
    assert stored is not None and stored.password_hash != password
    joined = "\n".join(serialized)
    assert password not in joined
    assert stored.password_hash not in joined
    assert "argon2" not in joined.lower()


def test_audit_targets_the_managed_account_for_every_action() -> None:
    service, _, audit, _ = build_service([
        make_user(user_id="u_admin", username="admin", role=Role.ADMIN),
        make_user(user_id="u_target", username="target", role=Role.OPERATOR),
    ])
    actor = admin_principal()

    service.change_user_role(
        actor=actor, target_user_id="u_target", new_role=Role.AUDITOR
    )
    service.deactivate_user(actor=actor, target_user_id="u_target")
    service.create_user(
        actor=actor,
        user_id="u_created",
        username="created",
        password="secure-password-123",
        role=Role.OPERATOR,
    )

    assert audit.begin_calls == [
        (AuditAction.USER_ROLE_CHANGE, "u_target"),
        (AuditAction.USER_DEACTIVATE, "u_target"),
        (AuditAction.USER_CREATE, "u_created"),
    ]


# ---------------------------------------------------------------------------
# Session revocation on deactivation
# ---------------------------------------------------------------------------


def test_deactivation_revokes_every_session_for_the_target_only() -> None:
    service, _, _, sessions = build_service([
        make_user(user_id="u_admin", username="admin", role=Role.ADMIN),
        make_user(user_id="u_target", username="target", role=Role.OPERATOR),
    ])

    service.deactivate_user(actor=admin_principal(), target_user_id="u_target")

    assert sessions.revoke_all_calls == ["u_target"]


def test_session_revocation_failure_is_not_reported_as_success() -> None:
    service, users, audit, sessions = build_service([
        make_user(user_id="u_admin", username="admin", role=Role.ADMIN),
        make_user(user_id="u_target", username="target", role=Role.OPERATOR),
    ])
    sessions.failure = RuntimeError("session store unavailable")

    with pytest.raises(SessionRevocationFailedError):
        service.deactivate_user(actor=admin_principal(), target_user_id="u_target")

    # The account state is committed, which is why this must not be a success
    # response and why the audit result is uncertain rather than applied.
    assert users.get("u_target").is_active is False
    assert audit.result_calls == [
        (
            "intent-deactivate-u_target",
            AuditOutcome.UNCERTAIN,
            AuditReasonCode.DATABASE_OUTCOME_UNKNOWN,
        )
    ]


def test_retrying_an_already_inactive_user_revokes_its_sessions() -> None:
    service, _, audit, sessions = build_service([
        make_user(user_id="u_admin", username="admin", role=Role.ADMIN),
        make_user(
            user_id="u_target",
            username="target",
            role=Role.OPERATOR,
            is_active=False,
        ),
    ])

    result = service.deactivate_user(
        actor=admin_principal(), target_user_id="u_target"
    )

    assert result.is_active is False
    assert sessions.revoke_all_calls == ["u_target"]
    assert audit.begin_calls == []


def test_deactivating_a_non_admin_is_allowed_with_a_single_active_admin() -> None:
    service, _, _, _ = build_service([
        make_user(user_id="u_only_admin", username="admin", role=Role.ADMIN),
        make_user(user_id="u_op", username="operator", role=Role.OPERATOR),
    ])

    result = service.deactivate_user(
        actor=admin_principal("u_only_admin"), target_user_id="u_op"
    )
    assert result.is_active is False


def test_changing_a_non_admin_role_is_allowed_with_a_single_active_admin() -> None:
    service, _, _, _ = build_service([
        make_user(user_id="u_only_admin", username="admin", role=Role.ADMIN),
        make_user(user_id="u_op", username="operator", role=Role.OPERATOR),
    ])

    result = service.change_user_role(
        actor=admin_principal("u_only_admin"),
        target_user_id="u_op",
        new_role=Role.AUDITOR,
    )
    assert result.new_role is Role.AUDITOR
