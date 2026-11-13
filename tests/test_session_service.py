"""Deterministic tests for the session lifecycle (P5.3).

Everything is offline: the repositories are in-memory doubles with the same
method surface as the P5.2 repositories, and time comes from an injected
clock that only moves when a test moves it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from src.config import SESSION_POLICY
from src.session_repository import (
    SessionAlreadyExists,
    SessionNotFound,
    SessionUserNotFound,
)
from src.session_service import (
    AuthenticatedSession,
    ClockError,
    InvalidSessionToken,
    IssuedSession,
    SessionExpired,
    SessionIssueError,
    SessionRevoked,
    SessionService,
    UserInactive,
    UserUnknown,
)
from src.session_token import generate_session_token, session_token_digest
from src.user import Role, User
from src.user_session import UserSession

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
IDLE = timedelta(hours=8)
ABSOLUTE = timedelta(hours=12)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class MutableClock:
    """A clock that only advances when the test says so."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta

    def set(self, value: datetime) -> None:
        self.now = value


class FakeUserRepository:
    """Minimal ``get``-only stand-in for :class:`UserRepository`."""

    def __init__(self, users: list[User] | None = None) -> None:
        self.users = {user.user_id: user for user in users or []}
        self.get_calls: list[str] = []
        self.failure: BaseException | None = None

    def get(self, user_id: str) -> User | None:
        self.get_calls.append(user_id)
        if self.failure is not None:
            raise self.failure
        return self.users.get(user_id)

    def deactivate(self, user_id: str) -> None:
        self.users[user_id] = replace(self.users[user_id], is_active=False)

    def remove(self, user_id: str) -> None:
        del self.users[user_id]


class FakeSessionRepository:
    """In-memory ``user_session`` table with recording and fault injection."""

    def __init__(self) -> None:
        self.rows: dict[str, UserSession] = {}
        self.insert_calls: list[UserSession] = []
        self.find_calls: list[str] = []
        self.touch_calls: list[tuple[str, datetime, datetime]] = []
        self.revoke_calls: list[tuple[str, datetime]] = []
        self.insert_failure: BaseException | None = None
        self.find_failure: BaseException | None = None
        self.touch_failure: BaseException | None = None
        self.drop_before_touch = False

    def insert(self, session: UserSession) -> UserSession:
        self.insert_calls.append(session)
        if self.insert_failure is not None:
            raise self.insert_failure
        if session.session_token_hash in self.rows:
            raise SessionAlreadyExists("duplicate digest")
        self.rows[session.session_token_hash] = session
        return session

    def find_by_token_hash(self, session_token_hash: str) -> UserSession | None:
        self.find_calls.append(session_token_hash)
        if self.find_failure is not None:
            raise self.find_failure
        return self.rows.get(session_token_hash)

    def touch(
        self,
        session_token_hash: str,
        *,
        last_seen_at: datetime,
        expires_at: datetime,
    ) -> UserSession:
        self.touch_calls.append((session_token_hash, last_seen_at, expires_at))
        if self.touch_failure is not None:
            raise self.touch_failure
        stored = self.rows.get(session_token_hash)
        if stored is None or self.drop_before_touch:
            raise SessionNotFound("gone")
        updated = replace(
            stored, last_seen_at=last_seen_at, expires_at=expires_at
        )
        self.rows[session_token_hash] = updated
        return updated

    def revoke(self, session_token_hash: str, *, revoked_at: datetime) -> UserSession:
        self.revoke_calls.append((session_token_hash, revoked_at))
        stored = self.rows.get(session_token_hash)
        if stored is None:
            raise SessionNotFound("not found")
        updated = replace(stored, revoked_at=stored.revoked_at or revoked_at)
        self.rows[session_token_hash] = updated
        return updated

    def revoke_all_for_user(self, user_id: str, *, revoked_at: datetime) -> int:
        changed = 0
        for digest, session in list(self.rows.items()):
            if session.user_id == user_id and session.revoked_at is None:
                self.rows[digest] = replace(session, revoked_at=revoked_at)
                changed += 1
        return changed


def make_user(
    *,
    user_id: str = "user-1",
    username: str = "ada",
    role: Role = Role.OPERATOR,
    is_active: bool = True,
) -> User:
    return User(
        user_id=user_id,
        username=username,
        password_hash="$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA",
        role=role,
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


def build(
    *,
    users: list[User] | None = None,
    clock: MutableClock | None = None,
) -> tuple[SessionService, FakeUserRepository, FakeSessionRepository, MutableClock]:
    user_repository = FakeUserRepository(users or [make_user()])
    session_repository = FakeSessionRepository()
    test_clock = clock or MutableClock()
    service = SessionService(
        users=user_repository,  # type: ignore[arg-type]
        sessions=session_repository,  # type: ignore[arg-type]
        clock=test_clock,
    )
    return service, user_repository, session_repository, test_clock


# ---------------------------------------------------------------------------
# Approved policy
# ---------------------------------------------------------------------------


def test_policy_matches_the_owner_decision() -> None:
    assert SESSION_POLICY.idle_timeout == timedelta(hours=8)
    assert SESSION_POLICY.absolute_lifetime == timedelta(hours=12)
    assert SESSION_POLICY.token_entropy_bits == 256


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------


def test_issue_persists_only_the_digest() -> None:
    service, _, sessions, _ = build()
    issued = service.issue_session("user-1")

    assert isinstance(issued, IssuedSession)
    digest = session_token_digest(issued.token)
    assert issued.session.session_token_hash == digest
    assert list(sessions.rows) == [digest]
    assert issued.token not in sessions.rows
    assert issued.token not in repr(issued)


def test_issue_applies_the_approved_windows() -> None:
    service, _, _, _ = build()
    issued = service.issue_session("user-1")
    session = issued.session

    assert session.issued_at == NOW
    assert session.last_seen_at == NOW
    assert session.expires_at == NOW + IDLE
    assert session.absolute_expires_at == NOW + ABSOLUTE
    assert session.revoked_at is None
    assert session.user_id == "user-1"


def test_issue_trims_the_supplied_user_id() -> None:
    service, users, _, _ = build()
    issued = service.issue_session("  user-1  ")
    assert users.get_calls == ["user-1"]
    assert issued.session.user_id == "user-1"


def test_issue_refuses_an_unknown_user() -> None:
    service, _, sessions, _ = build()
    with pytest.raises(UserUnknown):
        service.issue_session("ghost")
    assert sessions.insert_calls == []


def test_issue_refuses_an_inactive_user() -> None:
    service, _, sessions, _ = build(users=[make_user(is_active=False)])
    with pytest.raises(UserInactive):
        service.issue_session("user-1")
    assert sessions.insert_calls == []


@pytest.mark.parametrize("bad_id", ["", "   ", None, 42, b"user-1"])
def test_issue_refuses_a_non_string_user_id(bad_id: object) -> None:
    service, _, sessions, _ = build()
    with pytest.raises(ValueError):
        service.issue_session(bad_id)  # type: ignore[arg-type]
    assert sessions.insert_calls == []


def test_issue_fails_closed_on_a_digest_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, sessions, _ = build()
    fixed = generate_session_token()
    monkeypatch.setattr(
        "src.session_service.generate_session_token", lambda: fixed
    )

    service.issue_session("user-1")
    with pytest.raises(SessionIssueError):
        service.issue_session("user-1")
    assert len(sessions.rows) == 1


def test_issue_maps_a_missing_owner_to_user_unknown() -> None:
    service, _, sessions, _ = build()
    sessions.insert_failure = SessionUserNotFound("fk")
    with pytest.raises(UserUnknown):
        service.issue_session("user-1")


def test_issue_lets_unexpected_storage_failures_propagate() -> None:
    service, _, sessions, _ = build()
    sessions.insert_failure = RuntimeError("connection lost")
    with pytest.raises(RuntimeError):
        service.issue_session("user-1")
    assert sessions.rows == {}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_returns_the_owner_and_records_activity() -> None:
    service, _, sessions, clock = build()
    issued = service.issue_session("user-1")

    clock.advance(timedelta(hours=1))
    authenticated = service.validate_session(issued.token)

    assert isinstance(authenticated, AuthenticatedSession)
    assert authenticated.user.user_id == "user-1"
    assert authenticated.session.last_seen_at == NOW + timedelta(hours=1)
    digest = session_token_digest(issued.token)
    assert sessions.touch_calls == [
        (digest, NOW + timedelta(hours=1), NOW + timedelta(hours=9))
    ]


def test_activity_slides_the_idle_deadline() -> None:
    service, _, _, clock = build()
    issued = service.issue_session("user-1")

    clock.advance(timedelta(hours=1))
    assert service.validate_session(issued.token).session.expires_at == NOW + timedelta(hours=9)
    clock.advance(timedelta(hours=1))
    assert service.validate_session(issued.token).session.expires_at == NOW + timedelta(hours=10)


def test_activity_cannot_extend_the_absolute_cap() -> None:
    service, _, sessions, clock = build()
    issued = service.issue_session("user-1")

    clock.advance(timedelta(hours=5))
    authenticated = service.validate_session(issued.token)

    assert authenticated.session.expires_at == NOW + ABSOLUTE
    assert authenticated.session.absolute_expires_at == NOW + ABSOLUTE
    digest = session_token_digest(issued.token)
    assert sessions.rows[digest].expires_at == NOW + ABSOLUTE


def test_idle_expiry_is_rejected_at_the_boundary() -> None:
    service, _, sessions, clock = build()
    issued = service.issue_session("user-1")

    clock.advance(IDLE)
    with pytest.raises(SessionExpired):
        service.validate_session(issued.token)
    assert sessions.touch_calls == []


def test_absolute_cap_expires_an_otherwise_fresh_session() -> None:
    service, _, sessions, clock = build()
    issued = service.issue_session("user-1")

    clock.advance(timedelta(hours=6))
    service.validate_session(issued.token)  # idle deadline moved to the cap
    touches = len(sessions.touch_calls)

    clock.set(NOW + ABSOLUTE)
    with pytest.raises(SessionExpired):
        service.validate_session(issued.token)
    assert len(sessions.touch_calls) == touches


def test_revoked_session_is_rejected() -> None:
    service, _, sessions, clock = build()
    issued = service.issue_session("user-1")
    service.revoke_session(issued.token)

    clock.advance(timedelta(minutes=1))
    with pytest.raises(SessionRevoked):
        service.validate_session(issued.token)
    assert sessions.touch_calls == []


def test_deactivated_user_cannot_use_a_live_session() -> None:
    service, users, sessions, _ = build()
    issued = service.issue_session("user-1")
    users.deactivate("user-1")

    with pytest.raises(UserInactive):
        service.validate_session(issued.token)
    assert sessions.touch_calls == []


def test_deleted_user_cannot_use_a_live_session() -> None:
    service, users, _, _ = build()
    issued = service.issue_session("user-1")
    users.remove("user-1")

    with pytest.raises(UserUnknown):
        service.validate_session(issued.token)


def test_unknown_token_is_rejected_with_one_lookup() -> None:
    service, _, sessions, _ = build()
    with pytest.raises(InvalidSessionToken):
        service.validate_session(generate_session_token())
    assert len(sessions.find_calls) == 1
    assert sessions.touch_calls == []


@pytest.mark.parametrize("bad_token", ["", "short", "a" * 42, "x" * 44, None, 12])
def test_malformed_token_is_rejected_without_io(bad_token: object) -> None:
    service, _, sessions, _ = build()
    with pytest.raises(InvalidSessionToken):
        service.validate_session(bad_token)  # type: ignore[arg-type]
    assert sessions.find_calls == []


def test_only_the_digest_is_given_to_the_repository() -> None:
    service, _, sessions, clock = build()
    issued = service.issue_session("user-1")
    digest = session_token_digest(issued.token)
    clock.advance(timedelta(minutes=5))
    service.validate_session(issued.token)
    service.revoke_session(issued.token)

    seen = (
        [s.session_token_hash for s in sessions.insert_calls]
        + sessions.find_calls
        + [call[0] for call in sessions.touch_calls]
        + [call[0] for call in sessions.revoke_calls]
    )
    assert seen and all(value == digest for value in seen)
    assert issued.token not in seen


def test_touch_losing_the_row_fails_closed() -> None:
    service, _, sessions, _ = build()
    issued = service.issue_session("user-1")
    sessions.drop_before_touch = True

    with pytest.raises(InvalidSessionToken):
        service.validate_session(issued.token)


def test_unexpected_lookup_failure_propagates() -> None:
    service, _, sessions, _ = build()
    issued = service.issue_session("user-1")
    sessions.find_failure = RuntimeError("database unavailable")

    with pytest.raises(RuntimeError):
        service.validate_session(issued.token)


# ---------------------------------------------------------------------------
# Clock handling
# ---------------------------------------------------------------------------


def test_naive_clock_is_refused() -> None:
    naive = MutableClock(datetime(2026, 11, 13, 9, 0))
    service, _, sessions, _ = build(clock=naive)
    with pytest.raises(ClockError):
        service.issue_session("user-1")
    assert sessions.insert_calls == []


def test_non_datetime_clock_is_refused() -> None:
    class BrokenClock:
        def __call__(self) -> object:
            return 1234567890

    service = SessionService(
        users=FakeUserRepository([make_user()]),  # type: ignore[arg-type]
        sessions=FakeSessionRepository(),  # type: ignore[arg-type]
        clock=BrokenClock(),  # type: ignore[arg-type]
    )
    with pytest.raises(ClockError):
        service.issue_session("user-1")


def test_clock_earlier_than_issuance_fails_closed() -> None:
    service, _, _, clock = build()
    issued = service.issue_session("user-1")
    clock.set(NOW - timedelta(seconds=1))

    with pytest.raises(InvalidSessionToken):
        service.validate_session(issued.token)


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def test_revoke_session_uses_the_digest_and_the_clock() -> None:
    service, _, sessions, clock = build()
    issued = service.issue_session("user-1")

    clock.advance(timedelta(minutes=2))
    revoked = service.revoke_session(issued.token)

    assert revoked.revoked_at == NOW + timedelta(minutes=2)
    assert sessions.revoke_calls == [
        (session_token_digest(issued.token), NOW + timedelta(minutes=2))
    ]


def test_revoke_is_idempotent_on_the_first_instant() -> None:
    service, _, _, clock = build()
    issued = service.issue_session("user-1")
    first = service.revoke_session(issued.token)
    clock.advance(timedelta(minutes=10))
    second = service.revoke_session(issued.token)
    assert first.revoked_at == second.revoked_at


def test_revoke_unknown_token_propagates_not_found() -> None:
    service, _, _, _ = build()
    with pytest.raises(SessionNotFound):
        service.revoke_session(generate_session_token())


def test_revoke_malformed_token_is_rejected_without_io() -> None:
    service, _, sessions, _ = build()
    with pytest.raises(InvalidSessionToken):
        service.revoke_session("nope")
    assert sessions.revoke_calls == []


def test_revoke_all_ends_every_live_session_for_one_user(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, sessions, _ = build(users=[make_user(), make_user(user_id="user-2", username="grace")])
    tokens = iter(generate_session_token() for _ in range(3))
    monkeypatch.setattr("src.session_service.generate_session_token", lambda: next(tokens))

    service.issue_session("user-1")
    service.issue_session("user-1")
    service.issue_session("user-2")

    assert service.revoke_all_for_user("user-1") == 2
    assert service.revoke_all_for_user("user-1") == 0
    live = [row for row in sessions.rows.values() if row.revoked_at is None]
    assert [row.user_id for row in live] == ["user-2"]


def test_revoke_all_refuses_a_blank_user_id() -> None:
    service, _, _, _ = build()
    with pytest.raises(ValueError):
        service.revoke_all_for_user("  ")
