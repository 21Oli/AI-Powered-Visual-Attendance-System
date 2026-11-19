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
inspected before the first scenario (same database, distinct role, not a
superuser, ``DELETE`` only on the two tables), and its deletions are verified
through the runtime connection — a surviving row fails the test instead of
being silently left behind.
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
#: ``DELETE`` on the two security tables and nothing else, so the application
#: role never needs DELETE.
CLEANUP_ROLE: Final[str] = "attendance_tier2_cleanup"

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


def _require_cleanup_credential(dsn: str, target: VerifiedTestTarget) -> None:
    """Refuse teardown credentials that are not a distinct DELETE-only role."""
    connection = open_connection(dsn)
    try:
        connection.autocommit = True
        identity = connection.execute(
            "SELECT current_database(), current_user, "
            "current_setting('is_superuser')"
        ).fetchone()
        privilege_rows = connection.execute(
            "SELECT t.name, p.priv, "
            "has_table_privilege(current_user, 'public.' || t.name, p.priv) "
            "FROM unnest(ARRAY['app_user', 'user_session']) AS t(name) "
            "CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) "
            "AS p(priv)"
        ).fetchall()
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

    granted = {(row[0], row[1]): bool(row[2]) for row in privilege_rows}
    missing = [
        f"{table}/DELETE"
        for table in ("app_user", "user_session")
        if not granted.get((table, "DELETE"))
    ]
    excess = [
        f"{table}/{privilege}"
        for table in ("app_user", "user_session")
        for privilege in ("SELECT", "INSERT", "UPDATE")
        if granted.get((table, privilege))
    ]
    if missing or excess:
        pytest.fail(
            "the cleanup credential must hold DELETE only "
            f"(missing: {', '.join(missing) or 'none'}; "
            f"excess: {', '.join(excess) or 'none'})",
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
