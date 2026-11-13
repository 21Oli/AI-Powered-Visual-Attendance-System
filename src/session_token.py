"""Opaque session-token generation and SHA-256 digesting (P5.3).

The token and the stored digest are deliberately separated:

* :func:`generate_session_token` produces 256 bits from the operating
  system CSPRNG (``secrets``) and encodes them as 43 unpadded base64url
  characters — URL- and header-safe, and exactly 32 decoded bytes.
* :func:`session_token_digest` hashes the token with SHA-256 and returns the
  lowercase 64-character hex digest that ``user_session.session_token_hash
  CHAR(64)`` stores. **Only the digest is persisted**; the raw token is
  returned once to the caller and never written, logged or re-derived.
* :func:`is_valid_session_token` is a strict structural check: exactly 43
  base64url characters that decode to exactly 32 bytes. Anything else —
  shorter attacker-chosen strings, padding, uppercase hex digests, a
  non-string — is refused before a repository call, so a malformed value
  cannot become a lookup key.

A SHA-256 digest is appropriate here (unlike for passwords) because the input
is already 256 bits of uniform randomness: there is no dictionary to attack,
and the digest keeps lookup a single indexed primary-key hit.

Nothing in this module logs, stores or returns a raw token through ``repr``;
the raw value is a plain ``str`` the caller must keep out of logs and storage.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import secrets

from src.config import SESSION_POLICY

__all__ = [
    "SESSION_TOKEN_DIGEST_LENGTH",
    "SESSION_TOKEN_LENGTH",
    "SessionTokenError",
    "generate_session_token",
    "is_valid_session_token",
    "session_token_digest",
]

#: Decoded token size in bytes (256 bits of entropy).
TOKEN_BYTE_LENGTH: int = SESSION_POLICY.token_entropy_bits // 8

#: Length of the base64url encoding without padding (43 for 32 bytes).
SESSION_TOKEN_LENGTH: int = 43

#: Length of the lowercase SHA-256 hex digest (matches ``CHAR(64)``).
SESSION_TOKEN_DIGEST_LENGTH: int = 64

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class SessionTokenError(ValueError):
    """A session token is malformed or could not be generated safely."""


def is_valid_session_token(value: object) -> bool:
    """Whether ``value`` is a structurally valid 256-bit session token."""
    if not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None:
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (binascii.Error, ValueError):
        return False
    return len(decoded) == TOKEN_BYTE_LENGTH


def generate_session_token() -> str:
    """Return a fresh 256-bit opaque token as 43 base64url characters.

    Uses :mod:`secrets` (the OS CSPRNG). The result is validated before it is
    returned, so an unexpected platform behaviour fails closed instead of
    issuing a weak token.
    """
    token = secrets.token_urlsafe(TOKEN_BYTE_LENGTH)
    if not is_valid_session_token(token):
        # Defensive: never hand back a token we cannot validate.
        raise SessionTokenError("generated session token failed validation")
    return token


def session_token_digest(token: str) -> str:
    """Return the SHA-256 hex digest stored for ``token``.

    The token is validated first, so a malformed value never becomes a
    database lookup key. The raw token is not retained, logged or returned in
    the digest's stead.
    """
    if not is_valid_session_token(token):
        raise SessionTokenError(
            "session token must be a 43-character base64url string encoding "
            "32 bytes"
        )
    return hashlib.sha256(token.encode("ascii")).hexdigest()
