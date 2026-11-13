"""Offline tests for the Phase 5 session model (``src.user_session``)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from src.user_session import (
    SESSION_TOKEN_HASH_LENGTH,
    UserSession,
    SessionValidationError,
    is_valid_session_token_hash,
)

HASH = "a" * 64
ISSUED = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
EXPIRES = ISSUED + timedelta(hours=8)
ABSOLUTE = ISSUED + timedelta(hours=12)


def make_session(**overrides: object) -> UserSession:
    fields: dict[str, object] = {
        "session_token_hash": HASH,
        "user_id": "u_0001",
        "issued_at": ISSUED,
        "expires_at": EXPIRES,
        "absolute_expires_at": ABSOLUTE,
    }
    fields.update(overrides)
    return UserSession(**fields)  # type: ignore[arg-type]


def test_valid_session_defaults_last_seen_to_issued() -> None:
    session = make_session()
    assert session.last_seen_at == ISSUED
    assert session.revoked_at is None
    assert SESSION_TOKEN_HASH_LENGTH == 64


def test_token_hash_must_be_a_64_character_lowercase_hex_digest() -> None:
    for value in ("a" * 63, "a" * 65, "A" * 64, "g" * 64, "", "not-a-hash"):
        with pytest.raises(SessionValidationError):
            make_session(session_token_hash=value)
    assert is_valid_session_token_hash(HASH) is True
    assert is_valid_session_token_hash("A" * 64) is False
    assert is_valid_session_token_hash(None) is False


def test_raw_tokens_cannot_be_mistaken_for_hashes() -> None:
    # A plausible opaque cookie value is not a SHA-256 hex digest.
    with pytest.raises(SessionValidationError):
        make_session(session_token_hash="sess_9f8e7d6c5b4a3210")


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(SessionValidationError):
        make_session(issued_at=datetime(2026, 11, 12, 9, 0))
    with pytest.raises(SessionValidationError):
        make_session(expires_at=datetime(2026, 11, 12, 17, 0))


def test_idle_deadline_cannot_exceed_the_absolute_cap() -> None:
    with pytest.raises(SessionValidationError):
        make_session(expires_at=ABSOLUTE + timedelta(seconds=1))


def test_idle_deadline_cannot_precede_issuance() -> None:
    with pytest.raises(SessionValidationError):
        make_session(expires_at=ISSUED - timedelta(seconds=1))


def test_last_seen_cannot_precede_issuance() -> None:
    with pytest.raises(SessionValidationError):
        make_session(last_seen_at=ISSUED - timedelta(seconds=1))


def test_revocation_timestamp_is_optional_but_must_be_aware() -> None:
    revoked = make_session(revoked_at=ISSUED + timedelta(minutes=5))
    assert revoked.revoked_at is not None
    with pytest.raises(SessionValidationError):
        make_session(revoked_at=datetime(2026, 11, 12, 9, 5))
    with pytest.raises(SessionValidationError):
        make_session(revoked_at=ISSUED - timedelta(seconds=1))


def test_blank_user_id_is_refused() -> None:
    with pytest.raises(SessionValidationError):
        make_session(user_id="   ")


def test_session_is_immutable() -> None:
    session = make_session()
    with pytest.raises(FrozenInstanceError):
        session.user_id = "u_0002"  # type: ignore[misc]


def test_repr_contains_no_token_beyond_the_stored_digest() -> None:
    session = make_session()
    assert "sess_" not in repr(session)
    assert session.session_token_hash == HASH
