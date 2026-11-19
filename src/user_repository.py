"""User-identity persistence for the Phase 5 security layer (P5.2).

Follows the Phase 3 repository conventions exactly (``src.employee_repository``):

* static SQL constants, **bound parameters only** (``%s``) — no interpolation,
  so a username or id can never become SQL;
* one operation = one connection supplied by an injected
  ``connection_factory``; the factory owns commit/rollback/close;
* typed persistence-state errors
  (:class:`UserAlreadyExists`, :class:`UserNotFound`) so later milestones can
  map them to HTTP statuses without reading driver text;
* **no catch-all**: availability/config failures propagate unchanged, exactly
  as they do through the employee repository;
* no driver import at module level, no connection at import or construction.

Scope discipline
----------------

This module persists identity metadata only. It contains **no** password
hashing or verification, **no** session issuance/validation, **no** role
policy decision and **no** HTTP surface (all later milestones). It never logs
the ``password_hash`` — the value is written and read verbatim and the model
keeps it out of ``repr``.

Uniqueness
----------

``username_key`` is ``UNIQUE`` in PostgreSQL. A duplicate create therefore
surfaces as SQLSTATE ``23505`` and is translated to
:class:`UserAlreadyExists` (psycopg 3's ``sqlstate`` attribute, duck-typed —
no driver import; any other SQLSTATE propagates untouched).

Concurrency control for admin-invariant operations
--------------------------------------------------

Operations that can change the active-Admin count use **one** strategy:

1. the transaction is explicitly set to ``READ COMMITTED``;
2. the transaction-level advisory lock :data:`_ADMIN_INVARIANT_LOCK_KEY` is
   acquired with ``pg_advisory_xact_lock``;
3. only then is the invariant checked and the row changed, in the same
   transaction.

All four invariant-affecting operations use
:func:`_begin_admin_invariant_transaction`: ordinary Admin creation
(:meth:`UserRepository.create` when ``role is Role.ADMIN``), first-Admin
provisioning (:meth:`UserRepository.provision_first_admin`), Admin demotion
(:meth:`UserRepository.update_role_with_admin_check`) and Admin deactivation
(:meth:`UserRepository.update_active_with_admin_check`). Non-Admin creation
and non-Admin updates cannot change the active-Admin count, so they take no
lock.

Why ``READ COMMITTED`` is required and enforced
-----------------------------------------------

The advisory lock serializes the *decision*, but the decision reads committed
rows. Under ``READ COMMITTED`` each statement takes a fresh snapshot, so the
transaction that waited for the lock sees the winner's committed update. Under
``REPEATABLE READ`` or ``SERIALIZABLE`` the transaction's snapshot is fixed at
its first statement, so the waiter would still see the pre-update state and
could take a stale decision (zero Admins left, or a needless refusal).
Therefore each invariant transaction issues
``SET TRANSACTION ISOLATION LEVEL READ COMMITTED`` as its first statement,
overriding a stricter server/role/session default, before acquiring the lock.
This is why fresh connections (one operation = one connection) are required:
``SET TRANSACTION`` is only accepted before the transaction's first query.

Limitations
-----------

* The invariant is enforced only between operations that cooperate with this
  lock. Direct SQL, another application, or a repository method that does not
  take the lock can still break it; the database has no constraint for "at
  least one active Admin".
* The lock is a single global key, so invariant operations serialize with each
  other (a deliberate availability trade-off for a rarely used admin path).
* Correctness is asserted for ``READ COMMITTED`` only. The isolation level is
  set per transaction rather than assumed from server configuration.
* ``SET TRANSACTION`` fails if a caller hands in a connection whose transaction
  already executed a statement; the repository therefore never reuses a
  connection across operations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Final

from src.clock import utc_now
from src.config import USER_PAGINATION
from src.user import Role, User, username_key_for, UserValidationError

__all__ = [
    "UserAlreadyExists",
    "UserNotFound",
    "UserRepository",
]


class UserAlreadyExists(RuntimeError):
    """A create was refused because the ``user_id`` or ``username_key`` is taken."""


class UserNotFound(RuntimeError):
    """An update found no user for the given ``user_id``."""


class LastAdminProtectionError(RuntimeError):
    """An operation would leave the system without an active administrator."""



# ---------------------------------------------------------------------------
# Static SQL (bound parameters, named columns, no interpolation)
# ---------------------------------------------------------------------------

_SELECT_COLUMNS: Final[str] = (
    "user_id, username, username_key, password_hash, role, is_active, "
    "created_at, updated_at"
)

_INSERT_SQL: Final[str] = (
    "INSERT INTO app_user "
    "(user_id, username, username_key, password_hash, role, is_active, "
    "created_at, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)

_GET_SQL: Final[str] = f"SELECT {_SELECT_COLUMNS} FROM app_user WHERE user_id = %s"

_GET_BY_USERNAME_KEY_SQL: Final[str] = (
    f"SELECT {_SELECT_COLUMNS} FROM app_user WHERE username_key = %s"
)

_LIST_SQL_PREFIX: Final[str] = f"SELECT {_SELECT_COLUMNS} FROM app_user"

_KEYSET_CLAUSE_SQL: Final[str] = " WHERE user_id > %s"

_ORDER_SQL: Final[str] = " ORDER BY user_id ASC LIMIT %s"

_UPDATE_ROLE_SQL: Final[str] = (
    "UPDATE app_user SET role = %s, updated_at = %s WHERE user_id = %s "
    f"RETURNING {_SELECT_COLUMNS}"
)

_UPDATE_ACTIVE_SQL: Final[str] = (
    "UPDATE app_user SET is_active = %s, updated_at = %s WHERE user_id = %s "
    f"RETURNING {_SELECT_COLUMNS}"
)

#: Rehash persistence (P5.4.5): only the hash and ``updated_at`` change. Only
#: the identifier is returned — the caller does not need the row back.
_UPDATE_PASSWORD_HASH_SQL: Final[str] = (
    "UPDATE app_user SET password_hash = %s, updated_at = %s WHERE user_id = %s "
    "RETURNING user_id"
)

#: PostgreSQL unique-constraint violation SQLSTATE (duck-typed off the driver
#: exception object — no driver import needed for detection).
_UNIQUE_VIOLATION_SQLSTATE: Final[str] = "23505"

#: SQL to count active admins (used for last-admin protection).
_COUNT_ACTIVE_ADMINS_SQL: Final[str] = (
    "SELECT COUNT(*) FROM app_user WHERE role = 'Admin' AND is_active = TRUE"
)

#: PostgreSQL transaction-level advisory lock key for admin-invariant operations.
#: This is a fixed, application-chosen 64-bit integer. Transaction-level locks
#: (pg_advisory_xact_lock) are used so the lock auto-releases at commit/rollback.
_ADMIN_INVARIANT_LOCK_KEY: Final[int] = 0x41444D494E560001  # "ADMININV" + padding

#: Enforced isolation for every admin-invariant transaction. The decision reads
#: committed rows *after* the lock is granted, which is only correct with a
#: fresh snapshot per statement; see the module docstring for the full proof
#: obligation and its limits.
_SET_READ_COMMITTED_SQL: Final[str] = (
    "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
)

#: SQL to acquire the admin-invariant advisory lock (transaction-scoped).
_ACQUIRE_ADMIN_LOCK_SQL: Final[str] = (
    "SELECT pg_advisory_xact_lock(%s)"
)

#: SQL to check if any active admin exists (for first-admin provisioning guard).
_COUNT_ACTIVE_ADMINS_EXISTS_SQL: Final[str] = (
    "SELECT EXISTS(SELECT 1 FROM app_user WHERE role = 'Admin' AND is_active = TRUE)"
)

_ROW_COLUMNS: Final[tuple[str, ...]] = (
    "user_id",
    "username",
    "username_key",
    "password_hash",
    "role",
    "is_active",
    "created_at",
    "updated_at",
)


def _begin_admin_invariant_transaction(connection: object) -> None:
    """Start the shared admin-invariant transaction section.

    Must run before any other statement in the transaction: it pins
    ``READ COMMITTED`` and then blocks on the application-wide advisory lock.
    The caller must already hold the connection that owns the transaction.
    """
    connection.execute(_SET_READ_COMMITTED_SQL, ())  # type: ignore[attr-defined]
    connection.execute(  # type: ignore[attr-defined]
        _ACQUIRE_ADMIN_LOCK_SQL, (_ADMIN_INVARIANT_LOCK_KEY,)
    )


def _row_to_user(row: Iterable[object]) -> User:
    """Rebuild a validated :class:`User` from one result row.

    The model runs its own validation here, so a stored row that violates
    the model cannot come back as a valid-looking identity. An unrecognized
    stored role is refused with a sanitized message (the value is never
    echoed, and the raw :class:`ValueError` is not surfaced as a role string).
    """
    values = tuple(row)
    if len(values) != len(_ROW_COLUMNS):
        raise RuntimeError(
            "user row shape mismatch: expected exactly the eight app_user "
            "columns"
        )
    try:
        role = Role(values[4])
    except ValueError:
        raise UserValidationError(
            "stored role is not one of the approved Phase 5 roles"
        ) from None
    return User(
        user_id=values[0],  # type: ignore[arg-type]  # validated in-model
        username=values[1],  # type: ignore[arg-type]
        username_key=values[2],  # type: ignore[arg-type]
        password_hash=values[3],  # type: ignore[arg-type]
        role=role,
        is_active=values[5],  # type: ignore[arg-type]
        created_at=values[6],  # type: ignore[arg-type]
        updated_at=values[7],  # type: ignore[arg-type]
    )


class UserRepository:
    """Persistence facade for ``app_user`` (identity metadata only).

    Parameters
    ----------
    connection_factory:
        Zero-argument callable returning a **context manager** yielding a
        connection-like object with ``execute(sql, params)`` whose result
        provides ``fetchone()`` / ``fetchall()`` (the psycopg connection
        shape). One operation = one acquired connection; the context manager
        owns commit/close semantics, so this class performs no explicit
        transaction management and defines no rollback policy.
    """

    def __init__(
        self,
        connection_factory: Callable[..., AbstractContextManager[object]],
    ) -> None:
        self._connection_factory = connection_factory

    # -- helpers ------------------------------------------------------------

    def _run(self, sql: str, params: tuple[object, ...]) -> object:
        """Run one statement on a fresh connection; return its result.

        Exceptions propagate untouched: no catching, wrapping or retry.
        """
        with self._connection_factory() as connection:
            return connection.execute(sql, params)  # type: ignore[attr-defined]

    def _execute_insert(self, connection: object, user: User) -> None:
        """Insert on an existing connection, translating only SQLSTATE 23505."""
        try:
            connection.execute(  # type: ignore[attr-defined]
                _INSERT_SQL,
                (
                    user.user_id,
                    user.username,
                    user.username_key,
                    user.password_hash,
                    user.role.value,
                    user.is_active,
                    user.created_at,
                    user.updated_at,
                ),
            )
        except UserAlreadyExists:
            raise
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == _UNIQUE_VIOLATION_SQLSTATE:
                raise UserAlreadyExists(
                    "a user already exists for the requested id or username"
                ) from exc
            raise

    def _run_insert(self, user: User) -> None:
        """Insert on a plain connection (no invariant lock)."""
        with self._connection_factory() as connection:
            self._execute_insert(connection, user)

    # -- approved operations ------------------------------------------------

    def create(self, user: User) -> User:
        """Insert a row from an already-validated :class:`User`.

        Returns the same ``User`` on success. A duplicate ``user_id`` or
        ``username_key`` raises :class:`UserAlreadyExists`; anything else
        propagates unchanged.
        """
        if not isinstance(user, User):
            raise UserValidationError(
                "create expects an already-validated User instance, got "
                f"{type(user).__name__}"
            )
        if user.role is Role.ADMIN:
            # Ordinary Admin creation participates in the shared invariant lock:
            # it only increases the active-Admin count, but every operation that
            # touches that count must serialize through the same section so a
            # concurrent demotion/deactivation cannot decide on a stale count.
            with self._connection_factory() as connection:
                _begin_admin_invariant_transaction(connection)
                self._execute_insert(connection, user)
            return user
        self._run_insert(user)
        return user

    def get(self, user_id: str) -> User | None:
        """Exact-match fetch by id; ``None`` when absent."""
        if not isinstance(user_id, str) or not user_id.strip():
            raise UserValidationError("user_id must be a non-empty string")
        row = self._run(_GET_SQL, (user_id.strip(),)).fetchone()  # type: ignore[attr-defined]
        return None if row is None else _row_to_user(row)

    def find_by_username(self, username: str) -> User | None:
        """Fetch by normalized username, or ``None`` when absent.

        The lookup key is derived with the **one** normalization function
        (:func:`src.user.username_key_for`), so the caller never supplies a
        key itself and a case-only variant cannot miss the row.
        """
        key = username_key_for(username)
        row = self._run(_GET_BY_USERNAME_KEY_SQL, (key,)).fetchone()  # type: ignore[attr-defined]
        return None if row is None else _row_to_user(row)

    def list(
        self,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> list[User]:
        """Bounded keyset page (``user_id`` ascending).

        Defaults to :attr:`src.config.USER_PAGINATION.default_page_size` and
        clamps any larger request to
        :attr:`src.config.USER_PAGINATION.max_page_size`, so an identity dump
        is never unbounded. A non-string/blank ``after`` or a non-int, bool,
        or sub-1 ``limit`` is refused **before any DB call**.
        """
        if after is not None and (
            not isinstance(after, str) or not after.strip()
        ):
            raise UserValidationError(
                "after must be None or a non-blank user_id cursor"
            )
        resolved_limit = (
            USER_PAGINATION.default_page_size if limit is None else limit
        )
        if isinstance(resolved_limit, bool) or not isinstance(
            resolved_limit, int
        ):
            raise UserValidationError(
                "limit must be a plain int (bool and float rejected), got "
                f"{type(resolved_limit).__name__}"
            )
        if resolved_limit < 1:
            raise UserValidationError(f"limit must be >= 1, got {resolved_limit}")
        clamped = min(resolved_limit, USER_PAGINATION.max_page_size)

        if after is None:
            result = self._run(_LIST_SQL_PREFIX + _ORDER_SQL, (clamped,))
        else:
            result = self._run(
                _LIST_SQL_PREFIX + _KEYSET_CLAUSE_SQL + _ORDER_SQL,
                (after.strip(), clamped),
            )
        rows: Iterable[object] = result.fetchall()  # type: ignore[attr-defined]
        return [_row_to_user(row) for row in rows]

    def update_role(
        self, user_id: str, role: Role, *, updated_at: datetime
    ) -> User:
        """Set the user's role and ``updated_at``; return the read-back row.

        Missing user → :class:`UserNotFound`. Role *policy* (who may change
        whose role) belongs to a later milestone; this layer only persists an
        already-approved value.
        """
        if not isinstance(role, Role):
            raise UserValidationError(
                f"role must be a Role member, got {type(role).__name__}"
            )
        user, updated = _validated_update_args(user_id, updated_at)
        row = self._run(
            _UPDATE_ROLE_SQL, (role.value, updated, user)
        ).fetchone()  # type: ignore[attr-defined]
        if row is None:
            raise UserNotFound(
                "no user was updated because the requested user does not exist"
            )
        return _row_to_user(row)

    def update_active(
        self, user_id: str, is_active: bool, *, updated_at: datetime
    ) -> User:
        """Set ``is_active`` and ``updated_at``; return the read-back row.

        Missing user → :class:`UserNotFound`. Enforcing what an inactive
        account may do is authentication-policy work (later milestone).
        """
        if not isinstance(is_active, bool):
            raise UserValidationError(
                f"is_active must be a bool, got {type(is_active).__name__}"
            )
        user, updated = _validated_update_args(user_id, updated_at)
        row = self._run(
            _UPDATE_ACTIVE_SQL, (is_active, updated, user)
        ).fetchone()  # type: ignore[attr-defined]
        if row is None:
            raise UserNotFound(
                "no user was updated because the requested user does not exist"
            )
        return _row_to_user(row)

    def update_active_with_admin_check(
        self,
        target_user_id: str,
        is_active: bool,
        *,
        updated_at: datetime,
    ) -> User:
        """Atomically deactivate a user while protecting the last-admin invariant.

        This method performs the admin-count check and the update in a single
        connection/transaction to prevent TOCTOU races. Raises
        :class:`LastAdminProtectionError` if the operation would leave no active
        admin.

        Parameters
        ----------
        target_user_id:
            The user to deactivate.
        is_active:
            The new active state (typically False for deactivation).
        updated_at:
            The application-authored timestamp.

        Returns
        -------
        User
            The updated user row.

        Raises
        ------
        LastAdminProtectionError
            If deactivating this user would leave zero active admins.
        UserNotFound
            If the target user does not exist.
        """
        if not isinstance(is_active, bool):
            raise UserValidationError(
                f"is_active must be a bool, got {type(is_active).__name__}"
            )
        user, updated = _validated_update_args(target_user_id, updated_at)

        with self._connection_factory() as connection:
            _begin_admin_invariant_transaction(connection)

            target_row = connection.execute(_GET_SQL, (user,)).fetchone()
            if target_row is None:
                raise UserNotFound(
                    "no user was updated because the requested user does not exist"
                )
            target = _row_to_user(target_row)

            # Only an active Admin losing active status can reduce the count.
            # Every other case (reactivation, non-Admin, already inactive) is
            # allowed even when exactly one active Admin exists.
            affects_invariant = (
                target.role is Role.ADMIN
                and target.is_active is True
                and is_active is False
            )
            if affects_invariant:
                cursor = connection.execute(_COUNT_ACTIVE_ADMINS_SQL, ())
                count_row = cursor.fetchone()
                active_admin_count = int(count_row[0]) if count_row else 0
                if active_admin_count < 2:
                    raise LastAdminProtectionError(
                        "the last active administrator cannot be deactivated"
                    )

            row = connection.execute(
                _UPDATE_ACTIVE_SQL, (is_active, updated, user)
            ).fetchone()  # type: ignore[attr-defined]
            if row is None:
                raise UserNotFound(
                    "no user was updated because the requested user does not exist"
                )
            return _row_to_user(row)

    def provision_first_admin(
        self,
        user: User,
        *,
        updated_at: datetime,
    ) -> User:
        """Create the first Admin account in the shared invariant transaction.

        Runs in the same enforced-``READ COMMITTED`` advisory-lock section as
        every other invariant-affecting operation. Concurrent attempts serialize:
        the waiter re-reads the committed state after the winner releases the
        lock and refuses with :class:`LastAdminProtectionError`. The
        ``username_key`` / ``user_id`` unique constraints remain the final
        authority for duplicate identity.

        Parameters
        ----------
        user:
            The validated Admin user to create (must be the first Admin).
        updated_at:
            The application-authored timestamp.

        Returns
        -------
        User
            The created user.

        Raises
        ------
        UserAlreadyExists
            If another Admin was created concurrently, or the user_id/username
            is already taken.
        """
        if not isinstance(user, User):
            raise UserValidationError(
                "create expects an already-validated User instance, got "
                f"{type(user).__name__}"
            )
        if user.role is not Role.ADMIN:
            raise UserValidationError("provision_first_admin requires an Admin role")

        with self._connection_factory() as connection:
            _begin_admin_invariant_transaction(connection)

            # Under the lock, a fresh READ COMMITTED snapshot shows whether a
            # concurrent attempt has already committed an Admin.
            cursor = connection.execute(_COUNT_ACTIVE_ADMINS_EXISTS_SQL, ())
            row = cursor.fetchone()
            admin_exists = bool(row[0]) if row else False
            if admin_exists:
                raise LastAdminProtectionError(
                    "an active administrator already exists; provisioning refused"
                )

            self._execute_insert(connection, user)
            return user

    def update_role_with_admin_check(
        self,
        target_user_id: str,
        new_role: Role,
        *,
        updated_at: datetime,
    ) -> User:
        """Atomically change a user's role while protecting the last-admin invariant.

        Used when demoting an Admin. Acquires the admin-invariant advisory lock
        before checking that the demotion would not leave zero active admins.

        Parameters
        ----------
        target_user_id:
            The user whose role is being changed.
        new_role:
            The new role.
        updated_at:
            The application-authored timestamp.

        Returns
        -------
        User
            The updated user row.

        Raises
        ------
        LastAdminProtectionError
            If demoting this admin would leave zero active admins.
        UserNotFound
            If the target user does not exist.
        """
        if not isinstance(new_role, Role):
            raise UserValidationError(
                f"role must be a Role member, got {type(new_role).__name__}"
            )
        user, updated = _validated_update_args(target_user_id, updated_at)

        with self._connection_factory() as connection:
            _begin_admin_invariant_transaction(connection)

            target_row = connection.execute(_GET_SQL, (user,)).fetchone()
            if target_row is None:
                raise UserNotFound(
                    "no user was updated because the requested user does not exist"
                )
            target = _row_to_user(target_row)

            # Only demoting an active Admin can reduce the count.
            affects_invariant = (
                target.role is Role.ADMIN
                and target.is_active is True
                and new_role is not Role.ADMIN
            )
            if affects_invariant:
                cursor = connection.execute(_COUNT_ACTIVE_ADMINS_SQL, ())
                count_row = cursor.fetchone()
                active_admin_count = int(count_row[0]) if count_row else 0
                if active_admin_count < 2:
                    raise LastAdminProtectionError(
                        "the last active administrator cannot be demoted"
                    )

            row = connection.execute(
                _UPDATE_ROLE_SQL, (new_role.value, updated, user)
            ).fetchone()  # type: ignore[attr-defined]
            if row is None:
                raise UserNotFound(
                    "no user was updated because the requested user does not exist"
                )
            return _row_to_user(row)

    def update_password_hash(self, user_id: str, password_hash: str) -> None:
        """Replace one user's stored password hash (P5.4.5 rehash persistence).

        Only ``password_hash`` and ``updated_at`` are written; username, role,
        active status and the identifier are untouched. The encoding is the
        P5.3 Argon2id string and is persisted verbatim (the repository never
        re-hashes or truncates credential material). ``updated_at`` comes from
        the application clock (:func:`src.clock.utc_now`), consistent with the
        rest of this layer. Missing user → :class:`UserNotFound`.
        """
        if not isinstance(password_hash, str) or not password_hash.strip():
            raise UserValidationError(
                "password_hash must be a non-empty string"
            )
        user, updated = _validated_update_args(user_id, utc_now())
        row = self._run(
            _UPDATE_PASSWORD_HASH_SQL, (password_hash, updated, user)
        ).fetchone()  # type: ignore[attr-defined]
        if row is None:
            raise UserNotFound(
                "no user was updated because the requested user does not exist"
            )
        return None


def _validated_update_args(
    user_id: str, updated_at: datetime
) -> tuple[str, datetime]:
    """Validate an update's id and authored timestamp before any DB call."""
    if not isinstance(user_id, str) or not user_id.strip():
        raise UserValidationError("user_id must be a non-empty string")
    if not isinstance(updated_at, datetime):
        raise UserValidationError(
            f"updated_at must be a datetime, got {type(updated_at).__name__}"
        )
    if updated_at.tzinfo is None or updated_at.utcoffset() is None:
        raise UserValidationError(
            "updated_at must be timezone-aware (e.g. datetime.now(UTC))"
        )
    return user_id.strip(), updated_at
