"""Server-generated request identifiers and request correlation (P5.4.3).

Every HTTP request handled by the FastAPI application receives exactly one
request ID, generated **inside** this process:

* :func:`new_request_id` returns a canonical lowercase UUIDv4 string from
  :func:`uuid.uuid4` (the OS CSPRNG). Nothing is derived from the caller.
* :class:`RequestIDMiddleware` generates the ID before routing, stores it in
  the ASGI ``scope["state"]`` (Starlette's request-scoped state) and in a
  :class:`~contextvars.ContextVar`, then adds it to the response as
  ``X-Request-ID``.

Why inbound IDs are not trusted
-------------------------------

A caller-supplied ``X-Request-ID`` is **never** read, validated, echoed or
reused: the header is generated fresh for every request and any value the
client sent is ignored. Trusting it would let an unauthenticated caller choose
the identifier that later appears in audit records and incident timelines —
either colliding with a genuine ID or planting misleading correlation data.
Proxy headers are not consulted for the same reason (no ``Forwarded`` /
``X-Requested-With`` / hop-by-hop source is treated as authoritative).

Access from handlers
--------------------

Routes take the FastAPI/Starlette ``Request`` and read ``request.state``
through :func:`request_id_for` (or call :func:`current_request_id` when no
``Request`` object is in scope). Both are request-scoped:

* ``request.state`` lives in that request's ASGI scope, and
* the ``ContextVar`` is set per asyncio task (and copied into Starlette's
  thread pool for synchronous endpoints), then reset in a ``finally``.

There is no module-level "current request" object, so concurrent requests
cannot overwrite or leak one another's ID.

Response coverage and its one limitation
----------------------------------------

Responses produced inside the application — including every handled error
(``HTTPException``, request validation, typed handlers) — leave through this
middleware and therefore carry the header. A truly unhandled exception is
converted into a response by Starlette's *outermost* ``ServerErrorMiddleware``,
which sits above user middleware; that response never passes back through
here. The application's ``Exception`` handler therefore attaches the header
itself from ``request.state`` (see :mod:`src.api`).

Logging
-------

Only the request ID is used for correlation. Nothing here logs request bodies,
headers, cookies, credentials, tokens, hashes or employee/biometric data.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "REQUEST_ID_HEADER",
    "RequestIDMiddleware",
    "current_request_id",
    "new_request_id",
    "request_id_for",
]

#: Canonical response header name (HTTP header names are case-insensitive).
REQUEST_ID_HEADER: str = "X-Request-ID"

#: The id for the request being handled in this task/thread context.
_request_id: ContextVar[str | None] = ContextVar(
    "attendance_request_id", default=None
)


def new_request_id() -> str:
    """Return a fresh, unguessable request ID (canonical UUIDv4 string)."""
    return str(uuid.uuid4())


def current_request_id() -> str | None:
    """The current request's ID, or ``None`` outside a request context."""
    return _request_id.get()


def request_id_for(request: Any) -> str | None:
    """Read the request ID from a Starlette/FastAPI ``Request``.

    Defensive by design: a non-request object (or a request built without the
    middleware) yields ``None`` rather than raising, so error handlers keep
    working in direct unit tests. The value stored by the middleware is always
    a validated UUID string.
    """
    state = getattr(request, "state", None)
    value = getattr(state, "request_id", None)
    return value if isinstance(value, str) and value else None


class RequestIDMiddleware:
    """Pure-ASGI middleware: generate an ID, publish it, echo it.

    Ordering guarantee: the ID exists in ``scope["state"]`` and in the context
    variable before the application (and therefore any route or dependency)
    runs, and the response header is written on ``http.response.start``, so it
    is present on success and on every error the application renders itself.

    Non-HTTP scopes (lifespan, websocket) pass through untouched — no request,
    no ID.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        token = _request_id.set(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # Assignment replaces any existing value, so a response that
                # (incorrectly) carried its own X-Request-ID is corrected
                # rather than duplicated.
                headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            _request_id.reset(token)
