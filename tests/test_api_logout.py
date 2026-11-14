"""P5.4.6 logout API tests (offline, pure ASGI, in-memory repositories).

The endpoint is driven through the real ``create_app`` composition (request-ID
middleware, exception handlers, Origin guard, cookie writer) with a real
``SessionService`` over an in-memory session repository. Only the repositories
are doubled, so revocation semantics, the idempotent outcomes and the cookie
header are genuine.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import src.api as api_module
from src.api import create_app
from src.api_cookies import CLEAR_COOKIE_EXPIRES
from src.api_login import ORIGIN_NOT_ALLOWED_CODE, ORIGIN_NOT_ALLOWED_MESSAGE
from src.api_request_context import REQUEST_ID_HEADER
from src.config import SESSION_COOKIE, AppSettings
from src.db import DatabaseUnavailableError
from src.session_repository import SessionNotFound
from src.session_service import SessionService
from src.session_token import (
    generate_session_token,
    session_token_digest,
)
from src.user_session import UserSession

NOW = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
TEST_ORIGIN = "https://attendance.test.invalid"
LOGOUT_PATH = "/api/v1/auth/logout"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeUserRepository:
    """Logout never reads a user; the session service only needs the seam."""

    def get(self, user_id: str) -> None:
        return None


class FakeSessionRepository:
    """In-memory ``user_session`` with the real revoke semantics."""

    def __init__(self, rows: list[UserSession] | None = None) -> None:
        self.rows = {row.session_token_hash: row for row in rows or []}
        self.revoke_calls: list[str] = []
        self.revoke_all_calls: list[str] = []
        self.revoke_failure: BaseException | None = None

    def revoke(self, session_token_hash: str, *, revoked_at: datetime) -> UserSession:
        self.revoke_calls.append(session_token_hash)
        if self.revoke_failure is not None:
            raise self.revoke_failure
        stored = self.rows.get(session_token_hash)
        if stored is None:
            raise SessionNotFound(
                "no session was revoked because the requested session does "
                "not exist"
            )
        # COALESCE semantics: the first revocation instant is preserved.
        updated = (
            stored
            if stored.revoked_at is not None
            else replace(stored, revoked_at=revoked_at)
        )
        self.rows[session_token_hash] = updated
        return updated

    def revoke_all_for_user(self, user_id: str, *, revoked_at: datetime) -> int:
        self.revoke_all_calls.append(user_id)
        return 0


def make_session(
    *,
    token: str,
    user_id: str = "user-1",
    revoked_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> UserSession:
    return UserSession(
        session_token_hash=session_token_digest(token),
        user_id=user_id,
        issued_at=NOW - timedelta(hours=1),
        expires_at=expires_at or NOW + timedelta(hours=8),
        absolute_expires_at=NOW + timedelta(hours=12),
        revoked_at=revoked_at,
        last_seen_at=NOW - timedelta(hours=1),
    )


def build(
    *,
    rows: list[UserSession] | None = None,
) -> tuple[SessionService, FakeSessionRepository]:
    repository = FakeSessionRepository(rows)
    service = SessionService(
        users=FakeUserRepository(),  # type: ignore[arg-type]
        sessions=repository,  # type: ignore[arg-type]
        clock=MutableClock(),
    )
    return service, repository


# ---------------------------------------------------------------------------
# ASGI driver
# ---------------------------------------------------------------------------


def _app(
    service: SessionService,
    *,
    settings: AppSettings | None = None,
):
    resolved = settings or AppSettings(
        app_env="testing", login_trusted_origins=(TEST_ORIGIN,)
    )
    return create_app(settings=resolved, session_service=service)


async def _call(
    app,
    *,
    headers: list[tuple[str, str]],
    body: bytes = b"",
    method: str = "POST",
    path: str = LOGOUT_PATH,
    query_string: bytes = b"",
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

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query_string,
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
    except Exception:
        # Starlette's ServerErrorMiddleware returns the registered 500 response
        # and then re-raises; the response itself is what this driver asserts.
        if not any(m["type"] == "http.response.start" for m in messages):
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


def logout(
    app,
    *,
    token: str | None = None,
    origin: str | None = TEST_ORIGIN,
    extra_headers: list[tuple[str, str]] | None = None,
    omit_origin: bool = False,
    body: bytes = b"",
    query_string: bytes = b"",
) -> dict:
    headers: list[tuple[str, str]] = []
    if not omit_origin:
        assert origin is not None
        headers.append(("origin", origin))
    if token is not None:
        headers.append(("cookie", f"{SESSION_COOKIE.name}={token}"))
    headers.extend(extra_headers or [])
    return asyncio.run(
        _call(app, headers=headers, body=body, query_string=query_string)
    )


def _header_values(response: dict, name: str) -> list[str]:
    return [value for key, value in response["headers"] if key == name]


def _request_id(response: dict) -> str | None:
    values = _header_values(response, REQUEST_ID_HEADER.lower())
    return values[0] if values else None


def _deletion_cookie(response: dict) -> dict[str, str | None]:
    values = _header_values(response, "set-cookie")
    assert len(values) == 1, "exactly one cookie is cleared"
    parts = values[0].split("; ")
    name, _, value = parts[0].partition("=")
    attributes: dict[str, str | None] = {"value": value, "name": name}
    for part in parts[1:]:
        key, separator, attr = part.partition("=")
        attributes[key.lower()] = attr if separator else None
    return attributes


def _assert_204(response: dict) -> None:
    assert response["status"] == 204
    assert response["body"] == b"", "204 carries no body"
    assert _request_id(response)


def _assert_origin_refusal(response: dict) -> None:
    assert response["status"] == 403
    payload = json.loads(response["body"])
    assert payload == {
        "error": {
            "code": ORIGIN_NOT_ALLOWED_CODE,
            "message": ORIGIN_NOT_ALLOWED_MESSAGE,
        }
    }
    assert _header_values(response, "set-cookie") == []
    assert _request_id(response)


# ---------------------------------------------------------------------------
# A. Successful logout
# ---------------------------------------------------------------------------


def test_valid_session_is_revoked_and_the_cookie_cleared() -> None:
    token = generate_session_token()
    service, repository = build(rows=[make_session(token=token)])
    response = logout(_app(service), token=token)

    _assert_204(response)
    assert repository.revoke_calls == [session_token_digest(token)]
    stored = repository.rows[session_token_digest(token)]
    assert stored.revoked_at == NOW


def test_only_the_presented_session_is_revoked() -> None:
    token = generate_session_token()
    other = generate_session_token()
    service, repository = build(
        rows=[make_session(token=token), make_session(token=other)]
    )
    response = logout(_app(service), token=token)

    _assert_204(response)
    assert repository.rows[session_token_digest(token)].revoked_at == NOW
    assert repository.rows[session_token_digest(other)].revoked_at is None
    assert repository.revoke_all_calls == [], "never revoke every user session"


def test_deletion_cookie_matches_the_approved_policy() -> None:
    token = generate_session_token()
    service, _ = build(rows=[make_session(token=token)])
    attributes = _deletion_cookie(logout(_app(service), token=token))

    assert attributes["name"] == SESSION_COOKIE.name == "va_session"
    assert (attributes["value"] or "").strip('"') == ""
    assert attributes["path"] == "/api/v1"
    assert attributes["max-age"] == "0"
    assert attributes["expires"] == CLEAR_COOKIE_EXPIRES
    assert "httponly" in attributes
    assert attributes["samesite"] == "lax"
    assert "secure" not in attributes, "testing is not a Secure environment"
    assert "domain" not in attributes


def test_secure_is_set_for_staging_and_production() -> None:
    token = generate_session_token()
    service, _ = build(rows=[make_session(token=token)])
    settings = AppSettings(
        app_env="production", login_trusted_origins=(TEST_ORIGIN,)
    )
    attributes = _deletion_cookie(
        logout(_app(service, settings=settings), token=token)
    )
    assert "secure" in attributes


def test_response_never_exposes_the_token_or_its_digest() -> None:
    token = generate_session_token()
    digest = session_token_digest(token)
    service, _ = build(rows=[make_session(token=token)])
    response = logout(_app(service), token=token)

    text = response["body"].decode("utf-8", "replace")
    assert token not in text
    assert digest not in text
    headers = repr(
        [pair for pair in response["headers"] if pair[0] != "set-cookie"]
    )
    assert token not in headers
    assert digest not in headers


# ---------------------------------------------------------------------------
# B. Idempotency for a client that is already signed out
# ---------------------------------------------------------------------------


def test_missing_cookie_is_a_successful_sign_out() -> None:
    service, repository = build()
    response = logout(_app(service))

    _assert_204(response)
    assert repository.revoke_calls == []
    assert _deletion_cookie(response)["name"] == "va_session"


def test_empty_cookie_is_a_successful_sign_out() -> None:
    service, repository = build()
    response = logout(_app(service), token="")

    _assert_204(response)
    assert repository.revoke_calls == []
    assert _deletion_cookie(response)["name"] == "va_session"


@pytest.mark.parametrize(
    "token",
    ["short", "not a token!", "x" * 128, "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlq"],
)
def test_malformed_token_is_a_successful_sign_out_without_a_lookup(
    token: str,
) -> None:
    service, repository = build()
    response = logout(_app(service), token=token)

    _assert_204(response)
    assert repository.revoke_calls == [], "structurally invalid: no DB call"
    assert _deletion_cookie(response)["name"] == "va_session"


def test_unknown_token_is_a_successful_sign_out() -> None:
    service, repository = build()
    response = logout(_app(service), token=generate_session_token())

    _assert_204(response)
    assert len(repository.revoke_calls) == 1
    assert _deletion_cookie(response)["name"] == "va_session"


def test_already_revoked_session_keeps_its_first_revocation() -> None:
    token = generate_session_token()
    first = NOW - timedelta(minutes=5)
    service, repository = build(
        rows=[make_session(token=token, revoked_at=first)]
    )
    response = logout(_app(service), token=token)

    _assert_204(response)
    assert repository.rows[session_token_digest(token)].revoked_at == first


def test_expired_session_can_still_be_signed_out() -> None:
    token = generate_session_token()
    service, repository = build(
        rows=[make_session(token=token, expires_at=NOW - timedelta(minutes=1))]
    )
    response = logout(_app(service), token=token)

    _assert_204(response)
    assert repository.rows[session_token_digest(token)].revoked_at == NOW


def test_repeated_logout_is_idempotent() -> None:
    token = generate_session_token()
    service, repository = build(rows=[make_session(token=token)])
    app = _app(service)

    first = logout(app, token=token)
    second = logout(app, token=token)

    _assert_204(first)
    _assert_204(second)
    assert repository.rows[session_token_digest(token)].revoked_at == NOW


# ---------------------------------------------------------------------------
# C. Origin security
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.test",
        "http://localhost:3000",
        "https://attendance.test.invalid.evil",
        "",
        "null",
        "not-an-origin",
        "https://attendance.test.invalid/login",
        "https://user:pass@attendance.test.invalid",
        "ftp://attendance.test.invalid",
    ],
)
def test_untrusted_or_malformed_origin_is_rejected(origin: str) -> None:
    token = generate_session_token()
    service, repository = build(rows=[make_session(token=token)])
    response = logout(_app(service), token=token, origin=origin)

    _assert_origin_refusal(response)
    assert repository.revoke_calls == []
    assert repository.rows[session_token_digest(token)].revoked_at is None


def test_missing_origin_is_rejected() -> None:
    token = generate_session_token()
    service, repository = build(rows=[make_session(token=token)])
    response = logout(_app(service), token=token, omit_origin=True)

    _assert_origin_refusal(response)
    assert repository.revoke_calls == []


def test_duplicate_origin_headers_are_rejected() -> None:
    token = generate_session_token()
    service, repository = build(rows=[make_session(token=token)])
    response = logout(
        _app(service),
        token=token,
        extra_headers=[("origin", TEST_ORIGIN)],
    )

    _assert_origin_refusal(response)
    assert repository.revoke_calls == []


def test_forwarded_headers_cannot_bypass_origin_validation() -> None:
    token = generate_session_token()
    service, repository = build(rows=[make_session(token=token)])
    response = logout(
        _app(service),
        token=token,
        origin="https://evil.test",
        extra_headers=[
            ("x-forwarded-host", "attendance.test.invalid"),
            ("x-forwarded-proto", "https"),
        ],
    )
    _assert_origin_refusal(response)
    assert repository.revoke_calls == []


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_unconfigured_allowlist_fails_closed(app_env: str) -> None:
    token = generate_session_token()
    service, repository = build(rows=[make_session(token=token)])
    response = logout(
        _app(service, settings=AppSettings(app_env=app_env)),  # type: ignore[arg-type]
        token=token,
    )
    _assert_origin_refusal(response)
    assert repository.revoke_calls == []


# ---------------------------------------------------------------------------
# D. Infrastructure and unexpected failures
# ---------------------------------------------------------------------------


def test_database_unavailable_is_a_503_without_clearing_the_cookie() -> None:
    token = generate_session_token()
    service, repository = build(rows=[make_session(token=token)])
    repository.revoke_failure = DatabaseUnavailableError("storage gone")
    response = logout(_app(service), token=token)

    assert response["status"] == 503
    assert json.loads(response["body"]) == {
        "error": {
            "code": "DATABASE_UNAVAILABLE",
            "message": "Database is temporarily unavailable.",
        }
    }
    assert _header_values(response, "set-cookie") == [], (
        "revocation was not confirmed, so the cookie is retained for a retry"
    )
    assert repository.rows[session_token_digest(token)].revoked_at is None
    assert b"storage gone" not in response["body"]
    assert _request_id(response)


def test_unexpected_failure_is_a_sanitized_500() -> None:
    token = generate_session_token()
    service, repository = build(rows=[make_session(token=token)])
    repository.revoke_failure = RuntimeError("boom in the driver")
    response = logout(_app(service), token=token)

    assert response["status"] == 500
    assert json.loads(response["body"]) == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "Internal server error.",
        }
    }
    assert _header_values(response, "set-cookie") == []
    assert b"boom in the driver" not in response["body"]
    assert _request_id(response)


def test_database_failure_does_not_reveal_whether_the_token_existed() -> None:
    token = generate_session_token()
    service, repository = build(rows=[make_session(token=token)])
    repository.revoke_failure = DatabaseUnavailableError("storage gone")

    known = logout(_app(service), token=token)
    unknown = logout(_app(service), token=generate_session_token())

    assert known["status"] == unknown["status"] == 503
    assert known["body"] == unknown["body"]


# ---------------------------------------------------------------------------
# E. Token source discipline
# ---------------------------------------------------------------------------


def test_a_token_in_the_query_string_is_not_used() -> None:
    token = generate_session_token()
    service, repository = build(rows=[make_session(token=token)])
    response = logout(
        _app(service),
        query_string=f"token={token}".encode(),
    )

    _assert_204(response)
    assert repository.revoke_calls == [], "only the cookie is a credential"
    assert repository.rows[session_token_digest(token)].revoked_at is None


def test_a_token_in_the_body_is_not_used() -> None:
    token = generate_session_token()
    service, repository = build(rows=[make_session(token=token)])
    response = logout(
        _app(service),
        body=json.dumps({"session_token": token}).encode(),
    )

    _assert_204(response)
    assert repository.revoke_calls == []
    assert repository.rows[session_token_digest(token)].revoked_at is None


# ---------------------------------------------------------------------------
# F. Registration, correlation and logging hygiene
# ---------------------------------------------------------------------------


def test_logout_is_registered_exactly_once() -> None:
    service, _ = build()
    app = _app(service)
    matching = [
        route
        for route in app.routes
        if getattr(route, "path", None) == LOGOUT_PATH
        and "POST" in (getattr(route, "methods", None) or set())
    ]
    assert len(matching) == 1


def test_request_id_is_a_server_generated_uuid() -> None:
    service, _ = build()
    response = logout(_app(service))
    value = _request_id(response)
    assert value is not None
    assert uuid.UUID(value).version == 4


def test_client_supplied_request_id_is_ignored() -> None:
    supplied = "11111111-2222-4333-8444-555555555555"
    service, _ = build()
    response = logout(
        _app(service), extra_headers=[("x-request-id", supplied)]
    )
    assert _request_id(response) not in (None, supplied)


def test_no_credential_material_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def record(*args: object, **kwargs: object) -> None:
        captured.append(repr(args))

    for level in ("info", "warning", "error", "exception", "debug"):
        monkeypatch.setattr(api_module._logger, level, record)

    token = generate_session_token()
    service, _ = build(rows=[make_session(token=token)])
    app = _app(service)
    logout(app, token=token)
    logout(app, token="malformed-value")
    logout(app, token=generate_session_token())
    logout(app, token=token, origin="https://evil.test")

    rendered = "".join(captured)
    assert token not in rendered
    assert session_token_digest(token) not in rendered
    assert "va_session=" not in rendered
