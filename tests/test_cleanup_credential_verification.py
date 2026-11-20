"""Regression tests for the Tier-2 cleanup-credential verification.

These tests cover the identity, membership and *effective* privilege contract
that ``_require_cleanup_credential`` enforces before the Tier-2 concurrency
suite touches a database. They are offline: no connection, no DSN, no
credential. A synthetic psycopg-shaped connection answers the exact queries the
real helper issues, so the helper's real code path runs with no driver loaded.

Do not relax these assertions. Each one is a specific privilege-expansion or
identity-mismatch failure mode that the verification must reject: a missing
DELETE, a missing ``user_id`` read, a table-level SELECT, a read on another
column, a SELECT inherited through another role, and an over-privileged
runtime role.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest

from src.test_database_guard import VerifiedTestTarget

CLEANUP_ROLE: Final[str] = "attendance_tier2_cleanup"
RUNTIME_ROLE: Final[str] = "attendance_tier2_runtime"

#: The cluster fingerprint the fake verified target carries.
_SYNTHETIC_SYSID: Final[int] = 7687968999847354228

#: The V002 column contract, as the catalog reports it.
_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "app_user": (
        "user_id",
        "username",
        "username_key",
        "password_hash",
        "role",
        "is_active",
        "created_at",
        "updated_at",
    ),
    "user_session": (
        "session_token_hash",
        "user_id",
        "issued_at",
        "expires_at",
        "absolute_expires_at",
        "revoked_at",
        "last_seen_at",
    ),
}

_TABLES: Final[tuple[str, ...]] = ("app_user", "user_session")
_TABLE_PRIVILEGES: Final[tuple[str, ...]] = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
)
_COLUMN_PRIVILEGES: Final[tuple[str, ...]] = ("SELECT", "INSERT", "UPDATE")


class _Result:
    """Minimal cursor shape: ``fetchone``/``fetchall``."""

    def __init__(self, rows: Any) -> None:
        self._rows = rows

    def fetchone(self) -> Any:
        if not self._rows:
            return None
        return self._rows[0]

    def fetchall(self) -> Any:
        return list(self._rows or [])


class _SyntheticConnection:
    """Answers the helper's queries from configured privilege sets.

    ``table_privileges`` holds ``(role, table, privilege)`` for privileges
    ``has_table_privilege`` reports as held. ``column_privileges`` holds
    ``(role, table, column, privilege)`` for what ``has_column_privilege``
    reports as held — which, in PostgreSQL, is true for the whole table when the
    privilege is held at table level. ``column_grants`` holds the raw
    ``(role, table, column, privilege)`` ACL records, so a test can model a
    grant that is *effective* but not recorded directly (an inherited one).
    """

    def __init__(
        self,
        *,
        current_database: str = "visual_attendance_tier2_test",
        current_user: str = CLEANUP_ROLE,
        is_superuser: str = "off",
        table_privileges: frozenset[tuple[str, str, str]] = frozenset(),
        column_privileges: frozenset[tuple[str, str, str, str]] = frozenset(),
        column_grants: frozenset[tuple[str, str, str, str]] = frozenset(),
        membership: tuple[bool, bool] = (False, False),
        system_identifier: int | None = _SYNTHETIC_SYSID,
        identifier_raises: bool = False,
        identity_missing: bool = False,
    ) -> None:
        self._current_database = current_database
        self._current_user = current_user
        self._is_superuser = is_superuser
        self._table_privileges = table_privileges
        self._column_privileges = column_privileges
        self._column_grants = column_grants
        self._membership = membership
        self._system_identifier = system_identifier
        self._identifier_raises = identifier_raises
        self._identity_missing = identity_missing
        self.autocommit = False
        self.closed = False

    def execute(self, sql: str, params: tuple = ()) -> _Result:
        if "is_superuser" in sql:
            if self._identity_missing:
                return _Result([])
            return _Result(
                [(self._current_database, self._current_user, self._is_superuser)]
            )
        if "has_table_privilege" in sql:
            roles = list(params[0])
            rows = [
                (role, table, privilege, (role, table, privilege) in self._table_privileges)
                for role in roles
                for table in _TABLES
                for privilege in _TABLE_PRIVILEGES
            ]
            return _Result(rows)
        if "has_column_privilege" in sql:
            roles = list(params[0])
            rows = [
                (
                    role,
                    table,
                    column,
                    privilege,
                    (role, table, column, privilege) in self._column_privileges,
                )
                for role in roles
                for table in _TABLES
                for column in _COLUMNS[table]
                for privilege in _COLUMN_PRIVILEGES
            ]
            return _Result(rows)
        if "aclexplode" in sql:
            roles = set(params[0])
            return _Result(
                [row for row in sorted(self._column_grants) if row[0] in roles]
            )
        if "pg_has_role" in sql:
            return _Result([self._membership])
        if "system_identifier" in sql:
            if self._identifier_raises:
                raise RuntimeError("permission denied for function pg_control_system")
            if self._system_identifier is None:
                return _Result([])
            return _Result([(self._system_identifier,)])
        raise AssertionError(f"unexpected statement: {sql!r}")

    def close(self) -> None:
        self.closed = True


def _make_verified_target(
    *,
    dbname: str = "visual_attendance_tier2_test",
    host: str = "localhost",
    port: int = 5432,
    system_identifier: int = _SYNTHETIC_SYSID,
) -> VerifiedTestTarget:
    """Build a ``VerifiedTestTarget`` for offline tests.

    Uses the guard module's private proof object, which satisfies the
    cooperative value-object gate. Acceptable here: this is an offline test that
    already imports the guard, and nothing is verified by the object itself.
    """
    from src.test_database_guard import _TARGET_PROOF

    return VerifiedTestTarget(
        dbname=dbname,
        host=host,
        port=port,
        system_identifier=system_identifier,
        marker_created_at=datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC),
        marker_schema_version=2,
        ledger_schema_version=2,
        _proof=_TARGET_PROOF,
    )


def _state(**overrides: Any) -> dict[str, Any]:
    """The intended, minimal Tier-2 privilege configuration."""
    tables = {
        (CLEANUP_ROLE, table, "DELETE") for table in _TABLES
    } | {
        (RUNTIME_ROLE, table, privilege)
        for table in _TABLES
        for privilege in ("SELECT", "INSERT", "UPDATE")
    }
    cleanup_columns = frozenset(
        {(CLEANUP_ROLE, table, "user_id", "SELECT") for table in _TABLES}
    )
    runtime_columns = frozenset(
        (RUNTIME_ROLE, table, column, privilege)
        for table in _TABLES
        for column in _COLUMNS[table]
        for privilege in _COLUMN_PRIVILEGES
    )
    state: dict[str, Any] = {
        "table_privileges": frozenset(tables),
        "column_privileges": cleanup_columns | runtime_columns,
        "column_grants": cleanup_columns,
        "membership": (False, False),
    }
    state.update(overrides)
    return state


def _run(state: dict[str, Any]) -> None:
    """Drive the real helper with one synthetic connection."""
    import tests.test_tier2_user_concurrency as module

    original_open = module.open_connection
    try:
        module.open_connection = lambda dsn: _SyntheticConnection(**state)  # type: ignore[assignment]
        module._require_cleanup_credential(
            "cleanup-dsn-is-never-used", _make_verified_target()
        )
    finally:
        module.open_connection = original_open


def _failure(state: dict[str, Any]) -> str:
    with pytest.raises(pytest.fail.Exception) as err:
        _run(state)
    return str(err.value)


# ---------------------------------------------------------------------------
# Minimum permissions are accepted
# ---------------------------------------------------------------------------


def test_minimum_cleanup_permissions_pass() -> None:
    """DELETE on both tables plus user_id SELECT only: accepted."""
    _run(_state())


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_different_database_fails() -> None:
    message = _failure(_state(current_database="visual_attendance_app"))
    assert "visual_attendance_app" in message
    assert "visual_attendance_tier2_test" in message


def test_different_cluster_fails() -> None:
    message = _failure(_state(system_identifier=1234567890))
    assert "different PostgreSQL cluster" in message


def test_unreadable_system_identifier_fails() -> None:
    message = _failure(_state(system_identifier=None))
    assert "cannot read pg_control_system()" in message


def test_system_identifier_permission_denied_fails_closed() -> None:
    """A role without EXECUTE on pg_control_system() must block, not degrade."""
    message = _failure(_state(identifier_raises=True))
    assert "cannot read pg_control_system()" in message
    assert "name-only" in message


def test_wrong_role_fails() -> None:
    message = _failure(_state(current_user=RUNTIME_ROLE))
    assert CLEANUP_ROLE in message
    assert RUNTIME_ROLE in message


def test_superuser_cleanup_credential_fails() -> None:
    message = _failure(_state(is_superuser="on"))
    assert "superuser" in message


def test_mutual_role_membership_fails() -> None:
    message = _failure(_state(membership=(True, False)))
    assert "members of each other" in message


def test_uninspectable_identity_fails() -> None:
    message = _failure(_state(identity_missing=True))
    assert "could not be inspected" in message


# ---------------------------------------------------------------------------
# Cleanup privileges
# ---------------------------------------------------------------------------


def test_missing_delete_fails() -> None:
    tables = frozenset(
        privilege
        for privilege in _state()["table_privileges"]
        if privilege != (CLEANUP_ROLE, "app_user", "DELETE")
    )
    message = _failure(_state(table_privileges=tables))
    assert "cannot delete synthetic rows" in message
    assert "app_user/DELETE" in message


def test_missing_select_on_user_id_fails() -> None:
    columns = frozenset(
        privilege
        for privilege in _state()["column_privileges"]
        if privilege != (CLEANUP_ROLE, "user_session", "user_id", "SELECT")
    )
    message = _failure(_state(column_privileges=columns))
    assert "must be able to read user_id" in message
    assert "user_session.user_id" in message


def test_excess_table_level_select_fails() -> None:
    """A table-level SELECT makes every column readable to the cleanup role."""
    columns = frozenset(
        (CLEANUP_ROLE, table, column, "SELECT")
        for table in _TABLES
        for column in _COLUMNS[table]
    ) | frozenset(
        privilege
        for privilege in _state()["column_privileges"]
        if privilege[0] != CLEANUP_ROLE
    )
    message = _failure(_state(column_privileges=columns))
    assert "must be able to read user_id" in message
    assert "app_user.username" in message


def test_select_on_another_column_fails() -> None:
    columns = _state()["column_privileges"] | {
        (CLEANUP_ROLE, "app_user", "password_hash", "SELECT")
    }
    message = _failure(_state(column_privileges=frozenset(columns)))
    assert "app_user.password_hash" in message


def test_inherited_select_through_another_role_fails() -> None:
    """Effective access must be judged, not the direct grant records.

    The cleanup role holds no column grant on ``username``, yet a membership
    grants it — ``has_column_privilege`` reports the effective privilege, so the
    verification must refuse even though the ACL records look clean.
    """
    state = _state()
    assert not any(
        grant == (CLEANUP_ROLE, "app_user", "username", "SELECT")
        for grant in state["column_grants"]
    )
    state["column_privileges"] = state["column_privileges"] | {
        (CLEANUP_ROLE, "app_user", "username", "SELECT")
    }
    message = _failure(state)
    assert "app_user.username" in message


def test_column_level_write_grant_fails() -> None:
    columns = _state()["column_privileges"] | {
        (CLEANUP_ROLE, "app_user", "username", "UPDATE")
    }
    message = _failure(_state(column_privileges=frozenset(columns)))
    assert "column-level write privilege" in message
    assert "app_user.username/UPDATE" in message


def test_table_level_write_grant_fails() -> None:
    tables = _state()["table_privileges"] | {(CLEANUP_ROLE, "app_user", "UPDATE")}
    message = _failure(_state(table_privileges=frozenset(tables)))
    assert "table-level write privilege" in message
    assert "app_user/UPDATE" in message


# ---------------------------------------------------------------------------
# Runtime role
# ---------------------------------------------------------------------------


def test_unexpected_runtime_delete_fails() -> None:
    tables = _state()["table_privileges"] | {(RUNTIME_ROLE, "app_user", "DELETE")}
    message = _failure(_state(table_privileges=frozenset(tables)))
    assert "runtime role must not hold DELETE" in message
    assert "app_user" in message


def test_unexpected_runtime_column_privilege_fails() -> None:
    """A column grant on the runtime role is redundant with its table grant."""
    grants = _state()["column_grants"] | {
        (RUNTIME_ROLE, "app_user", "password_hash", "SELECT")
    }
    message = _failure(_state(column_grants=frozenset(grants)))
    assert "runtime role must hold no column-level grant" in message
    assert "app_user.password_hash/SELECT" in message


def test_runtime_role_missing_application_privilege_fails() -> None:
    tables = frozenset(
        privilege
        for privilege in _state()["table_privileges"]
        if privilege != (RUNTIME_ROLE, "user_session", "INSERT")
    )
    message = _failure(_state(table_privileges=tables))
    assert "missing required application privileges" in message
    assert "user_session/INSERT" in message
