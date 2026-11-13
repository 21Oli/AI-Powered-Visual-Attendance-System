"""P5.4.4 authentication-dependency tests (offline, pure ASGI, no database).

The dependency is exercised through the *production* application composition
(``create_app()``: request-ID middleware plus the registered exception handlers)
with a real :class:`~src.session_service.SessionService` over in-memory
repositories, so expiry, revocation, inactivity and storage-failure behaviour is
the real service's, not a mock's.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.requests import Request

import src.api as api_module
import src.api_auth as auth_module
from src.api import create_app
from src.api_auth import (
    AUTHENTICATION_REQUIRED_CODE,
    AUTHENTICATION_REQUIRED_MESSAGE,
    AuthenticatedPrincipal,
    AuthenticationRequired,
    build_session_service,
    make_session_dependency,
)
from src.api_request_context import REQUEST_ID_HEADER
from src.config import AppSettings
from src.db import DatabaseUnavailableError
from src.session_service import ClockError, SessionService
from src.session_token import generate_session_token, session_token_digest
from src.user import Role, User

NOW = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
_PASSWORD_HASH = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"


# ---------------------------------------------------------------------------
# Test doubles (in-memory P5.2 repositories)
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

    def deactivate(self, user_id: str) -> None:
        self.users[user_id] = replace(self.users[user_id], is_active=False)


class FakeSessionRepository:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}
        self.find_failure: BaseException | None = None

    def insert(self, session) -> object:
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
        changed = 0
        for digest, session in list(self.rows.items()):
            if session.user_id == user_id and session.revoked_at is None:
                self.rows[digest] = replace(session, revoked_at=revoked_at)
                changed += 1
        return changed


def make_user(
    *,
    user_id: str = "user-1",
    username: str = "ada",
    role: Role = Role.OPERATOR,
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


# ---------------------------------------------------------------------------
# ASGI driver and probe app
# ---------------------------------------------------------------------------


def _scope(path: str, headers: dict[str, str] | None = None) -> dict:
    base_path, _, query = path.partition("?")
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": base_path,
        "raw_path": base_path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8000),
    }


async def _call(
    app,
    path: str,
    headers: dict[str, str] | None = None,
    *,
    swallow: bool = False,
) -> dict:
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    try:
        await app(_scope(path, headers), receive, send)
    except BaseException:  # noqa: BLE001 - Starlette re-raises after rendering
        if not swallow:
            raise
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return {
        "status": start["status"],
        "headers": [
            (key.decode("latin-1").lower(), value.decode("latin-1"))
            for key, value in start["headers"]
        ],
        "body": body,
        "payload": json.loads(body),
    }


def request(
    app,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    swallow: bool = False,
) -> dict:
    return asyncio.run(_call(app, path, headers, swallow=swallow))


def _probe_app(service: SessionService):
    """Production app composition plus one route that uses the dependency."""
    app = create_app()
    dependency = make_session_dependency(service)

    @app.get("/probe/whoami")
    async def whoami(
        principal: AuthenticatedPrincipal = Depends(dependency),
    ) -> JSONResponse:
        # A small yield so gathered requests genuinely interleave.
        await asyncio.sleep(0.002)
        return JSONResponse(
            {"user_id": principal.user_id, "role": principal.role.value}
        )

    return app


def _cookie_header(token: str) -> dict[str, str]:
    return {"cookie": f"va_session={token}"}


def _request_id(response: dict) -> str | None:
    for key, value in response["headers"]:
        if key == REQUEST_ID_HEADER.lower():
            return value
    return None


def _assert_refused(response: dict) -> None:
    assert response["status"] == 401
    assert response["payload"] == {
        "error": {
            "code": AUTHENTICATION_REQUIRED_CODE,
            "message": AUTHENTICATION_REQUIRED_MESSAGE,
        }
    }
    assert _request_id(response), "the 401 still carries X-Request-ID"


# ---------------------------------------------------------------------------
# 1, 2. Missing cookie and the happy path
# ---------------------------------------------------------------------------


def test_missing_cookie_is_a_sanitized_401() -> None:
    service, _, _, _ = build()
    response = request(_probe_app(service), "/probe/whoami")
    _assert_refused(response)
    assert b"Traceback" not in response["body"]
    assert "va_session" not in response["body"].decode("utf-8")


def test_valid_cookie_resolves_the_server_side_identity() -> None:
    service, _, _, _ = build(
        users=[make_user(user_id="user-1", role=Role.HR_MANAGER)]
    )
    issued = service.issue_session("user-1")
    response = request(
        _probe_app(service), "/probe/whoami", headers=_cookie_header(issued.token)
    )
    assert response["status"] == 200
    assert response["payload"] == {"user_id": "user-1", "role": "HR Manager"}


# ---------------------------------------------------------------------------
# 3-6. Invalid, expired, revoked and idle-expired sessions
# ---------------------------------------------------------------------------


def test_unknown_token_is_a_401() -> None:
    service, _, _, _ = build()
    service.issue_session("user-1")
    response = request(
        _probe_app(service),
        "/probe/whoami",
        headers=_cookie_header(generate_session_token()),
    )
    _assert_refused(response)


def test_structurally_invalid_token_is_a_401() -> None:
    service, _, _, _ = build()
    response = request(
        _probe_app(service), "/probe/whoami", headers=_cookie_header("nonsense")
    )
    _assert_refused(response)


def test_idle_expired_session_is_a_401() -> None:
    service, _, _, clock = build()
    issued = service.issue_session("user-1")
    clock.advance(timedelta(hours=8, seconds=1))
    response = request(
        _probe_app(service), "/probe/whoami", headers=_cookie_header(issued.token)
    )
    _assert_refused(response)


def test_absolute_expired_session_is_a_401() -> None:
    service, _, _, clock = build()
    issued = service.issue_session("user-1")
    clock.advance(timedelta(hours=13))
    response = request(
        _probe_app(service), "/probe/whoami", headers=_cookie_header(issued.token)
    )
    _assert_refused(response)


def test_revoked_session_is_a_401() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    service.revoke_session(issued.token)
    response = request(
        _probe_app(service), "/probe/whoami", headers=_cookie_header(issued.token)
    )
    _assert_refused(response)


# ---------------------------------------------------------------------------
# 7. Inactive or vanished accounts
# ---------------------------------------------------------------------------


def test_inactive_user_cannot_authenticate() -> None:
    service, users, _, _ = build()
    issued = service.issue_session("user-1")
    users.deactivate("user-1")
    response = request(
        _probe_app(service), "/probe/whoami", headers=_cookie_header(issued.token)
    )
    _assert_refused(response)


def test_deleted_user_cannot_authenticate() -> None:
    service, users, _, _ = build()
    issued = service.issue_session("user-1")
    users.users.pop("user-1")
    response = request(
        _probe_app(service), "/probe/whoami", headers=_cookie_header(issued.token)
    )
    _assert_refused(response)


# ---------------------------------------------------------------------------
# 7b. Edge cases: empty cookie, no Set-Cookie on refusal, unexpected errors
# ---------------------------------------------------------------------------


def _set_cookie_values(response: dict) -> list[str]:
    return [value for name, value in response["headers"] if name == "set-cookie"]


def test_empty_session_cookie_is_a_sanitized_401_without_a_cookie() -> None:
    """``Cookie: va_session=`` is a missing credential, not an anonymous pass."""
    service, _, _, _ = build()
    service.issue_session("user-1")
    response = request(
        _probe_app(service), "/probe/whoami", headers={"cookie": "va_session="}
    )
    _assert_refused(response)
    assert _set_cookie_values(response) == []
    assert _request_id(response)


def test_authentication_failures_never_issue_a_session_cookie() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    service.revoke_session(issued.token)
    app = _probe_app(service)
    cases: dict[str, dict[str, str] | None] = {
        "missing": None,
        "malformed": _cookie_header("not-a-session-token"),
        "unknown": _cookie_header(generate_session_token()),
        "revoked": _cookie_header(issued.token),
    }
    for label, headers in cases.items():
        response = request(app, "/probe/whoami", headers=headers)
        _assert_refused(response)
        assert _set_cookie_values(response) == [], label


def test_unexpected_service_failure_remains_a_sanitized_500() -> None:
    """A non-authentication service fault must not become a 401."""
    service, users, sessions, _ = build()
    issued = service.issue_session("user-1")
    digest = session_token_digest(issued.token)

    # The real SessionService refuses a naive clock with ClockError, which is a
    # SessionServiceError, not an AuthenticationError.
    broken = SessionService(
        users=users,  # type: ignore[arg-type]
        sessions=sessions,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 1, 15, 9, 0),  # deliberately naive
    )
    dependency = make_session_dependency(broken)
    with pytest.raises(ClockError):
        dependency(Request(_scope("/probe/whoami", _cookie_header(issued.token))))

    response = request(
        _probe_app(broken),
        "/probe/whoami",
        headers=_cookie_header(issued.token),
        swallow=True,
    )
    assert response["status"] == 500
    assert response["payload"] == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "Internal server error.",
        }
    }
    assert _request_id(response)
    assert _set_cookie_values(response) == []
    text = response["body"].decode("utf-8")
    for secret in (issued.token, digest, _PASSWORD_HASH, "Traceback", "clock"):
        assert secret not in text


# ---------------------------------------------------------------------------
# 8. Storage failures stay 503, never 401
# ---------------------------------------------------------------------------


def test_repository_failure_remains_a_503() -> None:
    service, _, sessions, _ = build()
    issued = service.issue_session("user-1")
    sessions.find_failure = DatabaseUnavailableError("connection refused")
    response = request(
        _probe_app(service), "/probe/whoami", headers=_cookie_header(issued.token)
    )
    assert response["status"] == 503
    assert response["payload"] == {
        "error": {
            "code": "DATABASE_UNAVAILABLE",
            "message": "Database is temporarily unavailable.",
        }
    }
    assert _request_id(response)
    assert b"connection refused" not in response["body"]


def test_repository_failure_is_not_caught_by_the_dependency() -> None:
    """Direct call: the storage error propagates instead of becoming a 401."""
    service, _, sessions, _ = build()
    issued = service.issue_session("user-1")
    sessions.find_failure = DatabaseUnavailableError("offline")
    dependency = make_session_dependency(service)
    request_object = Request(_scope("/probe/whoami", _cookie_header(issued.token)))
    with pytest.raises(DatabaseUnavailableError):
        dependency(request_object)


# ---------------------------------------------------------------------------
# 9-10. The cookie is the only credential source; client identity is ignored
# ---------------------------------------------------------------------------


def test_client_supplied_user_id_and_role_cannot_override_the_server_identity() -> None:
    service, _, _, _ = build(
        users=[make_user(user_id="user-1", role=Role.OPERATOR)]
    )
    issued = service.issue_session("user-1")
    response = request(
        _probe_app(service),
        "/probe/whoami",
        headers={
            **_cookie_header(issued.token),
            "x-user-id": "admin-1",
            "x-role": "Admin",
            "x-user-role": "Admin",
        },
    )
    assert response["status"] == 200
    assert response["payload"] == {"user_id": "user-1", "role": "Operator"}
    assert "admin-1" not in response["body"].decode("utf-8")


def test_token_in_query_string_or_header_cannot_replace_the_cookie() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    app = _probe_app(service)

    query_only = request(
        app, f"/probe/whoami?va_session={issued.token}&token={issued.token}"
    )
    _assert_refused(query_only)

    header_only = request(
        app,
        "/probe/whoami",
        headers={"x-session-token": issued.token, "authorization": f"Bearer {issued.token}"},
    )
    _assert_refused(header_only)


# ---------------------------------------------------------------------------
# 11-12. No credential material in responses or logs; request IDs preserved
# ---------------------------------------------------------------------------


def test_tokens_and_hashes_never_reach_responses_or_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        api_module._logger,
        "info",
        lambda message, *args: captured.append((message, args)),
    )
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    digest = session_token_digest(issued.token)
    app = _probe_app(service)

    accepted = request(app, "/probe/whoami", headers=_cookie_header(issued.token))
    refused = request(app, "/probe/whoami", headers=_cookie_header(generate_session_token()))

    for response in (accepted, refused):
        text = response["body"].decode("utf-8")
        assert issued.token not in text
        assert digest not in text
        assert _PASSWORD_HASH not in text
        assert issued.token not in repr(response["headers"])
        assert digest not in repr(response["headers"])

    assert captured, "the refusal path logs"
    for message, args in captured:
        rendered = message + repr(args)
        assert issued.token not in rendered
        assert digest not in rendered
        assert "va_session=" not in rendered


def test_authenticated_response_carries_the_request_id() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    response = request(
        _probe_app(service), "/probe/whoami", headers=_cookie_header(issued.token)
    )
    assert response["status"] == 200
    assert _request_id(response)


# ---------------------------------------------------------------------------
# 13. Concurrency: no shared identity state
# ---------------------------------------------------------------------------


def test_concurrent_requests_cannot_share_identity() -> None:
    service, _, _, _ = build(
        users=[
            make_user(user_id="user-1", username="ada", role=Role.OPERATOR),
            make_user(user_id="user-2", username="grace", role=Role.AUDITOR),
        ]
    )
    first = service.issue_session("user-1")
    second = service.issue_session("user-2")
    app = _probe_app(service)

    async def run() -> list[dict]:
        return await asyncio.gather(
            *(
                _call(app, "/probe/whoami", _cookie_header(first.token))
                if index % 2 == 0
                else _call(app, "/probe/whoami", _cookie_header(second.token))
                for index in range(6)
            )
        )

    responses = asyncio.run(run())
    for index, response in enumerate(responses):
        assert response["status"] == 200
        expected = (
            {"user_id": "user-1", "role": "Operator"}
            if index % 2 == 0
            else {"user_id": "user-2", "role": "Auditor"}
        )
        assert response["payload"] == expected


# ---------------------------------------------------------------------------
# Principal shape, dependency validation and lazy composition
# ---------------------------------------------------------------------------


def test_principal_is_a_minimal_immutable_identity() -> None:
    service, _, _, _ = build(
        users=[make_user(user_id="user-1", username="ada", role=Role.ADMIN)]
    )
    issued = service.issue_session("user-1")
    principal = make_session_dependency(service)(
        Request(_scope("/probe/whoami", _cookie_header(issued.token)))
    )
    assert isinstance(principal, AuthenticatedPrincipal)
    assert principal == AuthenticatedPrincipal(
        user_id="user-1",
        username="ada",
        role=Role.ADMIN,
        is_active=True,
    )
    assert [field.name for field in dataclasses.fields(principal)] == [
        "user_id",
        "username",
        "role",
        "is_active",
    ]
    # The credential-bearing rows are not retained in any form.
    assert not hasattr(principal, "user")
    assert not hasattr(principal, "session")
    with pytest.raises(FrozenInstanceError):
        principal.role = Role.OPERATOR  # type: ignore[misc]


def test_principal_representation_and_serialization_are_credential_free() -> None:
    """P5.4.4.1: no hash, digest or raw token may leave through repr/encoder."""
    service, _, _, _ = build(
        users=[make_user(user_id="user-1", username="ada", role=Role.ADMIN)]
    )
    issued = service.issue_session("user-1")
    digest = session_token_digest(issued.token)
    principal = make_session_dependency(service)(
        Request(_scope("/probe/whoami", _cookie_header(issued.token)))
    )

    rendered = repr(principal)
    encoded = json.dumps(jsonable_encoder(principal), sort_keys=True)

    for blob in (rendered, encoded):
        assert _PASSWORD_HASH not in blob
        assert "$argon2id" not in blob
        assert digest not in blob
        assert issued.token not in blob
        assert "password_hash" not in blob
        assert "session_token_hash" not in blob

    assert encoded == json.dumps(
        {
            "user_id": "user-1",
            "username": "ada",
            "role": "Admin",
            "is_active": True,
        },
        sort_keys=True,
    )


def test_dependency_rejects_an_object_without_validate_session() -> None:
    with pytest.raises(TypeError):
        make_session_dependency(object())  # type: ignore[arg-type]


def test_authentication_required_is_registered_on_the_production_app() -> None:
    from src.api import handle_authentication_required

    app = create_app()
    assert app.exception_handlers[AuthenticationRequired] is handle_authentication_required


def test_build_session_service_without_a_database_is_a_typed_503_cause() -> None:
    with pytest.raises(DatabaseUnavailableError):
        build_session_service(AppSettings(database_url=None))
    with pytest.raises(DatabaseUnavailableError):
        build_session_service(AppSettings(database_url="   "))


def test_auth_module_keeps_no_logger() -> None:
    assert not hasattr(auth_module, "_logger")
    assert not hasattr(auth_module, "logger")
