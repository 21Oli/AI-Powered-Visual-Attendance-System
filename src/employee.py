"""Employee identity model for the AI-Powered Visual Attendance System
(Phase 1 — Step 03).

This module is the **identity layer only**: it answers "who is this
employee?" for future enrollment and recognition components. It contains
no face data, no embeddings, no camera frames, no recognition confidence,
and no attendance state — those belong to the recognition and attendance
layers (later steps), which *reference* an employee by ``employee_id``
but never extend this model with biometric fields.

Conventions follow ``src/config.py`` (Step 02):

* frozen dataclass — no global mutable state; mutating an Employee raises
  :class:`dataclasses.FrozenInstanceError`,
* validation in ``__post_init__`` — invalid instances cannot exist,
* standard library only — no third-party dependencies, no database, no
  network, no CV/ML stack,
* **no side effects on import** — this module opens no devices, creates
  no directories, and reads no environment.

Normalization is explicit and minimal (documented in the field notes and
tested in ``tests/test_employee.py``):

* ``employee_id`` — leading/trailing whitespace is stripped. Every other
  character is preserved exactly: letters, digits, hyphens, underscores,
  and leading zeros are all valid, so real business identifiers
  (e.g. ``"007"``, ``"EMP-0042"``) survive unchanged. No case-folding.
* ``full_name`` — leading/trailing whitespace is stripped and internal
  whitespace runs are collapsed to a single space, so ``"  Ada   Lovelace "``
  becomes ``"Ada Lovelace"``. Case is preserved as provided.

Invalid input is never silently converted into valid-looking data:
violations raise :class:`EmployeeValidationError` (a ``ValueError``
subclass) with an actionable message naming the field and the rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = ["Employee", "EmployeeValidationError"]

# Internal whitespace runs (spaces/tabs) collapse to a single space.
_WHITESPACE_RUN = re.compile(r"\s+")


class EmployeeValidationError(ValueError):
    """Raised when an Employee field violates its validation rule."""


def _require_utc_aware(value: datetime, field_name: str) -> datetime:
    """Reject naive datetimes; timezone-aware values pass through as-is."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise EmployeeValidationError(
            f"{field_name} must be timezone-aware "
            f"(e.g. datetime.now(UTC)), got naive datetime {value!r}"
        )
    return value


@dataclass(frozen=True, slots=True)
class Employee:
    """An employee's identity: stable ID, display name, and status.

    Attributes:
        employee_id: Stable, unique business identifier. Non-empty after
            stripping surrounding whitespace; otherwise preserved exactly
            (leading zeros, hyphens, mixed case are all valid). Uniqueness
            is enforced by callers/storage later — this model validates
            only the value's form.
        full_name: Display name, non-empty after normalization.
        is_active: Whether the employee is currently active. Must be a
            real ``bool`` — strings and other truthy values are rejected
            rather than coerced.
        created_at: Timezone-aware creation timestamp. Defaults to the
            current UTC time; naive datetimes are rejected.
    """

    employee_id: str
    full_name: str
    is_active: bool = True
    created_at: datetime | None = None  # None means "now" (set in post-init)

    def __post_init__(self) -> None:
        # -- employee_id ------------------------------------------------
        if not isinstance(self.employee_id, str):
            raise EmployeeValidationError(
                f"employee_id must be a str, got {type(self.employee_id).__name__}"
            )
        normalized_id = self.employee_id.strip()
        if not normalized_id:
            raise EmployeeValidationError(
                "employee_id must be non-empty after trimming whitespace"
            )
        object.__setattr__(self, "employee_id", normalized_id)

        # -- full_name --------------------------------------------------
        if not isinstance(self.full_name, str):
            raise EmployeeValidationError(
                f"full_name must be a str, got {type(self.full_name).__name__}"
            )
        normalized_name = _WHITESPACE_RUN.sub(" ", self.full_name).strip()
        if not normalized_name:
            raise EmployeeValidationError(
                "full_name must be non-empty after trimming whitespace"
            )
        object.__setattr__(self, "full_name", normalized_name)

        # -- is_active --------------------------------------------------
        if not isinstance(self.is_active, bool):
            raise EmployeeValidationError(
                f"is_active must be a bool, got {type(self.is_active).__name__}"
            )

        # -- created_at -------------------------------------------------
        if self.created_at is None:
            object.__setattr__(self, "created_at", datetime.now(UTC))
        else:
            if not isinstance(self.created_at, datetime):
                raise EmployeeValidationError(
                    "created_at must be a datetime, got "
                    f"{type(self.created_at).__name__}"
                )
            object.__setattr__(
                self,
                "created_at",
                _require_utc_aware(self.created_at, "created_at"),
            )

    # ------------------------------------------------------------------
    # Serialization (explicit dict only — no persistence is implied)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, str | bool]:
        """Return an explicit dictionary representation of the employee.

        Supported fields: ``employee_id`` (str, normalized), ``full_name``
        (str, normalized), ``is_active`` (bool), ``created_at``
        (ISO-8601 string including timezone offset).

        This is for logging, tests, and lightweight interchange. It is a
        dictionary — **not** database persistence, which arrives in a
        later step.
        """
        return {
            "employee_id": self.employee_id,
            "full_name": self.full_name,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else "",
        }
