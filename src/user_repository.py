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
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Final

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

#: PostgreSQL unique-constraint violation SQLSTATE (duck-typed off the driver
#: exception object — no driver import needed for detection).
_UNIQUE_VIOLATION_SQLSTATE: Final[str] = "23505"

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

    def _run_insert(self, user: User) -> None:
        """Insert, translating **only** a unique violation to a typed error."""
        with self._connection_factory() as connection:
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
