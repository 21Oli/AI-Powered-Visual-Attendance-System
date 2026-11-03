"""Phase 3, Step 1.5b — offline test-database-guard tests
(``src/test_database_guard.py``).

Synthetic connection identities only — every scenario in the approved
matrix is driven by fake connections; no PostgreSQL connection, no
provisioning, no marker creation, no destructive operation is ever
attempted (the guard's ``require_destructive_action`` merely checks
manifest listing; its callers would fail closed first).

Audit follow-up (Step 1.5b repairs) added coverage for: ``hostaddr`` /
``service`` indirection, missing identity components, connected-target
mismatches, default-port equivalence, enforced read-only before the
first query, connection closing on success and on every blocked path,
forged/incomplete verified targets, malformed manifests, and the
missing / failed / ambiguous marker distinction.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from src.db_migrate import LEDGER_TABLE
from src.test_database_guard import (
    GUARD_MARKER_TABLE,
    GUARD_MARKER_VALUE,
    GuardBlocked,
    GuardBlockedReason,
    VerifiedTestTarget,
    require_destructive_action,
    verify_test_database,
)

MARKER_CREATED_AT = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)

APP_DSN = "postgresql://db_user:secret-not-used-here@127.0.0.1:5432/attendance_app"
TEST_DSN = (
    "postgresql://db_user:secret-not-used-here@127.0.0.1:5432/attendance_test"
)


class DbIdentity:
    """Stand-in for psycopg's ``connection.info``."""

    def __init__(self, dbname: str, host: str | None = "127.0.0.1", port: object = 5432) -> None:
        self.dbname = dbname
        self.host = host
        self.port = port


class FakeGuardConnection:
    """Fake connection exposing exactly what the guard may read: read-only
    mode, identity info, the marker table's rows, the migration ledger,
    and ``current_database()``. Records the order of operations so tests
    can prove read-only mode precedes every verification query and that
    the connection is closed on every path."""

    def __init__(
        self,
        *,
        dbname: str = "attendance_test",
        host: str | None = "127.0.0.1",
        port: object = 5432,
        marker_rows: list[tuple[str, object, object]] | None = None,
        ledger_version: int | None = 1,
        info: DbIdentity | None = None,
        no_info: bool = False,
        no_current_database: bool = False,
        marker_error: Exception | None = None,
        read_only_mode: str = "ok",
    ) -> None:
        self.info = None if no_info else DbIdentity(dbname, host, port)
        self.no_current_database = no_current_database
        self._current_database = dbname
        self.marker_rows = (
            [(GUARD_MARKER_VALUE, MARKER_CREATED_AT, 1)]
            if marker_rows is None
            else marker_rows
        )
        self.ledger_version = ledger_version
        self.marker_error = marker_error
        self.read_only_mode = read_only_mode
        self._read_only = False
        self.read_only_before_first_query: bool | None = None
        self.closed = False
        self.executed: list[str] = []

    # -- read-only attribute (psycopg 3 shape: settable property) ----------
    @property
    def read_only(self) -> object:
        if self.read_only_mode == "not_reported":
            return None
        return self._read_only

    @read_only.setter
    def read_only(self, value: object) -> None:
        if self.read_only_mode == "set_fails":
            raise AttributeError("connection does not support read-only mode")
        self._read_only = bool(value)

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self.closed = True

    def execute(self, sql: str, params: tuple) -> "FakeGuardResult":
        if self.read_only_before_first_query is None:
            self.read_only_before_first_query = bool(self._read_only)
        self.executed.append(sql)
        if "current_database" in sql:
            if self.no_current_database:
                return FakeGuardResult([])
            return FakeGuardResult([(self._current_database,)])
        if GUARD_MARKER_TABLE in sql:
            if self.marker_error is not None:
                raise self.marker_error
            return FakeGuardResult([(row[0], row[1], row[2]) for row in self.marker_rows])  # type: ignore[misc]
        if LEDGER_TABLE in sql:
            return FakeGuardResult([(self.ledger_version,)])
        raise AssertionError(f"guard must only issue approved reads: {sql!r}")


class FakeGuardResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class FakeOpener:
    """Injectable connection factory recording which DSN it connected."""

    def __init__(self, connection: FakeGuardConnection | Exception) -> None:
        self.connection = connection
        self.opened_dsns: list[str] = []
        self.open_count = 0

    def __call__(self, dsn: str) -> FakeGuardConnection:
        self.opened_dsns.append(dsn)
        self.open_count += 1
        if isinstance(self.connection, Exception):
            raise self.connection
        return self.connection


def stdlib_dsn_parser(dsn: str) -> dict[str, object]:
    """Stdlib-only DSN parser for offline tests.

    Keeps the shared pytest process free of psycopg (the API suite's
    in-process tripwire contract) while exercising the guard's real
    parse-and-compare logic. The module's psycopg-backed **default**
    parser is proven in a fresh subprocess below — never weakened.
    """
    parts = urlsplit(dsn.strip())
    if not parts.hostname:
        raise ValueError("no host component: not a parseable URI DSN")
    return {
        "dbname": (parts.path.lstrip("/") or None) or "",
        "host": parts.hostname,
        "port": parts.port,
        "user": parts.username,
        "password": None,  # credentials are never materialized in tests
    }


def dsn_with_query(dsn: str, extra: dict[str, str]) -> dict[str, object]:
    """Parser output for a DSN carrying extra conninfo parameters."""
    mapping = dict(stdlib_dsn_parser(dsn))
    mapping.update(extra)
    return mapping


def verified(
    opener: FakeOpener, app: str = APP_DSN, test: str = TEST_DSN
) -> VerifiedTestTarget:
    return verify_test_database(
        app, test, connection_factory=opener, dsn_parser=stdlib_dsn_parser
    )


def blocked_reason(call) -> str:  # small helper: run, expect BLOCKED, return code
    with pytest.raises(GuardBlocked) as err:
        call()
    return err.value.reason


# -- ordered positive path ---------------------------------------------------


def test_successful_positive_verification_evidence() -> None:
    opener = FakeOpener(FakeGuardConnection())
    target = verified(opener)
    assert target.dbname == "attendance_test"
    assert target.marker_schema_version == target.ledger_schema_version == 1
    assert target.marker_created_at == MARKER_CREATED_AT
    assert "attendance_test" in opener.opened_dsns[0]
    assert opener.open_count == 1


def test_only_selects_issued_before_success() -> None:
    opener = FakeOpener(FakeGuardConnection())
    verified(opener)
    non_selects = [
        sql for sql in opener.connection.executed
        if not sql.lstrip().upper().startswith("SELECT")
    ]
    assert non_selects == []


def test_read_only_enforced_before_first_verification_query() -> None:
    """M-2: read-only is set on the connection *before* anything is read."""
    opener = FakeOpener(FakeGuardConnection())
    verified(opener)
    assert opener.connection.read_only_before_first_query is True


def test_read_only_not_supported_by_connection_blocks() -> None:
    opener = FakeOpener(FakeGuardConnection(read_only_mode="set_fails"))
    assert blocked_reason(lambda: verified(opener)) == (
        GuardBlockedReason.READ_ONLY_UNAVAILABLE
    )
    assert opener.connection.executed == []  # nothing was read


def test_read_only_requested_but_not_reported_blocks() -> None:
    opener = FakeOpener(FakeGuardConnection(read_only_mode="not_reported"))
    assert blocked_reason(lambda: verified(opener)) == (
        GuardBlockedReason.READ_ONLY_UNAVAILABLE
    )
    assert opener.connection.executed == []


# -- connection lifecycle (M-3) ----------------------------------------------


def test_connection_closed_on_success() -> None:
    opener = FakeOpener(FakeGuardConnection())
    verified(opener)
    assert opener.connection.closed is True


@pytest.mark.parametrize(
    "connection_kwargs",
    [
        {"dbname": "some_other_db"},
        {"marker_rows": []},
        {"marker_rows": [(GUARD_MARKER_VALUE, MARKER_CREATED_AT, 2)]},
        {"ledger_version": None},
        {"read_only_mode": "set_fails"},
    ],
)
def test_connection_closed_on_blocked_paths(connection_kwargs: dict) -> None:
    connection = FakeGuardConnection(**connection_kwargs)
    opener = FakeOpener(connection)
    with pytest.raises(GuardBlocked):
        verified(opener)
    assert connection.closed is True


def test_connection_closed_when_marker_query_raises() -> None:
    connection = FakeGuardConnection(
        marker_error=RuntimeError("synthetic: driver exploded mid-read")
    )
    opener = FakeOpener(connection)
    assert blocked_reason(lambda: verified(opener)) == (
        GuardBlockedReason.MARKER_QUERY_FAILED
    )
    assert connection.closed is True


def test_no_connection_opened_when_configuration_missing() -> None:
    opener = FakeOpener(FakeGuardConnection())
    with pytest.raises(GuardBlocked):
        verify_test_database(APP_DSN, None, connection_factory=opener, dsn_parser=stdlib_dsn_parser)
    assert opener.open_count == 0


# -- missing configuration ----------------------------------------------------


def test_missing_test_dsn_blocked() -> None:
    with pytest.raises(GuardBlocked) as err:
        verify_test_database(APP_DSN, None, dsn_parser=stdlib_dsn_parser)
    assert err.value.reason == GuardBlockedReason.TEST_DSN_MISSING


def test_blank_test_dsn_blocked() -> None:
    with pytest.raises(GuardBlocked) as err:
        verify_test_database(APP_DSN, "   ", dsn_parser=stdlib_dsn_parser)
    assert err.value.reason == GuardBlockedReason.TEST_DSN_MISSING


def test_missing_app_dsn_blocked() -> None:
    with pytest.raises(GuardBlocked) as err:
        verify_test_database(None, TEST_DSN, dsn_parser=stdlib_dsn_parser)
    assert err.value.reason == GuardBlockedReason.APP_DSN_MISSING


# -- distinctness (M-1) -------------------------------------------------------


def test_identical_targets_blocked() -> None:
    with pytest.raises(GuardBlocked) as err:
        verify_test_database(
            TEST_DSN,
            TEST_DSN,
            connection_factory=FakeOpener(FakeGuardConnection()),
            dsn_parser=stdlib_dsn_parser,
        )
    assert err.value.reason == GuardBlockedReason.TARGETS_NOT_DISTINCT


def test_host_alias_does_not_fake_distinctness() -> None:
    # Same database, but written with a localhost alias: normalization
    # must still call it the same identity (fail closed, not permissive).
    alias_test = "postgresql://u:pw@localhost:5432/attendance_app"
    with pytest.raises(GuardBlocked) as err:
        verify_test_database(
            APP_DSN,
            alias_test,
            connection_factory=FakeOpener(
                FakeGuardConnection(dbname="attendance_app")
            ),
            dsn_parser=stdlib_dsn_parser,
        )
    assert err.value.reason == GuardBlockedReason.TARGETS_NOT_DISTINCT


def test_default_port_equivalent_to_explicit_5432() -> None:
    """A DSN omitting the port is not a *different* target: libpq's
    default is 5432, so the two spellings are the same database."""
    without_port = "postgresql://db_user:pw@127.0.0.1/attendance_app"
    assert blocked_reason(
        lambda: verify_test_database(
            without_port,
            APP_DSN,
            connection_factory=FakeOpener(FakeGuardConnection(dbname="attendance_app")),
            dsn_parser=stdlib_dsn_parser,
        )
    ) == GuardBlockedReason.TARGETS_NOT_DISTINCT


def test_hostaddr_indirection_blocked() -> None:
    """M-1 audit bypass: ``hostaddr`` can point anywhere while ``host``
    (or its absence) looks distinct — must be BLOCKED as ambiguous."""
    def parser(dsn: str) -> dict[str, object]:
        if "hostaddr" in dsn:
            return {
                "dbname": "attendance_app",
                "hostaddr": "10.0.0.5",
                "port": 5432,
            }
        return stdlib_dsn_parser(dsn)

    assert blocked_reason(
        lambda: verify_test_database(
            "postgresql://u:p@10.0.0.5:5432/attendance_app",
            "postgresql://u:p@10.0.0.5:5432/attendance_app?hostaddr=10.0.0.5",
            connection_factory=FakeOpener(FakeGuardConnection(dbname="attendance_app")),
            dsn_parser=parser,
        )
    ) == GuardBlockedReason.IDENTITY_AMBIGUOUS


def test_service_indirection_blocked() -> None:
    def parser(dsn: str) -> dict[str, object]:
        if "service" in dsn:
            return {"service": "prod", "dbname": "attendance_test"}
        return stdlib_dsn_parser(dsn)

    assert blocked_reason(
        lambda: verify_test_database(
            APP_DSN,
            "postgresql:///attendance_test?service=prod",
            connection_factory=FakeOpener(FakeGuardConnection()),
            dsn_parser=parser,
        )
    ) == GuardBlockedReason.IDENTITY_AMBIGUOUS


def test_missing_host_is_ambiguous_not_distinct() -> None:
    """A missing host means libpq's local-socket default — never proof of
    distinctness."""
    def parser(dsn: str) -> dict[str, object]:
        return {"dbname": dsn.rsplit("/", 1)[-1]}

    assert blocked_reason(
        lambda: verify_test_database(
            APP_DSN,
            TEST_DSN,
            connection_factory=FakeOpener(FakeGuardConnection()),
            dsn_parser=parser,
        )
    ) == GuardBlockedReason.IDENTITY_AMBIGUOUS


def test_missing_dbname_is_ambiguous_not_distinct() -> None:
    def parser(dsn: str) -> dict[str, object]:
        return {"host": "127.0.0.1", "port": 5432}

    assert blocked_reason(
        lambda: verify_test_database(
            APP_DSN,
            TEST_DSN,
            connection_factory=FakeOpener(FakeGuardConnection()),
            dsn_parser=parser,
        )
    ) == GuardBlockedReason.IDENTITY_AMBIGUOUS


def test_unparsable_dsn_blocked() -> None:
    with pytest.raises(GuardBlocked) as err:
        verify_test_database(
            "not-a-dsn", TEST_DSN, dsn_parser=stdlib_dsn_parser
        )
    assert err.value.reason == GuardBlockedReason.DSN_UNPARSABLE


def test_connected_target_mismatch_blocked() -> None:
    """The driver-reported server address must agree with the declared
    test target (M-1: use actual connected information)."""
    opener = FakeOpener(
        FakeGuardConnection(dbname="attendance_test", host="10.9.9.9")
    )
    assert blocked_reason(lambda: verified(opener)) == (
        GuardBlockedReason.CONNECTED_TARGET_MISMATCH
    )


def test_connected_port_mismatch_blocked() -> None:
    opener = FakeOpener(FakeGuardConnection(dbname="attendance_test", port=5999))
    assert blocked_reason(lambda: verified(opener)) == (
        GuardBlockedReason.CONNECTED_TARGET_MISMATCH
    )


# -- connection and identity --------------------------------------------------


def test_connection_failure_blocked() -> None:
    def opener(dsn: str) -> FakeGuardConnection:
        raise RuntimeError("synthetic: cannot reach server")

    with pytest.raises(GuardBlocked) as err:
        verify_test_database(
            APP_DSN, TEST_DSN, connection_factory=opener, dsn_parser=stdlib_dsn_parser
        )
    assert err.value.reason == GuardBlockedReason.CONNECTION_FAILED


def test_identity_unverifiable_blocked() -> None:
    # Neither conn.info nor current_database() yields a usable identity:
    # the guard must refuse rather than trust anything else.
    opener = FakeOpener(FakeGuardConnection(no_info=True, no_current_database=True))
    with pytest.raises(GuardBlocked) as err:
        verified(opener)
    assert err.value.reason == GuardBlockedReason.IDENTITY_UNVERIFIABLE


def test_connected_database_name_mismatch_blocked() -> None:
    opener = FakeOpener(FakeGuardConnection(dbname="some_other_db"))
    with pytest.raises(GuardBlocked) as err:
        verified(opener)
    assert err.value.reason == GuardBlockedReason.CONNECTED_DB_MISMATCH


def test_connected_identity_via_current_database_fallback_allowed() -> None:
    # No conn.info (synthetic minimal driver), but current_database()
    # works: identity is still positively read from the connection.
    opener = FakeOpener(FakeGuardConnection(no_info=True))
    target = verified(opener)
    assert target.dbname == "attendance_test"


# -- marker checks ------------------------------------------------------------


def test_marker_table_missing_blocked() -> None:
    opener = FakeOpener(FakeGuardConnection(marker_rows=[]))
    with pytest.raises(GuardBlocked) as err:
        verified(opener)
    assert err.value.reason == GuardBlockedReason.MARKER_TABLE_MISSING


def test_marker_query_failure_distinguished_from_missing_table() -> None:
    """L-4: an unreadable marker is not the same as an absent one."""
    opener = FakeOpener(
        FakeGuardConnection(marker_error=RuntimeError("synthetic: permission denied"))
    )
    assert blocked_reason(lambda: verified(opener)) == (
        GuardBlockedReason.MARKER_QUERY_FAILED
    )


def test_marker_row_ambiguous_multiple_rows_blocked() -> None:
    opener = FakeOpener(FakeGuardConnection(marker_rows=[
        (GUARD_MARKER_VALUE, MARKER_CREATED_AT, 1),
        (GUARD_MARKER_VALUE, MARKER_CREATED_AT, 1),
    ]))
    with pytest.raises(GuardBlocked) as err:
        verified(opener)
    assert err.value.reason == GuardBlockedReason.MARKER_AMBIGUOUS


def test_marker_value_mismatch_blocked() -> None:
    opener = FakeOpener(FakeGuardConnection(marker_rows=[("SOME_OTHER_PHASE", MARKER_CREATED_AT, 1)]))
    with pytest.raises(GuardBlocked) as err:
        verified(opener)
    assert err.value.reason == GuardBlockedReason.MARKER_VALUE_MISMATCH


def test_marker_naive_created_at_blocked() -> None:
    naive = datetime(2026, 11, 1, 8, 0, 0)  # no timezone: ambiguous by contract
    opener = FakeOpener(FakeGuardConnection(marker_rows=[(GUARD_MARKER_VALUE, naive, 1)]))
    with pytest.raises(GuardBlocked) as err:
        verified(opener)
    assert err.value.reason == GuardBlockedReason.MARKER_AMBIGUOUS


def test_marker_schema_version_unusable_blocked() -> None:
    opener = FakeOpener(FakeGuardConnection(marker_rows=[(GUARD_MARKER_VALUE, MARKER_CREATED_AT, None)]))
    with pytest.raises(GuardBlocked) as err:
        verified(opener)
    assert err.value.reason == GuardBlockedReason.SCHEMA_VERSION_UNREADABLE


def test_marker_schema_version_mismatch_blocked() -> None:
    opener = FakeOpener(FakeGuardConnection(marker_rows=[(GUARD_MARKER_VALUE, MARKER_CREATED_AT, 2)], ledger_version=1))
    with pytest.raises(GuardBlocked) as err:
        verified(opener)
    assert err.value.reason == GuardBlockedReason.SCHEMA_VERSION_MISMATCH


def test_marker_claims_version_absent_ledger_blocked() -> None:
    opener = FakeOpener(FakeGuardConnection(ledger_version=None))
    with pytest.raises(GuardBlocked) as err:
        verified(opener)
    assert err.value.reason == GuardBlockedReason.SCHEMA_VERSION_UNREADABLE


# -- destructive-action manifest (check 5, M-5) -------------------------------


def test_destructive_action_allowed_only_in_manifest() -> None:
    opener = FakeOpener(FakeGuardConnection())
    target = verified(opener)
    require_destructive_action(target, "drop_whole_schema", ["drop_whole_schema", "seed_reset"])
    with pytest.raises(GuardBlocked) as err:
        require_destructive_action(target, "restore_hypervisor", ["drop_whole_schema"])
    assert err.value.reason == GuardBlockedReason.ACTION_NOT_IN_MANIFEST


def test_destructive_action_refused_without_verified_target() -> None:
    with pytest.raises(GuardBlocked) as err:
        require_destructive_action(None, "drop_whole_schema", ["drop_whole_schema"])
    assert err.value.reason == GuardBlockedReason.NOT_VERIFIED


def test_destructive_action_refused_for_non_target_object() -> None:
    forged = {"dbname": "attendance_test", "verified": True}
    with pytest.raises(GuardBlocked) as err:
        require_destructive_action(forged, "drop_whole_schema", ["drop_whole_schema"])  # type: ignore[arg-type]
    assert err.value.reason == GuardBlockedReason.NOT_VERIFIED


def test_direct_target_construction_refused() -> None:
    """M-5: the value object cannot be fabricated by an ordinary caller."""
    with pytest.raises(ValueError):
        VerifiedTestTarget(
            dbname="attendance_test",
            host="127.0.0.1",
            port=5432,
            marker_created_at=MARKER_CREATED_AT,
            marker_schema_version=1,
            ledger_schema_version=1,
        )


def test_manifest_as_bare_string_refused() -> None:
    """A string manifest must not pass by substring membership."""
    target = verified(FakeOpener(FakeGuardConnection()))
    with pytest.raises(GuardBlocked) as err:
        require_destructive_action(target, "drop", "drop_whole_schema")  # type: ignore[arg-type]
    assert err.value.reason == GuardBlockedReason.MANIFEST_INVALID


def test_manifest_with_blank_or_non_string_entry_refused() -> None:
    target = verified(FakeOpener(FakeGuardConnection()))
    for bad_manifest in (["drop_whole_schema", "  "], ["drop_whole_schema", 7]):
        with pytest.raises(GuardBlocked) as err:
            require_destructive_action(target, "drop_whole_schema", bad_manifest)
        assert err.value.reason == GuardBlockedReason.MANIFEST_INVALID


def test_manifest_not_iterable_refused() -> None:
    target = verified(FakeOpener(FakeGuardConnection()))
    with pytest.raises(GuardBlocked) as err:
        require_destructive_action(target, "drop_whole_schema", 42)  # type: ignore[arg-type]
    assert err.value.reason == GuardBlockedReason.MANIFEST_INVALID


# -- blocked results stay sanitized -------------------------------------------


def test_blocked_messages_never_contain_dsn_or_credentials() -> None:
    secret_dsn = APP_DSN.replace("attendance_app", "attendance_secret")
    with pytest.raises(GuardBlocked) as err:
        verify_test_database(
            secret_dsn,
            secret_dsn,
            connection_factory=FakeOpener(
                FakeGuardConnection(dbname="attendance_secret")
            ),
            dsn_parser=stdlib_dsn_parser,
        )
    text = str(err.value)
    assert "secret-not-used-here" not in text
    assert "postgresql://" not in text


# -- module hygiene -----------------------------------------------------------


def test_import_hygiene_fresh_process() -> None:
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "import src.test_database_guard;"
        "banned = {'psycopg', 'cv2', 'fastapi', 'uvicorn'};"
        "loaded = banned & set(sys.modules);"
        "assert not loaded, f'unexpected imports: {loaded}';"
        "assert src.test_database_guard.GUARD_MARKER_VALUE == "
        "'VISUAL_ATTENDANCE_PHASE3_TEST_ONLY';"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")


def test_default_psycopg_backed_parser_wiring_fresh_process() -> None:
    """The default (no injection) parser hook resolves to :mod:`src.db`'s
    lazy ``parse_dsn`` and reads declared identity correctly. Runs in a
    fresh subprocess because it legitimately loads psycopg (the real DSN
    parser); that is exactly the process-isolation pattern the repo's
    hygiene suites already use.
    """
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "from src.test_database_guard import _parse_dsn_mapping;"
        "info = _parse_dsn_mapping("
        "'postgresql://u:p@127.0.0.1:5432/attendance_test');"
        "assert info['dbname'] == 'attendance_test', info;"
        "assert str(info['password']).startswith('*'), info;"
        "assert not _parse_dsn_mapping('   '), 'blank DSN must refuse';"
        "assert not _parse_dsn_mapping('not-a-valid-dsn!!'), "
        "'syntax garbage must refuse (typed, sanitized)';"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")


# Supporting test hygiene (not an in-process assertion): every scenario
# above verifies through `connection_factory` fakes and the injected
# stdlib-only `dsn_parser`, so this suite never loads psycopg in the
# shared pytest process (test_api.py's in-process tripwire stays honest
# regardless of suite ordering). The psycopg-backed default path is
# covered by the fresh-process tests above and the existing test_db.py
# parser suite — never weakened.
