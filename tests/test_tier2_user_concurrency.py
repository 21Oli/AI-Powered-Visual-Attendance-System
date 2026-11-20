"""Tier-2 PostgreSQL concurrency verification for user administration (P5.6).

**Opt-in and currently BLOCKED.** The module skips itself in every ordinary
offline run, and it also skips — with an explicit ``BLOCKED`` reason — when the
verified test database does not contain the ``app_user`` / ``user_session``
schema. The owner has to apply ``migrations/V002__security.sql`` to that
database with a privileged role: the configured DSN authenticates as the
least-privilege runtime role, which has no ``CREATE`` on schema ``public``, so
this suite can neither create nor alter schema. The owner runs
``tools/apply_v002_tier2_test_database.sql`` (privileged single-transaction
apply of V002 verbatim, its ledger row and the runtime grants); the runbook is
in ``docs/PHASE5_IMPLEMENTATION_PLAN.md`` §10.1.

Opt-in contract
---------------

Four independent conditions must hold before a byte leaves the process:

1. ``ATTENDANCE_TIER2_OPT_IN=1`` — explicit, per-shell opt-in. Without it every
   item here is *skipped* (never silently "passed"), so the offline suite cannot
   drift into connecting to anything.
2. ``ATTENDANCE_TEST_DATABASE_URL`` — the dedicated test target.
3. ``ATTENDANCE_DATABASE_URL`` — required **only** so the existing guard can
   prove the test target differs from the application target. It is never
   handed to a repository.
4. ``ATTENDANCE_TIER2_CLEANUP_DATABASE_URL`` — a **separate, test-only**
   credential for teardown. The application/runtime role holds no ``DELETE``
   at all (see ``tools/apply_v002_tier2_test_database.sql``), so this suite
   removes its synthetic rows through the dedicated
   ``attendance_tier2_cleanup`` role, which has ``DELETE`` on the two security
   tables and nothing else. It is never used by the application, and without
   it the scenarios refuse to run rather than leave synthetic rows behind.

Then, and only then, :func:`src.test_database_guard.verify_test_database` runs
its ordered positive verification. Any blocked outcome skips the whole module
with the guard's own typed reason code.

What these tests prove (when runnable)
--------------------------------------

Each scenario uses independent connections, a ``threading.Barrier`` so both
transactions are in flight together, and asserts the **final committed database
state** — not just which call returned:

1. Two simultaneous first-Admin provisioning attempts.
2. Two Admins deactivating each other concurrently.
3. Two Admins demoting each other concurrently.
4. Concurrent Admin creation and Admin demotion.
5. Safe non-Admin deactivation while exactly one active Admin exists.
6. Safe non-Admin role change while exactly one active Admin exists.
7. The enforced ``READ COMMITTED`` isolation, proven with a session whose
   default is ``REPEATABLE READ``.

Row hygiene
-----------

No ``CREATE``/``DROP``/``ALTER``/``TRUNCATE`` and no migration execution. Each
test refuses to run if the database already contains active Admin rows it did
not create (it never deactivates or deletes someone else's rows), and removes
only the synthetic ``T2-UC-*`` rows plus their sessions that it created.

Teardown never uses the application credential: the runtime role has no
``DELETE`` privilege at all. It uses the separate test-only cleanup credential,
inspected before the first scenario (same database, same cluster by
   ``pg_control_system().system_identifier``, distinct role, not a superuser,
   ``DELETE`` on both tables and nothing else, plus column-level ``SELECT`` on
   ``user_id`` in those tables only, all read as *effective* privileges
   (``has_table_privilege``/``has_column_privilege`` plus the raw column ACLs),
   so a grant inherited through another role is caught as well), and its
   deletions are verified through the
   runtime connection — a surviving row fails the test instead of being silently
   left behind.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Final

import pytest

from src.config import AppSettings, load_settings
from src.db import open_connection
from src.db_security_repository import open_user_repository
from src.test_database_guard import (
    GuardBlocked,
    VerifiedTestTarget,
    require_destructive_action,
    verify_test_database,
)
from src.user import Role, User
from src.user_repository import (
    LastAdminProtectionError,
    UserRepository,
)

TIER2_OPT_IN_ENV: Final[str] = "ATTENDANCE_TIER2_OPT_IN"

#: Separate, test-only teardown credential. Never the application DSN.
CLEANUP_DSN_ENV: Final[str] = "ATTENDANCE_TIER2_CLEANUP_DATABASE_URL"

#: The dedicated teardown role the owner creates for this suite. It holds
#: ``DELETE`` on the two security tables and nothing else, plus column-level
#: ``SELECT`` on ``user_id`` in those tables only (the column that drives its
#: ``DELETE ... WHERE user_id = ANY(%s)`` queries), so the application role
#: never needs DELETE.
CLEANUP_ROLE: Final[str] = "attendance_tier2_cleanup"

#: The role ``ATTENDANCE_TEST_DATABASE_URL`` authenticates as. The cleanup
#: credential's *effective* privileges are inspected for both roles, because a
#: role membership alone would hand one role the other's privileges.
RUNTIME_ROLE: Final[str] = "attendance_tier2_runtime"

_CLEANUP_BLOCK_REASON: Final[str] = (
    f"BLOCKED: {CLEANUP_DSN_ENV} is not configured; the application/runtime "
    "role holds no DELETE, so synthetic T2-UC-* rows must be removed by a "
    f"separate, test-only {CLEANUP_ROLE} credential (see "
    "docs/PHASE5_IMPLEMENTATION_PLAN.md section 10.1)"
)

#: Every row this suite creates carries this prefix; nothing else is touched.
SYNTHETIC_ID_PREFIX: Final[str] = "T2-UC"

_BLOCK_MESSAGES: Final[dict[str, str]] = {
    "OPT_IN_MISSING": (
        f"{TIER2_OPT_IN_ENV}=1 is not set; Tier-2 verification is opt-in"
    ),
    "TEST_DSN_MISSING": "no explicit ATTENDANCE_TEST_DATABASE_URL is configured",
    "APP_DSN_MISSING": (
        "ATTENDANCE_DATABASE_URL must also be configured so the guard can "
        "prove the test target is distinct from it"
    ),
    "NOT_VERIFIED": (
        "no positively verified test target was supplied; refusing to touch "
        "a database"
    ),
}

HASH = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"
NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)

#: Exact reason reported when the owner has not migrated the test database.
#: The suite cannot migrate it itself: the configured DSN authenticates as the
#: least-privilege runtime role, which has no CREATE on schema public and does
#: not own the existing tables. The owner runbook lives in
#: ``docs/PHASE5_IMPLEMENTATION_PLAN.md`` §10.1.
SCHEMA_BLOCK_REASON: Final[str] = (
    "BLOCKED: the verified test database has no app_user/user_session schema; "
    "ATTENDANCE_TEST_DATABASE_URL authenticates as the least-privilege "
    "runtime role, which cannot create them; the owner must run "
    "tools/apply_v002_tier2_test_database.sql with a privileged role (see "
    "docs/PHASE5_IMPLEMENTATION_PLAN.md section 10.1) (this suite never "
    "creates schema)"
)


def tier2_opt_in_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the explicit Tier-2 opt-in switch is set to exactly ``"1"``."""
    source = os.environ if environ is None else environ
    return str(source.get(TIER2_OPT_IN_ENV, "")).strip() == "1"


def _configured(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def tier2_block_reason(
    settings: AppSettings, environ: Mapping[str, str] | None = None
) -> str | None:
    """Return the first missing pre-flight code, or ``None`` when complete."""
    if not tier2_opt_in_enabled(environ):
        return "OPT_IN_MISSING"
    if not _configured(settings.test_database_url):
        return "TEST_DSN_MISSING"
    if not _configured(settings.database_url):
        return "APP_DSN_MISSING"
    return None


def verified_target_or_skip(
    *,
    settings: AppSettings | None = None,
    environ: Mapping[str, str] | None = None,
) -> VerifiedTestTarget:
    """Fixture helper: turn any blocked outcome into an explicit skip."""
    resolved = settings if settings is not None else load_settings()
    reason = tier2_block_reason(resolved, environ)
    if reason is not None:
        pytest.skip(f"tier2 blocked: {reason}: {_BLOCK_MESSAGES[reason]}")
    try:
        return verify_test_database(
            resolved.database_url, resolved.test_database_url
        )
    except GuardBlocked as exc:
        pytest.skip(f"tier2 blocked: {exc}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SKIP_REASON: Final[str] = (
    "Tier-2 PostgreSQL verification is opt-in: set "
    f"{TIER2_OPT_IN_ENV}=1 with a guarded ATTENDANCE_TEST_DATABASE_URL"
)

pytestmark = [
    pytest.mark.tier2,
    pytest.mark.skipif(not tier2_opt_in_enabled(), reason=_SKIP_REASON),
]


@pytest.fixture(scope="module")
def tier2_settings() -> AppSettings:
    return load_settings()


@pytest.fixture(scope="module")
def verified_target(tier2_settings: AppSettings) -> VerifiedTestTarget:
    return verified_target_or_skip(settings=tier2_settings)


@pytest.fixture(scope="module")
def test_dsn(
    verified_target: VerifiedTestTarget, tier2_settings: AppSettings
) -> str:
    value = tier2_settings.test_database_url
    if not _configured(value):
        pytest.skip("tier2 blocked: TEST_DSN_MISSING")
    require_destructive_action(
        verified_target, "user_concurrency_test", ("user_concurrency_test",)
    )
    _require_schema(value)  # type: ignore[arg-type]
    return value  # type: ignore[return-value]


@pytest.fixture(scope="module")
def cleanup_dsn(test_dsn: str, verified_target: VerifiedTestTarget) -> str:
    """The dedicated teardown credential, validated before any row is created.

    Deliberately not the application DSN: the runtime role holds no DELETE.
    Missing -> the suite skips BLOCKED (nothing is created, so nothing can be
    left behind). Present but wrong -> pytest.fail, never a silent downgrade.
    """
    value = os.environ.get(CLEANUP_DSN_ENV, "").strip()
    if not value:
        pytest.skip(_CLEANUP_BLOCK_REASON)
    require_destructive_action(
        verified_target, "user_concurrency_test", ("user_concurrency_test",)
    )
    _require_cleanup_credential(value, verified_target)
    return value


#: Identity of the connection the cleanup credential actually authenticated.
_CLEANUP_IDENTITY_SQL: Final[str] = (
    "SELECT current_database(), current_user, current_setting('is_superuser')"
)

#: Effective table privileges for both Tier-2 roles. ``has_table_privilege``
#: includes anything inherited through role membership.
_CLEANUP_TABLE_PRIVILEGES_SQL: Final[str] = (
    "SELECT r.role_name, t.table_name, p.priv, "
    "has_table_privilege(r.role_name::name, 'public.' || t.table_name, "
    "p.priv::text) "
    "FROM unnest(%s::text[]) AS r(role_name) "
    "CROSS JOIN unnest(ARRAY['app_user', 'user_session']) AS t(table_name) "
    "CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) "
    "AS p(priv)"
)

#: Effective per-column privileges for *every* column of both tables.
#: ``has_column_privilege`` answers "held for the whole table, or granted for
#: this column", which is exactly the least-privilege question here: a
#: table-level grant appears on every column. The column list comes from the
#: catalog, so columns added by a later migration are covered automatically.
_CLEANUP_COLUMN_PRIVILEGES_SQL: Final[str] = (
    "SELECT r.role_name, c.table_name, c.column_name, p.priv, "
    "has_column_privilege(r.role_name::name, "
    "('public.' || c.table_name)::text, c.column_name::text, p.priv::text) "
    "FROM unnest(%s::text[]) AS r(role_name) "
    "CROSS JOIN information_schema.columns c "
    "CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE']) AS p(priv) "
    "WHERE c.table_schema = 'public' "
    "AND c.table_name IN ('app_user', 'user_session')"
)

#: Direct column ACL records, expanded from ``pg_attribute.attacl``.
#: ``information_schema.column_privileges`` only shows rows where the querying
#: role is the grantor or the grantee, so it cannot prove what *another* role
#: holds; ``aclexplode()`` on the raw ACL is not filtered that way. Every column
#: privilege held by the runtime role would be redundant beside its table-level
#: grant, so the expected record set for it is empty.
_CLEANUP_COLUMN_GRANTS_SQL: Final[str] = (
    "SELECT gr.rolname, c.relname, a.attname, x.privilege_type "
    "FROM unnest(%s::text[]) AS r(role_name) "
    "JOIN pg_roles gr ON gr.rolname::text = r.role_name::text "
    "JOIN pg_class c ON c.oid = ANY(ARRAY['public.app_user'::regclass, "
    "'public.user_session'::regclass]) "
    "JOIN pg_attribute a "
    "ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
    "CROSS JOIN LATERAL aclexplode(a.attacl) AS x "
    "WHERE x.grantee = gr.oid"
)

#: Neither Tier-2 role may be a member of the other: membership alone would
#: hand the application role DELETE, or the teardown role write access.
_CLEANUP_MEMBERSHIP_SQL: Final[str] = (
    "SELECT pg_has_role(%s::name, %s::name, 'MEMBER'), "
    "pg_has_role(%s::name, %s::name, 'MEMBER')"
)

_CLEANUP_SYSTEM_IDENTIFIER_SQL: Final[str] = (
    "SELECT system_identifier FROM pg_control_system()"
)

#: The only columns the teardown role may reach at column level.
_CLEANUP_COLUMNS: Final[frozenset[tuple[str, str]]] = frozenset(
    {("app_user", "user_id"), ("user_session", "user_id")}
)

_SECURITY_TABLES: Final[tuple[str, ...]] = ("app_user", "user_session")


def _require_cleanup_credential(dsn: str, target: VerifiedTestTarget) -> None:
    """Refuse a teardown credential that is not a distinct DELETE-only role.

    Verified against the already-verified Tier-2 target before the suite creates
    a single row:

    * identity: same database, same cluster (``pg_control_system()
      .system_identifier``), the expected role, not a superuser;
    * isolation: neither Tier-2 role is a member of the other;
    * effective privileges for **both** roles, so a grant inherited through
      another role is caught: the cleanup role holds ``DELETE`` on the two
      security tables and can read only ``user_id`` in them; the runtime role
      holds ``SELECT``/``INSERT``/``UPDATE`` and no ``DELETE``;
    * the raw column ACLs: the cleanup role's column grants are exactly
      ``user_id``, and the runtime role holds no column-level grant at all.

    A privilege that cannot be inspected is a failure, never a pass.
    """
    roles = [CLEANUP_ROLE, RUNTIME_ROLE]
    connection = open_connection(dsn)
    try:
        connection.autocommit = True
        identity = connection.execute(_CLEANUP_IDENTITY_SQL).fetchone()
        table_rows = connection.execute(
            _CLEANUP_TABLE_PRIVILEGES_SQL, (roles,)
        ).fetchall()
        column_rows = connection.execute(
            _CLEANUP_COLUMN_PRIVILEGES_SQL, (roles,)
        ).fetchall()
        grant_rows = connection.execute(
            _CLEANUP_COLUMN_GRANTS_SQL, (roles,)
        ).fetchall()
        membership = connection.execute(
            _CLEANUP_MEMBERSHIP_SQL,
            (CLEANUP_ROLE, RUNTIME_ROLE, RUNTIME_ROLE, CLEANUP_ROLE),
        ).fetchone()
        identifier_unreadable = False
        try:
            system_identifier_row = connection.execute(
                _CLEANUP_SYSTEM_IDENTIFIER_SQL, ()
            ).fetchone()
        except Exception:
            # Fail closed with the exact blocker instead of dropping the cluster
            # check: a matching database name is not proof of the same cluster.
            # pg_control_system() needs EXECUTE on the function (PUBLIC by
            # default) and a hardened cluster may have revoked it.
            system_identifier_row = None
            identifier_unreadable = True
    finally:
        connection.close()

    if identity is None:
        pytest.fail(
            "the tier-2 cleanup credential could not be inspected", pytrace=False
        )

    database, role, superuser = identity[0], identity[1], identity[2]
    if database != target.dbname:
        pytest.fail(
            f"the cleanup credential connects to {database!r}, not the verified "
            f"test database {target.dbname!r}",
            pytrace=False,
        )
    if role != CLEANUP_ROLE:
        pytest.fail(
            f"the cleanup credential must authenticate as {CLEANUP_ROLE!r}, "
            f"not {role!r}",
            pytrace=False,
        )
    if superuser != "off":
        pytest.fail("the cleanup credential must not be a superuser", pytrace=False)

    # Cluster identity: the cleanup credential must target the same cluster as
    # the verified test target. A matching database name alone is insufficient
    # (two clusters can both hold a visual_attendance_tier2_test database).
    if identifier_unreadable or system_identifier_row is None:
        pytest.fail(
            "the cleanup credential cannot read pg_control_system()"
            ".system_identifier, so the cluster identity cannot be proven and "
            "the guard refuses to fall back to a name-only match. Do not grant "
            "monitoring or superuser to bypass this: verify with a role that "
            "may read the function, or record the blocker",
            pytrace=False,
        )
    cleanup_sysid = system_identifier_row[0]
    if isinstance(cleanup_sysid, bool) or not isinstance(cleanup_sysid, int):
        pytest.fail(
            "the cleanup credential's cluster system_identifier is not a usable "
            "integer",
            pytrace=False,
        )
    if cleanup_sysid != target.system_identifier:
        pytest.fail(
            "the cleanup credential targets a different PostgreSQL cluster "
            "than the verified test target (system_identifier mismatch)",
            pytrace=False,
        )

    if membership is not None and (membership[0] or membership[1]):
        pytest.fail(
            f"{CLEANUP_ROLE!r} and {RUNTIME_ROLE!r} must not be members of each "
            "other; membership alone would hand one role the other's privileges",
            pytrace=False,
        )

    granted = {(row[0], row[1], row[2]): bool(row[3]) for row in table_rows}
    effective = {
        (row[0], row[1], row[2], row[3]): bool(row[4]) for row in column_rows
    }

    missing_delete = [
        f"{table}/DELETE"
        for table in _SECURITY_TABLES
        if not granted.get((CLEANUP_ROLE, table, "DELETE"))
    ]
    if missing_delete:
        pytest.fail(
            "the cleanup credential cannot delete synthetic rows (missing: "
            f"{', '.join(missing_delete)})",
            pytrace=False,
        )

    table_writes = [
        f"{table}/{privilege}"
        for table in _SECURITY_TABLES
        for privilege in ("INSERT", "UPDATE")
        if granted.get((CLEANUP_ROLE, table, privilege))
    ]
    if table_writes:
        pytest.fail(
            "the cleanup credential must hold no table-level write privilege "
            f"(found: {', '.join(table_writes)})",
            pytrace=False,
        )

    # Effective column reads: user_id only. A table-level SELECT grant, a grant
    # on another column, or a SELECT inherited from another role all surface
    # here as an extra readable column.
    readable = {
        (table, column)
        for (owner, table, column, privilege), held in effective.items()
        if owner == CLEANUP_ROLE and privilege == "SELECT" and held
    }
    if readable != set(_CLEANUP_COLUMNS):
        unexpected = sorted(readable - set(_CLEANUP_COLUMNS))
        absent = sorted(set(_CLEANUP_COLUMNS) - readable)
        unexpected_text = ", ".join(f"{t}.{c}" for t, c in unexpected) or "none"
        absent_text = ", ".join(f"{t}.{c}" for t, c in absent) or "none"
        pytest.fail(
            "the cleanup credential must be able to read user_id in both tables "
            f"and nothing else (unexpected: {unexpected_text}; "
            f"missing: {absent_text})",
            pytrace=False,
        )

    writable_columns = sorted(
        f"{table}.{column}/{privilege}"
        for (owner, table, column, privilege), held in effective.items()
        if owner == CLEANUP_ROLE and privilege in ("INSERT", "UPDATE") and held
    )
    if writable_columns:
        pytest.fail(
            "the cleanup credential must hold no column-level write privilege "
            f"(found: {', '.join(writable_columns)})",
            pytrace=False,
        )

    # The runtime role keeps exactly its application privileges.
    runtime_missing = [
        f"{table}/{privilege}"
        for table in _SECURITY_TABLES
        for privilege in ("SELECT", "INSERT", "UPDATE")
        if not granted.get((RUNTIME_ROLE, table, privilege))
    ]
    if runtime_missing:
        pytest.fail(
            "the runtime role is missing required application privileges "
            f"(missing: {', '.join(runtime_missing)})",
            pytrace=False,
        )
    runtime_delete = [
        table
        for table in _SECURITY_TABLES
        if granted.get((RUNTIME_ROLE, table, "DELETE"))
    ]
    if runtime_delete:
        pytest.fail(
            "the runtime role must not hold DELETE (found on: "
            f"{', '.join(runtime_delete)})",
            pytrace=False,
        )

    runtime_column_grants = sorted(
        f"{row[1]}.{row[2]}/{row[3]}" for row in grant_rows if row[0] == RUNTIME_ROLE
    )
    if runtime_column_grants:
        pytest.fail(
            "the runtime role must hold no column-level grant (found: "
            f"{', '.join(runtime_column_grants)})",
            pytrace=False,
        )

def _require_schema(dsn: str) -> None:
    """Skip with BLOCKED unless both security tables exist (read-only check)."""
    connection = open_connection(dsn)
    try:
        connection.autocommit = True
        row = connection.execute(
            "SELECT to_regclass('public.app_user'), "
            "to_regclass('public.user_session')"
        ).fetchone()
    finally:
        connection.close()
    if row is None or row[0] is None or row[1] is None:
        pytest.skip(SCHEMA_BLOCK_REASON)


@pytest.fixture
def user_repo(verified_target: VerifiedTestTarget, test_dsn: str) -> UserRepository:
    require_destructive_action(
        verified_target, "user_concurrency_test", ("user_concurrency_test",)
    )
    return open_user_repository(dsn=test_dsn)


@pytest.fixture
def run_id() -> str:
    return uuid.uuid4().hex[:8].upper()


@pytest.fixture
def created_ids(test_dsn: str, cleanup_dsn: str) -> Iterator[list[str]]:
    """Track synthetic ids and remove *only* those rows after the test."""
    ids: list[str] = []
    yield ids
    _delete_synthetic_users(cleanup_dsn, test_dsn, ids)


def _delete_synthetic_users(
    cleanup_dsn: str, verify_dsn: str, user_ids: list[str]
) -> None:
    """Delete this run's synthetic rows with the test-only teardown role.

    The runtime role holds no DELETE, so teardown uses the separate cleanup
    credential and then verifies through the runtime connection that nothing
    survived: a leftover row is an error, never a silent leak.
    """
    if not user_ids:
        return
    for user_id in user_ids:
        if not user_id.startswith(f"{SYNTHETIC_ID_PREFIX}-"):
            raise RuntimeError(
                "refusing to delete a non-synthetic user row during cleanup"
            )

    cleanup = open_connection(cleanup_dsn)
    try:
        cleanup.execute(
            "DELETE FROM user_session WHERE user_id = ANY(%s)", (user_ids,)
        )
        cleanup.execute(
            "DELETE FROM app_user WHERE user_id = ANY(%s)", (user_ids,)
        )
        cleanup.commit()
    finally:
        cleanup.close()

    verification = open_connection(verify_dsn)
    try:
        verification.autocommit = True
        row = verification.execute(
            "SELECT (SELECT count(*) FROM app_user WHERE user_id = ANY(%s)), "
            "(SELECT count(*) FROM user_session WHERE user_id = ANY(%s))",
            (user_ids, user_ids),
        ).fetchone()
    finally:
        verification.close()
    if row is None or int(row[0]) != 0 or int(row[1]) != 0:
        raise RuntimeError(
            "synthetic rows survived teardown; failing instead of leaving the "
            "tier-2 database dirty"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def synthetic_user_id(run_id: str, slot: int) -> str:
    return f"{SYNTHETIC_ID_PREFIX}-{run_id}-{slot:02d}"


def synthetic_username(run_id: str, slot: int) -> str:
    return f"t2uc-{run_id}-{slot:02d}"


def make_user(
    user_id: str, username: str, *, role: Role, is_active: bool = True
) -> User:
    return User(
        user_id=user_id,
        username=username,
        password_hash=HASH,
        role=role,
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


def create_user(
    repo: UserRepository,
    *,
    user_id: str,
    username: str,
    role: Role,
    is_active: bool = True,
) -> User:
    return repo.create(
        make_user(user_id, username, role=role, is_active=is_active)
    )


def active_admin_count(dsn: str) -> int:
    connection = open_connection(dsn)
    try:
        connection.autocommit = True
        row = connection.execute(
            "SELECT count(*) FROM app_user "
            "WHERE role = 'Admin' AND is_active = TRUE"
        ).fetchone()
    finally:
        connection.close()
    return int(row[0]) if row else 0


def _require_no_foreign_admins(dsn: str, run_id: str) -> None:
    """Refuse to disturb a database that already holds active Admins.

    The invariant is global to ``app_user``; scenarios assert exact counts, so
    a pre-existing active Admin would invalidate them. Rather than deactivate
    someone else's row, the test reports BLOCKED.
    """
    connection = open_connection(dsn)
    try:
        connection.autocommit = True
        row = connection.execute(
            "SELECT count(*) FROM app_user "
            "WHERE role = 'Admin' AND is_active = TRUE "
            "AND user_id NOT LIKE %s",
            (f"{SYNTHETIC_ID_PREFIX}-{run_id}-%",),
        ).fetchone()
    finally:
        connection.close()
    foreign = int(row[0]) if row else 0
    if foreign:
        pytest.skip(
            "tier2 blocked: the test database contains "
            f"{foreign} active Admin row(s) not created by this run; the "
            "suite will not deactivate or delete foreign rows"
        )


class _Threads:
    """Run two no-argument callables behind one barrier and collect outcomes."""

    def __init__(self) -> None:
        self._barrier = threading.Barrier(2)
        self._lock = threading.Lock()
        self._outcomes: dict[int, tuple[bool, BaseException | None]] = {}

    def _run(self, slot: int, operation: Callable[[], object]) -> None:
        try:
            self._barrier.wait(timeout=10.0)
            operation()
            outcome: tuple[bool, BaseException | None] = (True, None)
        except BaseException as exc:  # noqa: BLE001 - recorded for assertions
            outcome = (False, exc)
        with self._lock:
            self._outcomes[slot] = outcome

    def run(
        self, first: Callable[[], object], second: Callable[[], object]
    ) -> list[tuple[bool, BaseException | None]]:
        threads = [
            threading.Thread(target=self._run, args=(0, first)),
            threading.Thread(target=self._run, args=(1, second)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30.0)
        assert not any(thread.is_alive() for thread in threads), (
            "a concurrency scenario thread did not finish; this usually means "
            "the advisory lock was never released"
        )
        assert len(self._outcomes) == 2, (
            "both concurrent operations must report an outcome"
        )
        return [self._outcomes[0], self._outcomes[1]]


# ---------------------------------------------------------------------------
# Scenario 1: simultaneous first-Admin provisioning
# ---------------------------------------------------------------------------


def test_two_simultaneous_first_admin_provisions_create_exactly_one_admin(
    test_dsn: str, user_repo: UserRepository, run_id: str, created_ids: list[str]
) -> None:
    _require_no_foreign_admins(test_dsn, run_id)
    assert active_admin_count(test_dsn) == 0, (
        "synthetic Admins from an earlier test leaked; the suite refuses to "
        "guess which active Admin is legitimate"
    )

    ids = [synthetic_user_id(run_id, 1), synthetic_user_id(run_id, 2)]
    usernames = [synthetic_username(run_id, 1), synthetic_username(run_id, 2)]
    created_ids.extend(ids)

    def provision(slot: int) -> Callable[[], object]:
        return lambda: user_repo.provision_first_admin(
            make_user(ids[slot], usernames[slot], role=Role.ADMIN),
            updated_at=NOW,
        )

    outcomes = _Threads().run(provision(0), provision(1))

    successes = [outcome for outcome, _ in outcomes if outcome]
    failures = [error for success, error in outcomes if not success]
    assert len(successes) == 1, outcomes
    assert isinstance(failures[0], LastAdminProtectionError), failures

    # Final committed state, not just return values.
    assert active_admin_count(test_dsn) == 1
    row = open_connection(test_dsn)
    try:
        row.autocommit = True
        total = row.execute(
            "SELECT count(*) FROM app_user WHERE user_id = ANY(%s)", (ids,)
        ).fetchone()
    finally:
        row.close()
    assert int(total[0]) == 1


# ---------------------------------------------------------------------------
# Scenario 2: two Admins deactivating each other
# ---------------------------------------------------------------------------


def test_two_admins_deactivating_each_other_leave_one_active_admin(
    test_dsn: str, user_repo: UserRepository, run_id: str, created_ids: list[str]
) -> None:
    _require_no_foreign_admins(test_dsn, run_id)
    assert active_admin_count(test_dsn) == 0

    ids = [synthetic_user_id(run_id, 10), synthetic_user_id(run_id, 11)]
    created_ids.extend(ids)
    for slot, user_id in enumerate(ids):
        create_user(
            user_repo,
            user_id=user_id,
            username=synthetic_username(run_id, 10 + slot),
            role=Role.ADMIN,
        )
    assert active_admin_count(test_dsn) == 2

    def deactivate(target_id: str) -> Callable[[], object]:
        return lambda: user_repo.update_active_with_admin_check(
            target_id, False, updated_at=NOW
        )

    outcomes = _Threads().run(deactivate(ids[1]), deactivate(ids[0]))

    assert sum(1 for success, _ in outcomes if success) == 1, outcomes
    failure = [error for success, error in outcomes if not success][0]
    assert isinstance(failure, LastAdminProtectionError), failure
    assert active_admin_count(test_dsn) == 1


# ---------------------------------------------------------------------------
# Scenario 3: two Admins demoting each other
# ---------------------------------------------------------------------------


def test_two_admins_demoting_each_other_leave_one_active_admin(
    test_dsn: str, user_repo: UserRepository, run_id: str, created_ids: list[str]
) -> None:
    _require_no_foreign_admins(test_dsn, run_id)
    assert active_admin_count(test_dsn) == 0

    ids = [synthetic_user_id(run_id, 20), synthetic_user_id(run_id, 21)]
    created_ids.extend(ids)
    for slot, user_id in enumerate(ids):
        create_user(
            user_repo,
            user_id=user_id,
            username=synthetic_username(run_id, 20 + slot),
            role=Role.ADMIN,
        )

    def demote(target_id: str) -> Callable[[], object]:
        return lambda: user_repo.update_role_with_admin_check(
            target_id, Role.OPERATOR, updated_at=NOW
        )

    outcomes = _Threads().run(demote(ids[1]), demote(ids[0]))

    assert sum(1 for success, _ in outcomes if success) == 1, outcomes
    failure = [error for success, error in outcomes if not success][0]
    assert isinstance(failure, LastAdminProtectionError), failure
    assert active_admin_count(test_dsn) == 1


# ---------------------------------------------------------------------------
# Scenario 4: concurrent Admin creation and Admin demotion
# ---------------------------------------------------------------------------


def test_concurrent_admin_creation_and_demotion_never_leave_zero_admins(
    test_dsn: str, user_repo: UserRepository, run_id: str, created_ids: list[str]
) -> None:
    _require_no_foreign_admins(test_dsn, run_id)
    assert active_admin_count(test_dsn) == 0

    incumbent = synthetic_user_id(run_id, 30)
    newcomer = synthetic_user_id(run_id, 31)
    created_ids.extend([incumbent, newcomer])
    create_user(
        user_repo,
        user_id=incumbent,
        username=synthetic_username(run_id, 30),
        role=Role.ADMIN,
    )

    create_new = lambda: user_repo.create(  # noqa: E731
        make_user(newcomer, synthetic_username(run_id, 31), role=Role.ADMIN)
    )
    demote_incumbent = lambda: user_repo.update_role_with_admin_check(  # noqa: E731
        incumbent, Role.OPERATOR, updated_at=NOW
    )

    outcomes = _Threads().run(create_new, demote_incumbent)

    # The demotion may legitimately lose the race and be refused; the creation
    # must not make it possible to end with zero Admins either way.
    assert active_admin_count(test_dsn) >= 1, outcomes
    assert any(success for success, _ in outcomes), outcomes

    connection = open_connection(test_dsn)
    try:
        connection.autocommit = True
        row = connection.execute(
            "SELECT role, is_active FROM app_user WHERE user_id = %s",
            (newcomer,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None and row[0] == Role.ADMIN.value and row[1] is True


# ---------------------------------------------------------------------------
# Scenario 5 and 6: non-Admin operations with exactly one active Admin
# ---------------------------------------------------------------------------


def test_non_admin_deactivation_is_allowed_with_one_active_admin(
    test_dsn: str, user_repo: UserRepository, run_id: str, created_ids: list[str]
) -> None:
    _require_no_foreign_admins(test_dsn, run_id)
    assert active_admin_count(test_dsn) == 0

    admin_id = synthetic_user_id(run_id, 40)
    operator_id = synthetic_user_id(run_id, 41)
    created_ids.extend([admin_id, operator_id])
    create_user(
        user_repo,
        user_id=admin_id,
        username=synthetic_username(run_id, 40),
        role=Role.ADMIN,
    )
    create_user(
        user_repo,
        user_id=operator_id,
        username=synthetic_username(run_id, 41),
        role=Role.OPERATOR,
    )

    updated = user_repo.update_active_with_admin_check(
        operator_id, False, updated_at=NOW
    )

    assert updated.is_active is False
    assert active_admin_count(test_dsn) == 1


def test_non_admin_role_change_is_allowed_with_one_active_admin(
    test_dsn: str, user_repo: UserRepository, run_id: str, created_ids: list[str]
) -> None:
    _require_no_foreign_admins(test_dsn, run_id)
    assert active_admin_count(test_dsn) == 0

    admin_id = synthetic_user_id(run_id, 50)
    operator_id = synthetic_user_id(run_id, 51)
    created_ids.extend([admin_id, operator_id])
    create_user(
        user_repo,
        user_id=admin_id,
        username=synthetic_username(run_id, 50),
        role=Role.ADMIN,
    )
    create_user(
        user_repo,
        user_id=operator_id,
        username=synthetic_username(run_id, 51),
        role=Role.OPERATOR,
    )

    updated = user_repo.update_role_with_admin_check(
        operator_id, Role.AUDITOR, updated_at=NOW
    )

    assert updated.role is Role.AUDITOR
    assert active_admin_count(test_dsn) == 1


# ---------------------------------------------------------------------------
# Scenario 7: the enforced isolation level, proven against a stricter default
# ---------------------------------------------------------------------------


def test_invariant_section_forces_read_committed_over_a_stricter_default(
    test_dsn: str, run_id: str, created_ids: list[str]
) -> None:
    """With the session default at REPEATABLE READ, the invariant still holds.

    The repository issues ``SET TRANSACTION ISOLATION LEVEL READ COMMITTED`` as
    the first statement of each invariant transaction. Without it, the waiting
    transaction's snapshot would be fixed at the lock statement and it could
    demote the surviving Admin too.
    """
    _require_no_foreign_admins(test_dsn, run_id)
    assert active_admin_count(test_dsn) == 0

    def strict_opener(dsn: str | None = None):
        connection = open_connection(dsn)
        connection.autocommit = True
        connection.execute(
            "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL "
            "REPEATABLE READ"
        )
        connection.autocommit = False
        return connection

    strict_repo = open_user_repository(
        dsn=test_dsn, connection_opener=strict_opener
    )

    ids = [synthetic_user_id(run_id, 60), synthetic_user_id(run_id, 61)]
    created_ids.extend(ids)
    for slot, user_id in enumerate(ids):
        create_user(
            strict_repo,
            user_id=user_id,
            username=synthetic_username(run_id, 60 + slot),
            role=Role.ADMIN,
        )

    def demote(target_id: str) -> Callable[[], object]:
        return lambda: strict_repo.update_role_with_admin_check(
            target_id, Role.OPERATOR, updated_at=NOW
        )

    outcomes = _Threads().run(demote(ids[1]), demote(ids[0]))

    assert sum(1 for success, _ in outcomes if success) == 1, outcomes
    assert active_admin_count(test_dsn) == 1
