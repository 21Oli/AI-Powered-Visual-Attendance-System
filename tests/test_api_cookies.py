"""P5.4.4 session-cookie utility tests (offline, no server, no browser).

The assertions read the real ``Set-Cookie`` header produced by Starlette, so a
regression in an attribute is caught even if the utility's call looks right.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

import src.api_cookies as cookie_module
from src.api_cookies import (
    CLEAR_COOKIE_EXPIRES,
    clear_session_cookie,
    set_session_cookie,
)
from src.config import SESSION_COOKIE, AppSettings
from src.session_token import (
    SessionTokenError,
    generate_session_token,
    session_token_digest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_cookie_headers(response) -> list[str]:
    return list(response.headers.getlist("set-cookie"))


def _parse_set_cookie(raw: str) -> tuple[str, str, dict[str, str | None]]:
    """Split one ``Set-Cookie`` value into (name, value, lowercased attrs)."""
    parts = raw.split("; ")
    name, _, value = parts[0].partition("=")
    attributes: dict[str, str | None] = {}
    for part in parts[1:]:
        key, separator, attr_value = part.partition("=")
        attributes[key.lower()] = attr_value if separator else None
    return name, value.strip('"'), attributes


def _cookie(settings: AppSettings, token: str) -> tuple[str, str, dict[str, str | None]]:
    response = JSONResponse({"ok": True})
    set_session_cookie(response, token=token, settings=settings)
    headers = _set_cookie_headers(response)
    assert len(headers) == 1, "exactly one session cookie is attached"
    return _parse_set_cookie(headers[0])


def _cleared(settings: AppSettings) -> tuple[str, str, dict[str, str | None]]:
    response = JSONResponse({"ok": True})
    clear_session_cookie(response, settings=settings)
    headers = _set_cookie_headers(response)
    assert len(headers) == 1, "exactly one deletion cookie is attached"
    return _parse_set_cookie(headers[0])


def _scope(path: str = "/set") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8000),
    }


async def _exchange(app, path: str) -> tuple[int, list[str], bytes]:
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await app(_scope(path), receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    headers = [
        value.decode("latin-1")
        for key, value in start["headers"]
        if key.lower() == b"set-cookie"
    ]
    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    return start["status"], headers, body


# ---------------------------------------------------------------------------
# 1-2, 5-8. Name, path, HttpOnly, SameSite, Secure, Max-Age, no Domain
# ---------------------------------------------------------------------------


def test_cookie_name_and_path_are_exactly_the_approved_values() -> None:
    name, value, attributes = _cookie(AppSettings(app_env="development"), generate_session_token())
    assert name == "va_session"
    assert SESSION_COOKIE.name == "va_session"
    assert attributes["path"] == "/api/v1"


def test_cookie_value_is_the_raw_token_and_not_its_digest() -> None:
    token = generate_session_token()
    _, value, _ = _cookie(AppSettings(app_env="development"), token)
    assert value == token
    assert value != session_token_digest(token)


def test_cookie_is_httponly_and_lax() -> None:
    _, _, attributes = _cookie(AppSettings(app_env="development"), generate_session_token())
    assert "httponly" in attributes
    assert attributes["samesite"] == "lax"
    assert attributes["samesite"] == SESSION_COOKIE.same_site.lower()


def test_max_age_is_exactly_twelve_hours() -> None:
    _, _, attributes = _cookie(AppSettings(app_env="development"), generate_session_token())
    assert attributes["max-age"] == "43200"
    assert attributes["max-age"] == str(SESSION_COOKIE.max_age_seconds)
    assert SESSION_COOKIE.max_age_seconds == 43_200


@pytest.mark.parametrize("app_env", ["development", "testing"])
def test_secure_is_absent_outside_staging_and_production(app_env: str) -> None:
    settings = AppSettings(app_env=app_env)  # type: ignore[arg-type]
    assert settings.session_cookie_secure is False
    _, _, attributes = _cookie(settings, generate_session_token())
    assert "secure" not in attributes


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_secure_is_present_in_staging_and_production(app_env: str) -> None:
    settings = AppSettings(app_env=app_env)  # type: ignore[arg-type]
    assert settings.session_cookie_secure is True
    _, _, attributes = _cookie(settings, generate_session_token())
    assert "secure" in attributes


def test_no_domain_attribute_is_ever_emitted() -> None:
    for app_env in ("development", "testing", "staging", "production"):
        settings = AppSettings(app_env=app_env)  # type: ignore[arg-type]
        _, _, attributes = _cookie(settings, generate_session_token())
        _, _, clear_attributes = _cleared(settings)
        assert "domain" not in attributes, app_env
        assert "domain" not in clear_attributes, app_env
        assert SESSION_COOKIE.domain is None


def test_no_unapproved_cookie_attributes_appear() -> None:
    """Pin the whole attribute set: no extras (no Domain, no Expires on set)."""
    _, _, attributes = _cookie(AppSettings(app_env="staging"), generate_session_token())
    assert set(attributes) == {"httponly", "max-age", "path", "samesite", "secure"}


# ---------------------------------------------------------------------------
# 9. Clearing the cookie
# ---------------------------------------------------------------------------


def test_clear_cookie_expires_immediately_and_matches_every_attribute() -> None:
    settings = AppSettings(app_env="development")
    name, value, attributes = _cleared(settings)
    assert name == "va_session"
    assert value == ""
    assert attributes["max-age"] == "0"
    assert attributes["expires"] == CLEAR_COOKIE_EXPIRES
    assert attributes["expires"] == "Thu, 01 Jan 1970 00:00:00 GMT"
    # Deletion only removes the intended cookie when the other attributes match.
    _, _, live = _cookie(settings, generate_session_token())
    assert attributes["path"] == live["path"]
    assert attributes["samesite"] == live["samesite"]
    assert ("httponly" in attributes) == ("httponly" in live)
    assert ("secure" in attributes) == ("secure" in live)
    assert "domain" not in attributes


@pytest.mark.parametrize("app_env", ["development", "testing", "staging", "production"])
def test_clear_cookie_keeps_the_environment_secure_flag(app_env: str) -> None:
    settings = AppSettings(app_env=app_env)  # type: ignore[arg-type]
    _, _, attributes = _cleared(settings)
    assert ("secure" in attributes) == settings.session_cookie_secure


# ---------------------------------------------------------------------------
# 10. The token never reaches a response body
# ---------------------------------------------------------------------------


def test_set_and_clear_never_put_the_token_in_the_response_body() -> None:
    token = generate_session_token()
    settings = AppSettings(app_env="development")
    app = FastAPI()

    @app.get("/set")
    async def issue() -> JSONResponse:
        response = JSONResponse({"ok": True})
        set_session_cookie(response, token=token, settings=settings)
        return response

    @app.get("/clear")
    async def revoke() -> JSONResponse:
        response = JSONResponse({"ok": True})
        clear_session_cookie(response, settings=settings)
        return response

    status, headers, body = asyncio.run(_exchange(app, "/set"))
    assert status == 200
    assert len(headers) == 1
    assert token in headers[0], "the credential travels only in Set-Cookie"
    assert token.encode() not in body
    assert json.loads(body) == {"ok": True}

    status, headers, body = asyncio.run(_exchange(app, "/clear"))
    assert status == 200
    assert len(headers) == 1
    assert token.encode() not in body
    assert token not in headers[0]
    assert json.loads(body) == {"ok": True}


# ---------------------------------------------------------------------------
# Refusals and logging hygiene
# ---------------------------------------------------------------------------


def test_set_cookie_refuses_a_malformed_token() -> None:
    response = JSONResponse({"ok": True})
    with pytest.raises(SessionTokenError):
        set_session_cookie(
            response, token="not-a-session-token", settings=AppSettings()
        )
    assert _set_cookie_headers(response) == []


def test_cookie_module_keeps_no_logger() -> None:
    """Cookie values are bearer credentials: this module must not log at all."""
    assert not hasattr(cookie_module, "_logger")
    assert not hasattr(cookie_module, "logger")
