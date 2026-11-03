"""Employee-metadata persistence for the Phase 3 PostgreSQL backend
(Step 1.5b) — the approved repository contract from ``DB_DESIGN.md``
Section 4 (Step 1.5a), implemented offline-first.

Scope discipline
----------------

* PostgreSQL stores **employee metadata only** (approved D1): the SQL
  below touches exactly the four approved columns — ``employee_id``,
  ``full_name``, ``is_active``, ``created_at``. No biometric column, no
  embedding column, no attendance-event table, no face/image/path field
  ever appears here.
* ``created_at`` is application-authoritative (approved D2): the
  repository inserts the model's value verbatim and **never** modifies
  it on update; ordinary update operations cannot change ``employee_id``
  or ``is_active`` (approved Step 1.5b contract).
* The repository **injects** its connection source at construction,
  holds no module-level connection, imports no driver at module level
  (hygiene test enforced, mirroring :mod:`src.db`), and therefore never
  connects at import time nor on construction.
* **Blank-DSN refusals belong to the connection layer** and travel
  through untouched: this module adds no fallback, no implicit retry,
  and no alternate read source (approved Step 1.5b; no enrollment-artifact
  or JSONL fallback exists or will be invented here).
* Error taxonomy stays two-family (:mod:`src.db` discipline): contract
  violations are ``ValueError``-shaped (the existing
  :class:`~src.employee.EmployeeValidationError`); persistence-state
  results are the new typed :class:`EmployeeAlreadyExists` /
  :class:`EmployeeNotFound`; availability failures
  (:class:`~src.db.DatabaseUnavailableError` and the rest) propagate
  untouched — the repository introduces no catch-all.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from typing import Final

from src.config import PAGINATION
from src.employee import Employee, EmployeeValidationError

__all__ = [
    "EmployeeAlreadyExists",
    "EmployeeNotFound",
    "EmployeeRepository",
]

# ---------------------------------------------------------------------------
# Typed persistence-state results
# ---------------------------------------------------------------------------


class EmployeeAlreadyExists(RuntimeError):
    """A create was refused because ``employee_id`` is already recorded.

    Typed (not a raw driver error surfaced) precisely so the API layer
    can produce the approved 409 mapping (DB_DESIGN.md D7). No retry and
    no alternate-ID generation exist by contract.
    """


class EmployeeNotFound(RuntimeError):
    """A get/update/deactivate found no employee for the given ID.

    Typed so the API layer can produce the approved 404 mapping (D7).
    """


# ---------------------------------------------------------------------------
# Static SQL (metadata columns only, bound parameters, no interpolation)
# ---------------------------------------------------------------------------

_SELECT_COLUMNS: Final[str] = (
    "employee_id, full_name, is_active, created_at"
)

_INSERT_SQL: Final[str] = (
    "INSERT INTO employee "
    "(employee_id, full_name, is_active, created_at) "
    "VALUES (%s, %s, %s, %s)"
)

_GET_SQL: Final[str] = (
    f"SELECT {_SELECT_COLUMNS} FROM employee WHERE employee_id = %s"
)

_LIST_SQL_PREFIX: Final[str] = f"SELECT {_SELECT_COLUMNS} FROM employee"

_KEYSET_CLAUSE_SQL: Final[str] = " WHERE employee_id > %s"

_ORDER_SQL: Final[str] = " ORDER BY employee_id ASC LIMIT %s"

_UPDATE_NAME_SQL: Final[str] = (
    "UPDATE employee SET full_name = %s WHERE employee_id = %s "
    f"RETURNING {_SELECT_COLUMNS}"
)

_DEACTIVATE_SQL: Final[str] = (
    "UPDATE employee SET is_active = FALSE WHERE employee_id = %s "
    f"RETURNING {_SELECT_COLUMNS}"
)

#: PostgreSQL unique-constraint violation SQLSTATE (duck-typed off the
#: driver exception object — no driver import needed for detection).
#: psycopg **3** exposes ``sqlstate`` (psycopg2's ``pgcode`` attribute
#: does not exist in psycopg 3, which is this project's pinned driver);
#: fakes set the same ``sqlstate`` attribute so the offline tests model
#: the real driver instead of an attribute it never carries.
_UNIQUE_VIOLATION_SQLSTATE: Final[str] = "23505"

_ROW_COLUMNS: Final[tuple[str, ...]] = (
    "employee_id",
    "full_name",
    "is_active",
    "created_at",
)


def _row_to_employee(row: Iterable[object]) -> Employee:
    """Rebuild a validated :class:`Employee` from one result row.

    The model's own validation runs here too (tz-aware ``created_at``,
    non-empty fields): a stored row that violates the model cannot
    silently come back as a valid-looking object. Repository code never
    coerces what the model would reject.
    """
    values = tuple(row)
    if len(values) != len(_ROW_COLUMNS):
        raise RuntimeError(
            "employee row shape mismatch: expected exactly the four "
            "approved metadata columns"
        )
    return Employee(
        employee_id=values[0],  # type: ignore[arg-type]  # validated in-model
        full_name=values[1],
        is_active=values[2],
        created_at=values[3],
    )


class EmployeeRepository:
    """Persistence facade for the approved metadata-only contract.

    Parameters
    ----------
    connection_factory:
        Zero-argument callable returning a **context manager** yielding a
        connection-like object with a ``execute(sql, params)`` method
        whose result provides ``fetchone()`` / ``fetchall()`` (the
        psycopg connection/cursor shape). One operation = one acquired
        connection; the context manager owns commit/close semantics, so
        this class performs no explicit transaction management and
        defines no rollback policy of its own.

    Pagination (approved OQ-6, Step 1.5b)
    ------------------------------------
    Keyset-only: ``list`` orders by ``employee_id`` ascending and walks
    with ``WHERE employee_id > after``. ``limit`` above
    :attr:`src.config.PAGINATION.max_page_size` is **clamped** to the
    hard maximum (bounded resource protection); a non-integer, boolean,
    or sub-1 ``limit`` is a :class:`~src.employee.EmployeeValidationError`
    raised **before any DB call**. There is **no consistent-snapshot
    claim across separate requests** — each request has its own
    consistency boundary; rows inserted between requests can affect
    subsequent pages (approved wording, never claim more).
    """

    def __init__(
        self,
        connection_factory: Callable[..., AbstractContextManager[object]],
    ) -> None:
        self._connection_factory = connection_factory

    # -- read/write helpers -------------------------------------------------

    def _run(
        self, sql: str, params: tuple[object, ...]
    ) -> object:
        """Run one statement on a freshly acquired connection and return
        the result (callers fetch rows). Exceptions propagate untouched:
        no catching, no wrapping, no implicit retry anywhere."""
        with self._connection_factory() as connection:
            return connection.execute(sql, params)  # type: ignore[attr-defined]

    def _run_insert(self, employee: Employee) -> object:
        """Run the insert with the approved explicit duplicate mapping:
        the driver's unique-constraint violation (SQLSTATE ``23505``,
        duck-typed on psycopg 3's ``sqlstate`` attribute — no driver
        import) becomes the typed :class:`EmployeeAlreadyExists`; every
        other exception propagates untouched so availability/config
        failures keep their own identity (Gate 1.2 discipline).

        Only ``23505`` is a duplicate. Any other SQLSTATE (``23502`` not-
        null violation, ``42P01`` undefined table, ...) and any
        exception without a readable SQLSTATE propagates unchanged: a
        non-unique failure must never be reported as "already exists".
        """
        with self._connection_factory() as connection:
            try:
                return connection.execute(  # type: ignore[attr-defined]
                    _INSERT_SQL,
                    (
                        employee.employee_id,
                        employee.full_name,
                        employee.is_active,
                        employee.created_at,
                    ),
                )
            except EmployeeAlreadyExists:
                raise
            except Exception as exc:
                if getattr(exc, "sqlstate", None) == _UNIQUE_VIOLATION_SQLSTATE:
                    raise EmployeeAlreadyExists(
                        "employee already exists for the requested id"
                    ) from exc
                raise

    # -- approved operations (Section 4.1) ---------------------------------

    def create(self, employee: Employee) -> Employee:
        """Insert a metadata row from an already-validated model.

        Returns the same ``Employee`` on success. A duplicate ID raises
        :class:`EmployeeAlreadyExists`; anything else is left as-is.
        """
        if not isinstance(employee, Employee):
            raise EmployeeValidationError(
                "create expects an already-validated Employee instance, got "
                f"{type(employee).__name__}"
            )
        result = self._run_insert(employee)
        _ = result  # fetch-only hygiene: insertion confirmed by no exception
        return employee

    def get(self, employee_id: str) -> Employee | None:
        """Exact-match fetch; ``None`` when absent (caller maps to 404,
        never this layer)."""
        if not isinstance(employee_id, str) or not employee_id.strip():
            raise EmployeeValidationError(
                "employee_id must be a non-empty string"
            )
        result = self._run(_GET_SQL, (employee_id.strip(),))
        row = result.fetchone()  # type: ignore[attr-defined]
        return None if row is None else _row_to_employee(row)

    def list(
        self,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> list[Employee]:
        """Keyset page (``employee_id`` ascending), default page
        :attr:`src.config.PAGINATION.default_page_size`, hard clamp at
        :attr:`src.config.PAGINATION.max_page_size`.

        Parameters
        ----------
        after:
            Exclusive keyset cursor: the last ``employee_id`` seen
            (``employee_id > after``), or ``None`` for the first page.
        limit:
            Requested page size (validated, then clamped to the hard
            maximum as documented in the class docstring).

        Raises
        ------
        EmployeeValidationError
            Non-string / blank ``after``; non-int / bool / sub-1
            ``limit`` — all before any DB call.
        """
        if after is not None and (
            not isinstance(after, str) or not after.strip()
        ):
            raise EmployeeValidationError(
                "after must be None or a non-blank employee_id cursor"
            )
        resolved_limit = (
            PAGINATION.default_page_size if limit is None else limit
        )
        if isinstance(resolved_limit, bool) or not isinstance(
            resolved_limit, int
        ):
            raise EmployeeValidationError(
                "limit must be a plain int (bool and float rejected), got "
                f"{type(resolved_limit).__name__}"
            )
        if resolved_limit < 1:
            raise EmployeeValidationError(
                f"limit must be >= 1, got {resolved_limit}"
            )
        clamped = min(resolved_limit, PAGINATION.max_page_size)

        if after is None:
            result = self._run(_LIST_SQL_PREFIX + _ORDER_SQL, (clamped,))
        else:
            result = self._run(
                _LIST_SQL_PREFIX + _KEYSET_CLAUSE_SQL + _ORDER_SQL,
                (after.strip(), clamped),
            )
        rows: Iterable[object] = result.fetchall()  # type: ignore[attr-defined]
        return [_row_to_employee(row) for row in rows]

    def update_full_name(self, employee_id: str, full_name: str) -> Employee:
        """Update **the name only**; the returned read-back reflects the
        database (original ``is_active``/``created_at`` preserved).
        Missing employee → :class:`EmployeeNotFound`."""
        # Validation (name rules, ID form) happens in-model, before any
        # DB call, with exactly the model's normalization rules.
        validated = _validated_pair(employee_id, full_name)
        result = self._run(_UPDATE_NAME_SQL, (validated.full_name, validated.employee_id))
        row = result.fetchone()  # type: ignore[attr-defined]
        if row is None:
            raise EmployeeNotFound(
                "no employee was updated because the requested employee "
                "does not exist"
            )
        return _row_to_employee(row)

    def deactivate(self, employee_id: str) -> Employee:
        """Set ``is_active = FALSE`` and return the read-back row.
        Missing employee → :class:`EmployeeNotFound`. (Inactivity
        enforcement stays downstream, engine/API — this layer persists
        status only.)"""
        if not isinstance(employee_id, str) or not employee_id.strip():
            raise EmployeeValidationError(
                "employee_id must be a non-empty string"
            )
        result = self._run(_DEACTIVATE_SQL, (employee_id.strip(),))
        row = result.fetchone()  # type: ignore[attr-defined]
        if row is None:
            raise EmployeeNotFound(
                "no employee was deactivated because the requested "
                "employee does not exist"
            )
        return _row_to_employee(row)


def _validated_pair(employee_id: str, full_name: str) -> Employee:
    """Validate an ``(employee_id, full_name)`` pair through the model.

    Reuses the model's exact normalization and rejection rules so the
    repository never re-implements (or silently diverges from) them.
    Invalid input raises :class:`EmployeeValidationError` from the
    model, **before any DB call**.
    """
    return Employee(employee_id=employee_id, full_name=full_name)
