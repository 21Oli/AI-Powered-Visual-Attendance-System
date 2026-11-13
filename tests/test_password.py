"""Deterministic tests for the Argon2id password service (P5.3).

Every test here runs offline against the real ``argon2-cffi`` hasher (there is
no plaintext or fake crypto): the point is to pin the approved parameters and
the fail-closed error mapping, not to re-test the library's math.
"""

from __future__ import annotations

import inspect

import pytest
from argon2 import PasswordHasher as Argon2Hasher
from argon2 import extract_parameters
from argon2.low_level import Type

import src.password as password_module
from src.config import PASSWORD_HASHING, PasswordHashingConfig
from src.password import (
    InvalidPasswordHash,
    PasswordPolicyError,
    PasswordService,
)

PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def service() -> PasswordService:
    return PasswordService()


@pytest.fixture(scope="module")
def stored_hash(service: PasswordService) -> str:
    return service.hash_password(PASSWORD)


# ---------------------------------------------------------------------------
# Approved configuration
# ---------------------------------------------------------------------------


def test_approved_parameters_are_the_owner_decision() -> None:
    assert PASSWORD_HASHING.memory_cost_kib == 19456
    assert PASSWORD_HASHING.time_cost == 2
    assert PASSWORD_HASHING.parallelism == 1
    assert PASSWORD_HASHING.salt_length == 16
    assert PASSWORD_HASHING.hash_length == 32
    assert PASSWORD_HASHING.min_password_length == 8


def test_hash_encoding_carries_the_approved_argon2id_parameters(
    stored_hash: str,
) -> None:
    assert stored_hash.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    parameters = extract_parameters(stored_hash)
    assert parameters.type is Type.ID
    assert parameters.version == 19
    assert parameters.memory_cost == 19456
    assert parameters.time_cost == 2
    assert parameters.parallelism == 1
    assert parameters.salt_len == PASSWORD_HASHING.salt_length
    assert parameters.hash_len == PASSWORD_HASHING.hash_length


def test_config_rejects_a_password_policy_below_the_approved_floor() -> None:
    with pytest.raises(ValueError):
        PasswordHashingConfig(min_password_length=7)
    with pytest.raises(ValueError):
        PasswordHashingConfig(salt_length=8, hash_length=8)
    with pytest.raises(ValueError):
        PasswordHashingConfig(memory_cost_kib=4)


# ---------------------------------------------------------------------------
# Hashing and verification
# ---------------------------------------------------------------------------


def test_hash_then_verify_round_trip(service: PasswordService) -> None:
    encoded = service.hash_password(PASSWORD)
    assert service.verify_password(PASSWORD, encoded) is True


def test_wrong_password_is_a_false_not_an_exception(service: PasswordService) -> None:
    encoded = service.hash_password(PASSWORD)
    assert service.verify_password("not the password", encoded) is False


def test_each_hash_uses_a_fresh_salt(service: PasswordService) -> None:
    first = service.hash_password(PASSWORD)
    second = service.hash_password(PASSWORD)
    # The salt is the fourth ``$``-delimited field of the PHC encoding.
    assert first.split("$")[4] != second.split("$")[4]
    assert len(first.split("$")[4]) >= 16
    assert service.verify_password(PASSWORD, first)
    assert service.verify_password(PASSWORD, second)


def test_password_is_never_trimmed_or_normalized(service: PasswordService) -> None:
    encoded = service.hash_password("  spaced  ")
    assert service.verify_password("  spaced  ", encoded) is True
    assert service.verify_password("spaced", encoded) is False


def test_unicode_is_not_nfkc_normalized(service: PasswordService) -> None:
    composed = "pässwörd"
    decomposed = "pa\u0308sswo\u0308rd"
    encoded = service.hash_password(composed)
    assert service.verify_password(composed, encoded) is True
    assert service.verify_password(decomposed, encoded) is False


def test_minimum_length_is_eight_characters(service: PasswordService) -> None:
    with pytest.raises(PasswordPolicyError):
        service.hash_password("1234567")
    encoded = service.hash_password("12345678")
    assert service.verify_password("12345678", encoded) is True


def test_short_legacy_password_can_still_be_verified(
    service: PasswordService,
) -> None:
    """The floor is an issuance rule; verification only checks type/size."""
    short = "short"
    legacy = Argon2Hasher(
        time_cost=2,
        memory_cost=19456,
        parallelism=1,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    ).hash(short)
    assert service.verify_password(short, legacy) is True
    assert service.needs_rehash(legacy) is False


def test_non_string_passwords_are_refused(service: PasswordService) -> None:
    for value in (None, 12345678, b"password", ["password"]):
        with pytest.raises(PasswordPolicyError):
            service.hash_password(value)  # type: ignore[arg-type]
        with pytest.raises(PasswordPolicyError):
            service.verify_password(value, "$argon2id$v=19$m=19456,t=2,p=1$x$y")  # type: ignore[arg-type]


def test_defensive_maximum_length_is_enforced(service: PasswordService) -> None:
    maximum = PASSWORD_HASHING.max_password_length
    encoded = service.hash_password("a" * maximum)
    assert service.verify_password("a" * maximum, encoded) is True
    with pytest.raises(PasswordPolicyError):
        service.hash_password("a" * (maximum + 1))


def test_service_repr_and_module_hold_no_credentials(
    service: PasswordService, stored_hash: str
) -> None:
    assert PASSWORD not in repr(service)
    assert stored_hash not in repr(service)
    assert not hasattr(password_module, "logging")
    assert not hasattr(password_module, "logger")
    source = inspect.getsource(password_module)
    assert "logging" not in source


# ---------------------------------------------------------------------------
# Rehash detection
# ---------------------------------------------------------------------------


def test_current_hash_does_not_need_a_rehash(
    service: PasswordService, stored_hash: str
) -> None:
    assert service.needs_rehash(stored_hash) is False


def test_weaker_hash_needs_a_rehash(service: PasswordService) -> None:
    weaker = Argon2Hasher(
        time_cost=1,
        memory_cost=8192,
        parallelism=1,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    ).hash(PASSWORD)
    assert service.needs_rehash(weaker) is True
    assert service.verify_password(PASSWORD, weaker) is True


def test_argon2i_hash_is_refused_rather_than_upgraded(
    service: PasswordService,
) -> None:
    argon2i = Argon2Hasher(
        time_cost=2,
        memory_cost=19456,
        parallelism=1,
        hash_len=32,
        salt_len=16,
        type=Type.I,
    ).hash(PASSWORD)
    with pytest.raises(InvalidPasswordHash):
        service.verify_password(PASSWORD, argon2i)
    with pytest.raises(InvalidPasswordHash):
        service.needs_rehash(argon2i)


# ---------------------------------------------------------------------------
# Fail-closed handling of malformed or hostile stored hashes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_hash",
    [
        "",
        "   ",
        "not-a-hash",
        "$argon2id$v=19$m=19456,t=2,p=1$only-four-parts",
        "$argon2id$v=19$m=19456,t=2,p=1$$",
        "$pbkdf2-sha256$29000$c2FsdA$hash",
    ],
)
def test_malformed_stored_hashes_are_refused(
    service: PasswordService, bad_hash: str
) -> None:
    with pytest.raises(InvalidPasswordHash):
        service.verify_password(PASSWORD, bad_hash)
    with pytest.raises(InvalidPasswordHash):
        service.needs_rehash(bad_hash)


def test_non_string_stored_hash_is_refused(service: PasswordService) -> None:
    for value in (None, 123, b"$argon2id$"):
        with pytest.raises(InvalidPasswordHash):
            service.verify_password(PASSWORD, value)  # type: ignore[arg-type]


def test_over_budget_memory_cost_is_refused(
    service: PasswordService, stored_hash: str
) -> None:
    parts = stored_hash.split("$")
    parts[3] = "m=1048576,t=2,p=1"
    hostile = "$".join(parts)
    assert extract_parameters(hostile).memory_cost == 1048576
    with pytest.raises(InvalidPasswordHash):
        service.verify_password(PASSWORD, hostile)
    with pytest.raises(InvalidPasswordHash):
        service.needs_rehash(hostile)


def test_over_budget_time_cost_is_refused(
    service: PasswordService, stored_hash: str
) -> None:
    parts = stored_hash.split("$")
    parts[3] = "m=19456,t=99,p=1"
    with pytest.raises(InvalidPasswordHash):
        service.verify_password(PASSWORD, "$".join(parts))


def test_unsupported_argon2_version_is_refused(
    service: PasswordService, stored_hash: str
) -> None:
    hostile = stored_hash.replace("v=19", "v=16", 1)
    with pytest.raises(InvalidPasswordHash):
        service.verify_password(PASSWORD, hostile)


def test_truncated_hash_mismatches_are_handled_by_the_library(
    service: PasswordService, stored_hash: str
) -> None:
    """A structurally valid but altered digest is a mismatch, not a crash."""
    index = len(stored_hash) - 5
    replacement = "A" if stored_hash[index] != "A" else "B"
    altered = stored_hash[:index] + replacement + stored_hash[index + 1 :]
    assert altered != stored_hash
    assert service.verify_password(PASSWORD, altered) is False
