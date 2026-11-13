"""Deterministic tests for opaque session tokens and their digests (P5.3)."""

from __future__ import annotations

import base64
import hashlib

import pytest

from src.session_token import (
    SESSION_TOKEN_DIGEST_LENGTH,
    SESSION_TOKEN_LENGTH,
    SessionTokenError,
    generate_session_token,
    is_valid_session_token,
    session_token_digest,
)
from src.user_session import is_valid_session_token_hash


def test_generated_token_has_the_approved_shape() -> None:
    token = generate_session_token()
    assert isinstance(token, str)
    assert len(token) == SESSION_TOKEN_LENGTH == 43
    assert is_valid_session_token(token) is True
    decoded = base64.urlsafe_b64decode(token + "=")
    assert len(decoded) == 32, "256 bits of entropy"


def test_generated_tokens_are_unique() -> None:
    tokens = {generate_session_token() for _ in range(200)}
    assert len(tokens) == 200


def test_tokens_avoid_padding_and_unsafe_characters() -> None:
    for _ in range(50):
        token = generate_session_token()
        assert "=" not in token
        assert "+" not in token and "/" not in token


def test_digest_is_sha256_hex_of_the_token() -> None:
    token = generate_session_token()
    digest = session_token_digest(token)
    assert digest == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert len(digest) == SESSION_TOKEN_DIGEST_LENGTH == 64
    assert digest == digest.lower()


def test_digest_is_accepted_by_the_v002_hash_validator() -> None:
    """The digest must fit ``user_session.session_token_hash CHAR(64)``."""
    assert is_valid_session_token_hash(session_token_digest(generate_session_token()))


def test_digest_is_stable_for_the_same_token() -> None:
    token = generate_session_token()
    assert session_token_digest(token) == session_token_digest(token)


def test_digest_differs_for_different_tokens() -> None:
    first, second = generate_session_token(), generate_session_token()
    assert session_token_digest(first) != session_token_digest(second)


@pytest.mark.parametrize(
    "bad_token",
    [
        "",
        "a" * 42,
        "a" * 44,
        "a" * 43 + "=",
        "a" * 42 + "=",
        " " + "a" * 42,
        "a" * 42 + "\n",
        "a" * 21 + "+" + "a" * 21,
        "a" * 21 + "/" + "a" * 21,
        "ä" * 43,
        None,
        12345,
        b"a" * 43,
    ],
)
def test_invalid_tokens_are_refused_before_hashing(bad_token: object) -> None:
    assert is_valid_session_token(bad_token) is False
    with pytest.raises(SessionTokenError):
        session_token_digest(bad_token)  # type: ignore[arg-type]


def test_hex_digest_is_not_mistaken_for_a_token() -> None:
    digest = session_token_digest(generate_session_token())
    assert is_valid_session_token(digest) is False
    assert SESSION_TOKEN_DIGEST_LENGTH != SESSION_TOKEN_LENGTH


def test_a_valid_token_with_a_padded_variant_is_rejected() -> None:
    token = generate_session_token()
    assert is_valid_session_token(token + "=") is False
    assert is_valid_session_token(token[:-1]) is False
