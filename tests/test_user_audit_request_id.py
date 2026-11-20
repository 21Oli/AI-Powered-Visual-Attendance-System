"""Regression tests: user-administration audit records carry the request ID.

P5.9 integration review found a real defect. The four user-management
mutations (``user.create``, ``user.role_change``, ``user.deactivate`` and
``first_admin.provision``) built their audit context with a *fresh* random
``uuid4()`` instead of the server-generated request ID that the response
echoes in ``X-Request-ID``. Every other audited mutation (employee
create/update/deactivate, attendance checkout, login, logout) already used the
request ID, so these privileged records could not be correlated with the
request that produced them, its response or its structured logs — contrary to
the approved audit contract.

These tests pin the corrected seam at both levels:

* the route passes the request's own server-generated ID to the service
  (and a client-supplied ``X-Request-ID`` cannot override it);
* the service never fabricates a correlation value; it reuses the current
  request's ID, and reports ``None`` when there is no request (CLI).

All offline: in-memory fakes, no database, no driver, no filesystem.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from src.api_auth import (
    AUTHENTICATION_REQUIRED_CODE,
    AUTHENTICATION_REQUIRED_MESSAGE,
    AuthenticationRequired,
)
from src.api_request_context import (
    REQUEST_ID_HEADER,
    RequestIDMiddleware,
    current_request_id,
)
from src.api_user import register_user_routes
from src.authz import PERMISSION_DENIED_CODE, PermissionDenied
from src.user import Role, User
from src.user_service import UserService

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
HASH = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"


def make_user(
    user_id: str = "u-admin",
    username: str = "admin",
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


class FakeUserRepository:
    """Minimal in-memory repository covering the four mutation paths."""

    def __init__(self, users: list[User]) -> None:
        self.users = {user.user_id: user for user in users}

    def get(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    def list(self, *, after=None, limit=None) -> list[User]:
        users = sorted(self.users.values(), key=lambda u: u.user_id)
        return users if limit is None else users[:limit]

    def create(self, user: User) -> User:
        self.users[user.user_id] = user
        return user

    def _replace(self, user: User, **changes: object) -> User:
        updated = User(
            user_id=user.user_id,
            username=user.username,
            password_hash=user.password_hash,
            role=changes.get("role", user.role),  # type: ignore[arg-type]
            is_active=changes.get("is_active", user.is_active),  # type: ignore[arg-type]
            created_at=user.created_at,
            updated_at=NOW,
            username_key=user.username_key,
        )
        self.users[updated.user_id] = updated
        return updated

    def update_role(self, user_id: str, role: Role, *, updated_at):
        return self._replace(self.users[user_id], role=role)

    def update_role_with_admin_check(self, target_user_id: str, new_role: Role, *, updated_at):
        return self.update_role(target_user_id, new_role, updated_at=updated_at)

    def update_active(self, user_id: str, is_active: bool, *, updated_at):
        return self._replace(self.users[user_id], is_active=is_active)

    def update_active_with_admin_check(self, target_user_id: str, is_active: bool, *, updated_at):
        return self.update_active(target_user_id, is_active, updated_at=updated_at)

    def provision_first_admin(self, user: User, *, updated_at):
        self.users[user.user_id] = user
        return user


class FakePasswordService:
    def hash_password(self, password: str) -> str:
        return HASH


class FakeSessionService:
    """Authenticates one token per user id and revokes without side effects."""

    def __init__(self, users: dict[str, User]) -> None:
        self._users = users
        self.revoked: list[str] = []

    def validate_session(self, token: str):
        user = self._users.get(token)
        if user is None:
            raise AuthenticationRequired("not authenticated")
        return type("AuthenticatedSession", (), {"user": user, "session": object()})()

    def revoke_all_for_user(self, user_id: str) -> int:
        self.revoked.append(user_id)
        return 1


class CapturingAuditService:
    """Records the ``AuditContext`` handed to every intent write."""

    def __init__(self) -> None:
        self.contexts: list[object] = []

    def _intent(self, target_id: str, context: object):
        self.contexts.append(context)
        return type("Intent", (), {"audit_id": f"intent-{target_id}"})()

    def begin_user_create(self, *, target_id: str, context: object, metadata=None, source=None):
        return self._intent(target_id, context)

    def begin_user_role_change(self, *, target_id: str, context: object, metadata=None, source=None):
        return self._intent(target_id, context)

    def begin_user_deactivate(self, *, target_id: str, context: object, metadata=None, source=None):
        return self._intent(target_id, context)

    def begin_first_admin_provision(self, *, target_id: str, context: object, metadata=None, source=None):
        return self._intent(target_id, context)

    def record_applied(self, intent, *, metadata=None):
        return None

    def record_not_applied(self, intent, reason_code, *, metadata=None):
        return None

    def record_unknown(self, intent, *, reason_code=None, metadata=None):
        return None


def build_app(users: list[User]):
    repository = FakeUserRepository(users)
    sessions = FakeSessionService(repository.users)
    audit = CapturingAuditService()
    service = UserService(
        users=repository,
        passwords=FakePasswordService(),
        audit=audit,
        sessions=sessions,
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
            content={"error": {"code": PERMISSION_DENIED_CODE, "message": "denied"}},
            headers={REQUEST_ID_HEADER: getattr(request.state, "request_id", "")},
        )

    register_user_routes(
        app,
        path="/api/v1",
        get_user_service=lambda: service,
        get_session_service=lambda: sessions,
    )
    return app, audit


def call(app, *, method: str, path: str, token: str | None = None, body=None, headers=()):
    messages: list[dict] = []
    body_bytes = b""

    async def receive():
        nonlocal body_bytes
        if body_bytes:
            payload, body_bytes = body_bytes, b""
            return {"type": "http.request", "body": payload, "more_body": False}
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
        scope["headers"].append((b"cookie", f"va_session={token}".encode()))
    for name, value in headers:
        scope["headers"].append((name.encode(), value.encode()))
    if body is not None:
        body_bytes = json.dumps(body).encode()
        scope["headers"].append((b"content-type", b"application/json"))

    asyncio.run(app(scope, receive, send))

    start = next(m for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return {
        "status": start["status"],
        "headers": {k.decode().lower(): v.decode() for k, v in start["headers"]},
        "payload": json.loads(raw) if raw else None,
    }


def test_route_passes_the_server_generated_request_id_to_the_audit_context() -> None:
    app, audit = build_app([make_user()])

    response = call(
        app,
        method="POST",
        path="/api/v1/users",
        token="u-admin",
        body={
            "user_id": "u-new",
            "username": "new-operator",
            "password": "correct-horse-battery",
            "role": "Operator",
        },
    )

    assert response["status"] == 201
    echoed = response["headers"][REQUEST_ID_HEADER.lower()]
    assert echoed
    # The record must be able to name the request that produced it.
    assert audit.contexts[0].request_id == echoed


def test_client_supplied_request_id_cannot_displace_the_server_id() -> None:
    app, audit = build_app([make_user()])
    attacker = str(uuid.uuid4())

    response = call(
        app,
        method="POST",
        path="/api/v1/users",
        token="u-admin",
        body={
            "user_id": "u-new",
            "username": "new-operator",
            "password": "correct-horse-battery",
            "role": "Operator",
        },
        headers=[(REQUEST_ID_HEADER, attacker)],
    )

    assert response["status"] == 201
    echoed = response["headers"][REQUEST_ID_HEADER.lower()]
    assert audit.contexts[0].request_id == echoed
    assert audit.contexts[0].request_id != attacker


def test_every_user_mutation_uses_the_given_request_id() -> None:
    users = [make_user(), make_user("u-target", "target", Role.OPERATOR)]
    repository = FakeUserRepository(users)
    sessions = FakeSessionService(repository.users)
    audit = CapturingAuditService()
    service = UserService(
        users=repository,
        passwords=FakePasswordService(),
        audit=audit,
        sessions=sessions,
    )
    principal = make_user()
    first = str(uuid.uuid4())

    service.create_user(
        actor=principal,
        user_id="u-third",
        username="third-account",
        password="correct-horse-battery",
        role=Role.AUDITOR,
        request_id=first,
    )
    service.change_user_role(
        actor=principal,
        target_user_id="u-target",
        new_role=Role.HR_MANAGER,
        request_id=first,
    )
    service.deactivate_user(
        actor=principal,
        target_user_id="u-target",
        request_id=first,
    )
    service.provision_first_admin(
        user_id="u-cli",
        username="cli-admin",
        password="correct-horse-battery",
        request_id=first,
    )

    assert len(audit.contexts) == 4
    assert [context.request_id for context in audit.contexts] == [first] * 4


def test_no_request_id_is_fabricated_outside_a_request() -> None:
    """A CLI call has no request; inventing one would break correlation."""

    # No middleware has run in this test, so there is genuinely no request ID.
    # Assert the absence directly rather than comparing two helper calls, which
    # would also pass if both fabricated a value.
    assert current_request_id() is None
    assert UserService._audit_request_id(None) is None
    explicit = str(uuid.uuid4())
    assert UserService._audit_request_id(explicit) == explicit
