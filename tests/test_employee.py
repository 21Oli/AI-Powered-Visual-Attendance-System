"""Step 03 employee-model verification.

Maps to the Step 03 test requirements:

1. Creating a valid employee.
2. Validating required identity fields.
3. Rejecting empty or whitespace-only IDs and names.
4. Validating ``is_active``.
5. Consistent normalization behavior.
6. Preserving IDs with leading zeros and permitted punctuation.
7. Timestamp behavior (default UTC now; naive datetimes rejected).
8. Serialization round-trip.
9. Import without unrelated dependencies or side effects.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from src.employee import Employee, EmployeeValidationError


# ---------------------------------------------------------------------------
# 1. Creating a valid employee
# ---------------------------------------------------------------------------


def test_valid_employee_with_defaults() -> None:
    employee = Employee(employee_id="EMP-0042", full_name="Ada Lovelace")
    assert employee.employee_id == "EMP-0042"
    assert employee.full_name == "Ada Lovelace"
    assert employee.is_active is True
    assert employee.created_at is not None
    assert employee.created_at.tzinfo is not None


def test_valid_employee_with_explicit_timestamp() -> None:
    ts = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
    employee = Employee(
        employee_id="007",
        full_name="Grace Hopper",
        is_active=False,
        created_at=ts,
    )
    assert employee.employee_id == "007"
    assert employee.is_active is False
    assert employee.created_at == ts


# ---------------------------------------------------------------------------
# 2 & 3. Required identity fields; empty/whitespace-only values rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "field", "message_part"),
    [
        ({"employee_id": "", "full_name": "Ada"}, "employee_id", "employee_id"),
        ({"employee_id": "   ", "full_name": "Ada"}, "employee_id", "employee_id"),
        ({"employee_id": "\t\n", "full_name": "Ada"}, "employee_id", "employee_id"),
        ({"employee_id": "E1", "full_name": ""}, "full_name", "full_name"),
        ({"employee_id": "E1", "full_name": "   "}, "full_name", "full_name"),
    ],
)
def test_empty_or_whitespace_ids_and_names_rejected(
    kwargs: dict[str, str], field: str, message_part: str
) -> None:
    with pytest.raises(EmployeeValidationError) as excinfo:
        Employee(**kwargs)
    assert message_part in str(excinfo.value)


def test_non_string_id_rejected() -> None:
    with pytest.raises(EmployeeValidationError):
        Employee(employee_id=42, full_name="Ada Lovelace")  # type: ignore[arg-type]


def test_non_string_name_rejected() -> None:
    with pytest.raises(EmployeeValidationError):
        Employee(employee_id="E1", full_name=None)  # type: ignore[arg-type]


def test_validation_error_is_value_error() -> None:
    assert issubclass(EmployeeValidationError, ValueError)


def test_error_message_is_actionable() -> None:
    with pytest.raises(EmployeeValidationError) as excinfo:
        Employee(employee_id="", full_name="Ada")
    message = str(excinfo.value)
    assert "employee_id" in message
    assert "whitespace" in message


# ---------------------------------------------------------------------------
# 4. is_active validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", ["true", 1, 0, None, [], ["x"]])
def test_is_active_must_be_bool(bad_value: object) -> None:
    with pytest.raises(EmployeeValidationError) as excinfo:
        Employee(employee_id="E1", full_name="Ada", is_active=bad_value)  # type: ignore[arg-type]
    assert "is_active" in str(excinfo.value)


@pytest.mark.parametrize("good_value", [True, False])
def test_is_active_bools_accepted(good_value: bool) -> None:
    assert Employee(employee_id="E1", full_name="Ada", is_active=good_value).is_active is good_value


# ---------------------------------------------------------------------------
# 5. Normalization behavior (documented and consistent)
# ---------------------------------------------------------------------------


def test_id_is_trimmed_only() -> None:
    assert Employee(employee_id="  E-100  ", full_name="Ada").employee_id == "E-100"


def test_name_collapses_internal_whitespace() -> None:
    employee = Employee(employee_id="E1", full_name="  Ada   Byron\tLovelace ")
    assert employee.full_name == "Ada Byron Lovelace"


def test_name_case_is_preserved() -> None:
    assert Employee(employee_id="E1", full_name="ada LOVELACE").full_name == "ada LOVELACE"


def test_id_case_is_preserved() -> None:
    assert Employee(employee_id="emp001a", full_name="Ada").employee_id == "emp001a"


# ---------------------------------------------------------------------------
# 6. Leading zeros and permitted punctuation are preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw_id", ["007", "EMP-0042", "EMP_004", "A1-B2_C3", "0000"])
def test_ids_with_zeros_and_punctuation_preserved(raw_id: str) -> None:
    employee = Employee(employee_id=raw_id, full_name="Ada")
    assert employee.employee_id == raw_id


# ---------------------------------------------------------------------------
# 7. Timestamp behavior
# ---------------------------------------------------------------------------


def test_default_created_at_is_utc_now() -> None:
    before = datetime.now(UTC)
    employee = Employee(employee_id="E1", full_name="Ada")
    after = datetime.now(UTC)
    assert before <= employee.created_at <= after  # type: ignore[operator]


def test_naive_created_at_rejected() -> None:
    naive = datetime(2026, 1, 1, 9, 30, 0)  # no tzinfo
    with pytest.raises(EmployeeValidationError) as excinfo:
        Employee(employee_id="E1", full_name="Ada", created_at=naive)
    assert "timezone-aware" in str(excinfo.value)


def test_non_utc_timezones_accepted() -> None:
    tokyo = timezone(timedelta(hours=9))
    ts = datetime(2026, 10, 9, 21, 0, 0, tzinfo=tokyo)
    employee = Employee(employee_id="E1", full_name="Ada", created_at=ts)
    assert employee.created_at == ts
    assert employee.created_at is not None and employee.created_at.utcoffset() == timedelta(hours=9)


# ---------------------------------------------------------------------------
# 8. Serialization
# ---------------------------------------------------------------------------


def test_to_dict_contains_all_fields() -> None:
    ts = datetime(2026, 10, 9, 8, 0, 0, tzinfo=UTC)
    employee = Employee(
        employee_id="EMP-0042", full_name="Ada Lovelace", is_active=False, created_at=ts
    )
    payload = employee.to_dict()
    assert payload == {
        "employee_id": "EMP-0042",
        "full_name": "Ada Lovelace",
        "is_active": False,
        "created_at": "2026-10-09T08:00:00+00:00",
    }


def test_serialization_reflects_normalization() -> None:
    employee = Employee(employee_id=" E1 ", full_name="  Ada   Lovelace ")
    payload = employee.to_dict()
    assert payload["employee_id"] == "E1"
    assert payload["full_name"] == "Ada Lovelace"


def test_serialized_created_at_parses_back_to_same_instant() -> None:
    ts = datetime(2026, 10, 9, 10, 30, 0, tzinfo=UTC)
    payload = Employee(employee_id="E1", full_name="Ada", created_at=ts).to_dict()
    restored = datetime.fromisoformat(payload["created_at"])
    assert restored == ts


# ---------------------------------------------------------------------------
# 9. Immutability & import hygiene
# ---------------------------------------------------------------------------


def test_employee_is_frozen() -> None:
    employee = Employee(employee_id="E1", full_name="Ada")
    with pytest.raises(dataclasses.FrozenInstanceError):
        employee.full_name = " Changed "  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        employee.is_active = False  # type: ignore[misc]


def test_employee_import_pulls_no_cv_stack() -> None:
    """Importing src.employee in a fresh interpreter must not import cv2 or
    any ML/serialization library — identity is independent of the CV stack."""
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "import src.employee;"
        "banned = {'cv2', 'mediapipe', 'insightface', 'onnxruntime', 'numpy'};"
        "loaded = banned & set(sys.modules);"
        "assert not loaded, f'unexpected imports: {loaded}';"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")
