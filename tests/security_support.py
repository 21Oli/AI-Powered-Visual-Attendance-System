"""Shared offline fakes for the Phase 5 identity repository tests.

A minimal psycopg-3-shaped "server": connections record every ``execute`` call
(SQL text plus bound parameters), return scripted rows, and expose
``commit``/``rollback``/``close``. Nothing here opens a socket, reads a DSN, or
touches a database.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from src.user import Role, User
from src.user_session import UserSession

CREATED = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
SESSION_HASH = "b" * 64


class FakeDriverError(Exception):
    """Driver exception carrying a SQLSTATE and a message that must not leak."""

    def __init__(self, sqlstate: str, message: str = "driver detail leak") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class FakeCursor:
    def __init__(self, rows: Sequence[Sequence[object]]) -> None:
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, server: "FakeServer") -> None:
        self._server = server
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, sql: str, params: tuple[object, ...]):
        self._server.calls.append((sql, params))
        if self._server.error is not None:
            raise self._server.error
        return FakeCursor(self._server.rows)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class FakeServer:
    """Hands out connections and records every statement it receives."""

    def __init__(self, rows: Sequence[Sequence[object]] = ()) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rows: list[Sequence[object]] = list(rows)
        self.error: Exception | None = None
        self.connections: list[FakeConnection] = []

    def connect(self, dsn: str | None = None) -> FakeConnection:
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    @property
    def last_sql(self) -> str:
        return self.calls[-1][0]

    @property
    def last_params(self) -> tuple[object, ...]:
        return self.calls[-1][1]


def user_row(
    user_id: str = "u_0001",
    username: str = "Ada",
    username_key: str = "ada",
    role: str = "Admin",
    is_active: bool = True,
) -> tuple[object, ...]:
    return (
        user_id,
        username,
        username_key,
        PASSWORD_HASH,
        role,
        is_active,
        CREATED,
        CREATED,
    )


def session_row(
    session_token_hash: str = SESSION_HASH,
    user_id: str = "u_0001",
    revoked_at: datetime | None = None,
) -> tuple[object, ...]:
    return (
        session_token_hash,
        user_id,
        CREATED,
        CREATED.replace(hour=17),
        CREATED.replace(hour=21),
        revoked_at,
        CREATED,
    )


def make_user(**overrides: object) -> User:
    fields: dict[str, object] = {
        "user_id": "u_0001",
        "username": "Ada",
        "password_hash": PASSWORD_HASH,
        "role": Role.ADMIN,
        "created_at": CREATED,
    }
    fields.update(overrides)
    return User(**fields)  # type: ignore[arg-type]


def make_session(user_id: str = "u_0001", **overrides: object) -> UserSession:
    fields: dict[str, object] = {
        "session_token_hash": SESSION_HASH,
        "user_id": user_id,
        "issued_at": CREATED,
        "expires_at": CREATED.replace(hour=17),
        "absolute_expires_at": CREATED.replace(hour=21),
    }
    fields.update(overrides)
    return UserSession(**fields)  # type: ignore[arg-type]
