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
  ``DELETE`` and no DDL, and the test-only cleanup role holds ``DELETE`` only.
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
    "GRANT USAGE ON SCHEMA public TO attendance_tier2_cleanup;",
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
    assert "USAGE WITH GRANT OPTION" in text
    assert "pg_get_userbyid(c.relowner) <> current_user" in text


def test_schema_contract_checks_are_present() -> None:
    text = _script()
    assert "contype = 'u'" in text  # username_key UNIQUE
    assert "contype = 'f'" in text  # user_session -> app_user
    assert "character_maximum_length" in text
    assert "indisprimary" in text
    assert "user_session_user_id_idx" in text
    assert "user_session_expires_at_idx" in text


def test_script_is_not_a_shipped_migration() -> None:
    assert SCRIPT_PATH.parent.name != "migrations"
    assert SCRIPT_PATH.name not in {script.name for script in load_migrations()}
