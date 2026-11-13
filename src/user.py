"""Identity model for the Phase 5 security layer (P5.2).

This module is **data and validation only**: a role vocabulary, a validated
:class:`User` value, and the documented username-normalization contract. It
contains no password hashing, no password verification, no session logic, no
SQL, no driver import and no HTTP surface — those arrive in later Phase 5
milestones (P5.3 and beyond).

Conventions follow :mod:`src.employee` and :mod:`src.config`:

* frozen dataclass — invalid instances cannot exist, mutation raises,
* validation in ``__post_init__`` with an actionable ``ValueError`` subclass,
* standard library only, no side effects on import.

Username normalization (the one documented rule)
------------------------------------------------

``username`` is user-facing text, ``username_key`` is the deterministic
lookup form. Both are derived from the same input by the same function, so
they cannot silently disagree:

1. Unicode NFKC normalization (composes compatibles, e.g. full-width forms),
2. leading/trailing whitespace stripped,
3. internal whitespace runs collapsed to a single ASCII space,
4. case preserved for ``username``; ``username_key`` is the casefolded
   form of the result.

The database's UNIQUE constraint is on ``username_key``, so ``Ada`` and
``ada`` are the same account eligibility-wise while the displayed name is
never rewritten. Casefolding is deterministic for a given Python version;
the project recommends ASCII usernames and does not claim confusable/
homoglyph protection (documented limitation, not a security control).

Security note: ``password_hash`` is excluded from the dataclass ``repr`` and
must never be logged, serialized to a client, or copied into an audit
record. The model does not validate the hash *format* — Argon2id encoding is
P5.3's concern — but it refuses blank or control-character-bearing values so
an empty column can never look like a credential.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

__all__ = [
    "MAX_USERNAME_LENGTH",
    "MIN_USERNAME_LENGTH",
    "Role",
    "User",
    "UserValidationError",
    "normalize_username",
    "username_key_for",
]

#: Inclusive display-length bounds applied **after** normalization.
MIN_USERNAME_LENGTH: int = 3
MAX_USERNAME_LENGTH: int = 64

_WHITESPACE_RUN = re.compile(r"\s+")


class UserValidationError(ValueError):
    """Raised when a user identity field violates its validation rule."""


class Role(str, Enum):
    """The approved Phase 5 roles (owner decision, P5.1).

    Values are the exact strings stored in ``app_user.role`` and enforced by
    the migration's ``CHECK`` constraint; keep the two in lockstep.
    """

    ADMIN = "Admin"
    HR_MANAGER = "HR Manager"
    OPERATOR = "Operator"
    AUDITOR = "Auditor"

    @property
    def label(self) -> str:
        """The persisted/database spelling of this role."""
        return self.value


def normalize_username(value: str) -> str:
    """Return the normalized display username for ``value``.

    Raises :class:`UserValidationError` for non-strings, lengths outside
    :data:`MIN_USERNAME_LENGTH`..:data:`MAX_USERNAME_LENGTH`, and control
    characters. Case is preserved; see the module docstring for the exact
    rule and its limits.
    """
    if not isinstance(value, str):
        raise UserValidationError(
            f"username must be a str, got {type(value).__name__}"
        )
    normalized = _WHITESPACE_RUN.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    if not normalized:
        raise UserValidationError("username must be non-empty after trimming")
    if len(normalized) < MIN_USERNAME_LENGTH:
        raise UserValidationError(
            f"username must be at least {MIN_USERNAME_LENGTH} characters after "
            f"normalization, got {len(normalized)}"
        )
    if len(normalized) > MAX_USERNAME_LENGTH:
        raise UserValidationError(
            f"username must be at most {MAX_USERNAME_LENGTH} characters after "
            f"normalization, got {len(normalized)}"
        )
    for character in normalized:
        code_point = ord(character)
        if code_point < 0x20 or code_point == 0x7F:
            raise UserValidationError(
                "username must not contain control characters"
            )
    return normalized


def username_key_for(value: str) -> str:
    """Return the deterministic lookup key for ``value``.

    This is :func:`normalize_username` followed by ``str.casefold`` — the
    single definition used by the model and the repository.
    """
    return normalize_username(value).casefold()


def _require_utc_aware(value: datetime, field_name: str) -> datetime:
    """Reject naive datetimes; timezone-aware values pass through as-is."""
    if not isinstance(value, datetime):
        raise UserValidationError(
            f"{field_name} must be a datetime, got {type(value).__name__}"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise UserValidationError(
            f"{field_name} must be timezone-aware (e.g. datetime.now(UTC)), "
            f"got naive datetime {value!r}"
        )
    return value


@dataclass(frozen=True, slots=True)
class User:
    """One application user (identity metadata only).

    Attributes
    ----------
    user_id:
        Stable application identifier (text). Leading/trailing whitespace is
        stripped; the value is otherwise preserved.
    username:
        Display name, normalized per the module docstring.
    password_hash:
        The complete Argon2id encoded hash supplied by P5.3. Opaque here:
        the repository stores it verbatim and it is excluded from ``repr``.
        It must **never** be logged, returned to a client, or audited.
    role:
        A :class:`Role` member. Raw strings are refused so a typo cannot
        become an unrecognized privilege state.
    is_active:
        Real ``bool``; an inactive user keeps their row but may not sign in
        (enforcement arrives with authentication).
    created_at / updated_at:
        Timezone-aware, application-authored timestamps. ``updated_at`` may
        not precede ``created_at``.
    username_key:
        Optional explicit lookup key. When omitted it is derived from
        ``username``; when supplied (for example when reading a row) it must
        equal the derived key, so a corrupted row cannot pass as valid.
    """

    user_id: str
    username: str
    password_hash: str = field(repr=False)
    role: Role
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
    username_key: str | None = None

    def __post_init__(self) -> None:
        # -- user_id -----------------------------------------------------
        if not isinstance(self.user_id, str):
            raise UserValidationError(
                f"user_id must be a str, got {type(self.user_id).__name__}"
            )
        normalized_id = self.user_id.strip()
        if not normalized_id:
            raise UserValidationError(
                "user_id must be non-empty after trimming whitespace"
            )
        object.__setattr__(self, "user_id", normalized_id)

        # -- username / username_key -------------------------------------
        normalized_name = normalize_username(self.username)
        object.__setattr__(self, "username", normalized_name)
        derived_key = normalized_name.casefold()
        if self.username_key is None:
            object.__setattr__(self, "username_key", derived_key)
        else:
            if not isinstance(self.username_key, str):
                raise UserValidationError(
                    "username_key must be a str, got "
                    f"{type(self.username_key).__name__}"
                )
            if self.username_key != derived_key:
                raise UserValidationError(
                    "username_key does not match the normalized username; "
                    "stored identity rows must never be silently reinterpreted"
                )

        # -- password_hash ----------------------------------------------
        if not isinstance(self.password_hash, str):
            raise UserValidationError(
                "password_hash must be a str, got "
                f"{type(self.password_hash).__name__}"
            )
        if not self.password_hash.strip():
            raise UserValidationError("password_hash must be non-empty")
        if self.password_hash != self.password_hash.strip():
            raise UserValidationError(
                "password_hash must not carry surrounding whitespace"
            )
        for character in self.password_hash:
            code_point = ord(character)
            if code_point < 0x20 or code_point == 0x7F:
                raise UserValidationError(
                    "password_hash must not contain control characters"
                )

        # -- role ---------------------------------------------------------
        if not isinstance(self.role, Role):
            raise UserValidationError(
                "role must be a Role member, got "
                f"{type(self.role).__name__}"
                + (
                    f" ({self.role!r}); use Role(value) to convert a stored string"
                    if isinstance(self.role, str)
                    else ""
                )
            )

        # -- is_active ----------------------------------------------------
        if not isinstance(self.is_active, bool):
            raise UserValidationError(
                f"is_active must be a bool, got {type(self.is_active).__name__}"
            )

        # -- timestamps ---------------------------------------------------
        now = datetime.now(UTC)
        created_at = now if self.created_at is None else self.created_at
        updated_at = created_at if self.updated_at is None else self.updated_at
        object.__setattr__(
            self, "created_at", _require_utc_aware(created_at, "created_at")
        )
        object.__setattr__(
            self, "updated_at", _require_utc_aware(updated_at, "updated_at")
        )
        if self.updated_at < self.created_at:  # type: ignore[operator]
            raise UserValidationError(
                "updated_at must not precede created_at"
            )
