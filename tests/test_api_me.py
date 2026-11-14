"""P5.4.7 current-user (`/me`) API tests (offline, pure ASGI, no database).

The endpoint is driven through the real ``create_app`` composition (request-ID
middleware, exception handlers, registered authentication dependency) with a
real ``SessionService`` over in-memory P5.2 repositories, so expiry, revocation,
inactivity and storage-failure behaviour is the real service's.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.api import APPROVED_ROUTES, API_PREFIX, create_app
from src.api_auth import AUTHENTICATION_REQUIRED_CODE, AUTHENTICATION_REQUIRED_MESSAGE
from src.api_request_context import REQUEST_ID_HEADER
from src.config import SESSION_COOKIE, AppSettings
from src.db import DatabaseUnavailableError
from src.session_service import SessionService
from src.session_token import generate_session_token, session_token_digest
from src.user import Role, User

NOW = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
ME_PATH = "/api/v1/auth/me"
TEST_ORIGIN = "https://attendance.test.invalid"
_PASSWORD_HASH = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"
_APPROVED_FIELDS = ["is_active", "role", "user_id", "username"]


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class FakeUserRepository:
    def __init__(self, users: list[User]) -> None:
        self.users = {user.user_id: user for user in users}
        self.failure: BaseException | None = None

    def get(self, user_id: str) -> User | None:
        if self.failure is not None:
            raise self.failure
        return self.users.get(user_id)


class FakeSessionRepository:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}
        self.find_failure: BaseException | None = None

    def insert(self, session):
        self.rows[session.session_token_hash] = session
        return session

    def find_by_token_hash(self, digest: str):
        if self.find_failure is not None:
            raise self.find_failure
        return self.rows.get(digest)

    def touch(self, digest: str, *, last_seen_at: datetime, expires_at: datetime):
        stored = self.rows[digest]
        updated = replace(stored, last_seen_at=last_seen_at, expires_at=expires_at)
        self.rows[digest] = updated
        return updated

    def revoke(self, digest: str, *, revoked_at: datetime):
        stored = self.rows[digest]
        updated = replace(stored, revoked_at=stored.revoked_at or revoked_at)
        self.rows[digest] = updated
        return updated

    def revoke_all_for_user(self, user_id: str, *, revoked_at: datetime) -> int:
        return 0


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
        password_hash=_PASSWORD_HASH,
        role=role,
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


def build(
    *,
    users: list[User] | None = None,
    clock: MutableClock | None = None,
) -> tuple[SessionService, FakeUserRepository, FakeSessionRepository, MutableClock]:
    user_repository = FakeUserRepository(users or [make_user()])
    session_repository = FakeSessionRepository()
    test_clock = clock or MutableClock()
    service = SessionService(
        users=user_repository,  # type: ignore[arg-type]
        sessions=session_repository,  # type: ignore[arg-type]
        clock=test_clock,
    )
    return service, user_repository, session_repository, test_clock


def _app(service: SessionService):
    return create_app(
        settings=AppSettings(
            app_env="testing", login_trusted_origins=(TEST_ORIGIN,)
        ),
        session_service=service,
    )


# ---------------------------------------------------------------------------
# ASGI driver
# ---------------------------------------------------------------------------


async def _call(
    app,
    *,
    headers: list[tuple[str, str]],
    method: str = "GET",
    path: str = ME_PATH,
    query_string: bytes = b"",
    body: bytes = b"",
    swallow: bool = False,
) -> dict:
    messages: list[dict] = []
    sent = {"done": False}

    async def receive() -> dict:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    base_path, _, inline_query = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": base_path,
        "raw_path": base_path.encode("ascii"),
        "query_string": query_string or inline_query.encode("ascii"),
        "root_path": "",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in headers
        ],
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8000),
    }
    try:
        await app(scope, receive, send)
    except BaseException:  # noqa: BLE001 - Starlette re-raises after rendering
        if not swallow:
            raise
    start = next(m for m in messages if m["type"] == "http.response.start")
    raw_body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return {
        "status": start["status"],
        "headers": [
            (key.decode("latin-1").lower(), value.decode("latin-1"))
            for key, value in start["headers"]
        ],
        "body": raw_body,
    }


def me(
    app,
    *,
    token: str | None = None,
    headers: list[tuple[str, str]] | None = None,
    method: str = "GET",
    path: str = ME_PATH,
    query_string: bytes = b"",
    swallow: bool = False,
) -> dict:
    resolved: list[tuple[str, str]] = []
    if token is not None:
        resolved.append(("cookie", f"{SESSION_COOKIE.name}={token}"))
    resolved.extend(headers or [])
    response = asyncio.run(
        _call(
            app,
            headers=resolved,
            method=method,
            path=path,
            query_string=query_string,
            swallow=swallow,
        )
    )
    response["payload"] = (
        json.loads(response["body"]) if response["body"] else None
    )
    return response


def _header_values(response: dict, name: str) -> list[str]:
    return [value for key, value in response["headers"] if key == name]


def _request_id(response: dict) -> str | None:
    values = _header_values(response, REQUEST_ID_HEADER.lower())
    return values[0] if values else None


def _assert_refused(response: dict) -> None:
    assert response["status"] == 401
    assert response["payload"] == {
        "error": {
            "code": AUTHENTICATION_REQUIRED_CODE,
            "message": AUTHENTICATION_REQUIRED_MESSAGE,
        }
    }
    assert _header_values(response, "set-cookie") == []
    assert _request_id(response)


# ---------------------------------------------------------------------------
# A. Successful authentication
# ---------------------------------------------------------------------------


def test_valid_session_returns_the_authenticated_profile() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    response = me(_app(service), token=issued.token)

    assert response["status"] == 200
    assert response["payload"] == {
        "user_id": "user-1",
        "username": "ada",
        "role": "Admin",
        "is_active": True,
    }


def test_response_contains_exactly_the_four_approved_fields() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    payload = me(_app(service), token=issued.token)["payload"]

    assert sorted(payload) == _APPROVED_FIELDS


@pytest.mark.parametrize("role", list(Role))
def test_roles_use_the_project_serialization(role: Role) -> None:
    service, _, _, _ = build(users=[make_user(role=role)])
    issued = service.issue_session("user-1")
    payload = me(_app(service), token=issued.token)["payload"]
    assert payload["role"] == role.value


def test_inactive_flag_is_reported_for_an_active_account() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    assert me(_app(service), token=issued.token)["payload"]["is_active"] is True


def test_success_carries_a_request_id_and_a_no_store_cache_policy() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    response = me(_app(service), token=issued.token)

    assert _request_id(response)
    cache_control = " ".join(_header_values(response, "cache-control")).lower()
    assert "no-store" in cache_control
    assert _header_values(response, "content-type")[0].startswith(
        "application/json"
    )


def test_success_never_sets_or_clears_a_cookie() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    response = me(_app(service), token=issued.token)
    assert _header_values(response, "set-cookie") == []


def test_identity_comes_from_the_session_not_the_request() -> None:
    service, _, _, _ = build(
        users=[
            make_user(user_id="user-1", username="ada", role=Role.OPERATOR),
            make_user(user_id="user-2", username="grace", role=Role.ADMIN),
        ]
    )
    issued = service.issue_session("user-1")
    response = me(
        _app(service),
        token=issued.token,
        headers=[
            ("x-user-id", "user-2"),
            ("x-role", "Admin"),
            ("x-username", "grace"),
        ],
        path=f"{ME_PATH}?user_id=user-2&role=Admin&username=grace",
    )

    assert response["status"] == 200
    assert response["payload"] == {
        "user_id": "user-1",
        "username": "ada",
        "role": "Operator",
        "is_active": True,
    }
    assert "grace" not in response["body"].decode("utf-8")


# ---------------------------------------------------------------------------
# B. Authentication failures share one public contract
# ---------------------------------------------------------------------------


def test_missing_cookie_is_a_sanitized_401() -> None:
    service, _, _, _ = build()
    service.issue_session("user-1")
    _assert_refused(me(_app(service)))


def test_empty_cookie_is_a_sanitized_401() -> None:
    service, _, _, _ = build()
    service.issue_session("user-1")
    _assert_refused(me(_app(service), token=""))


def test_malformed_token_is_a_sanitized_401() -> None:
    service, _, _, _ = build()
    _assert_refused(me(_app(service), token="not-a-session-token"))


def test_unknown_token_is_a_sanitized_401() -> None:
    service, _, _, _ = build()
    service.issue_session("user-1")
    _assert_refused(me(_app(service), token=generate_session_token()))


def test_revoked_session_is_a_sanitized_401() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    service.revoke_session(issued.token)
    _assert_refused(me(_app(service), token=issued.token))


def test_idle_expired_session_is_a_sanitized_401() -> None:
    service, _, _, clock = build()
    issued = service.issue_session("user-1")
    clock.advance(timedelta(hours=8, seconds=1))
    _assert_refused(me(_app(service), token=issued.token))


def test_absolute_expired_session_is_a_sanitized_401() -> None:
    service, _, _, clock = build()
    issued = service.issue_session("user-1")
    clock.advance(timedelta(hours=13))
    _assert_refused(me(_app(service), token=issued.token))


def test_inactive_account_is_a_sanitized_401() -> None:
    service, users, _, _ = build()
    issued = service.issue_session("user-1")
    users.users["user-1"] = replace(users.users["user-1"], is_active=False)
    _assert_refused(me(_app(service), token=issued.token))


def test_vanished_account_is_a_sanitized_401() -> None:
    service, users, _, _ = build()
    issued = service.issue_session("user-1")
    users.users.pop("user-1")
    _assert_refused(me(_app(service), token=issued.token))


def test_every_authentication_failure_is_byte_identical() -> None:
    service, users, _, clock = build()
    issued = service.issue_session("user-1")
    revoked = service.issue_session("user-1")
    service.revoke_session(revoked.token)
    app = _app(service)

    baseline = me(app)
    assert baseline["status"] == 401
    cases = [
        me(app),
        me(app, token=""),
        me(app, token="malformed-token-value"),
        me(app, token=generate_session_token()),
        me(app, token=revoked.token),
    ]
    for response in cases:
        assert response["status"] == 401
        assert response["body"] == baseline["body"]

    # An expired session produces the same envelope too.
    clock.advance(timedelta(hours=13))
    expired = me(app, token=issued.token)
    assert expired["status"] == 401
    assert expired["body"] == baseline["body"]


def test_inactive_account_refusal_matches_the_generic_contract() -> None:
    service, users, _, _ = build()
    issued = service.issue_session("user-1")
    users.users["user-1"] = replace(users.users["user-1"], is_active=False)
    inactive = me(_app(service), token=issued.token)

    baseline = me(_app(service))
    assert inactive["status"] == baseline["status"] == 401
    assert inactive["body"] == baseline["body"]


# ---------------------------------------------------------------------------
# C. Response security
# ---------------------------------------------------------------------------


def test_no_credential_material_or_session_state_is_returned() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    digest = session_token_digest(issued.token)
    response = me(_app(service), token=issued.token)
    text = response["body"].decode("utf-8")

    for forbidden in (
        _PASSWORD_HASH,
        "$argon2id",
        "password_hash",
        issued.token,
        digest,
        "session_token_hash",
        "expires_at",
        "absolute_expires_at",
        "revoked_at",
        "last_seen_at",
    ):
        assert forbidden not in text, forbidden
    assert issued.token not in repr(response["headers"])
    assert digest not in repr(response["headers"])


def test_no_token_is_accepted_from_the_query_string_or_headers() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    app = _app(service)

    _assert_refused(
        me(app, path=f"{ME_PATH}?session_token={issued.token}&token={issued.token}")
    )
    _assert_refused(
        me(
            app,
            headers=[
                ("authorization", f"Bearer {issued.token}"),
                ("x-session-token", issued.token),
            ],
        )
    )


def test_response_never_mentions_the_cookie_or_internal_objects() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    text = me(_app(service), token=issued.token)["body"].decode("utf-8")
    for forbidden in ("va_session", "UserSession", "AuthenticatedSession", "object at"):
        assert forbidden not in text


# ---------------------------------------------------------------------------
# D. Infrastructure and unexpected failures
# ---------------------------------------------------------------------------


def test_database_unavailable_is_a_sanitized_503() -> None:
    service, _, sessions, _ = build()
    issued = service.issue_session("user-1")
    sessions.find_failure = DatabaseUnavailableError("connection refused")
    response = me(_app(service), token=issued.token)

    assert response["status"] == 503
    assert response["payload"] == {
        "error": {
            "code": "DATABASE_UNAVAILABLE",
            "message": "Database is temporarily unavailable.",
        }
    }
    assert _header_values(response, "set-cookie") == []
    assert b"connection refused" not in response["body"]
    assert _request_id(response)


def test_user_lookup_failure_is_a_sanitized_503_not_a_401() -> None:
    service, users, _, _ = build()
    issued = service.issue_session("user-1")
    users.failure = DatabaseUnavailableError("storage offline")
    response = me(_app(service), token=issued.token)

    assert response["status"] == 503
    assert response["payload"]["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert _header_values(response, "set-cookie") == []


def test_unexpected_failure_is_a_sanitized_500() -> None:
    service, users, sessions, _ = build()
    issued = service.issue_session("user-1")
    digest = session_token_digest(issued.token)

    # A naive clock is a SessionServiceError (ClockError), not an authentication
    # failure: it must stay a 500 rather than be reported as "not signed in".
    broken = SessionService(
        users=users,  # type: ignore[arg-type]
        sessions=sessions,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 1, 15, 9, 0),
    )
    response = me(_app(broken), token=issued.token, swallow=True)

    assert response["status"] == 500
    assert response["payload"] == {
        "error": {"code": "INTERNAL_ERROR", "message": "Internal server error."}
    }
    text = response["body"].decode("utf-8")
    for secret in (issued.token, digest, _PASSWORD_HASH, "Traceback"):
        assert secret not in text
    assert _header_values(response, "set-cookie") == []
    assert _request_id(response)


# ---------------------------------------------------------------------------
# E. Registration, method and unchanged neighbours
# ---------------------------------------------------------------------------


def test_route_is_registered_exactly_once() -> None:
    service, _, _, _ = build()
    app = _app(service)
    matching = [
        route
        for route in app.routes
        if getattr(route, "path", None) == ME_PATH
        and "GET" in (getattr(route, "methods", None) or set())
    ]
    assert len(matching) == 1


def test_approved_inventory_includes_me() -> None:
    assert ("/auth/me", "GET") in APPROVED_ROUTES
    assert len(APPROVED_ROUTES) == 11

    service, _, _, _ = build()
    app = _app(service)
    served = {
        (getattr(route, "path", ""), method)
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
        if method != "HEAD"
    }
    assert (f"{API_PREFIX}/auth/me", "GET") in served


def test_me_has_no_other_method_variant() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    app = _app(service)

    for method in ("POST", "PATCH", "PUT", "DELETE"):
        response = me(app, token=issued.token, method=method)
        assert response["status"] == 405, method


def test_health_stays_anonymous_and_liveness_only() -> None:
    service, _, _, _ = build()
    response = me(_app(service), path="/api/v1/health")
    assert response["status"] == 200
    assert response["payload"] == {"status": "ok"}


def test_login_and_logout_keep_their_origin_requirement() -> None:
    service, _, _, _ = build()
    app = _app(service)

    for path in ("/api/v1/auth/login", "/api/v1/auth/logout"):
        response = me(app, path=path, method="POST")
        assert response["status"] == 403, path
        assert response["payload"]["error"]["code"] == "ORIGIN_NOT_ALLOWED"


def test_me_does_not_require_an_origin_header() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    # `me()` sends no Origin at all; a read-only GET must still succeed.
    assert me(_app(service), token=issued.token)["status"] == 200
