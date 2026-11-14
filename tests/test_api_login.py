"""P5.4.5 login API tests (offline, pure ASGI, in-memory repositories).

The endpoint is driven through the real ``create_app`` composition (request-ID
middleware, exception handlers, cookie writer) with a real ``PasswordService``
and a real ``SessionService`` over in-memory P5.2 repositories. Only the
repositories are doubled, so Argon2id verification, rehash detection, session
issuance and the cookie header are genuine.
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
from src.api_auth import AUTHENTICATION_REQUIRED_CODE, AUTHENTICATION_REQUIRED_MESSAGE
from src.api_cookies import CLEAR_COOKIE_EXPIRES
from src.api_login import (
    DUMMY_PASSWORD_HASH,
    ORIGIN_NOT_ALLOWED_CODE,
    ORIGIN_NOT_ALLOWED_MESSAGE,
    LoginService,
    build_login_service,
)
from src.api_request_context import REQUEST_ID_HEADER
from src.config import SESSION_COOKIE, AppSettings, PasswordHashingConfig
from src.db import DatabaseUnavailableError
from src.password import PasswordService
from src.session_service import SessionService
from src.session_token import session_token_digest
from src.user import Role, User, username_key_for
from src.user_session import UserSession

NOW = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
TEST_ORIGIN = "https://attendance.test.invalid"
LOGIN_PATH = "/api/v1/auth/login"
_PASSWORD = "correct horse battery staple"
_PASSWORD_SERVICE = PasswordService()
_HASH = _PASSWORD_SERVICE.hash_password(_PASSWORD)
_LEGACY_HASH = PasswordService(
    config=PasswordHashingConfig(time_cost=1)
).hash_password(_PASSWORD)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeUserRepository:
    def __init__(self, users: list[User]) -> None:
        self.users = {user.user_id: user for user in users}
        self.updated: list[tuple[str, str]] = []
        self.find_failure: BaseException | None = None
        self.update_failure: BaseException | None = None

    def get(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    def find_by_username(self, username: str) -> User | None:
        if self.find_failure is not None:
            raise self.find_failure
        key = username_key_for(username)
        for user in self.users.values():
            if username_key_for(user.username) == key:
                return user
        return None

    def update_password_hash(self, user_id: str, password_hash: str) -> None:
        self.updated.append((user_id, password_hash))
        if self.update_failure is not None:
            raise self.update_failure
        self.users[user_id] = replace(
            self.users[user_id], password_hash=password_hash
        )


class FakeSessionRepository:
    def __init__(self) -> None:
        self.rows: dict[str, UserSession] = {}
        self.insert_failure: BaseException | None = None

    def insert(self, session: UserSession) -> UserSession:
        if self.insert_failure is not None:
            raise self.insert_failure
        self.rows[session.session_token_hash] = session
        return session

    def find_by_token_hash(self, digest: str) -> UserSession | None:
        return self.rows.get(digest)

    def touch(self, digest: str, *, last_seen_at: datetime, expires_at: datetime):
        updated = replace(
            self.rows[digest], last_seen_at=last_seen_at, expires_at=expires_at
        )
        self.rows[digest] = updated
        return updated

    def revoke(self, digest: str, *, revoked_at: datetime):
        updated = replace(self.rows[digest], revoked_at=revoked_at)
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
    password_hash: str = _HASH,
) -> User:
    return User(
        user_id=user_id,
        username=username,
        password_hash=password_hash,
        role=role,
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


def build(
    *,
    users: list[User] | None = None,
    clock: MutableClock | None = None,
) -> tuple[LoginService, FakeUserRepository, FakeSessionRepository, MutableClock]:
    user_repository = FakeUserRepository(users or [make_user()])
    session_repository = FakeSessionRepository()
    test_clock = clock or MutableClock()
    sessions = SessionService(
        users=user_repository,  # type: ignore[arg-type]
        sessions=session_repository,  # type: ignore[arg-type]
        clock=test_clock,
    )
    service = LoginService(
        users=user_repository,  # type: ignore[arg-type]
        passwords=_PASSWORD_SERVICE,
        sessions=sessions,
    )
    return service, user_repository, session_repository, test_clock


# ---------------------------------------------------------------------------
# ASGI driver
# ---------------------------------------------------------------------------


def _app(
    service: LoginService,
    *,
    settings: AppSettings | None = None,
):
    resolved = settings or AppSettings(
        app_env="testing", login_trusted_origins=(TEST_ORIGIN,)
    )
    return create_app(settings=resolved, login_service=service)


async def _call(
    app,
    *,
    headers: list[tuple[str, str]],
    body: bytes = b"",
    method: str = "POST",
    path: str = LOGIN_PATH,
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
        "query_string": b"",
        "root_path": "",
        "headers": [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in headers],
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8000),
    }
    await app(scope, receive, send)
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
        "payload": json.loads(raw_body),
    }


def login(
    app,
    *,
    username: object = "ada",
    password: object = _PASSWORD,
    origin: str | None = TEST_ORIGIN,
    extra_headers: list[tuple[str, str]] | None = None,
    omit_origin: bool = False,
) -> dict:
    headers: list[tuple[str, str]] = [("content-type", "application/json")]
    if not omit_origin:
        assert origin is not None
        headers.append(("origin", origin))
    headers.extend(extra_headers or [])
    body = json.dumps({"username": username, "password": password}).encode()
    return asyncio.run(_call(app, headers=headers, body=body))


def _header_values(response: dict, name: str) -> list[str]:
    return [value for key, value in response["headers"] if key == name]


def _request_id(response: dict) -> str | None:
    values = _header_values(response, REQUEST_ID_HEADER.lower())
    return values[0] if values else None


def _set_cookie(response: dict) -> tuple[str, str, dict[str, str | None]]:
    values = _header_values(response, "set-cookie")
    assert len(values) == 1, "exactly one session cookie is set"
    parts = values[0].split("; ")
    name, _, value = parts[0].partition("=")
    attributes: dict[str, str | None] = {}
    for part in parts[1:]:
        key, separator, attr = part.partition("=")
        attributes[key.lower()] = attr if separator else None
    return name, value.strip('"'), attributes


def _assert_generic_refusal(response: dict) -> None:
    assert response["status"] == 401
    assert response["payload"] == {
        "error": {
            "code": AUTHENTICATION_REQUIRED_CODE,
            "message": AUTHENTICATION_REQUIRED_MESSAGE,
        }
    }
    assert _header_values(response, "set-cookie") == []
    assert _request_id(response)


def _assert_origin_refusal(response: dict) -> None:
    assert response["status"] == 403
    assert response["payload"] == {
        "error": {
            "code": ORIGIN_NOT_ALLOWED_CODE,
            "message": ORIGIN_NOT_ALLOWED_MESSAGE,
        }
    }
    assert _header_values(response, "set-cookie") == []
    assert _request_id(response)


# ---------------------------------------------------------------------------
# A. Successful authentication
# ---------------------------------------------------------------------------


def test_active_user_with_correct_password_authenticates() -> None:
    service, users, sessions, _ = build()
    response = login(_app(service), username="ada", password=_PASSWORD)

    assert response["status"] == 200
    assert response["payload"] == {
        "user": {
            "user_id": "user-1",
            "username": "ada",
            "role": "Admin",
            "is_active": True,
        }
    }
    assert len(sessions.rows) == 1
    assert users.updated == []


def test_success_response_contains_no_credential_material() -> None:
    service, _, sessions, _ = build()
    response = login(_app(service))
    cookie_name, token, _ = _set_cookie(response)
    digest = session_token_digest(token)

    text = response["body"].decode("utf-8")
    assert _PASSWORD not in text
    assert _HASH not in text
    assert "$argon2id" not in text
    assert token not in text
    assert digest not in text
    assert "session_token_hash" not in text
    assert set(response["payload"]) == {"user"}
    assert set(response["payload"]["user"]) == {
        "user_id",
        "username",
        "role",
        "is_active",
    }
    assert cookie_name == "va_session"
    assert digest in sessions.rows


def test_session_cookie_matches_the_approved_policy_in_testing() -> None:
    service, _, _, _ = build()
    response = login(_app(service))
    name, token, attributes = _set_cookie(response)

    assert name == SESSION_COOKIE.name == "va_session"
    assert attributes["path"] == "/api/v1"
    assert "httponly" in attributes
    assert attributes["samesite"] == "lax"
    assert attributes["max-age"] == "43200"
    assert "secure" not in attributes, "testing is not a Secure environment"
    assert "domain" not in attributes
    assert token and token != ""


def test_session_lifetimes_follow_the_approved_policy() -> None:
    service, _, sessions, clock = build()
    response = login(_app(service))
    assert response["status"] == 200
    (row,) = sessions.rows.values()
    assert row.user_id == "user-1"
    assert row.issued_at == clock.now
    assert row.expires_at == clock.now + timedelta(hours=8)
    assert row.absolute_expires_at == clock.now + timedelta(hours=12)
    assert row.revoked_at is None


def test_username_is_normalized_before_lookup() -> None:
    service, _, _, _ = build(users=[make_user(username="Ada Lovelace")])
    response = login(_app(service), username="  ADA LOVELACE  ")
    assert response["status"] == 200
    assert response["payload"]["user"]["username"] == "Ada Lovelace"


# ---------------------------------------------------------------------------
# B. Failed authentication is indistinguishable
# ---------------------------------------------------------------------------


def test_incorrect_password_is_a_generic_401() -> None:
    service, users, sessions, _ = build()
    response = login(_app(service), password="wrong password value")
    _assert_generic_refusal(response)
    assert sessions.rows == {}
    assert users.updated == []


def test_unknown_username_is_the_same_generic_401() -> None:
    service, _, sessions, _ = build()
    response = login(_app(service), username="nobody", password=_PASSWORD)
    _assert_generic_refusal(response)
    assert sessions.rows == {}


def test_inactive_account_is_the_same_generic_401() -> None:
    service, users, sessions, _ = build(
        users=[make_user(username="ada", is_active=False)]
    )
    response = login(_app(service), password=_PASSWORD)
    _assert_generic_refusal(response)
    assert sessions.rows == {}
    assert users.updated == [], "an inactive account is never rehashed"


def test_all_credential_failures_are_byte_identical() -> None:
    service, users, _, _ = build(users=[make_user(username="ada")])
    users.users["user-2"] = make_user(
        user_id="user-2", username="inactive", is_active=False
    )
    app = _app(service)

    wrong = login(app, password="another wrong password")
    unknown = login(app, username="missing-person")
    inactive = login(app, username="inactive", password=_PASSWORD)

    for response in (wrong, unknown, inactive):
        assert response["status"] == 401
        assert response["body"] == wrong["body"]
        assert _header_values(response, "set-cookie") == []


def test_malformed_username_is_a_generic_401_not_a_server_error() -> None:
    service, _, sessions, _ = build()
    response = login(_app(service), username="ab")
    _assert_generic_refusal(response)
    assert sessions.rows == {}


# ---------------------------------------------------------------------------
# C. Dummy hash
# ---------------------------------------------------------------------------


def test_dummy_hash_is_a_current_argon2id_hash_of_a_discarded_value() -> None:
    assert isinstance(DUMMY_PASSWORD_HASH, str)
    assert DUMMY_PASSWORD_HASH.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert _PASSWORD_SERVICE.needs_rehash(DUMMY_PASSWORD_HASH) is False
    assert _PASSWORD_SERVICE.verify_password(_PASSWORD, DUMMY_PASSWORD_HASH) is False
    assert _PASSWORD_SERVICE.verify_password("anything else", DUMMY_PASSWORD_HASH) is False


def test_unknown_user_still_runs_a_real_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, sessions, _ = build()
    seen: list[tuple[str, str]] = []
    original = service._passwords

    class Recording(PasswordService):
        def verify_password(self, password: str, password_hash: str) -> bool:
            seen.append((password, password_hash))
            return original.verify_password(password, password_hash)

    service._passwords = Recording()  # type: ignore[attr-defined]
    response = login(_app(service), username="missing-person", password=_PASSWORD)

    _assert_generic_refusal(response)
    assert seen == [(_PASSWORD, DUMMY_PASSWORD_HASH)]
    assert sessions.rows == {}


def test_inactive_user_verifies_the_real_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, _, _ = build(users=[make_user(is_active=False)])
    seen: list[str] = []
    original = service._passwords

    class Recording(PasswordService):
        def verify_password(self, password: str, password_hash: str) -> bool:
            seen.append(password_hash)
            return original.verify_password(password, password_hash)

    service._passwords = Recording()  # type: ignore[attr-defined]
    response = login(_app(service), password=_PASSWORD)

    _assert_generic_refusal(response)
    assert seen == [_HASH]


def test_dummy_hash_is_a_constant_reused_across_requests() -> None:
    service, _, _, _ = build()
    app = _app(service)
    hashes: list[str] = []
    original = service._passwords

    class Recording(PasswordService):
        def verify_password(self, password: str, password_hash: str) -> bool:
            hashes.append(password_hash)
            return original.verify_password(password, password_hash)

    service._passwords = Recording()  # type: ignore[attr-defined]
    login(app, username="nobody-1")
    login(app, username="nobody-2")

    assert hashes == [DUMMY_PASSWORD_HASH, DUMMY_PASSWORD_HASH]


def test_dummy_hash_is_never_exposed() -> None:
    service, _, _, _ = build()
    response = login(_app(service), username="missing-person")
    text = response["body"].decode("utf-8")
    assert DUMMY_PASSWORD_HASH not in text
    assert "$argon2id" not in text
    assert DUMMY_PASSWORD_HASH not in repr(response["headers"])


# ---------------------------------------------------------------------------
# D. Password rehash
# ---------------------------------------------------------------------------


def test_hash_needing_a_rehash_is_replaced_before_issuance() -> None:
    service, users, sessions, _ = build(
        users=[make_user(password_hash=_LEGACY_HASH)]
    )
    assert _PASSWORD_SERVICE.needs_rehash(_LEGACY_HASH) is True

    response = login(_app(service), password=_PASSWORD)
    assert response["status"] == 200
    assert len(users.updated) == 1
    user_id, replacement = users.updated[0]
    assert user_id == "user-1"
    assert replacement != _LEGACY_HASH
    assert _PASSWORD_SERVICE.needs_rehash(replacement) is False
    assert _PASSWORD_SERVICE.verify_password(_PASSWORD, replacement) is True
    assert len(sessions.rows) == 1


def test_a_current_hash_is_not_rewritten() -> None:
    service, users, _, _ = build()
    response = login(_app(service), password=_PASSWORD)
    assert response["status"] == 200
    assert users.updated == []


def test_wrong_password_never_triggers_a_rehash() -> None:
    service, users, _, _ = build(users=[make_user(password_hash=_LEGACY_HASH)])
    response = login(_app(service), password="definitely the wrong password")
    _assert_generic_refusal(response)
    assert users.updated == []


def test_inactive_account_never_triggers_a_rehash() -> None:
    service, users, _, _ = build(
        users=[make_user(password_hash=_LEGACY_HASH, is_active=False)]
    )
    response = login(_app(service), password=_PASSWORD)
    _assert_generic_refusal(response)
    assert users.updated == []


def test_rehash_persistence_failure_prevents_session_creation() -> None:
    service, users, sessions, _ = build(
        users=[make_user(password_hash=_LEGACY_HASH)]
    )
    users.update_failure = DatabaseUnavailableError("storage gone")

    response = login(_app(service), password=_PASSWORD)

    assert response["status"] == 503
    assert response["payload"] == {
        "error": {
            "code": "DATABASE_UNAVAILABLE",
            "message": "Database is temporarily unavailable.",
        }
    }
    assert _header_values(response, "set-cookie") == []
    assert sessions.rows == {}
    assert _request_id(response)
    assert b"storage gone" not in response["body"]


# ---------------------------------------------------------------------------
# E. Session failures
# ---------------------------------------------------------------------------


def test_session_persistence_failure_does_not_return_success() -> None:
    service, _, sessions, _ = build()
    sessions.insert_failure = DatabaseUnavailableError("storage gone")

    response = login(_app(service), password=_PASSWORD)

    assert response["status"] == 503
    assert response["payload"]["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert _header_values(response, "set-cookie") == []
    assert sessions.rows == {}
    assert b"storage gone" not in response["body"]


def test_lookup_database_failure_is_a_503_not_a_401() -> None:
    service, users, _, _ = build()
    users.find_failure = DatabaseUnavailableError("storage gone")
    response = login(_app(service))
    assert response["status"] == 503
    assert response["payload"]["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert _header_values(response, "set-cookie") == []


# ---------------------------------------------------------------------------
# F. Origin validation
# ---------------------------------------------------------------------------


def test_trusted_origin_is_accepted() -> None:
    service, _, _, _ = build()
    response = login(_app(service), origin=TEST_ORIGIN)
    assert response["status"] == 200


def test_trusted_origin_is_canonicalized_before_comparison() -> None:
    service, _, _, _ = build()
    response = login(_app(service), origin="HTTPS://Attendance.Test.Invalid/")
    assert response["status"] == 200


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
        "https://attendance.test.invalid?x=1",
        "https://attendance.test.invalid#f",
        "https://attendance.test.invalid?",
        "https://attendance.test.invalid#",
        "https://attendance.test.invalid?#",
        "https://user:pass@attendance.test.invalid",
        "https://attendance.test.invalid:8443",
        "ftp://attendance.test.invalid",
    ],
)
def test_untrusted_or_malformed_origin_is_rejected(origin: str) -> None:
    service, _, sessions, _ = build()
    response = login(_app(service), origin=origin)
    _assert_origin_refusal(response)
    assert sessions.rows == {}


def test_missing_origin_is_rejected() -> None:
    service, _, _, _ = build()
    response = login(_app(service), omit_origin=True)
    _assert_origin_refusal(response)


def test_duplicate_origin_headers_are_rejected() -> None:
    service, _, _, _ = build()
    response = login(
        _app(service),
        extra_headers=[("origin", TEST_ORIGIN)],
    )
    _assert_origin_refusal(response)


def test_forwarded_headers_cannot_bypass_origin_validation() -> None:
    service, _, _, _ = build()
    response = login(
        _app(service),
        origin="https://evil.test",
        extra_headers=[
            ("x-forwarded-host", "attendance.test.invalid"),
            ("x-forwarded-proto", "https"),
            ("forwarded", f"host=attendance.test.invalid;proto=https"),
        ],
    )
    _assert_origin_refusal(response)


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_empty_allowlist_fails_closed(app_env: str) -> None:
    service, _, _, _ = build()
    response = login(
        _app(service, settings=AppSettings(app_env=app_env)),  # type: ignore[arg-type]
        origin="https://attendance.test.invalid",
    )
    _assert_origin_refusal(response)


def test_origin_refusal_is_logged_without_the_origin_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        api_module._logger,
        "warning",
        lambda message, *args: captured.append((message, args)),
    )
    service, _, _, _ = build()
    login(_app(service), origin="https://evil.test")

    assert captured
    rendered = "".join(f"{message}{args}" for message, args in captured)
    assert "evil.test" not in rendered
    assert "attendance.test.invalid" not in rendered


# ---------------------------------------------------------------------------
# G. Validation of the request body
# ---------------------------------------------------------------------------


def test_missing_password_is_a_standard_validation_response() -> None:
    service, _, sessions, _ = build()
    body = json.dumps({"username": "ada"}).encode()
    response = asyncio.run(
        _call(
            _app(service),
            headers=[("content-type", "application/json"), ("origin", TEST_ORIGIN)],
            body=body,
        )
    )
    assert response["status"] == 422
    assert response["payload"]["error"]["code"] == "REQUEST_INVALID"
    assert sessions.rows == {}
    assert _request_id(response)


@pytest.mark.parametrize(
    "extra",
    [{"user_id": "admin-1"}, {"role": "Admin"}, {"is_active": True}],
)
def test_client_supplied_identity_fields_are_rejected(extra: dict) -> None:
    service, _, sessions, _ = build()
    body = json.dumps({"username": "ada", "password": _PASSWORD, **extra}).encode()
    response = asyncio.run(
        _call(
            _app(service),
            headers=[("content-type", "application/json"), ("origin", TEST_ORIGIN)],
            body=body,
        )
    )
    assert response["status"] == 422
    assert sessions.rows == {}


def test_oversized_password_is_rejected_without_hashing() -> None:
    service, users, sessions, _ = build()
    response = login(_app(service), password="x" * 2000)
    assert response["status"] == 422
    assert users.updated == []
    assert sessions.rows == {}


# ---------------------------------------------------------------------------
# H. Request correlation and logging hygiene
# ---------------------------------------------------------------------------


def test_success_and_failure_carry_a_server_generated_request_id() -> None:
    service, _, _, _ = build()
    app = _app(service)
    for response in (login(app), login(app, password="wrong password")):
        value = _request_id(response)
        assert value is not None
        uuid.UUID(value)
        assert uuid.UUID(value).version == 4


def test_client_supplied_request_id_is_ignored() -> None:
    service, _, _, _ = build()
    supplied = "11111111-2222-4333-8444-555555555555"
    response = login(
        _app(service), extra_headers=[("x-request-id", supplied)]
    )
    value = _request_id(response)
    assert value is not None
    assert value != supplied
    assert "11111111-2222-4333-8444-555555555555" not in response["body"].decode()


def test_no_credential_material_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []

    def record(*args: object, **kwargs: object) -> None:
        captured.append(repr(args))

    for level in ("info", "warning", "error", "exception", "debug"):
        monkeypatch.setattr(api_module._logger, level, record)

    service, _, _, _ = build(users=[make_user(password_hash=_LEGACY_HASH)])
    app = _app(service)
    response = login(app, password=_PASSWORD)
    (_, token, _) = _set_cookie(response)
    login(app, password="wrong password value")
    login(app, username="missing-person")
    login(app, origin="https://evil.test")

    rendered = "".join(captured)
    for secret in (
        _PASSWORD,
        "wrong password value",
        _HASH,
        _LEGACY_HASH,
        DUMMY_PASSWORD_HASH,
        token,
        session_token_digest(token),
        "va_session=",
    ):
        assert secret not in rendered


# ---------------------------------------------------------------------------
# Lazy composition
# ---------------------------------------------------------------------------


def test_build_login_service_requires_a_database_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.db as db_module

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("construction must not open a database connection")

    monkeypatch.setattr(db_module, "open_connection", forbidden)

    with pytest.raises(DatabaseUnavailableError) as error:
        build_login_service(AppSettings(database_url=None))

    assert "No application database" in str(error.value)
    assert "://" not in str(error.value)


@pytest.mark.parametrize("dsn", ["", "   ", "\t\n", None])
def test_blank_database_configuration_fails_closed(
    dsn: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.db as db_module
    import src.db_security_repository as security_repository

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("no connection or repository may be built")

    monkeypatch.setattr(db_module, "open_connection", forbidden)
    monkeypatch.setattr(
        security_repository, "open_user_repository", forbidden, raising=False
    )

    with pytest.raises(DatabaseUnavailableError):
        build_login_service(AppSettings(database_url=dsn))


def test_build_login_service_shares_one_user_repository_and_opens_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.db as db_module
    import src.db_security_repository as security_repository

    opened: list[object] = []
    monkeypatch.setattr(
        db_module, "open_connection", lambda *a, **k: opened.append(a)
    )

    calls = {"user": 0, "session": 0}
    original_user = security_repository.open_user_repository
    original_session = security_repository.open_session_repository

    def user_factory(*args: object, **kwargs: object):
        calls["user"] += 1
        return original_user()

    def session_factory(*args: object, **kwargs: object):
        calls["session"] += 1
        return original_session()

    monkeypatch.setattr(
        security_repository, "open_user_repository", user_factory
    )
    monkeypatch.setattr(
        security_repository, "open_session_repository", session_factory
    )

    service = build_login_service(
        AppSettings(database_url="postgresql://app@localhost/attendance")
    )

    assert isinstance(service, LoginService)
    assert calls == {"user": 1, "session": 1}, "no duplicate user repository"
    assert opened == [], "construction opens no connection"
    assert service._sessions._users is service._users  # type: ignore[attr-defined]


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_configured_deployment_origin_is_accepted(app_env: str) -> None:
    service, _, sessions, _ = build()
    settings = AppSettings(
        app_env=app_env,  # type: ignore[arg-type]
        login_trusted_origins=("https://dash.example.com",),
    )
    response = login(
        _app(service, settings=settings), origin="https://dash.example.com"
    )

    assert response["status"] == 200
    assert len(sessions.rows) == 1


def test_other_origins_are_still_rejected_when_one_is_configured() -> None:
    service, _, sessions, _ = build()
    settings = AppSettings(
        app_env="production",
        login_trusted_origins=("https://dash.example.com",),
    )
    response = login(
        _app(service, settings=settings), origin="https://evil.example.com"
    )

    _assert_origin_refusal(response)
    assert sessions.rows == {}


def test_login_is_not_available_without_configuration() -> None:
    import src.api_login as login_module

    assert not hasattr(login_module, "_logger")
    assert CLEAR_COOKIE_EXPIRES == "Thu, 01 Jan 1970 00:00:00 GMT"
