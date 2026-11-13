"""Offline tests for the Phase 5 user identity model (``src.user``).

Pure data: no database, no driver, no connection, no file access.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from src.user import (
    MAX_USERNAME_LENGTH,
    MIN_USERNAME_LENGTH,
    Role,
    User,
    UserValidationError,
    normalize_username,
    username_key_for,
)

HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNo"
CREATED = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def make_user(**overrides: object) -> User:
    fields: dict[str, object] = {
        "user_id": "u_0001",
        "username": "ada",
        "password_hash": HASH,
        "role": Role.ADMIN,
        "created_at": CREATED,
    }
    fields.update(overrides)
    return User(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def test_role_values_are_exactly_the_four_approved_roles() -> None:
    assert [role.value for role in Role] == [
        "Admin",
        "HR Manager",
        "Operator",
        "Auditor",
    ]
    assert Role("HR Manager") is Role.HR_MANAGER
    assert Role.ADMIN.label == "Admin"


def test_unknown_role_value_is_refused() -> None:
    with pytest.raises(ValueError):
        Role("Superuser")


# ---------------------------------------------------------------------------
# Username normalization
# ---------------------------------------------------------------------------


def test_username_is_nfkc_normalized_and_trimmed() -> None:
    # Full-width characters compose to their ASCII equivalents.
    assert normalize_username("  ａｄａ  ") == "ada"
    assert normalize_username("\tada  lovelace ") == "ada lovelace"


def test_username_case_is_preserved_in_display_form() -> None:
    assert normalize_username("Ada Lovelace") == "Ada Lovelace"
    assert username_key_for("Ada Lovelace") == "ada lovelace"
    assert username_key_for("ADA LOVELACE") == username_key_for("ada lovelace")


def test_username_key_matches_the_normalized_display_form() -> None:
    assert username_key_for("  Ｍａｒｉｅ   Curie ") == "marie curie"


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_username_is_refused(value: str) -> None:
    with pytest.raises(UserValidationError):
        normalize_username(value)


def test_short_username_is_refused() -> None:
    too_short = "a" * (MIN_USERNAME_LENGTH - 1)
    with pytest.raises(UserValidationError):
        normalize_username(too_short)


def test_long_username_is_refused() -> None:
    too_long = "a" * (MAX_USERNAME_LENGTH + 1)
    with pytest.raises(UserValidationError):
        normalize_username(too_long)


def test_username_with_control_characters_is_refused() -> None:
    with pytest.raises(UserValidationError):
        normalize_username("ada\u0000lovelace")
    with pytest.raises(UserValidationError):
        normalize_username("ada\u007flovelace")


def test_non_string_username_is_refused() -> None:
    with pytest.raises(UserValidationError):
        normalize_username(12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------


def test_valid_user_derives_its_username_key() -> None:
    user = make_user(username="  Ada  Lovelace ")
    assert user.username == "Ada Lovelace"
    assert user.username_key == "ada lovelace"
    assert user.role is Role.ADMIN
    assert user.is_active is True
    assert user.updated_at == user.created_at


def test_explicit_username_key_must_match_the_derived_key() -> None:
    with pytest.raises(UserValidationError):
        make_user(username="ada", username_key="someone-else")


def test_user_id_is_trimmed_and_blank_is_refused() -> None:
    assert make_user(user_id="  u_1  ").user_id == "u_1"
    with pytest.raises(UserValidationError):
        make_user(user_id="   ")


def test_raw_string_role_is_refused() -> None:
    with pytest.raises(UserValidationError):
        make_user(role="Admin")


def test_is_active_must_be_a_real_bool() -> None:
    with pytest.raises(UserValidationError):
        make_user(is_active="true")
    with pytest.raises(UserValidationError):
        make_user(is_active=1)


@pytest.mark.parametrize("value", ["", "   ", " hash "])
def test_blank_or_padded_password_hash_is_refused(value: str) -> None:
    with pytest.raises(UserValidationError):
        make_user(password_hash=value)


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(UserValidationError):
        make_user(created_at=datetime(2026, 11, 12, 9, 0))
    with pytest.raises(UserValidationError):
        make_user(updated_at=datetime(2026, 11, 12, 9, 0))


def test_updated_at_must_not_precede_created_at() -> None:
    with pytest.raises(UserValidationError):
        make_user(updated_at=CREATED - timedelta(seconds=1))


def test_password_hash_is_excluded_from_repr() -> None:
    user = make_user()
    assert HASH not in repr(user)
    assert user.password_hash == HASH


def test_user_is_immutable() -> None:
    user = make_user()
    with pytest.raises(FrozenInstanceError):
        user.role = Role.AUDITOR  # type: ignore[misc]
