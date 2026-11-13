"""Offline tests for ``src.session_repository`` (SQL shape, params, errors)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.db_security_repository import open_session_repository
from src.session_repository import (
    SessionAlreadyExists,
    SessionNotFound,
    SessionUserNotFound,
)
from src.user_session import SessionValidationError
from tests.security_support import (
    CREATED,
    SESSION_HASH,
    FakeDriverError,
    FakeServer,
    make_session,
    session_row,
)


def build(rows=()):
    """A repository wired exactly as production uses it, over a fake server."""
    server = FakeServer(rows)
    return server, open_session_repository(connection_opener=server.connect)


# ---------------------------------------------------------------------------
# insert
# ---------------------------------------------------------------------------


def test_insert_persists_the_digest_and_deadlines_only() -> None:
    server, repository = build()
    session = make_session()

    assert repository.insert(session) is session
    assert "INSERT INTO user_session" in server.last_sql
    assert server.last_params == (
        SESSION_HASH,
        "u_0001",
        session.issued_at,
        session.expires_at,
        session.absolute_expires_at,
        None,
        session.last_seen_at,
    )
    # Nothing token-shaped beyond the digest is ever bound.
    assert not any(
        isinstance(value, str) and value.startswith("sess_")
        for value in server.last_params
    )
    assert server.connections[0].committed is True


def test_insert_duplicate_digest_maps_to_a_typed_error() -> None:
    server, repository = build()
    server.error = FakeDriverError("23505")
    with pytest.raises(SessionAlreadyExists) as excinfo:
        repository.insert(make_session())
    assert "driver detail leak" not in str(excinfo.value)


def test_insert_missing_user_maps_to_a_typed_error() -> None:
    server, repository = build()
    server.error = FakeDriverError("23503")
    with pytest.raises(SessionUserNotFound) as excinfo:
        repository.insert(make_session())
    assert "driver detail leak" not in str(excinfo.value)


def test_insert_other_failures_propagate_unchanged() -> None:
    server, repository = build()
    server.error = FakeDriverError("08006")
    with pytest.raises(FakeDriverError):
        repository.insert(make_session())


def test_insert_requires_a_validated_session() -> None:
    _, repository = build()
    with pytest.raises(SessionValidationError):
        repository.insert(object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


def test_find_by_token_hash_parses_the_row() -> None:
    server, repository = build([session_row()])
    session = repository.find_by_token_hash(SESSION_HASH)
    assert session is not None
    assert session.session_token_hash == SESSION_HASH
    assert session.user_id == "u_0001"
    assert session.revoked_at is None
    assert server.last_params == (SESSION_HASH,)


def test_find_returns_none_when_absent() -> None:
    _, repository = build()
    assert repository.find_by_token_hash(SESSION_HASH) is None


@pytest.mark.parametrize(
    "value", ["", "a" * 63, "A" * 64, "sess_123", None, 12345]
)
def test_find_refuses_anything_but_a_sha256_hex_digest(value) -> None:
    server, repository = build([session_row()])
    with pytest.raises(SessionValidationError):
        repository.find_by_token_hash(value)  # type: ignore[arg-type]
    assert server.calls == []


def test_find_does_not_interpret_expiry_or_revocation() -> None:
    """The repository reports stored facts; usability policy is P5.4's."""
    expired = session_row()
    server, repository = build([expired])
    session = repository.find_by_token_hash(SESSION_HASH)
    assert session is not None
    # No filtering clause exists in the query: every row is returned as stored.
    assert "expires_at" not in server.last_sql.split("WHERE")[-1]


def test_row_shape_mismatch_is_refused() -> None:
    _, repository = build([(SESSION_HASH, "u_0001")])
    with pytest.raises(RuntimeError):
        repository.find_by_token_hash(SESSION_HASH)


# ---------------------------------------------------------------------------
# touch
# ---------------------------------------------------------------------------


def test_touch_slides_the_idle_deadline_and_last_seen() -> None:
    seen = CREATED + timedelta(hours=1)
    deadline = CREATED + timedelta(hours=9)
    server, repository = build([session_row()])
    repository.touch(SESSION_HASH, last_seen_at=seen, expires_at=deadline)
    assert "SET last_seen_at = %s, expires_at = %s" in server.last_sql
    assert server.last_params == (seen, deadline, SESSION_HASH)


def test_touch_missing_session_raises_typed_error() -> None:
    _, repository = build([])
    with pytest.raises(SessionNotFound):
        repository.touch(SESSION_HASH, last_seen_at=CREATED, expires_at=CREATED)


@pytest.mark.parametrize("keyword", ["last_seen_at", "expires_at"])
def test_touch_requires_aware_timestamps(keyword: str) -> None:
    server, repository = build([session_row()])
    kwargs = {
        "last_seen_at": CREATED,
        "expires_at": CREATED + timedelta(hours=1),
    }
    kwargs[keyword] = datetime(2026, 11, 12, 9, 0)
    with pytest.raises(SessionValidationError):
        repository.touch(SESSION_HASH, **kwargs)  # type: ignore[arg-type]
    assert server.calls == []


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


def test_revoke_preserves_the_first_revocation_instant() -> None:
    revoked_at = CREATED + timedelta(minutes=30)
    server, repository = build([session_row(revoked_at=revoked_at)])
    session = repository.revoke(SESSION_HASH, revoked_at=revoked_at)
    assert session.revoked_at == revoked_at
    assert "COALESCE(revoked_at, %s)" in server.last_sql
    assert server.last_params == (revoked_at, SESSION_HASH)


def test_revoke_missing_session_raises_typed_error() -> None:
    _, repository = build([])
    with pytest.raises(SessionNotFound):
        repository.revoke(SESSION_HASH, revoked_at=CREATED)


def test_revoke_requires_an_aware_timestamp() -> None:
    server, repository = build([session_row()])
    with pytest.raises(SessionValidationError):
        repository.revoke(
            SESSION_HASH, revoked_at=datetime(2026, 11, 12, 9, 0)  # naive
        )
    assert server.calls == []


def test_revoke_all_for_user_reports_the_sessions_it_ended() -> None:
    rows = [(SESSION_HASH,), ("c" * 64,)]
    server, repository = build(rows)
    assert repository.revoke_all_for_user(" u_0001 ", revoked_at=CREATED) == 2
    assert "WHERE user_id = %s AND revoked_at IS NULL" in server.last_sql
    assert server.last_params == (CREATED, "u_0001")


def test_revoke_all_for_user_with_no_live_sessions_returns_zero() -> None:
    _, repository = build([])
    assert repository.revoke_all_for_user("u_0001", revoked_at=CREATED) == 0


def test_revoke_all_requires_a_user_id_and_aware_timestamp() -> None:
    server, repository = build()
    with pytest.raises(SessionValidationError):
        repository.revoke_all_for_user("  ", revoked_at=CREATED)
    with pytest.raises(SessionValidationError):
        repository.revoke_all_for_user("u_0001", revoked_at=datetime(2026, 11, 12))
    assert server.calls == []


def test_scope_commits_a_successful_read_and_closes_it() -> None:
    """The wiring's scope (not the repository class) owns the transaction."""
    server, repository = build([session_row()])
    repository.find_by_token_hash(SESSION_HASH)
    connection = server.connections[0]
    assert connection.committed is True
    assert connection.rolled_back is False
    assert connection.closed is True
