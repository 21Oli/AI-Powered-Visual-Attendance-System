"""Offline tests for ``src.user_repository`` (SQL shape, params, errors).

Every case drives a fake psycopg-3-shaped server: no PostgreSQL connection, no
DSN, no environment read, nothing under ``data/`` touched.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.config import USER_PAGINATION
from src.db_security_repository import open_user_repository
from src.user import Role, UserValidationError
from src.user_repository import (
    UserAlreadyExists,
    UserNotFound,
)
from tests.security_support import (
    CREATED,
    FakeDriverError,
    FakeServer,
    make_user,
    user_row,
)


def build(rows=()):
    """A repository wired exactly as production uses it, over a fake server."""
    server = FakeServer(rows)
    return server, open_user_repository(connection_opener=server.connect)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_inserts_every_identity_column_with_bound_parameters() -> None:
    server, repository = build()
    user = make_user(username="Ada Lovelace", role=Role.HR_MANAGER)

    assert repository.create(user) is user
    assert "INSERT INTO app_user" in server.last_sql
    assert server.last_params == (
        user.user_id,
        user.username,
        "ada lovelace",
        user.password_hash,
        "HR Manager",
        user.is_active,
        user.created_at,
        user.updated_at,
    )
    assert server.connections[0].committed is True
    assert server.connections[0].closed is True


def test_create_never_interpolates_values_into_sql() -> None:
    server, repository = build()
    repository.create(make_user(username="Ada"))
    assert "Ada" not in server.last_sql
    assert "argon2" not in server.last_sql
    assert server.last_sql.count("%s") == 8


def test_duplicate_identity_maps_to_a_typed_error_only_for_sqlstate_23505() -> None:
    server, repository = build()
    server.error = FakeDriverError("23505")
    with pytest.raises(UserAlreadyExists) as excinfo:
        repository.create(make_user())
    # The driver message is never surfaced.
    assert "driver detail leak" not in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, FakeDriverError)


def test_other_sqlstates_propagate_unchanged() -> None:
    server, repository = build()
    server.error = FakeDriverError("42P01")
    with pytest.raises(FakeDriverError):
        repository.create(make_user())


def test_create_requires_a_validated_user() -> None:
    _, repository = build()
    with pytest.raises(UserValidationError):
        repository.create(object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def test_get_parses_the_row_through_the_model() -> None:
    server, repository = build([user_row()])
    user = repository.get(" u_0001 ")
    assert user is not None
    assert user.user_id == "u_0001"
    assert user.username == "Ada"
    assert user.username_key == "ada"
    assert user.role is Role.ADMIN
    assert server.last_params == ("u_0001",)


def test_get_returns_none_when_absent() -> None:
    _, repository = build()
    assert repository.get("u_missing") is None


@pytest.mark.parametrize("value", ["", "   ", None])
def test_get_refuses_blank_ids_before_any_query(value) -> None:
    server, repository = build([user_row()])
    with pytest.raises(UserValidationError):
        repository.get(value)  # type: ignore[arg-type]
    assert server.calls == []


def test_find_by_username_uses_the_normalized_casefolded_key() -> None:
    server, repository = build([user_row(username="Ada", username_key="ada")])
    user = repository.find_by_username("  ADA  ")
    assert user is not None and user.username == "Ada"
    assert "username_key = %s" in server.last_sql
    assert server.last_params == ("ada",)


def test_find_by_username_returns_none_when_absent() -> None:
    _, repository = build()
    assert repository.find_by_username("nobody") is None


def test_unknown_stored_role_is_refused_without_echoing_it() -> None:
    server, repository = build([user_row(role="Superuser")])
    with pytest.raises(UserValidationError) as excinfo:
        repository.get("u_0001")
    assert "Superuser" not in str(excinfo.value)


def test_row_shape_mismatch_is_refused() -> None:
    server, repository = build([("u_0001", "Ada")])
    with pytest.raises(RuntimeError):
        repository.get("u_0001")


# ---------------------------------------------------------------------------
# list (bounded, deterministic)
# ---------------------------------------------------------------------------


def test_list_defaults_to_the_configured_page_size() -> None:
    server, repository = build([user_row()])
    users = repository.list()
    assert [user.user_id for user in users] == ["u_0001"]
    assert "ORDER BY user_id ASC LIMIT %s" in server.last_sql
    assert server.last_params == (USER_PAGINATION.default_page_size,)


def test_list_clamps_to_the_hard_maximum() -> None:
    server, repository = build()
    repository.list(limit=10_000)
    assert server.last_params == (USER_PAGINATION.max_page_size,)


def test_list_uses_an_exclusive_keyset_cursor() -> None:
    server, repository = build()
    repository.list(after=" u_0001 ", limit=5)
    assert "WHERE user_id > %s" in server.last_sql
    assert server.last_params == ("u_0001", 5)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "10"])
def test_list_refuses_invalid_limits_before_any_query(limit) -> None:
    server, repository = build()
    with pytest.raises(UserValidationError):
        repository.list(limit=limit)
    assert server.calls == []


def test_list_refuses_a_blank_cursor() -> None:
    server, repository = build()
    with pytest.raises(UserValidationError):
        repository.list(after="  ")
    assert server.calls == []


# ---------------------------------------------------------------------------
# updates
# ---------------------------------------------------------------------------


def test_update_role_sets_role_and_authored_timestamp() -> None:
    server, repository = build([user_row(role="Auditor")])
    updated = repository.update_role(" u_0001 ", Role.AUDITOR, updated_at=CREATED)
    assert updated.role is Role.AUDITOR
    assert "UPDATE app_user SET role = %s, updated_at = %s" in server.last_sql
    assert server.last_params == ("Auditor", CREATED, "u_0001")


def test_update_role_missing_user_raises_typed_error() -> None:
    _, repository = build([])
    with pytest.raises(UserNotFound):
        repository.update_role("u_missing", Role.OPERATOR, updated_at=CREATED)


def test_update_role_requires_a_role_member() -> None:
    server, repository = build([user_row()])
    with pytest.raises(UserValidationError):
        repository.update_role("u_0001", "Admin", updated_at=CREATED)  # type: ignore[arg-type]
    assert server.calls == []


def test_update_active_persists_false() -> None:
    server, repository = build([user_row(is_active=False)])
    updated = repository.update_active("u_0001", False, updated_at=CREATED)
    assert updated.is_active is False
    assert "UPDATE app_user SET is_active = %s, updated_at = %s" in server.last_sql
    assert server.last_params == (False, CREATED, "u_0001")


def test_update_active_missing_user_raises_typed_error() -> None:
    _, repository = build([])
    with pytest.raises(UserNotFound):
        repository.update_active("u_missing", True, updated_at=CREATED)


def test_update_active_requires_a_real_bool() -> None:
    server, repository = build([user_row()])
    with pytest.raises(UserValidationError):
        repository.update_active("u_0001", "yes", updated_at=CREATED)  # type: ignore[arg-type]
    assert server.calls == []


@pytest.mark.parametrize(
    "updated_at", [datetime(2026, 11, 12, 9, 0), "2026-11-12T09:00:00+00:00"]
)
def test_updates_require_an_aware_authored_timestamp(updated_at) -> None:
    server, repository = build([user_row()])
    with pytest.raises(UserValidationError):
        repository.update_role("u_0001", Role.ADMIN, updated_at=updated_at)  # type: ignore[arg-type]
    assert server.calls == []


def test_naive_timestamp_never_reaches_the_database() -> None:
    server, repository = build([user_row()])
    with pytest.raises(UserValidationError):
        repository.update_active(
            "u_0001", True, updated_at=datetime(2026, 11, 12, 9, 0, tzinfo=UTC).replace(tzinfo=None)
        )
    assert server.calls == []


def test_module_import_is_driver_free_in_a_fresh_process() -> None:
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{root}');"
        "import src.user_repository, src.session_repository, src.db_security_repository;"
        "banned = {'psycopg', 'src.db'} & set(sys.modules);"
        "assert not banned, banned;"
        "print('OK')"
    )
    result = subprocess.run(
        [__import__("sys").executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


# ---------------------------------------------------------------------------
# update_password_hash (P5.4.5 rehash persistence)
# ---------------------------------------------------------------------------

_NEW_HASH = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"


def test_update_password_hash_sets_only_hash_and_updated_at() -> None:
    from src.clock import utc_now

    server, repository = build(rows=[user_row()])
    before = utc_now()
    assert repository.update_password_hash("u_0001", _NEW_HASH) is None
    after = utc_now()

    assert "UPDATE app_user SET password_hash = %s, updated_at = %s WHERE user_id = %s" in server.last_sql
    assert "RETURNING user_id" in server.last_sql
    assert "role" not in server.last_sql
    assert "is_active" not in server.last_sql
    assert "username" not in server.last_sql

    password_hash, updated_at, user_id = server.last_params
    assert password_hash == _NEW_HASH
    assert user_id == "u_0001"
    assert updated_at.tzinfo is not None
    assert before <= updated_at <= after
    assert server.connections[0].committed is True
    assert server.connections[0].closed is True


def test_update_password_hash_never_interpolates_the_hash_into_sql() -> None:
    server, repository = build(rows=[user_row()])
    repository.update_password_hash("u_0001", _NEW_HASH)
    assert _NEW_HASH not in server.last_sql
    assert "argon2" not in server.last_sql
    assert server.last_sql.count("%s") == 3


def test_update_password_hash_missing_user_is_a_typed_error() -> None:
    server, repository = build(rows=[])
    with pytest.raises(UserNotFound):
        repository.update_password_hash("u_missing", _NEW_HASH)


def test_update_password_hash_propagates_driver_failures_unchanged() -> None:
    server, repository = build(rows=[user_row()])
    server.error = FakeDriverError("42P01")
    with pytest.raises(FakeDriverError) as excinfo:
        repository.update_password_hash("u_0001", _NEW_HASH)
    # The repository propagates the driver error unchanged (the API layer is
    # what sanitizes it); no typed mapping swallows it.
    assert isinstance(excinfo.value, FakeDriverError)
    assert server.connections[0].closed is True


def test_update_password_hash_rejects_an_empty_hash_before_any_query() -> None:
    server, repository = build(rows=[user_row()])
    for bad in ("", "   ", None, 123):
        with pytest.raises(UserValidationError):
            repository.update_password_hash("u_0001", bad)  # type: ignore[arg-type]
    assert server.calls == []
