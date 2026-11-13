"""Offline structural tests for ``migrations/V002__security.sql``.

The migration text is read as a file — no database, no driver, no SQL execution.
Structure is asserted so the schema contract, the repositories and the DDL
cannot silently drift apart.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.db_migrate import DEFAULT_MIGRATIONS_DIR, load_migrations
from src.schema_contract import (
    APP_USER_TABLE,
    CONTRACT_MIGRATION_VERSION,
    MIGRATION_SET_DIGEST,
    REQUIRED_TABLES_BY_NAME,
    USER_SESSION_TABLE,
    migration_set_digest,
)
from src.user import MAX_USERNAME_LENGTH, MIN_USERNAME_LENGTH, Role

V001_CHECKSUM = "9e6ac6b5d1e0fd016a99c007ec5b91070dd480853d54d37cdab5b069dd2deac9"
V002_CHECKSUM = "35bc00cd7a947a9eca31a7502d3a17e0ff55163f1c5222cf8fcb88a6328082d3"


def _scripts():
    return {script.version: script for script in load_migrations()}


def _sql() -> str:
    return (DEFAULT_MIGRATIONS_DIR / "V002__security.sql").read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    without_line = re.sub(r"--[^\n]*", "", text)
    return re.sub(r"/\*.*?\*/", "", without_line, flags=re.DOTALL)


def _normalized() -> str:
    return re.sub(r"\s+", " ", _strip_comments(_sql())).strip()


def _table_body(normalized: str, table: str) -> str:
    match = re.search(rf"CREATE TABLE {table} \((.*?)\);", normalized)
    assert match is not None, f"CREATE TABLE {table} missing"
    return match.group(1)


# ---------------------------------------------------------------------------
# Loader / ordering
# ---------------------------------------------------------------------------


def test_v002_is_shipped_and_ordered_after_v001() -> None:
    scripts = _scripts()
    assert sorted(scripts) == [1, 2]
    assert scripts[2].name == "V002__security.sql"
    assert scripts[1].name == "V001__employee.sql"
    assert [s.version for s in load_migrations()] == [1, 2]


def test_shipped_checksums_are_unchanged() -> None:
    scripts = _scripts()
    assert scripts[1].checksum == V001_CHECKSUM
    assert scripts[2].checksum == V002_CHECKSUM
    assert migration_set_digest((v, s.checksum) for v, s in scripts.items()) == (
        MIGRATION_SET_DIGEST
    )


def test_contract_version_tracks_the_shipped_set() -> None:
    assert CONTRACT_MIGRATION_VERSION == 2


def test_file_is_utf8_without_bom_and_uses_lf() -> None:
    raw = (DEFAULT_MIGRATIONS_DIR / "V002__security.sql").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw


def test_v002_passes_the_runner_prohibited_statement_scan() -> None:
    """``load_migrations`` refuses CREATE DATABASE / CREATE INDEX CONCURRENTLY."""
    assert "CREATE DATABASE" not in _normalized().upper()
    assert "CONCURRENTLY" not in _normalized().upper()


# ---------------------------------------------------------------------------
# app_user
# ---------------------------------------------------------------------------


def test_app_user_table_shape() -> None:
    normalized = _normalized()
    body = _table_body(normalized, "app_user")
    for column in (
        "user_id TEXT PRIMARY KEY",
        "username TEXT NOT NULL",
        "username_key TEXT UNIQUE NOT NULL",
        "password_hash TEXT NOT NULL",
        "role TEXT NOT NULL CHECK",
        "is_active BOOLEAN NOT NULL DEFAULT TRUE",
        "created_at TIMESTAMPTZ NOT NULL",
        "updated_at TIMESTAMPTZ NOT NULL",
    ):
        assert column in body, column


def test_role_check_allows_exactly_the_documented_roles() -> None:
    body = _table_body(_normalized(), "app_user")
    match = re.search(r"CHECK \( ?role IN \((.*?)\) ?\)", body)
    assert match is not None
    allowed = re.findall(r"'([^']+)'", match.group(1))
    assert allowed == [role.value for role in Role]


def test_no_plaintext_or_salt_columns_exist() -> None:
    normalized = _normalized().lower()
    assert "password_salt" not in normalized
    assert "salt text" not in normalized
    assert "password text" not in normalized
    # The only credential-bearing column is the encoded hash.
    assert "password_hash" in normalized


def test_timestamp_columns_have_no_database_defaults() -> None:
    body = _table_body(_normalized(), "app_user")
    for column in ("created_at", "updated_at"):
        line = next(
            part for part in body.split(",") if part.strip().startswith(column)
        )
        assert "DEFAULT" not in line.upper(), line
        assert "NOT NULL" in line.upper(), line
    # ``is_active`` is the only defaulted column, and it is TRUE.
    assert body.upper().count("DEFAULT") == 1


# ---------------------------------------------------------------------------
# user_session
# ---------------------------------------------------------------------------


def test_user_session_table_shape() -> None:
    body = _table_body(_normalized(), "user_session")
    for column in (
        "session_token_hash CHAR(64) PRIMARY KEY",
        "user_id TEXT NOT NULL REFERENCES app_user(user_id)",
        "issued_at TIMESTAMPTZ NOT NULL",
        "expires_at TIMESTAMPTZ NOT NULL",
        "absolute_expires_at TIMESTAMPTZ NOT NULL",
        "revoked_at TIMESTAMPTZ NULL",
        "last_seen_at TIMESTAMPTZ NOT NULL",
    ):
        assert column in body, column


def test_session_table_has_no_defaults_and_no_token_column() -> None:
    body = _table_body(_normalized(), "user_session")
    assert "DEFAULT" not in body.upper()
    lowered = body.lower()
    assert "session_token " not in lowered
    assert "token text" not in lowered
    # Only the digest column is token-related.
    assert lowered.count("session_token_hash") == 1


def test_session_maintenance_indexes_are_declared() -> None:
    normalized = _normalized()
    indexes = re.findall(r"CREATE INDEX (\w+) ON (\w+) \(([^)]+)\)", normalized)
    assert indexes == [
        ("user_session_user_id_idx", "user_session", "user_id"),
        ("user_session_expires_at_idx", "user_session", "expires_at"),
    ]


# ---------------------------------------------------------------------------
# Scope discipline
# ---------------------------------------------------------------------------


def test_migration_creates_only_the_two_security_tables() -> None:
    created = re.findall(r"CREATE TABLE (\w+)", _normalized())
    assert created == ["app_user", "user_session"]


@pytest.mark.parametrize(
    "forbidden",
    [
        "audit",
        "attendance",
        "embedding",
        "biometric",
        "face",
        "enrollment",
    ],
)
def test_forbidden_domains_are_absent_from_the_ddl(forbidden: str) -> None:
    normalized = _normalized().lower()
    # The prose comments may mention them; the *DDL* must not.
    ddl = " ".join(
        statement
        for statement in normalized.split(";")
        if statement.strip().startswith(("create table", "create index"))
    )
    # Token-bounded: the Auditor role legitimately contains "audit" as a
    # substring, so the check is for a whole word / table prefix, not a spy.
    assert not re.search(rf"\b{forbidden}\b", ddl), ddl


def test_migration_does_not_touch_the_employee_table() -> None:
    assert "employee" not in _normalized().lower().replace("app_user", "")


def _declared_columns(raw: str, table: str) -> list[str]:
    block = re.search(
        rf"CREATE TABLE {table} \((.*?)\n\);", raw, re.DOTALL
    )
    assert block is not None
    return re.findall(
        r"^\s{4}([a-z_]+)\s+(?:TEXT|BOOLEAN|TIMESTAMPTZ|CHAR)",
        block.group(1),
        re.MULTILINE,
    )


def test_contract_tables_match_the_migration_columns() -> None:
    raw = _strip_comments(_sql())
    assert tuple(_declared_columns(raw, "app_user")) == APP_USER_TABLE.column_names
    assert (
        tuple(_declared_columns(raw, "user_session"))
        == USER_SESSION_TABLE.column_names
    )


def test_username_length_bounds_are_documented_in_the_model_not_the_ddl() -> None:
    """The DDL stores text; length policy is the application's documented rule."""
    assert MIN_USERNAME_LENGTH < MAX_USERNAME_LENGTH
    assert "VARCHAR" not in _normalized().upper()
    assert REQUIRED_TABLES_BY_NAME["app_user"].column("username").pg_type == "text"
