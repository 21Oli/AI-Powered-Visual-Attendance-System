"""P5.4.3 request-ID and correlation tests (offline, pure ASGI).

The application is driven directly through its ASGI interface — no port, no
server process, no HTTP client dependency. That keeps the suite deterministic
and lets concurrency be exercised inside one event loop.

Nothing here connects to PostgreSQL, reads a DSN, touches a camera or handles
employee/biometric data.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import src.api as api_module
from src.api import create_app, handle_unexpected_error
from src.api_request_context import (
    REQUEST_ID_HEADER,
    RequestIDMiddleware,
    current_request_id,
    new_request_id,
    request_id_for,
)
from src.db import DatabaseUnavailableError

# The one fake sensitive value used by the sanitization assertions.
_FAKE_SECRET = "postgresql://u:leak-password@10.0.0.9:5432/secretdb"


# ---------------------------------------------------------------------------
# ASGI driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AsgiResponse:
    """The response messages produced by one ASGI call."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    raised: BaseException | None = None

    def header(self, name: str) -> str | None:
        values = self.header_values(name)
        return values[0] if values else None

    def header_values(self, name: str) -> list[str]:
        wanted = name.lower()
        return [value for key, value in self.headers if key.lower() == wanted]

    @property
    def payload(self) -> dict:
        return json.loads(self.body)


def _scope(
    path: str = "/api/v1/health",
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> dict:
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8000),
    }


async def _call(
    app, scope: dict, *, swallow: bool = False
) -> AsgiResponse:
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    raised: BaseException | None = None
    try:
        await app(scope, receive, send)
    except BaseException as exc:  # noqa: BLE001 - recorded, then optionally re-raised
        raised = exc
        if not swallow:
            raise

    start = next(
        message
        for message in messages
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    headers = tuple(
        (key.decode("latin-1"), value.decode("latin-1"))
        for key, value in start["headers"]
    )
    return AsgiResponse(start["status"], headers, body, raised)


def request(
    app,
    path: str = "/api/v1/health",
    *,
    headers: dict[str, str] | None = None,
    swallow: bool = False,
) -> AsgiResponse:
    """Perform one ASGI request and return the rendered response."""
    return asyncio.run(_call(app, _scope(path, headers=headers), swallow=swallow))


def _assert_uuid4(value: str) -> uuid.UUID:
    parsed = uuid.UUID(value)
    assert parsed.version == 4
    assert str(parsed) == value, "canonical lowercase UUID form"
    return parsed


# ---------------------------------------------------------------------------
# Test applications
# ---------------------------------------------------------------------------


def _probe_app() -> FastAPI:
    """A minimal app whose only job is to expose the request ID it received."""
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/probe")
    async def probe(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "context": current_request_id(),
                "state": request.state.request_id,
                "helper": request_id_for(request),
            }
        )

    @app.get("/probe-sync")
    def probe_sync(request: Request) -> JSONResponse:
        return JSONResponse({"context": current_request_id(), "state": request.state.request_id})

    @app.get("/spoofed-header")
    async def spoofed_header() -> JSONResponse:
        return JSONResponse({"ok": True}, headers={REQUEST_ID_HEADER: "spoofed"})

    return app


def _failing_app() -> FastAPI:
    """The production composition for the unexpected-error path."""
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    app.add_exception_handler(Exception, handle_unexpected_error)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(f"boom with {_FAKE_SECRET}")

    return app


# ---------------------------------------------------------------------------
# 1-3. Generation, uniqueness and format
# ---------------------------------------------------------------------------


def test_every_request_receives_a_server_request_id() -> None:
    response = request(create_app())
    assert response.status == 200
    value = response.header(REQUEST_ID_HEADER)
    assert value is not None
    _assert_uuid4(value)


def test_two_requests_receive_different_ids() -> None:
    app = create_app()
    first = request(app).header(REQUEST_ID_HEADER)
    second = request(app).header(REQUEST_ID_HEADER)
    assert first is not None and second is not None
    assert first != second


def test_generated_id_is_a_valid_uuid() -> None:
    value = new_request_id()
    assert isinstance(value, str)
    assert len(value) == 36
    _assert_uuid4(value)


def test_request_ids_are_unique_across_many_generations() -> None:
    values = {new_request_id() for _ in range(200)}
    assert len(values) == 200
    for value in values:
        _assert_uuid4(value)


# ---------------------------------------------------------------------------
# 4-5. Caller-supplied IDs are ignored, response carries the server ID
# ---------------------------------------------------------------------------


def test_caller_supplied_request_id_is_ignored() -> None:
    supplied = "00000000-0000-4000-8000-000000000000"
    response = request(
        create_app(),
        headers={REQUEST_ID_HEADER: supplied, "X-Correlation-ID": supplied},
    )
    value = response.header(REQUEST_ID_HEADER)
    assert value is not None
    assert value != supplied
    _assert_uuid4(value)
    assert supplied not in response.body.decode("utf-8")
    assert supplied not in repr(response.headers)


def test_response_carries_exactly_one_request_id_header() -> None:
    response = request(create_app())
    assert len(response.header_values(REQUEST_ID_HEADER)) == 1


def test_a_spoofed_response_header_is_replaced_not_duplicated() -> None:
    response = request(_probe_app(), "/spoofed-header")
    values = response.header_values(REQUEST_ID_HEADER)
    assert len(values) == 1
    assert values[0] != "spoofed"
    _assert_uuid4(values[0])


# ---------------------------------------------------------------------------
# 6. Route handlers can read the generated ID without generating one
# ---------------------------------------------------------------------------


def test_async_route_handlers_see_the_same_id_as_the_header() -> None:
    response = request(_probe_app(), "/probe")
    header = response.header(REQUEST_ID_HEADER)
    payload = response.payload
    assert header is not None
    assert payload["context"] == header
    assert payload["state"] == header
    assert payload["helper"] == header


def test_sync_route_handlers_see_the_same_id_as_the_header() -> None:
    """The context variable is copied into Starlette's thread pool."""
    response = request(_probe_app(), "/probe-sync")
    header = response.header(REQUEST_ID_HEADER)
    payload = response.payload
    assert header is not None
    assert payload["context"] == header
    assert payload["state"] == header


def test_request_id_for_tolerates_a_non_request_object() -> None:
    assert request_id_for(object()) is None


def test_context_is_cleared_after_the_request() -> None:
    request(_probe_app(), "/probe")
    assert current_request_id() is None


# ---------------------------------------------------------------------------
# 7-8. Error responses carry the ID
# ---------------------------------------------------------------------------


def test_handled_http_error_carries_the_request_id() -> None:
    response = request(create_app(), "/api/v1/not-a-route")
    assert response.status == 404
    assert _assert_uuid4(response.header(REQUEST_ID_HEADER) or "")


def test_not_found_envelope_is_unchanged() -> None:
    response = request(create_app(), "/api/v1/not-a-route")
    payload = response.payload
    assert set(payload) == {"error"}
    assert payload["error"]["code"] == "HTTP_ERROR"
    assert payload["error"]["message"] == "Not Found."
    assert b"Traceback" not in response.body


def test_unhandled_exception_response_carries_the_request_id() -> None:
    response = request(_failing_app(), "/boom", swallow=True)
    # Starlette's ServerErrorMiddleware renders the response and re-raises for
    # the server's own logging; the rendered response is what a client sees.
    assert response.raised is not None
    assert response.status == 500
    header = response.header(REQUEST_ID_HEADER)
    assert header is not None
    _assert_uuid4(header)


def test_unhandled_exception_envelope_stays_sanitized() -> None:
    response = request(_failing_app(), "/boom", swallow=True)
    payload = response.payload
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["message"] == "Internal server error."
    text = response.body.decode("utf-8")
    assert _FAKE_SECRET not in text
    assert "leak-password" not in text
    assert "Traceback" not in text


# ---------------------------------------------------------------------------
# 9. Concurrency isolation
# ---------------------------------------------------------------------------


def test_concurrent_requests_do_not_share_request_ids() -> None:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/slow")
    async def slow(request: Request) -> JSONResponse:
        before = current_request_id()
        await asyncio.sleep(0.005)  # force interleaving between requests
        after = current_request_id()
        return JSONResponse(
            {
                "before": before,
                "after": after,
                "state": request.state.request_id,
            }
        )

    async def run_them() -> list[AsgiResponse]:
        return await asyncio.gather(
            *(_call(app, _scope("/slow")) for _ in range(5))
        )

    responses = asyncio.run(run_them())
    ids = [response.header(REQUEST_ID_HEADER) for response in responses]

    assert len(set(ids)) == 5, "each concurrent request has its own ID"
    for response, request_id in zip(responses, ids):
        assert request_id is not None
        _assert_uuid4(request_id)
        payload = response.payload
        assert payload["before"] == request_id
        assert payload["after"] == request_id
        assert payload["state"] == request_id


# ---------------------------------------------------------------------------
# Middleware wiring and scope handling
# ---------------------------------------------------------------------------


def test_non_http_scopes_are_passed_through_without_an_id() -> None:
    seen: list[dict] = []

    async def app(scope, receive, send) -> None:
        seen.append(scope)

    async def receive() -> dict:  # pragma: no cover - unused
        return {"type": "lifespan.startup"}

    async def send(message: dict) -> None:  # pragma: no cover - unused
        return None

    middleware = RequestIDMiddleware(app)
    asyncio.run(
        middleware({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    )
    assert len(seen) == 1
    assert "state" not in seen[0]


def test_create_app_installs_exactly_one_request_id_middleware() -> None:
    app = create_app()
    assert [middleware.cls for middleware in app.user_middleware] == [
        RequestIDMiddleware
    ]
    assert app.exception_handlers[Exception] is handle_unexpected_error


def test_error_handlers_log_the_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        api_module._logger,
        "warning",
        lambda message, *args: captured.append((message, args)),
    )
    request_id = new_request_id()
    request_object = Request(_scope("/api/v1/employees"))
    request_object.scope["state"] = {"request_id": request_id}

    response = api_module.handle_database_unavailable(
        request_object, DatabaseUnavailableError("postgres unreachable")
    )

    assert response.status_code == 503
    assert captured, "the refusal must be logged"
    message, args = captured[0]
    assert "request_id" in message
    assert args[-1] == request_id
    assert "postgres unreachable" not in message


def test_unexpected_error_handler_adds_the_header_from_request_state() -> None:
    request_id = new_request_id()
    request_object = Request(_scope("/api/v1/employees"))
    request_object.scope["state"] = {"request_id": request_id}

    response = handle_unexpected_error(request_object, RuntimeError(_FAKE_SECRET))

    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == request_id
    assert _FAKE_SECRET.encode() not in response.body
