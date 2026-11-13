"""Session-cookie utilities for the authentication API (P5.4.4).

This module is the *only* place that turns a session token into a cookie and
back out of one. It owns no policy of its own: every attribute comes from the
approved P5.4.2 configuration (:data:`src.config.SESSION_COOKIE`) and from the
environment-derived :attr:`src.config.AppSettings.session_cookie_secure`, so a
second, drifting copy of the cookie policy cannot exist.

Approved attributes (owner decision, P5.4.1/P5.4.2)
---------------------------------------------------

==================  =========================================================
Name                ``va_session``
Path                ``/api/v1`` (the API namespace only)
HttpOnly            ``True`` — script cannot read the credential
Secure              ``False`` in development/testing, ``True`` in
                    staging/production (``SECURE_COOKIE_ENVIRONMENTS``)
SameSite            ``Lax``
Max-Age             ``43,200`` seconds (12 h, the absolute session cap)
Domain              unset — host-only; there is no override
==================  =========================================================

The browser lifetime equals the server-side absolute lifetime, never the
sliding idle timeout: the cookie is a maximum age, not a renewal mechanism.
Starlette renders the header attribute ``SameSite=lax``; header attribute
values are case-insensitive (RFC 6265 §5.2), so this is the approved ``Lax``
policy, and the mapping is explicit rather than a lowercase of user input.

Security notes
--------------

* The raw token only ever travels as the cookie **value** in ``Set-Cookie``.
  Neither helper takes or returns a body, so a token can never be placed in
  JSON by accident; a future endpoint still has to keep it out of its payload.
* Nothing here logs. A cookie value is a bearer credential and must not reach
  logs, audit records, traces or error text.
* ``set_session_cookie`` refuses a token that fails the P5.3 structural check
  instead of writing an unusable credential into a browser.
* ``clear_session_cookie`` repeats the *same* name, path, Secure, HttpOnly and
  SameSite attributes (and no Domain). A deletion cookie with different
  attributes addresses a different cookie and would leave the live one behind.
  It expires at the Unix epoch with ``Max-Age=0``, so the removal does not
  depend on the client's clock being close to the server's.
"""

from __future__ import annotations

from starlette.responses import Response

from src.config import SESSION_COOKIE, AppSettings
from src.session_token import SessionTokenError, is_valid_session_token

__all__ = [
    "CLEAR_COOKIE_EXPIRES",
    "clear_session_cookie",
    "set_session_cookie",
]

#: Expiry written by the deletion cookie (the Unix epoch, unambiguous).
CLEAR_COOKIE_EXPIRES: str = "Thu, 01 Jan 1970 00:00:00 GMT"

#: The approved SameSite policy, mapped to the value Starlette renders.
_SAME_SITE_VALUES: dict[str, str] = {
    "Lax": "lax",
    "Strict": "strict",
    "None": "none",
}


def _same_site() -> str:
    """Return the configured SameSite policy in Starlette's spelling.

    ``SESSION_COOKIE`` only admits ``Lax``; the explicit table (rather than a
    ``.lower()`` of caller-supplied text) keeps the mapping a fixed decision and
    fails closed if the approved literal ever changes shape.
    """
    value = _SAME_SITE_VALUES.get(SESSION_COOKIE.same_site)
    if value is None:  # pragma: no cover - configuration refuses this first
        raise ValueError(
            f"unsupported SameSite policy {SESSION_COOKIE.same_site!r}"
        )
    return value


def _cookie_attributes(settings: AppSettings) -> dict[str, object]:
    """The attributes shared by setting and clearing (single source).

    Deliberately no ``domain`` key: Starlette defaults it to ``None`` and emits
    no ``Domain`` attribute, which is exactly the approved host-only policy.
    """
    return {
        "key": SESSION_COOKIE.name,
        "path": SESSION_COOKIE.path,
        "secure": settings.session_cookie_secure,
        "httponly": SESSION_COOKIE.http_only,
        "samesite": _same_site(),
    }


def set_session_cookie(
    response: Response, *, token: str, settings: AppSettings
) -> None:
    """Attach the session cookie for ``token`` to ``response``.

    The value is the raw token; only its digest is ever persisted server-side.
    ``max_age`` is the approved 12-hour maximum, never a sliding renewal.
    """
    if not is_valid_session_token(token):
        raise SessionTokenError(
            "refusing to set a session cookie from a malformed session token"
        )
    response.set_cookie(
        value=token,
        max_age=SESSION_COOKIE.max_age_seconds,
        **_cookie_attributes(settings),
    )


def clear_session_cookie(response: Response, *, settings: AppSettings) -> None:
    """Attach an expired session cookie that removes the live one.

    Same name/path/Secure/HttpOnly/SameSite as :func:`set_session_cookie`, no
    ``Domain``, empty value, ``Max-Age=0`` and an epoch ``Expires``.
    """
    response.set_cookie(
        value="",
        max_age=0,
        expires=CLEAR_COOKIE_EXPIRES,
        **_cookie_attributes(settings),
    )
