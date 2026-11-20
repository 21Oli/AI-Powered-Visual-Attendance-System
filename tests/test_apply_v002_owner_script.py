"""Static review tests for the owner-run V002 test-database script.

``tools/apply_v002_tier2_test_database.sql`` is a security-sensitive artifact
that no automated gate may execute: it needs privileges the application role
does not have. These tests review it as *data* instead — no connection, no
migration, no application or production database. They keep three properties
under review honest:

* the embedded V002 block stays byte-identical to ``migrations/V002__security.sql``
  (whose checksum is pinned elsewhere), and the ledger checksums in the script
  match the ones the migration loader derives;
* the script stays a single transaction around DDL + ledger row + grants;
* the grant set stays least-privilege: the application runtime role holds no
  ``DELETE`` and no DDL; the test-only cleanup role holds ``DELETE`` on exactly
  the two security tables and column-level ``SELECT`` on ``user_id`` in those
  tables only (the column that drives its ``DELETE ... WHERE user_id = ANY(%s)``
  queries), with no table-level ``SELECT``/``INSERT``/``UPDATE`` and no
  ``SELECT`` on any other column; schema ``USAGE`` is granted only when the
  cleanup role does not already hold it.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.db_migrate import load_migrations

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "tools" / "apply_v002_tier2_test_database.sql"
MIGRATION_PATH = PROJECT_ROOT / "migrations" / "V002__security.sql"

V001_CHECKSUM = (
    "9e6ac6b5d1e0fd016a99c007ec5b91070dd480853d54d37cdab5b069dd2deac9"
)
V002_CHECKSUM = (
    "35bc00cd7a947a9eca31a7502d3a17e0ff55163f1c5222cf8fcb88a6328082d3"
)

_BEGIN = b"-- --- begin verbatim migrations/V002__security.sql ---"
_END = b"-- --- end verbatim migrations/V002__security.sql ---"

_EXPECTED_GRANTS = {
    "GRANT SELECT, INSERT, UPDATE ON public.app_user TO attendance_tier2_runtime;",
    "GRANT SELECT, INSERT, UPDATE ON public.user_session TO attendance_tier2_runtime;",
    "GRANT SELECT (user_id) ON public.app_user TO attendance_tier2_cleanup;",
    "GRANT SELECT (user_id) ON public.user_session TO attendance_tier2_cleanup;",
    "GRANT DELETE ON public.app_user TO attendance_tier2_cleanup;",
    "GRANT DELETE ON public.user_session TO attendance_tier2_cleanup;",
}


def _canonical(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _script_bytes() -> bytes:
    return _canonical(SCRIPT_PATH.read_bytes())


def _script() -> str:
    return _script_bytes().decode("utf-8")


def _embedded() -> bytes:
    raw = _script_bytes()
    start = raw.index(_BEGIN) + len(_BEGIN)
    return raw[start : raw.index(_END)]


def _grants() -> set[str]:
    statements = re.findall(r"^GRANT .*;$", _script(), flags=re.MULTILINE)
    return {re.sub(r"\s+", " ", statement).strip() for statement in statements}


def test_embedded_v002_is_byte_identical_to_the_migration() -> None:
    expected = _canonical(MIGRATION_PATH.read_bytes())
    assert expected.endswith(b"\n")
    assert _embedded() == b"\n" + expected


def test_ledger_checksums_match_the_migration_loader() -> None:
    checksums = {script.version: script.checksum for script in load_migrations()}
    assert checksums[1] == V001_CHECKSUM
    assert checksums[2] == V002_CHECKSUM

    text = _script()
    assert text.count(V001_CHECKSUM) >= 2  # precondition + verification
    assert text.count(V002_CHECKSUM) >= 2  # ledger insert + verification


def test_grants_are_exactly_the_least_privilege_set() -> None:
    assert _grants() == _EXPECTED_GRANTS


def test_runtime_role_never_receives_delete_or_ddl() -> None:
    text = _script().lower()
    assert "all privileges" not in text
    assert "on public.employee" not in text
    for statement in _grants():
        lowered = statement.lower()
        assert "all" not in lowered
        assert (
            "attendance_tier2_runtime" in statement
            or "attendance_tier2_cleanup" in statement
        ), statement
        if "attendance_tier2_runtime" in statement:
            assert "delete" not in lowered, statement


def test_single_transaction_wraps_ddl_ledger_and_grants() -> None:
    text = _script()
    assert text.count("BEGIN;") == 1
    assert text.count("COMMIT;") == 1
    begin = text.index("BEGIN;")
    embedded = text.index(_BEGIN.decode())
    ledger_match = re.search(
        r"^INSERT INTO schema_version \(version, checksum, applied_at\)$",
        text,
        flags=re.MULTILINE,
    )
    assert ledger_match is not None
    ledger = ledger_match.start()
    grant = text.index("GRANT SELECT, INSERT, UPDATE ON public.app_user")
    commit = text.rindex("COMMIT;")
    assert begin < embedded < ledger < grant < commit


def test_target_database_and_cluster_identity_are_guarded() -> None:
    text = _script()
    assert "visual_attendance_tier2_test" in text
    assert "pg_control_system()" in text
    assert "7687968999847354228" in text
    assert "system_identifier % does not match the expected test-cluster identity" in text


def test_executing_authority_is_verified_before_ddl() -> None:
    text = _script()
    assert "has_schema_privilege(current_user, 'public', 'CREATE')" in text
    assert "pg_get_userbyid(c.relowner) <> current_user" in text


def test_cleanup_schema_usage_grant_is_conditional() -> None:
    """USAGE is granted only when the cleanup role lacks it.

    An unconditional ``GRANT`` needs grant option from the owner even
    when the privilege already exists (PostgreSQL grants ``USAGE`` on
    schema ``public`` to ``PUBLIC`` by default), so the grant must sit
    behind a has_schema_privilege check and be executed dynamically.
    """
    text = _script()
    assert not re.search(
        r"^GRANT USAGE ON SCHEMA public TO attendance_tier2_cleanup;$",
        text,
        flags=re.MULTILINE,
    )
    assert "EXECUTE 'GRANT USAGE ON SCHEMA public TO attendance_tier2_cleanup'" in text
    assert text.count("EXECUTE '") == 1  # no other dynamic SQL
    guarded = re.search(
        r"IF NOT has_schema_privilege\('attendance_tier2_cleanup', 'public', 'USAGE'\) THEN\s*\n"
        r"\s*IF NOT has_schema_privilege\(\s*\n"
        r"\s*current_user, 'public', 'USAGE WITH GRANT OPTION'\s*\n"
        r"\s*\) THEN\s*\n"
        r"\s*RAISE EXCEPTION",
        text,
    )
    assert guarded is not None, (
        "the grant-option requirement must depend on the cleanup role "
        "actually lacking USAGE"
    )


def test_schema_contract_checks_are_present() -> None:
    text = _script()
    assert "contype = 'u'" in text  # username_key UNIQUE
    assert "contype = 'f'" in text  # user_session -> app_user
    assert "character_maximum_length" in text
    assert "indisprimary" in text
    assert "user_session_user_id_idx" in text
    assert "user_session_expires_at_idx" in text


def test_cleanup_role_gets_column_level_select_on_user_id_only() -> None:
    """The cleanup role gets column-level SELECT on user_id in both tables,

    and no table-level SELECT, INSERT or UPDATE, and no column-level SELECT on
    any other column. Enforced here as static review; the effective-privilege
    checks in the script's verification blocks (5o/5p) assert the same shape at
    apply time.
    """
    text = _script()
    # The cleanup role must receive exactly these column-level grants.
    assert (
        "GRANT SELECT (user_id) ON public.app_user TO attendance_tier2_cleanup;"
        in text
    )
    assert (
        "GRANT SELECT (user_id) ON public.user_session TO attendance_tier2_cleanup;"
        in text
    )
    # There must be no table-level SELECT grant to the cleanup role.
    assert not re.search(
        r"^GRANT SELECT ON public\.(app_user|user_session) TO attendance_tier2_cleanup;$",
        text,
        flags=re.MULTILINE,
    ), "the cleanup role must not receive a table-level SELECT grant"
    # Verification must consult effective privileges per column, not only the
    # grant records the script itself wrote.
    assert "has_column_privilege(" in text
    assert "'user_id'" in text


def test_cleanup_verification_rejects_any_read_outside_user_id() -> None:
    """5p must sweep every catalog column and reject everything but user_id.

    ``has_column_privilege`` succeeds when the privilege is held for the whole
    table or granted for the column, so this per-column sweep also rejects a
    table-level SELECT grant and any SELECT inherited through role membership.
    """
    text = _script()
    assert "'user_id', 'SELECT'" in text
    assert "AND NOT (c.table_name = 'app_user' AND c.column_name = 'user_id')" in text
    assert (
        "AND NOT (c.table_name = 'user_session' AND c.column_name = 'user_id')"
        in text
    )
    # Positive and negative assertions, plus the column-level write check.
    assert text.count("has_column_privilege(") >= 3
    # The column list is read from the catalog, so a future column is covered.
    assert "FROM information_schema.columns c" in text


def test_grantee_names_are_never_resolved_through_pg_get_userbyid() -> None:
    """``information_schema...grantee`` is a role NAME, not a PostgreSQL OID.

    Passing it to ``pg_get_userbyid()`` is a type error at apply time, so the
    whole verification block would fail (or, worse, compare nothing). Only
    OID-valued catalog columns may be resolved that way.
    """
    text = _script()
    assert "pg_get_userbyid(c.grantee)" not in text
    assert "regrole" not in text
    resolved = re.findall(r"pg_get_userbyid\(\s*([^)]*)\)", text)
    assert resolved, "the ownership assertion must still resolve the owner OID"
    for argument in resolved:
        assert "grantee" not in argument, argument
    # Role-name comparisons are direct, and use the catalog's own name values.
    assert "c.grantee = 'attendance_tier2_runtime'" in text


def test_cleanup_verification_checks_delete_effectively() -> None:
    """DELETE must be asserted through ``has_table_privilege``, not the ACL text."""
    text = _script()
    assert "has_table_privilege(\n               'attendance_tier2_cleanup'" in text
    assert "the cleanup role lacks DELETE on %" in text
    assert "must hold no table-level write privilege" in text


def test_runtime_verification_rejects_column_level_grants() -> None:
    """The runtime role must hold no column-level grant at all.

    Its table-level SELECT already covers every column, so a column-level record
    would be an unintended grant (and a sign the grant set drifted).
    """
    text = _script()
    assert "the runtime role must not hold DELETE" in text
    assert "required runtime privileges missing" in text
    assert "the runtime role must not hold column-level grants" in text


def test_script_is_not_a_shipped_migration() -> None:
    assert SCRIPT_PATH.parent.name != "migrations"
    assert SCRIPT_PATH.name not in {script.name for script in load_migrations()}
