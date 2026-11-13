"""Offline tests for ``src.db_security_repository`` (connection lifecycle).

The fake server stands in for psycopg: no connection is opened, no DSN is read
from the environment, and no statement reaches PostgreSQL.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.db_security_repository import (
    open_session_repository,
    open_user_repository,
)
from src.session_repository import SessionRepository
from src.user_repository import UserRepository
from tests.security_support import FakeServer, user_row

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_open_user_repository_returns_the_approved_repository() -> None:
    assert isinstance(open_user_repository(), UserRepository)


def test_open_session_repository_returns_the_approved_repository() -> None:
    assert isinstance(open_session_repository(), SessionRepository)


def test_success_path_commits_and_closes() -> None:
    server = FakeServer([user_row()])
    repository = open_user_repository(connection_opener=server.connect)

    assert repository.get("u_0001") is not None
    connection = server.connections[0]
    assert connection.committed is True
    assert connection.rolled_back is False
    assert connection.closed is True


def test_failure_rolls_back_closes_and_propagates_unchanged() -> None:
    server = FakeServer()
    server.error = RuntimeError("boom")
    repository = open_user_repository(connection_opener=server.connect)

    with pytest.raises(RuntimeError, match="boom"):
        repository.get("u_0001")
    connection = server.connections[0]
    assert connection.committed is False
    assert connection.rolled_back is True
    assert connection.closed is True


def test_rollback_failure_never_masks_the_original_error() -> None:
    server = FakeServer()
    server.error = RuntimeError("primary failure")
    repository = open_user_repository(connection_opener=server.connect)

    def exploding_rollback() -> None:
        raise RuntimeError("rollback failure")

    original_connect = server.connect

    def connect(dsn: str | None = None):
        connection = original_connect(dsn)
        connection.rollback = exploding_rollback  # type: ignore[method-assign]
        return connection

    with pytest.raises(RuntimeError, match="primary failure"):
        open_user_repository(connection_opener=connect).get("u_0001")


def test_close_failure_is_attached_as_a_note_not_raised_over_the_cause() -> None:
    server = FakeServer()
    server.error = RuntimeError("primary failure")

    def connect(dsn: str | None = None):
        connection = server.connect(dsn)

        def exploding_close() -> None:
            raise RuntimeError("close failure")

        connection.close = exploding_close  # type: ignore[method-assign]
        return connection

    repository = open_user_repository(connection_opener=connect)
    with pytest.raises(RuntimeError, match="primary failure") as excinfo:
        repository.get("u_0001")
    assert any("closing the connection also failed" in note for note in excinfo.value.__notes__)


def test_dsn_is_passed_through_to_the_opener() -> None:
    seen: list[str | None] = []
    server = FakeServer([user_row()])

    def connect(dsn: str | None = None):
        seen.append(dsn)
        return server.connect(dsn)

    open_user_repository("postgresql:///example", connection_opener=connect).get(
        "u_0001"
    )
    assert seen == ["postgresql:///example"]


def test_wiring_does_not_request_read_only_mode() -> None:
    """Identity repositories are read/write; no dry-run flag exists here."""
    server = FakeServer([user_row()])
    open_user_repository(connection_opener=server.connect).get("u_0001")
    connection = server.connections[0]
    assert not hasattr(connection, "read_only")


def test_module_import_is_driver_free_in_a_fresh_process() -> None:
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "import src.db_security_repository;"
        "banned = {'psycopg', 'src.db'} & set(sys.modules);"
        "assert not banned, banned;"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
