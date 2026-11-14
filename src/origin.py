"""Strict ``Origin`` header parsing and exact allowlist matching (P5.4.5).

Login is a state-changing authentication endpoint, so it is protected by
same-origin validation rather than a token. This module implements the
*parsing* half of that rule: it accepts exactly one well-formed RFC 6454
origin and rejects everything else. Matching is an exact comparison against
the owner-approved allowlist (:data:`src.config.TRUSTED_ORIGINS`); there are
no wildcards, no suffix matching and no inference from request headers.

What counts as an origin
------------------------

An origin is ``scheme "://" host [":" port]`` and nothing else. The following
are all rejected, because a lenient parser would let an attacker smuggle a
different origin past a naive string comparison:

* anything that is not a string, is empty, or carries whitespace/controls;
* the literal ``null`` (sent for sandboxed or opaque contexts);
* schemes other than ``http``/``https`` (``file:``, ``chrome-extension:``,
  ``ftp:``, ``ws:`` …);
* user information (``https://user:pass@host``);
* any path other than ``/`` (``https://host/login``, ``https://host/../x``);
* query strings and fragments;
* bracketed-IPv6 mistakes, unbracketed IPv6, empty/invalid ports, port ``0``
  and ports outside ``1..65535``;
* hostnames with illegal labels or non-ASCII text (browsers send punycode).

Canonical form
--------------

Scheme and host are lowercased, the default port is dropped, and IPv6 hosts
are re-bracketed, so ``HTTPS://Example.COM:443`` and ``https://example.com``
compare equal. Only canonical origins are compared, which is why this module
returns a normalised string rather than a boolean for the caller to guess
with.

This module never reads a request, never logs and never falls back to a
permissive default: an empty allowlist matches nothing, so an environment
without a configured origin fails closed.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Final
from urllib.parse import urlsplit

__all__ = [
    "OriginSyntaxError",
    "normalize_origin",
    "origin_is_allowed",
]

#: The only schemes an approved dashboard origin may use.
_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Default port per scheme (omitted from the canonical form).
_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}

_HOST_LABEL: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_FORBIDDEN_CHARACTERS: Final[tuple[str, ...]] = tuple(
    character
    for character in (" ", "\t", "\r", "\n", "\x00", "\\", ",", ";", '"', "'")
)


class OriginSyntaxError(ValueError):
    """The value is not exactly one well-formed ``scheme://host[:port]``."""


def _split_host_and_port(netloc: str) -> tuple[str, str | None]:
    """Split a URL authority into host and optional port text (strictly)."""
    if netloc.startswith("["):
        end = netloc.find("]")
        if end == -1:
            raise OriginSyntaxError("an IPv6 host must be enclosed in brackets")
        host = netloc[1:end]
        remainder = netloc[end + 1 :]
        if remainder == "":
            return host, None
        if not remainder.startswith(":"):
            raise OriginSyntaxError("unexpected characters after the host")
        return host, remainder[1:]

    host, separator, port_text = netloc.partition(":")
    if separator and ":" in port_text:
        raise OriginSyntaxError("an IPv6 host must be enclosed in brackets")
    return host, (port_text if separator else None)


def _canonical_host(host: str) -> tuple[str, bool]:
    """Return the canonical host text and whether it is an IP literal."""
    if not host:
        raise OriginSyntaxError("an origin must name a host")
    if any(character in host for character in _FORBIDDEN_CHARACTERS):
        raise OriginSyntaxError("the host contains illegal characters")
    if "%" in host:
        # ``ipaddress`` accepts scoped IPv6 literals (``fe80::1%eth0``); an
        # origin never carries a zone identifier.
        raise OriginSyntaxError("IPv6 zone identifiers are not allowed")
    if not host.isascii():
        raise OriginSyntaxError("the host must be ASCII (punycode) text")

    candidate = host.lower()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        return str(address), True

    if candidate.endswith(".") or ".." in candidate:
        raise OriginSyntaxError("the host is not a valid hostname")
    labels = candidate.split(".")
    if not all(_HOST_LABEL.fullmatch(label) for label in labels):
        raise OriginSyntaxError("the host is not a valid hostname")
    return candidate, False


def normalize_origin(value: object) -> str:
    """Return the canonical ``scheme://host[:port]`` form of one origin.

    Raises :class:`OriginSyntaxError` for anything that is not exactly one
    well-formed origin. Surrounding whitespace is rejected rather than
    stripped: a header that arrives padded is not the origin it claims to be.
    """
    if not isinstance(value, str):
        raise OriginSyntaxError("an origin must be a string")
    if value == "":
        raise OriginSyntaxError("the origin is empty")
    if any(character in value for character in _FORBIDDEN_CHARACTERS):
        raise OriginSyntaxError("the origin contains whitespace or controls")
    if value == "null":
        raise OriginSyntaxError("the literal 'null' is not an origin")
    if "?" in value or "#" in value:
        # Checked before parsing: ``urlsplit`` reports an empty query/fragment
        # for a trailing ``?`` or ``#``, so a trailing delimiter would
        # otherwise survive as an accepted origin.
        raise OriginSyntaxError("an origin has no query or fragment")

    try:
        parts = urlsplit(value)
    except ValueError:
        # e.g. an unbalanced IPv6 bracket: rejected as malformed, never
        # propagated as a different exception type.
        raise OriginSyntaxError("the origin is not a valid URL") from None
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise OriginSyntaxError("the origin scheme must be http or https")
    if "@" in parts.netloc:
        raise OriginSyntaxError("user information is not part of an origin")
    if parts.query or parts.fragment:
        raise OriginSyntaxError("an origin has no query or fragment")
    if parts.path not in ("", "/"):
        raise OriginSyntaxError("an origin has no path")

    host, port_text = _split_host_and_port(parts.netloc)
    canonical_host, is_ip = _canonical_host(host)

    if port_text is None:
        port = _DEFAULT_PORTS[scheme]
    else:
        if not port_text.isdigit() or not port_text.isascii():
            raise OriginSyntaxError("the port must be a decimal number")
        port = int(port_text)
        if port < 1 or port > 65535:
            raise OriginSyntaxError("the port is outside 1..65535")

    rendered_host = f"[{canonical_host}]" if is_ip and ":" in canonical_host else canonical_host
    if port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{rendered_host}"
    return f"{scheme}://{rendered_host}:{port}"


def origin_is_allowed(origin: str, trusted_origins: tuple[str, ...]) -> bool:
    """Exact-match ``origin`` (already canonical) against an allowlist.

    An empty allowlist matches nothing — this is the fail-closed behaviour for
    staging/production until the owner supplies the exact dashboard origin.
    Unparseable allowlist entries are ignored rather than widened to a
    wildcard; configuration validation rejects them before this point.
    """
    for entry in trusted_origins:
        try:
            if normalize_origin(entry) == origin:
                return True
        except OriginSyntaxError:
            continue
    return False
