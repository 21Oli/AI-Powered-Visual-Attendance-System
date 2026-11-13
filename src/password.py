"""Argon2id password hashing and verification (P5.3).

Scope
-----

This module hashes and verifies passwords. It does not authenticate users,
issue sessions, read a database, or expose HTTP — those are P5.4/P5.5. It
imports ``argon2-cffi`` (the one approved dependency) and uses the parameters
in :data:`src.config.PASSWORD_HASHING` — never a per-call override.

Approved parameters (owner decision, P5.3)
------------------------------------------

====================  =========================================
algorithm             Argon2id (``Type.ID``)
memory cost           19,456 KiB
time cost             2
parallelism           1
salt length           16 bytes (CSPRNG, per hash)
hash length           32 bytes
minimum password      8 characters
====================  =========================================

Password handling rules
-----------------------

* **Never trimmed, folded, normalized or truncated.** A password is used
  exactly as supplied: ``" Password "`` and ``"Password"`` are different
  secrets. Validation rejects (never rewrites) anything outside the policy.
* The minimum length applies when a password is **created**. Verification
  enforces only type and the defensive maximum, so a shorter pre-existing
  secret simply fails to match instead of being re-policy-checked.
* Nothing here logs. There is no logger in this module, no message contains
  a password or a hash, and no exception carries either value.

Fail-closed semantics
---------------------

* A stored hash that is not a well-formed Argon2id v19 encoding, or whose
  parameters exceed the verification bounds below, raises
  :class:`InvalidPasswordHash` instead of ever returning ``True``.
* Only a genuine mismatch returns ``False``.
* The encoded result of :meth:`PasswordService.hash_password` is re-parsed
  before it is returned, so a misconfigured library cannot silently produce
  a weaker credential than the approved policy.

Verification bounds exist because the parameters live *in the stored hash*: a
tampered or foreign hash could otherwise request gigabytes of memory.
"""

from __future__ import annotations

from typing import Any

from argon2 import PasswordHasher as _Argon2PasswordHasher
from argon2 import extract_parameters
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from argon2.low_level import Type

from src.config import PASSWORD_HASHING, PasswordHashingConfig

__all__ = [
    "InvalidPasswordHash",
    "PasswordError",
    "PasswordPolicyError",
    "PasswordService",
]

#: Argon2 version 19 (0x13) is the only version the approved parameters use.
_ARGON2_VERSION: int = 19
#: Upper bound on the iterations a stored hash may request during verification.
_MAX_ACCEPTED_TIME_COST: int = 16
#: Upper bound on the lanes a stored hash may request during verification.
_MAX_ACCEPTED_PARALLELISM: int = 8
#: Minimum salt/hash sizes accepted from a stored hash.
_MIN_ACCEPTED_SALT_LENGTH: int = 8
_MIN_ACCEPTED_HASH_LENGTH: int = 16


class PasswordError(ValueError):
    """Base class for password-policy and password-hash failures."""


class PasswordPolicyError(PasswordError):
    """A supplied password violates the approved policy (never rewritten)."""


class InvalidPasswordHash(PasswordError):
    """A stored hash is malformed, foreign, or outside the accepted bounds."""


def _build_hasher(config: PasswordHashingConfig) -> _Argon2PasswordHasher:
    """Construct the Argon2id hasher from the approved configuration."""
    return _Argon2PasswordHasher(
        time_cost=config.time_cost,
        memory_cost=config.memory_cost_kib,
        parallelism=config.parallelism,
        hash_len=config.hash_length,
        salt_len=config.salt_length,
        type=Type.ID,
    )


def _require_password(
    value: object,
    *,
    enforce_minimum: bool,
    config: PasswordHashingConfig,
) -> str:
    """Validate a password without altering it.

    The value is returned exactly as supplied. Length is measured in
    characters, as the policy is stated in characters.
    """
    if not isinstance(value, str):
        raise PasswordPolicyError(
            f"password must be a str, got {type(value).__name__}"
        )
    if enforce_minimum and len(value) < config.min_password_length:
        raise PasswordPolicyError(
            f"password must be at least {config.min_password_length} "
            "characters"
        )
    if len(value) > config.max_password_length:
        raise PasswordPolicyError(
            f"password must be at most {config.max_password_length} characters"
        )
    return value


def _require_hash_text(value: object) -> str:
    """Reject a non-string or blank stored hash before parsing it."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidPasswordHash("stored password hash is missing or malformed")
    return value


class PasswordService:
    """Argon2id hashing, verification and rehash detection.

    Parameters
    ----------
    config:
        Approved parameters; defaults to :data:`src.config.PASSWORD_HASHING`.
    hasher:
        Test seam. When omitted, an Argon2id hasher is built from ``config``.
        Supplying one allows tests to prove that the bounds and error mapping
        hold regardless of the concrete hasher.
    """

    def __init__(
        self,
        *,
        config: PasswordHashingConfig | None = None,
        hasher: _Argon2PasswordHasher | None = None,
    ) -> None:
        self._config = PASSWORD_HASHING if config is None else config
        self._hasher = (
            _build_hasher(self._config) if hasher is None else hasher
        )

    # -- internals ----------------------------------------------------------

    def _parameters(self, password_hash: object) -> Any:
        """Parse and bound-check a stored hash (fail closed).

        Returns ``argon2``'s parameter object (a private-module type this
        project deliberately does not import); callers here only read the
        documented ``type``/``version``/cost/length attributes.
        """
        text = _require_hash_text(password_hash)
        try:
            params = extract_parameters(text)
        except (InvalidHashError, VerificationError, ValueError, TypeError):
            raise InvalidPasswordHash(
                "stored password hash is not a well-formed Argon2 encoding"
            ) from None
        if params.type is not Type.ID:
            raise InvalidPasswordHash(
                "stored password hash is not Argon2id; refusing to verify it"
            )
        if params.version != _ARGON2_VERSION:
            raise InvalidPasswordHash(
                "stored password hash uses an unsupported Argon2 version"
            )
        if params.memory_cost > self._config.memory_cost_kib:
            raise InvalidPasswordHash(
                "stored password hash requests more memory than the approved "
                "verification bound"
            )
        if params.time_cost > _MAX_ACCEPTED_TIME_COST:
            raise InvalidPasswordHash(
                "stored password hash requests more iterations than the "
                "approved verification bound"
            )
        if params.parallelism > _MAX_ACCEPTED_PARALLELISM:
            raise InvalidPasswordHash(
                "stored password hash requests more lanes than the approved "
                "verification bound"
            )
        if params.salt_len < _MIN_ACCEPTED_SALT_LENGTH:
            raise InvalidPasswordHash("stored password hash has too short a salt")
        if params.hash_len < _MIN_ACCEPTED_HASH_LENGTH:
            raise InvalidPasswordHash(
                "stored password hash has too short a derived key"
            )
        return params

    # -- approved operations ------------------------------------------------

    def hash_password(self, password: str) -> str:
        """Hash ``password`` with the approved Argon2id parameters.

        The password is used verbatim (never trimmed or normalized). A value
        shorter than :attr:`PasswordHashingConfig.min_password_length` or
        longer than the defensive maximum raises :class:`PasswordPolicyError`
        before any hashing work is done.
        """
        candidate = _require_password(
            password, enforce_minimum=True, config=self._config
        )
        encoded = self._hasher.hash(candidate)
        # Re-parse the result: the returned credential must itself satisfy
        # the approved type/version/bounds, or the caller gets an error
        # instead of a weaker hash.
        self._parameters(encoded)
        return encoded

    def verify_password(self, password: str, password_hash: str) -> bool:
        """Whether ``password`` matches the stored Argon2id ``password_hash``.

        Returns ``False`` only for a genuine mismatch. A malformed, foreign
        (non-Argon2id) or over-budget stored hash raises
        :class:`InvalidPasswordHash` — verification never silently accepts a
        credential it could not properly check.
        """
        candidate = _require_password(
            password, enforce_minimum=False, config=self._config
        )
        self._parameters(password_hash)
        try:
            self._hasher.verify(password_hash, candidate)
        except VerifyMismatchError:
            return False
        except InvalidHashError:
            raise InvalidPasswordHash(
                "stored password hash is not a well-formed Argon2 encoding"
            ) from None
        except VerificationError:
            raise InvalidPasswordHash(
                "stored password hash could not be verified"
            ) from None
        return True

    def needs_rehash(self, password_hash: str) -> bool:
        """Whether a stored hash should be replaced with a current one.

        True when the stored parameters are weaker than (or different from)
        the approved configuration, so a successful sign-in can transparently
        upgrade the credential. A malformed or over-budget hash raises
        :class:`InvalidPasswordHash` instead of being reported as "fine".
        """
        self._parameters(password_hash)
        return bool(self._hasher.check_needs_rehash(password_hash))
