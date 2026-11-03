"""Positive-verification guard for the dedicated Phase 3 integration-test
database (Step 1.5b, approved OQ-5 contract from ``DB_DESIGN.md``
Sections 5.2/5.3 and the owner's recorded decisions).

Purpose
-------

Tier-2 (PostgreSQL-connecting) tests may touch a database only after
**all** ordered checks pass positively; any missing, ambiguous,
inconsistent, or failed condition produces a typed :class:`GuardBlocked`
result — fail closed, no write, no teardown, no guessing, and never a
fallback to the application database. Name patterns and substrings are
**never** sufficient: the only accepted proof is server-side truth
inside the positively connected target.

Approved fixed contract
-----------------------

* marker table: :data:`GUARD_MARKER_TABLE` (``_phase3_test_guard``)
* marker literal: :data:`GUARD_MARKER_VALUE`
  (``VISUAL_ATTENDANCE_PHASE3_TEST_ONLY``)
* marker columns: ``marker TEXT NOT NULL UNIQUE``,
  ``created_at TIMESTAMPTZ NOT NULL``,
  ``schema_version INTEGER NOT NULL`` — the last must equal the value
  recorded in the migration ledger
  (:attr:`src.db_migrate.LEDGER_TABLE`) at check time.
* The marker is **established by the owner** when provisioning the test
  database; establishing it is itself a database write and therefore
  outside every automated gate (provisioning is owner-run).

Read-only until verified
------------------------

Read-only is **enforced on the connection**, not merely a convention of
this module's own queries: before the first verification query the
guard sets ``connection.read_only = True`` (psycopg 3's attribute, which
makes the session's transactions read-only server-side) and refuses
with ``READ_ONLY_UNAVAILABLE`` when that cannot be established. The
guard's statements are SELECTs as well; the two layers are independent
so a future edit cannot silently lose the read-only property.

Connection lifecycle
--------------------

The guard opens exactly one connection and **always closes it** — on
success and on every blocked path (``try``/``finally``). Nothing is
committed: the verification transaction is left to be rolled back by
``close()``, so no write can be introduced by ending it.

Identity evidence
-----------------

Distinctness is only ever *positively* established. Declared identity is
read from the configured DSNs (dbname, host, port) and connected
identity from the connection object itself (``connection.info`` then
``current_database()``). Because a DSN can point somewhere other than it
appears to — ``hostaddr``, ``service`` indirection, a missing host (the
libpq local-socket default) — any identity component that cannot be read
for **both** targets is ambiguity, never permission: the guard returns
``IDENTITY_AMBIGUOUS``. The connected target is compared against the
declared test target and against the application target as well.

Trust model (deliberately modest)
---------------------------------

:class:`VerifiedTestTarget` is a cooperative value object: constructing
one directly raises, so an ordinary caller cannot accidentally forge
"verified" status. This is **not** a security boundary — in-process
Python can reach the module's private proof object — and the module
makes no claim to resist malicious in-process code. The guard's job is
to stop *mistakes*: a test that was never verified, a manifest typo, a
marker copied from the wrong database.

Module hygiene: no driver import at module level; the default opener
imports the lazy :mod:`src.db` helpers only when a verification actually
connects. Nothing here touches real runtime data: every offline test
drives it with synthetic fake connections.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from src.db_migrate import LEDGER_TABLE

__all__ = [
    "GUARD_MARKER_TABLE",
    "GUARD_MARKER_VALUE",
    "GuardBlockedReason",
    "GuardBlocked",
    "VerifiedTestTarget",
    "require_destructive_action",
    "verify_test_database",
]

#: Approved marker table name (exists inside the test database only).
GUARD_MARKER_TABLE: Final[str] = "_phase3_test_guard"

#: Approved fixed, non-secret marker literal (phase-tagged).
GUARD_MARKER_VALUE: Final[str] = "VISUAL_ATTENDANCE_PHASE3_TEST_ONLY"

_MARKERS_READ_SQL: Final[str] = (
    f"SELECT marker, created_at, schema_version FROM {GUARD_MARKER_TABLE}"
)
_CURRENT_DATABASE_SQL: Final[str] = "SELECT current_database()"
_LEDGER_VERSION_SQL: Final[str] = f"SELECT MAX(version) FROM {LEDGER_TABLE}"

#: libpq's documented default port: a DSN that omits the port does not
#: mean "unknown target", it means this port. Treating it as unknown
#: would refuse ordinary DSNs; treating it as *anything else* would be an
#: invention.
_DEFAULT_PORT: Final[int] = 5432

#: Conninfo keys that reroute a connection away from the host named in
#: the same DSN. A DSN carrying either one cannot be positively compared
#: with a plain host/port DSN, so the guard refuses (ambiguity) instead of
#: assuming the targets differ.
_REROUTING_KEYS: Final[frozenset[str]] = frozenset({"hostaddr", "service"})

#: Textual host variants that name this single-machine deployment's local
#: server. Collapsing them keeps the comparison honest in the direction it
#: must never fail toward (two spellings of one server are one server).
_LOCAL_HOST_ALIASES: Final[frozenset[str]] = frozenset(
    {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
)
_LOCAL_HOST_TOKEN: Final[str] = "_local_server_"

#: Private proof object: only :func:`verify_test_database` can hand out a
#: :class:`VerifiedTestTarget`. Cooperative by design (see module
#: docstring) — it stops mistakes, not hostile in-process code.
_TARGET_PROOF: Final[object] = object()


class GuardBlockedReason:
    """Machine-readable fail-closed reason codes (one per ordered check)."""

    TEST_DSN_MISSING: Final[str] = "TEST_DSN_MISSING"
    APP_DSN_MISSING: Final[str] = "APP_DSN_MISSING"
    DSN_UNPARSABLE: Final[str] = "DSN_UNPARSABLE"
    IDENTITY_AMBIGUOUS: Final[str] = "IDENTITY_AMBIGUOUS"
    TARGETS_NOT_DISTINCT: Final[str] = "TARGETS_NOT_DISTINCT"
    CONNECTION_FAILED: Final[str] = "CONNECTION_FAILED"
    READ_ONLY_UNAVAILABLE: Final[str] = "READ_ONLY_UNAVAILABLE"
    IDENTITY_UNVERIFIABLE: Final[str] = "IDENTITY_UNVERIFIABLE"
    CONNECTED_DB_MISMATCH: Final[str] = "CONNECTED_DB_MISMATCH"
    CONNECTED_TARGET_MISMATCH: Final[str] = "CONNECTED_TARGET_MISMATCH"
    MARKER_TABLE_MISSING: Final[str] = "MARKER_TABLE_MISSING"
    MARKER_QUERY_FAILED: Final[str] = "MARKER_QUERY_FAILED"
    MARKER_AMBIGUOUS: Final[str] = "MARKER_AMBIGUOUS"
    MARKER_VALUE_MISMATCH: Final[str] = "MARKER_VALUE_MISMATCH"
    SCHEMA_VERSION_UNREADABLE: Final[str] = "SCHEMA_VERSION_UNREADABLE"
    SCHEMA_VERSION_MISMATCH: Final[str] = "SCHEMA_VERSION_MISMATCH"
    NOT_VERIFIED: Final[str] = "NOT_VERIFIED"
    MANIFEST_INVALID: Final[str] = "MANIFEST_INVALID"
    ACTION_NOT_IN_MANIFEST: Final[str] = "ACTION_NOT_IN_MANIFEST"


class GuardBlocked(RuntimeError):
    """Typed fail-closed result.

    Message discipline (sanitized): the reason code plus one static,
    owner-reviewable sentence — never a DSN, credential, raw driver
    error text, or traceback.
    """

    def __init__(self, reason: str, note: str) -> None:
        super().__init__(f"{reason}: {note}")
        self.reason = reason


@dataclass(frozen=True)
class VerifiedTestTarget:
    """Positive-verification evidence (safe to log/show: no DSN, no
    credentials; identity fields are server-confirmed values).

    Construct it only through :func:`verify_test_database`: direct
    construction raises ``ValueError``. The restriction is a
    mistake-guard for ordinary callers, **not** a security boundary
    (module docstring).
    """

    dbname: str
    host: str
    port: object
    marker_created_at: datetime
    marker_schema_version: int
    ledger_schema_version: int
    #: Private provenance proof; never passed by callers.
    _proof: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _TARGET_PROOF:
            raise ValueError(
                "VerifiedTestTarget must be produced by verify_test_database(); "
                "direct construction is refused"
            )


# ---------------------------------------------------------------------------
# Identity helpers (declared and connected)
# ---------------------------------------------------------------------------


def _normalized_identity(
    mapping: Mapping[str, object],
) -> tuple[str, str, int] | None:
    """Normalized ``(dbname, host, port)`` for target comparison, or
    ``None`` when the identity cannot be positively read.

    ``None`` (refuse) is returned for:

    * a DSN carrying ``hostaddr`` or ``service`` — both reroute the
      connection, so the visible host/port do not describe the target;
    * a missing/blank ``dbname`` or ``host`` — a missing host means libpq's
      local-socket default, which cannot be compared with a TCP host, and
      a missing dbname cannot identify anything;
    * an unparseable port.

    A missing port is **not** ambiguous: libpq's default is fixed
    (:data:`_DEFAULT_PORT`).

    User and password are deliberately excluded: different credentials on
    the same database never make it a different database, so credential
    differences must not establish distinctness.
    """
    if any(key in mapping for key in _REROUTING_KEYS):
        return None

    dbname = mapping.get("dbname")
    if not isinstance(dbname, str) or not dbname.strip():
        return None

    host_raw = mapping.get("host")
    if not isinstance(host_raw, str) or not host_raw.strip():
        return None
    host = host_raw.strip().lower()
    if host in _LOCAL_HOST_ALIASES:
        host = _LOCAL_HOST_TOKEN

    port = mapping.get("port")
    if port is None:
        port_value = _DEFAULT_PORT
    elif isinstance(port, bool) or not isinstance(port, (int, str)):
        return None
    else:
        try:
            port_value = int(str(port))
        except ValueError:
            return None

    return (dbname.strip(), host, port_value)


def _parse_dsn_mapping(
    dsn: str | None,
    parser: Callable[[str], Mapping[str, object]] | None = None,
) -> Mapping[str, object]:
    """Parse a DSN via the injected ``parser`` (default: :mod:`src.db`'s
    lazy, redaction-preserving ``parse_dsn``).

    A guard input the guard cannot interpret is ambiguity → the caller
    refuses fail-closed; no exception text is surfaced upward.

    The injectable hook exists for offline hygiene: verification itself
    needs only *declared* identity parameters, so offline tests inject a
    stdlib-only parser and keep the real driver unloaded in their pytest
    process (the API suite's ``psycopg should not be imported`` tripwire
    stays honest). The psycopg-backed default is exercised in fresh
    subprocesses by the guard test suite — never weakened, only
    process-isolated, exactly like the repository's other hygiene
    contracts.
    """
    if parser is None:
        from src.db import parse_dsn  # lazy, redaction-preserving

        parser = parse_dsn
    if dsn is None or not dsn.strip():
        return {}
    try:
        return dict(parser(dsn))
    except (ValueError, RuntimeError):
        return {}


def _connected_dbname(connection: Any) -> str | None:
    """Read the connected database name from the connection object:
    driver-reported ``connection.info.dbname`` first (as psycopg reports
    it from the established connection), then ``current_database()``.
    ``None`` means "cannot positively verify" — never a fallback."""
    info = getattr(connection, "info", None)
    dbname = getattr(info, "dbname", None) if info is not None else None
    if isinstance(dbname, str) and dbname.strip():
        return dbname
    try:
        row = connection.execute(_CURRENT_DATABASE_SQL, ()).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    value = row[0] if len(row) else None
    return value if isinstance(value, str) and value.strip() else None


def _connected_host_port(connection: Any) -> tuple[str | None, object]:
    """Connected host/port as reported by the driver, or ``(None, None)``
    when unavailable. Region values are never synthesized here."""
    info = getattr(connection, "info", None)
    host = getattr(info, "host", None) if info is not None else None
    port = getattr(info, "port", None) if info is not None else None
    if host is not None and not isinstance(host, str):
        host = None
    return (host, port)


# ---------------------------------------------------------------------------
# Read-only enforcement and connection lifecycle (M-2 / M-3)
# ---------------------------------------------------------------------------


def _enforce_read_only(connection: Any) -> None:
    """Put the session in read-only mode before any verification query.

    psycopg 3 exposes ``Connection.read_only``; setting it makes every
    transaction on the session read-only server-side, so a write attempted
    by any later code path is refused by PostgreSQL rather than by
    convention. The value is read back afterwards: a connection whose
    read-only attribute is not observable is refused (fail closed), since
    the approved contract requires *enforced* read-only, not asserted.
    """
    try:
        connection.read_only = True
    except Exception:
        raise GuardBlocked(
            GuardBlockedReason.READ_ONLY_UNAVAILABLE,
            "the test connection could not be put in read-only mode; "
            "refusing to verify",
        ) from None
    if getattr(connection, "read_only", None) is not True:
        raise GuardBlocked(
            GuardBlockedReason.READ_ONLY_UNAVAILABLE,
            "the test connection did not report read-only mode after it "
            "was requested; refusing to verify",
        )


def _close_quietly(connection: Any) -> None:
    """Close the verification connection; never mask the original error."""
    close = getattr(connection, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Marker and ledger reads (L-4: missing / failed / ambiguous are distinct)
# ---------------------------------------------------------------------------

#: Marker read outcomes: a successful single row, an absent table or no
#: row, a driver/read failure, or more than one row.
_MARKER_OK: Final[str] = "ok"
_MARKER_MISSING: Final[str] = "missing"
_MARKER_FAILED: Final[str] = "failed"
_MARKER_AMBIGUOUS: Final[str] = "ambiguous"


def _read_marker_row(connection: Any) -> tuple[str, tuple[Any, ...] | None]:
    """Read the marker rows, distinguishing the three failure shapes."""
    try:
        result = connection.execute(_MARKERS_READ_SQL, ())
        rows = result.fetchall()
    except Exception:
        return (_MARKER_FAILED, None)
    if not rows:
        return (_MARKER_MISSING, None)
    if len(rows) != 1:
        return (_MARKER_AMBIGUOUS, None)
    return (_MARKER_OK, tuple(rows[0]))


def _read_ledger_version(connection: Any) -> int | None:
    try:
        result = connection.execute(_LEDGER_VERSION_SQL, ())
        row = result.fetchone()
    except Exception:
        return None
    if row is None:
        return None
    version = row[0] if len(row) else None
    if isinstance(version, int) and not isinstance(version, bool) and version >= 1:
        return version
    return None


# ---------------------------------------------------------------------------
# Ordered positive-verification checks (fail closed, in approved order)
# ---------------------------------------------------------------------------


def _default_connection_factory(dsn: str) -> Any:
    """Real opener (lazy): psycopg only when verification actually runs."""
    from src.db import open_connection  # lazy: preserves import hygiene

    return open_connection(dsn)


def verify_test_database(
    app_dsn: str | None,
    test_dsn: str | None,
    *,
    connection_factory: Callable[[str], Any] | None = None,
    dsn_parser: Callable[[str], Mapping[str, object]] | None = None,
) -> VerifiedTestTarget:
    """Run the approved ordered positive-verification checks.

    Parameters
    ----------
    app_dsn / test_dsn:
        The configured ``ATTENDANCE_DATABASE_URL`` and
        ``ATTENDANCE_TEST_DATABASE_URL`` values. **Both required** for
        the distinctness comparison (approved tightening); either
        blank/missing → fail closed before anything else happens.
    connection_factory:
        Injectable opener (offline tests pass synthetic connections);
        the default opens a real psycopg connection via :mod:`src.db`.
    dsn_parser:
        Injectable DSN parser for the *declared*-identity comparison
        (offline tests inject a stdlib-only parser and keep any driver
        unloaded in their process); the default is :mod:`src.db`'s
        lazy ``parse_dsn``.

    Returns
    -------
    VerifiedTestTarget
        Only after every check passes positively.

    Raises
    ------
    GuardBlocked
        For each missing / ambiguous / inconsistent / unverifiable
        condition, before any destructive anything; messages never
        contain DSNs, credentials, host details, or driver text.
    """
    opener = connection_factory or _default_connection_factory

    # Check 1: explicit test DSN (never a blank-DSN local-default fallback).
    if test_dsn is None or not test_dsn.strip():
        raise GuardBlocked(
            GuardBlockedReason.TEST_DSN_MISSING,
            "no explicit ATTENDANCE_TEST_DATABASE_URL is configured; "
            "refusing to verify any target",
        )
    # Required companion (approved tightening): the distinctness check
    # must have the application's target to compare against.
    if app_dsn is None or not app_dsn.strip():
        raise GuardBlocked(
            GuardBlockedReason.APP_DSN_MISSING,
            "ATTENDANCE_DATABASE_URL must also be configured so the test "
            "target can be proven distinct from it",
        )

    app_mapping = _parse_dsn_mapping(app_dsn, dsn_parser)
    test_mapping = _parse_dsn_mapping(test_dsn, dsn_parser)
    if not app_mapping or not test_mapping:
        raise GuardBlocked(
            GuardBlockedReason.DSN_UNPARSABLE,
            "a configured DSN could not be parsed; refusing to guess a "
            "target from unverifiable syntax",
        )

    # Check 4 (declared half): both identities must be positively readable
    # before any comparison, and they must differ.
    app_identity = _normalized_identity(app_mapping)
    test_identity = _normalized_identity(test_mapping)
    if app_identity is None or test_identity is None:
        raise GuardBlocked(
            GuardBlockedReason.IDENTITY_AMBIGUOUS,
            "the configured targets cannot both be positively identified "
            "(hostaddr/service indirection, a missing host or database "
            "name, or an unusable port); refusing to assume they differ",
        )
    if app_identity == test_identity:
        raise GuardBlocked(
            GuardBlockedReason.TARGETS_NOT_DISTINCT,
            "the configured application and test DSNs describe the same "
            "database identity",
        )

    # Check 2: connect through the exact test DSN. Connection failure is
    # fail-closed BLOCKED.
    try:
        connection = opener(test_dsn)
    except Exception:
        raise GuardBlocked(
            GuardBlockedReason.CONNECTION_FAILED,
            "could not connect through the configured test DSN",
        ) from None

    # From here on the connection is closed on every path (M-3).
    try:
        # Check 2a: read-only before any verification query (M-2).
        _enforce_read_only(connection)

        connected_dbname = _connected_dbname(connection)
        if connected_dbname is None:
            raise GuardBlocked(
                GuardBlockedReason.IDENTITY_UNVERIFIABLE,
                "the connected database identity could not be read from the "
                "connection object",
            )
        if connected_dbname != test_identity[0]:
            raise GuardBlocked(
                GuardBlockedReason.CONNECTED_DB_MISMATCH,
                "the connected database differs from the configured test "
                "target",
            )

        # Check 2b: when the driver reports the connected host/port, they
        # must agree with the declared test target — a redirect the
        # driver can see is a different target, not a detail (M-1).
        connected_host, connected_port = _connected_host_port(connection)
        if connected_host is not None:
            connected_identity_map = {
                "dbname": connected_dbname,
                "host": connected_host,
                "port": connected_port,
            }
            connected_identity = _normalized_identity(connected_identity_map)
            if (
                connected_identity is not None
                and connected_identity != test_identity
            ):
                raise GuardBlocked(
                    GuardBlockedReason.CONNECTED_TARGET_MISMATCH,
                    "the connected server address differs from the configured "
                    "test target",
                )
            # The connected target must also differ from the application's.
            if connected_identity is not None and connected_identity == app_identity:
                raise GuardBlocked(
                    GuardBlockedReason.TARGETS_NOT_DISTINCT,
                    "the connected test target is the application's database",
                )

        # Check 3: the marker is server-side truth inside the target.
        marker_status, marker_row = _read_marker_row(connection)
        if marker_status == _MARKER_MISSING:
            raise GuardBlocked(
                GuardBlockedReason.MARKER_TABLE_MISSING,
                f"{GUARD_MARKER_TABLE} is absent or empty in the connected "
                "target: no positive marker evidence",
            )
        if marker_status == _MARKER_FAILED:
            raise GuardBlocked(
                GuardBlockedReason.MARKER_QUERY_FAILED,
                f"the {GUARD_MARKER_TABLE} marker could not be read (driver "
                "error); refusing on unreadable evidence",
            )
        if marker_status == _MARKER_AMBIGUOUS:
            raise GuardBlocked(
                GuardBlockedReason.MARKER_AMBIGUOUS,
                f"{GUARD_MARKER_TABLE} returned more than one row; the "
                "marker is ambiguous",
            )
        assert marker_row is not None  # _MARKER_OK implies exactly one row
        marker_value, created_at, marker_schema_version = marker_row
        if marker_value != GUARD_MARKER_VALUE:
            raise GuardBlocked(
                GuardBlockedReason.MARKER_VALUE_MISMATCH,
                "the marker row does not carry the approved literal",
            )
        if not isinstance(created_at, datetime) or created_at.tzinfo is None:
            raise GuardBlocked(
                GuardBlockedReason.MARKER_AMBIGUOUS,
                "the marker row's created_at is missing or naive; refusing on "
                "ambiguity",
            )
        if (
            isinstance(marker_schema_version, bool)
            or not isinstance(marker_schema_version, int)
            or marker_schema_version < 1
        ):
            raise GuardBlocked(
                GuardBlockedReason.SCHEMA_VERSION_UNREADABLE,
                "the marker row's schema_version is missing or not a usable "
                "positive integer",
            )

        # Ledger cross-check: the marker's claimed version must match the
        # applied migration ledger at check time (approved marker contract).
        ledger_version = _read_ledger_version(connection)
        if ledger_version is None:
            raise GuardBlocked(
                GuardBlockedReason.SCHEMA_VERSION_UNREADABLE,
                "the migration ledger is absent or unreadable, so the marker's "
                "schema claim cannot be positively matched",
            )
        if marker_schema_version != ledger_version:
            raise GuardBlocked(
                GuardBlockedReason.SCHEMA_VERSION_MISMATCH,
                "the marker claims a different schema version than the "
                "applied ledger records",
            )

        return VerifiedTestTarget(
            dbname=connected_dbname,
            host=(
                connected_host
                if isinstance(connected_host, str)
                else test_identity[1]
            ),
            port=(
                connected_port if connected_port is not None else test_identity[2]
            ),
            marker_created_at=created_at,
            marker_schema_version=marker_schema_version,
            ledger_schema_version=ledger_version,
            _proof=_TARGET_PROOF,
        )
    finally:
        _close_quietly(connection)


# ---------------------------------------------------------------------------
# Check 5: destructive-action manifest
# ---------------------------------------------------------------------------


def require_destructive_action(
    target: VerifiedTestTarget | None,
    action: str,
    manifest: Iterable[str],
) -> None:
    """Check 5: permit one named destructive operation for a positively
    verified target **only** when it is listed in the authorized
    destructive-action manifest.

    A ``None``/unverified target refuses everything; a malformed manifest
    (not an iterable of non-blank strings — a bare string is refused so
    ``"drop" in "drop_tables"`` cannot pass by substring) refuses
    everything; an unlisted action is refused without attempting it.
    """
    if not isinstance(target, VerifiedTestTarget):
        raise GuardBlocked(
            GuardBlockedReason.NOT_VERIFIED,
            "no positively verified test target exists in this context, "
            "so no destructive operation is allowed",
        )
    if target._proof is not _TARGET_PROOF:  # defence in depth; cannot occur
        raise GuardBlocked(
            GuardBlockedReason.NOT_VERIFIED,
            "the supplied target was not produced by verification",
        )
    if not isinstance(action, str) or not action.strip():
        raise GuardBlocked(
            GuardBlockedReason.ACTION_NOT_IN_MANIFEST,
            "the requested operation name is empty or malformed",
        )
    if isinstance(manifest, (str, bytes)) or not isinstance(manifest, Iterable):
        raise GuardBlocked(
            GuardBlockedReason.MANIFEST_INVALID,
            "the destructive-action manifest must be a collection of "
            "operation names, not a bare string",
        )
    allowed: set[str] = set()
    for entry in manifest:
        if not isinstance(entry, str) or not entry.strip():
            raise GuardBlocked(
                GuardBlockedReason.MANIFEST_INVALID,
                "the destructive-action manifest contains a blank or "
                "non-string entry",
            )
        allowed.add(entry)
    if action not in allowed:
        raise GuardBlocked(
            GuardBlockedReason.ACTION_NOT_IN_MANIFEST,
            f"operation {action!r} is not listed in the authorized "
            "destructive-action manifest",
        )
