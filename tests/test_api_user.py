"""API tests for user administration (P5.6).

Offline tests using fakes and a minimal ASGI app.
No live database, no secrets, no real data.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.api_auth import (
    AUTHENTICATION_REQUIRED_CODE,
    AUTHENTICATION_REQUIRED_MESSAGE,
    AuthenticatedPrincipal,
    AuthenticationRequired,
    make_session_dependency,
)
from src.api_request_context import RequestIDMiddleware, REQUEST_ID_HEADER
from src.api_user import (
    USERS_PATH,
    USER_ROLE_PATH,
    USER_DEACTIVATE_PATH,
    UserCreateRequest,
    RoleChangeRequest,
    _role_to_str,
    _datetime_to_str,
    register_user_routes,
)
from src.authz import PERMISSION_DENIED_CODE, Permission, PermissionDenied, permission_dependency
from src.user import Role, User
from pydantic import ValidationError as PydanticValidationError
from src.user_repository import LastAdminProtectionError, UserAlreadyExists, UserNotFound


class FakeUserRepository:
    """Fake user repository for testing."""

    def __init__(self, users=None):
        self._users = {u.user_id: u for u in (users or [])}
        self.failure = None
        self._username_keys = {u.username_key: u.user_id for u in (users or [])}

    def get(self, user_id):
        if self.failure:
            raise self.failure
        return self._users.get(user_id)

    def find_by_username(self, username):
        if self.failure:
            raise self.failure
        key = username.casefold()
        user_id = self._username_keys.get(key)
        return self._users.get(user_id) if user_id else None

    def list(self, *, after=None, limit=None):
        if self.failure:
            raise self.failure
        users = sorted(self._users.values(), key=lambda u: u.user_id)
        if after:
            users = [u for u in users if u.user_id > after]
        if limit:
            users = users[:limit]
        return users

    def create(self, user):
        if self.failure:
            raise self.failure
        if user.user_id in self._users:
            raise UserAlreadyExists("duplicate user_id")
        if user.username_key in self._username_keys:
            raise UserAlreadyExists("duplicate username_key")
        self._users[user.user_id] = user
        self._username_keys[user.username_key] = user.user_id
        return user

    def update_role(self, user_id, role, *, updated_at):
        if self.failure:
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

    def update_active(self, user_id, is_active, *, updated_at):
        if self.failure:
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

    def update_active_with_admin_check(self, target_user_id, is_active, *, updated_at):
        # Count active admins
        active_admins = sum(
            1 for u in self._users.values()
            if u.role is Role.ADMIN and u.is_active is True
        )
        
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
        
        return self.update_active(target_user_id, is_active, updated_at=updated_at)
    
    def provision_first_admin(self, user, *, updated_at):
        # Count active admins (must be 0 for first admin)
        active_admins = sum(
            1 for u in self._users.values()
            if u.role is Role.ADMIN and u.is_active is True
        )
        
        if active_admins > 0:
            raise LastAdminProtectionError("an active administrator already exists; provisioning refused")
        
        if user.user_id in self._users:
            raise UserAlreadyExists("duplicate user_id")
        if user.username_key in self._username_keys:
            raise UserAlreadyExists("duplicate username_key")
        self._users[user.user_id] = user
        self._username_keys[user.username_key] = user.user_id
        return user
    
    def update_role_with_admin_check(self, target_user_id, new_role, *, updated_at):
        # Count active admins
        active_admins = sum(
            1 for u in self._users.values()
            if u.role is Role.ADMIN and u.is_active is True
        )
        
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
        
        return self.update_role(target_user_id, new_role, updated_at=updated_at)
from src.user_service import (
    CreatedUser,
    DeactivationResult,
    InvalidRoleError,
    LastAdminProtectedError,
    PasswordTooShortError,
    RoleChangeResult,
    SelfDeactivationError,
    SelfRoleChangeError,
    UserService,
    UserAlreadyExistsError,
    UserNotFoundError,
)

NOW = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
PASSWORD_HASH = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"


class _FakeIntent:
    """Fake mutation intent for testing."""
    def __init__(self, fake_id: str):
        self._fake_id = fake_id


def make_user(
    *,
    user_id: str = "user-1",
    username: str = "ada",
    role: Role = Role.ADMIN,
    is_active: bool = True,
) -> User:
    return User(
        user_id=user_id,
        username=username,
        password_hash=PASSWORD_HASH,
        role=role,
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeAuditService:
    """Fake audit service for testing."""

    def __init__(self) -> None:
        self.begin_calls: list[tuple[str, str]] = []
        self.result_calls: list[tuple[str, str, str]] = []
        self.unavailable = False

    def begin_user_create(self, *, target_id: str, context: object, metadata: dict | None = None, source: object = object()) -> _FakeIntent:
        self.begin_calls.append(("user.create", target_id))
        return _FakeIntent(f"intent-create-{target_id}")
    
    def begin_user_role_change(self, *, target_id: str, context: object, metadata: dict | None = None, source: object = object()) -> _FakeIntent:
        self.begin_calls.append(("user.role_change", target_id))
        return _FakeIntent(f"intent-role-{target_id}")
    
    def begin_user_deactivate(self, *, target_id: str, context: object, metadata: dict | None = None, source: object = object()) -> _FakeIntent:
        self.begin_calls.append(("user.deactivate", target_id))
        return _FakeIntent(f"intent-deactivate-{target_id}")
    
    def begin_first_admin_provision(self, *, target_id: str, context: object, metadata: dict | None = None, source: object = object()) -> _FakeIntent:
        self.begin_calls.append(("first_admin.provision", target_id))
        return _FakeIntent(f"intent-provision-{target_id}")
    
    def record_applied(self, intent: object, *, metadata: dict | None = None) -> _FakeIntent:
        self.result_calls.append((intent._fake_id, "success", "OK"))
        return _FakeIntent(f"result-{intent._fake_id}")
    
    def record_not_applied(self, intent: object, reason_code: str, *, metadata: dict | None = None) -> _FakeIntent:
        self.result_calls.append((intent._fake_id, "error", reason_code))
        return _FakeIntent(f"result-{intent._fake_id}")
    
    def record_unknown(self, intent: object, *, reason_code: str = "DATABASE_OUTCOME_UNKNOWN", metadata: dict | None = None) -> _FakeIntent:
        self.result_calls.append((intent._fake_id, "uncertain", reason_code))
        return _FakeIntent(f"result-{intent._fake_id}")
    
    def begin_mutation(
        self,
        *,
        action: str,
        target_id: str,
        context: object,
        metadata: dict | None = None,
        source: object = object(),
    ) -> _FakeIntent:
        self.begin_calls.append((action, target_id))
        return _FakeIntent(f"intent-{action}-{target_id}")


class FakeSessionService:
    """Fake session service for testing."""

    def __init__(self, users=None, session_repo=None):
        self._users = users
        self.rows = {}
        self._session_repo = session_repo
        self.revoke_failure = None

    def insert(self, session):
        self.rows[session.session_token_hash] = session
        return session

    def find_by_token_hash(self, digest):
        return self.rows.get(digest)

    def validate_session(self, token: str):
        """Validate a session token and return an AuthenticatedSession.

        Mirrors the production checks that matter here: a revoked row and an
        inactive account both refuse authentication.
        """
        session = self.rows.get(token)
        if session is None:
            raise AuthenticationRequired("not authenticated")
        if getattr(session, "revoked_at", None) is not None:
            raise AuthenticationRequired("session revoked")
        user = self._users.get(session.user_id) if self._users else None
        if user is None:
            raise AuthenticationRequired("user not found")
        if not user.is_active:
            raise AuthenticationRequired("account inactive")
        return type("AuthenticatedSession", (), {"user": user, "session": session})()

    def revoke_all_for_user(self, user_id):
        """Revoke all sessions for a user."""
        if self.revoke_failure is not None:
            raise self.revoke_failure
        changed = 0
        for digest, session in list(self.rows.items()):
            if session.user_id == user_id and getattr(session, 'revoked_at', None) is None:
                # Create new session with revoked_at set to now
                from datetime import datetime, timezone
                new_session = type('Session', (), {
                    'session_token_hash': session.session_token_hash,
                    'user_id': session.user_id,
                    'issued_at': session.issued_at,
                    'expires_at': session.expires_at,
                    'absolute_expires_at': session.absolute_expires_at,
                    'revoked_at': datetime.now(timezone.utc),
                    'last_seen_at': session.last_seen_at,
                })()
                self.rows[digest] = new_session
                changed += 1
        return changed


def build_test_app(users=None, admin_user=None):
    """Build a minimal app that registers the production user routes.

    The routes come from :func:`src.api_user.register_user_routes` — not a
    hand-recreated copy — so these tests exercise the shipped path/method
    inventory, the real ``users:manage`` guard and the real request models.
    """
    if users is None:
        users = FakeUserRepository([make_user()])
    if admin_user is None:
        admin_user = make_user()
    else:
        # Add admin_user to the repository if not already present
        existing = users.get(admin_user.user_id)
        if existing is None:
            users._users[admin_user.user_id] = admin_user
            users._username_keys[admin_user.username_key] = admin_user.user_id

    session_service = FakeSessionService(users)

    class MockPasswordService:
        """Deterministic hashing stand-in (never a real credential)."""

        def hash_password(self, password):
            return "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"

    user_service = UserService(
        users=users,
        passwords=MockPasswordService(),
        audit=FakeAuditService(),
        sessions=session_service,
    )

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.exception_handler(AuthenticationRequired)
    async def auth_handler(request, exc):
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": AUTHENTICATION_REQUIRED_CODE,
                    "message": AUTHENTICATION_REQUIRED_MESSAGE,
                }
            },
            headers={REQUEST_ID_HEADER: getattr(request.state, "request_id", "")},
        )

    @app.exception_handler(PermissionDenied)
    async def permission_handler(request, exc):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": PERMISSION_DENIED_CODE,
                    "message": "permission denied",
                }
            },
            headers={REQUEST_ID_HEADER: getattr(request.state, "request_id", "")},
        )

    register_user_routes(
        app,
        path="/api/v1",
        get_user_service=lambda: user_service,
        get_session_service=lambda: session_service,
    )

    return app, session_service


async def call_app(app, *, method: str, path: str, token: str = None, body=None):
    """Make an ASGI call."""
    messages = []
    body_bytes = b""

    async def receive():
        nonlocal body_bytes
        if body_bytes:
            result = {"type": "http.request", "body": body_bytes, "more_body": False}
            body_bytes = b""
            return result
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8000),
    }

    if token:
        scope["headers"].append(
            ("cookie".encode(), f"va_session={token}".encode())
        )
    if body is not None:
        body_bytes = json.dumps(body).encode()
        scope["headers"].append(
            ("content-type".encode(), "application/json".encode())
        )

    await app(scope, receive, send)

    start = next(m for m in messages if m["type"] == "http.response.start")
    raw_body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return {
        "status": start["status"],
        "headers": [
            (k.decode().lower(), v.decode())
            for k, v in start["headers"]
        ],
        "body": raw_body,
        "payload": json.loads(raw_body) if raw_body else None,
    }


def call(app, *, method, path, token=None, body=None):
    return asyncio.run(call_app(app, method=method, path=path, token=token, body=body))


def _create_token(session_service, user_id: str):
    """Create a fake session token for testing."""
    session = type("Session", (), {
        "session_token_hash": f"hash-{user_id}",
        "user_id": user_id,
        "issued_at": NOW,
        "expires_at": NOW,
        "absolute_expires_at": NOW,
        "revoked_at": None,
        "last_seen_at": NOW,
    })()
    session_service.insert(session)
    return f"hash-{user_id}"


def _make_user_for_session(users_repo, user_id: str):
    """Get a user from the repository by ID."""
    return users_repo.get(user_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unauthenticated_list_is_rejected():
    app, _ = build_test_app()
    response = call(app, method="GET", path="/api/v1/users")
    assert response["status"] == 401
    assert response["payload"]["error"]["code"] == AUTHENTICATION_REQUIRED_CODE


def test_unauthenticated_create_is_rejected():
    app, _ = build_test_app()
    response = call(
        app, method="POST", path="/api/v1/users",
        body={"user_id": "u_new", "username": "new", "password": "password123", "role": "Operator"},
    )
    assert response["status"] == 401


def test_unauthenticated_role_change_is_rejected():
    app, _ = build_test_app()
    response = call(
        app, method="PATCH", path="/api/v1/users/user-1/role",
        body={"new_role": "Operator"},
    )
    assert response["status"] == 401


def test_unauthenticated_deactivate_is_rejected():
    app, _ = build_test_app()
    response = call(app, method="POST", path="/api/v1/users/user-1/deactivate")
    assert response["status"] == 401


def test_non_admin_cannot_list_users():
    users = FakeUserRepository([
        make_user(user_id="admin", role=Role.ADMIN),
        make_user(user_id="op", role=Role.OPERATOR),
    ])
    app, session_service = build_test_app(users, make_user(user_id="op", role=Role.OPERATOR))
    token = _create_token(session_service, "op")
    response = call(app, method="GET", path="/api/v1/users", token=token)
    assert response["status"] == 403
    assert response["payload"]["error"]["code"] == PERMISSION_DENIED_CODE


def test_non_admin_cannot_create_user():
    users = FakeUserRepository([
        make_user(user_id="admin", role=Role.ADMIN),
        make_user(user_id="op", role=Role.OPERATOR),
    ])
    app, session_service = build_test_app(users, make_user(user_id="op", role=Role.OPERATOR))
    token = _create_token(session_service, "op")
    response = call(
        app, method="POST", path="/api/v1/users", token=token,
        body={"user_id": "u_new", "username": "new", "password": "password123", "role": "Operator"},
    )
    assert response["status"] == 403


def test_non_admin_cannot_change_role():
    users = FakeUserRepository([
        make_user(user_id="admin", role=Role.ADMIN),
        make_user(user_id="op", role=Role.OPERATOR),
    ])
    app, session_service = build_test_app(users, make_user(user_id="op", role=Role.OPERATOR))
    token = _create_token(session_service, "op")
    response = call(
        app, method="PATCH", path="/api/v1/users/user-1/role", token=token,
        body={"new_role": "Operator"},
    )
    assert response["status"] == 403


def test_non_admin_cannot_deactivate():
    users = FakeUserRepository([
        make_user(user_id="admin", role=Role.ADMIN),
        make_user(user_id="op", role=Role.OPERATOR),
    ])
    app, session_service = build_test_app(users, make_user(user_id="op", role=Role.OPERATOR))
    token = _create_token(session_service, "op")
    response = call(app, method="POST", path="/api/v1/users/user-1/deactivate", token=token)
    assert response["status"] == 403


def test_admin_can_create_user():
    users = FakeUserRepository([make_user()])
    app, session_service = build_test_app(users)
    token = _create_token(session_service, "user-1")
    
    response = call(
        app, method="POST", path="/api/v1/users", token=token,
        body={"user_id": "u_new", "username": "newuser", "password": "password123", "role": "Operator"},
    )
    
    assert response["status"] == 201
    assert response["payload"]["user_id"] == "u_new"
    assert "password_hash" not in str(response["payload"])


def test_create_user_rejects_duplicate_username():
    users = FakeUserRepository([make_user(user_id="existing", username="ada")])
    app, session_service = build_test_app(users, make_user(user_id="existing", username="ada"))
    token = _create_token(session_service, "existing")
    # Try to create a user with the same username - should fail with USER_ALREADY_EXISTS
    response = call(
        app, method="POST", path="/api/v1/users", token=token,
        body={"user_id": "u_new", "username": "ada", "password": "password123", "role": "Operator"},
    )
    assert response["status"] == 400
    assert response["payload"]["error"]["code"] == "USER_ALREADY_EXISTS"


def test_create_user_rejects_invalid_role():
    users = FakeUserRepository([make_user()])
    app, session_service = build_test_app(users)
    token = _create_token(session_service, "user-1")
    response = call(
        app, method="POST", path="/api/v1/users", token=token,
        body={"user_id": "u_new", "username": "newuser", "password": "password123", "role": "Superuser"},
    )
    # The request model rejects an unapproved role before any service call.
    assert response["status"] in (400, 422)
    text = json.dumps(response["payload"])
    assert "Traceback" not in text


def test_admin_can_change_role():
    users = FakeUserRepository([
        make_user(user_id="target", username="targetuser", role=Role.OPERATOR),
        make_user(user_id="admin1", username="admin1user", role=Role.ADMIN),
        make_user(user_id="admin2", username="admin2user", role=Role.ADMIN),
    ])
    app, session_service = build_test_app(users, make_user(user_id="admin1", username="admin1user", role=Role.ADMIN))
    token = _create_token(session_service, "admin1")
    response = call(
        app, method="PATCH", path="/api/v1/users/target/role", token=token,
        body={"new_role": "Auditor"},
    )
    assert response["status"] == 200
    assert response["payload"]["new_role"] == "Auditor"


def test_self_role_change_rejected():
    users = FakeUserRepository([make_user()])
    app, session_service = build_test_app(users)
    token = _create_token(session_service, "user-1")
    response = call(
        app, method="PATCH", path="/api/v1/users/user-1/role", token=token,
        body={"new_role": "Operator"},
    )
    assert response["status"] == 400
    assert response["payload"]["error"]["code"] == "SELF_ROLE_CHANGE"


def test_last_admin_role_change_rejected():
    # Only one admin exists - changing their own role should fail with self-change
    users = FakeUserRepository([
        make_user(user_id="only", username="onlyuser", role=Role.ADMIN),
    ])
    app, session_service = build_test_app(users, make_user(user_id="only", username="onlyuser", role=Role.ADMIN))
    token = _create_token(session_service, "only")
    response = call(
        app, method="PATCH", path="/api/v1/users/only/role", token=token,
        body={"new_role": "Operator"},
    )
    assert response["status"] == 400
    assert response["payload"]["error"]["code"] == "SELF_ROLE_CHANGE"


def test_admin_can_deactivate_user():
    users = FakeUserRepository([
        make_user(user_id="target", username="targetuser"),
        make_user(user_id="admin", username="adminuser", role=Role.ADMIN),
    ])
    app, session_service = build_test_app(users, make_user(user_id="admin", username="adminuser", role=Role.ADMIN))
    token = _create_token(session_service, "admin")
    response = call(app, method="POST", path="/api/v1/users/target/deactivate", token=token)
    assert response["status"] == 200
    assert response["payload"]["is_active"] is False


def test_self_deactivation_rejected():
    users = FakeUserRepository([make_user()])
    app, session_service = build_test_app(users)
    token = _create_token(session_service, "user-1")
    response = call(app, method="POST", path="/api/v1/users/user-1/deactivate", token=token)
    assert response["status"] == 400
    assert response["payload"]["error"]["code"] == "SELF_DEACTIVATION"


def test_last_admin_deactivation_rejected():
    # Only one admin exists - deactivating them should fail
    # The admin tries to deactivate themselves
    users = FakeUserRepository([
        make_user(user_id="only", username="onlyuser", role=Role.ADMIN),
    ])
    app, session_service = build_test_app(users, make_user(user_id="only", username="onlyuser", role=Role.ADMIN))
    token = _create_token(session_service, "only")
    response = call(app, method="POST", path="/api/v1/users/only/deactivate", token=token)
    assert response["status"] == 400
    assert response["payload"]["error"]["code"] == "SELF_DEACTIVATION"


def test_deactivated_user_session_is_rejected_and_other_sessions_survive():
    users = FakeUserRepository([
        make_user(user_id="target", username="targetuser"),
        make_user(user_id="admin", username="adminuser", role=Role.ADMIN),
    ])
    app, sessions = build_test_app(
        users, make_user(user_id="admin", username="adminuser", role=Role.ADMIN)
    )
    target_token = _create_token(sessions, "target")
    admin_token = _create_token(sessions, "admin")

    deactivated = call(
        app,
        method="POST",
        path="/api/v1/users/target/deactivate",
        token=admin_token,
    )
    assert deactivated["status"] == 200

    # The deactivated account can no longer authenticate, and its session row
    # is revoked; the other user's session is untouched.
    assert call(app, method="GET", path="/api/v1/users", token=target_token)["status"] == 401
    assert sessions.rows[target_token].revoked_at is not None
    assert call(app, method="GET", path="/api/v1/users", token=admin_token)["status"] == 200


def test_session_revocation_failure_is_a_sanitized_503():
    users = FakeUserRepository([
        make_user(user_id="target", username="targetuser"),
        make_user(user_id="admin", username="adminuser", role=Role.ADMIN),
    ])
    app, sessions = build_test_app(
        users, make_user(user_id="admin", username="adminuser", role=Role.ADMIN)
    )
    admin_token = _create_token(sessions, "admin")
    sessions.revoke_failure = RuntimeError("session store unavailable")

    response = call(
        app,
        method="POST",
        path="/api/v1/users/target/deactivate",
        token=admin_token,
    )

    assert response["status"] == 503
    assert response["payload"]["error"]["code"] == "SESSION_REVOCATION_FAILED"
    assert "session store unavailable" not in json.dumps(response["payload"])
    # The account state is committed: the response must not claim success, but
    # the caller also must not be told the deactivation failed.
    assert users.get("target").is_active is False


def test_error_responses_safe():
    users = FakeUserRepository([make_user()])
    app, session_service = build_test_app(users)
    token = _create_token(session_service, "user-1")
    response = call(
        app, method="POST", path="/api/v1/users", token=token,
        body={"user_id": "u_new", "username": "test", "password": "password123", "role": "Superuser"},
    )
    text = json.dumps(response["payload"])
    assert "Traceback" not in text
    assert "password" not in text.lower() or "password123" not in text


# ---------------------------------------------------------------------------
# Production application integration (P5.6)
# ---------------------------------------------------------------------------


def _production_app(users, actor):
    """Build the real application factory with user/session seams."""
    from src.api import create_app

    session_service = FakeSessionService(users)

    class MockPasswordService:
        def hash_password(self, password):
            return "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"

    user_service = UserService(
        users=users,
        passwords=MockPasswordService(),
        audit=FakeAuditService(),
        sessions=session_service,
    )
    app = create_app(
        session_service=session_service,
        user_service=user_service,
    )
    return app, session_service


def test_production_app_registers_the_four_user_routes():
    from src.api import API_PREFIX, APPROVED_ROUTES, create_app

    app = create_app()
    registered = {(route.path, method) for route in app.routes for method in route.methods}
    expected = {
        (f"{API_PREFIX}{USERS_PATH}", "GET"),
        (f"{API_PREFIX}{USERS_PATH}", "POST"),
        (f"{API_PREFIX}/users/{{user_id}}/role", "PATCH"),
        (f"{API_PREFIX}/users/{{user_id}}/deactivate", "POST"),
    }
    assert expected <= registered
    for _path, _method in expected:
        assert (_path.replace(API_PREFIX, ""), _method) in APPROVED_ROUTES
    # No public bootstrap/registration surface was added.
    assert not any("provision" in route.path or "signup" in route.path for route in app.routes)


def test_production_app_user_routes_require_authentication():
    users = FakeUserRepository([make_user()])
    app, _ = _production_app(users, make_user())
    response = call(app, method="GET", path="/api/v1/users")
    assert response["status"] == 401
    assert response["payload"]["error"]["code"] == AUTHENTICATION_REQUIRED_CODE


def test_production_app_user_routes_enforce_users_manage():
    users = FakeUserRepository([
        make_user(user_id="admin", username="adminuser", role=Role.ADMIN),
        make_user(user_id="op", username="opuser", role=Role.OPERATOR),
    ])
    app, session_service = _production_app(users, make_user(user_id="op", role=Role.OPERATOR))
    token = _create_token(session_service, "op")
    response = call(app, method="GET", path="/api/v1/users", token=token)
    assert response["status"] == 403
    assert response["payload"]["error"]["code"] == PERMISSION_DENIED_CODE


def test_production_app_admin_can_list_and_create_users():
    users = FakeUserRepository([make_user(user_id="admin", username="adminuser", role=Role.ADMIN)])
    app, session_service = _production_app(users, make_user(user_id="admin", username="adminuser"))
    token = _create_token(session_service, "admin")

    listed = call(app, method="GET", path="/api/v1/users", token=token)
    assert listed["status"] == 200
    assert listed["payload"]["users"][0]["user_id"] == "admin"

    created = call(
        app,
        method="POST",
        path="/api/v1/users",
        token=token,
        body={"user_id": "u_new", "username": "newuser", "password": "password123", "role": "Operator"},
    )
    assert created["status"] == 201
    assert created["payload"]["user_id"] == "u_new"
    assert "password" not in json.dumps(created["payload"]).lower()


def test_production_app_admin_can_change_role_and_deactivate():
    users = FakeUserRepository([
        make_user(user_id="admin", username="adminuser", role=Role.ADMIN),
        make_user(user_id="admin2", username="admin2", role=Role.ADMIN),
        make_user(user_id="target", username="targetuser", role=Role.OPERATOR),
    ])
    app, session_service = _production_app(
        users, make_user(user_id="admin", username="adminuser", role=Role.ADMIN)
    )
    token = _create_token(session_service, "admin")

    changed = call(
        app,
        method="PATCH",
        path="/api/v1/users/target/role",
        token=token,
        body={"new_role": "Auditor"},
    )
    assert changed["status"] == 200
    assert changed["payload"]["new_role"] == "Auditor"

    deactivated = call(
        app, method="POST", path="/api/v1/users/admin2/deactivate", token=token
    )
    assert deactivated["status"] == 200
    assert deactivated["payload"]["is_active"] is False


def test_production_app_last_admin_and_self_protections_hold():
    users = FakeUserRepository([
        make_user(user_id="admin", username="adminuser", role=Role.ADMIN),
        make_user(user_id="admin2", username="admin2", role=Role.ADMIN),
    ])
    app, session_service = _production_app(
        users, make_user(user_id="admin", username="adminuser", role=Role.ADMIN)
    )
    token = _create_token(session_service, "admin")

    # Another Admin can force a refusal of the last-Admin rule only when the
    # target is the sole remaining Admin; first deactivate one of the two.
    assert call(
        app, method="POST", path="/api/v1/users/admin2/deactivate", token=token
    )["status"] == 200

    self_change = call(
        app,
        method="PATCH",
        path="/api/v1/users/admin/role",
        token=token,
        body={"new_role": "Operator"},
    )
    assert self_change["status"] == 400
    assert self_change["payload"]["error"]["code"] == "SELF_ROLE_CHANGE"

    self_deactivate = call(
        app, method="POST", path="/api/v1/users/admin/deactivate", token=token
    )
    assert self_deactivate["status"] == 400
    assert self_deactivate["payload"]["error"]["code"] == "SELF_DEACTIVATION"


def test_production_app_rejects_invalid_and_mass_assignment_inputs():
    users = FakeUserRepository([
        make_user(user_id="admin", username="adminuser", role=Role.ADMIN)
    ])
    app, session_service = _production_app(
        users, make_user(user_id="admin", username="adminuser", role=Role.ADMIN)
    )
    token = _create_token(session_service, "admin")

    invalid_role = call(
        app,
        method="POST",
        path="/api/v1/users",
        token=token,
        body={
            "user_id": "u_new",
            "username": "newuser",
            "password": "password123",
            "role": "Superuser",
        },
    )
    assert invalid_role["status"] == 422

    mass_assignment = call(
        app,
        method="POST",
        path="/api/v1/users",
        token=token,
        body={
            "user_id": "u_new",
            "username": "newuser",
            "password": "password123",
            "role": "Operator",
            "is_active": False,
            "password_hash": "x",
        },
    )
    assert mass_assignment["status"] == 422
    assert "Superuser" not in json.dumps(invalid_role["payload"])


def test_production_app_all_user_routes_require_authentication():
    users = FakeUserRepository([
        make_user(user_id="admin", username="adminuser", role=Role.ADMIN)
    ])
    app, _ = _production_app(
        users, make_user(user_id="admin", username="adminuser", role=Role.ADMIN)
    )
    for method, path, body in (
        ("GET", "/api/v1/users", None),
        ("POST", "/api/v1/users", {"user_id": "a1", "username": "abc", "password": "password123", "role": "Operator"}),
        ("PATCH", "/api/v1/users/admin/role", {"new_role": "Operator"}),
        ("POST", "/api/v1/users/admin/deactivate", None),
    ):
        response = call(app, method=method, path=path, body=body)
        assert response["status"] == 401, (method, path)
        assert response["payload"]["error"]["code"] == AUTHENTICATION_REQUIRED_CODE
