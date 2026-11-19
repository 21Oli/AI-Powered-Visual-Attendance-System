"""P5.5.2 audit event schema and hash-chain primitive tests (offline, pure).

Covers the approved design in ``docs/AUDIT_JOURNAL_DESIGN.md`` §2–§4:

* the closed vocabularies and every field/nullability rule,
* the metadata allowlist and size bounds,
* canonical JSON serialization and timestamp normalization,
* SHA-256 record hashing and constant-time digest comparison, and
* chain verification including genesis, segment boundaries and sequence
  continuity.

Nothing here touches the filesystem, PostgreSQL, a camera or employee data:
events are fixed fixtures with deterministic UUIDs and timestamps.
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

import src.audit_schema as audit_schema
from src.audit_schema import (
    AUDIT_SCHEMA_VERSION,
    GENESIS_PREVIOUS_HASH,
    METADATA_ALLOWED_KEYS,
    AuditAction,
    AuditCanonicalizationError,
    AuditChainError,
    AuditEvent,
    AuditHashFormatError,
    AuditHashMismatchError,
    AuditMetadataError,
    AuditOutcome,
    AuditPreviousHashMismatchError,
    AuditReasonCode,
    AuditSchemaError,
    AuditSegmentError,
    AuditSequenceError,
    AuditSource,
    AuditTargetType,
    AuditTimestampError,
    canonical_json_bytes,
    canonical_json_text,
    compute_record_hash,
    format_timestamp,
    parse_timestamp,
    validate_metadata,
    verify_chain,
    verify_record_hash,
)
from src.user import Role

# ---------------------------------------------------------------------------
# Deterministic fixtures
# ---------------------------------------------------------------------------

FIXED_TS = datetime(2026, 11, 16, 9, 30, 0, 123456, tzinfo=UTC)
INTENT_ID = "0a0e1f2a-3b4c-4d5e-8f6a-7b8c9d0e1f20"
AUDIT_ID = "6a0e1f2a-3b4c-4d5e-8f6a-7b8c9d0e1f2a"
REQUEST_ID = "0b1d2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"

#: Stable SHA-256 vector for the default fixture (P5.5.2 known-answer test).
DEFAULT_EVENT_HASH = (
    "32bc3ce6e7fb6746a2f89b6568ae6709292d10c9bab009b7e59d2bc6f8488a9a"
)


def make_event(**overrides: object) -> AuditEvent:
    """Build one valid, fully deterministic audit event."""
    fields: dict[str, object] = {
        "sequence": 1,
        "segment_id": "2026-11-001",
        "segment_sequence": 1,
        "action": AuditAction.EMPLOYEE_CREATE,
        "outcome": AuditOutcome.SUCCESS,
        "reason_code": AuditReasonCode.OK,
        "source": AuditSource.API,
        "previous_hash": GENESIS_PREVIOUS_HASH,
        "timestamp": FIXED_TS,
        "actor_user_id": "user-1",
        "actor_role": Role.ADMIN,
        "target_type": AuditTargetType.EMPLOYEE,
        "target_id": "E1",
        "correlation_id": INTENT_ID,
        "request_id": REQUEST_ID,
        "metadata": {
            "endpoint": "/api/v1/employees",
            "method": "POST",
            "http_status": 201,
        },
        "audit_id": AUDIT_ID,
    }
    fields.update(overrides)
    return AuditEvent.create(**fields)  # type: ignore[arg-type]


def build_chain(segment_ids: list[str]) -> list[AuditEvent]:
    """Build a valid chain, one record per entry, continuing its predecessor."""
    records: list[AuditEvent] = []
    previous = GENESIS_PREVIOUS_HASH
    segment_counts: dict[str, int] = {}
    for position, segment_id in enumerate(segment_ids, start=1):
        segment_counts[segment_id] = segment_counts.get(segment_id, 0) + 1
        record = make_event(
            sequence=position,
            segment_id=segment_id,
            segment_sequence=segment_counts[segment_id],
            previous_hash=previous,
            audit_id=str(uuid.uuid4()),
        )
        records.append(record)
        previous = record.record_hash
    return records


# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------


def test_action_vocabulary_is_closed_and_exact() -> None:
    assert {action.value for action in AuditAction} == {
        "employee.create",
        "employee.update",
        "employee.deactivate",
        "attendance.checkout",
        "auth.login",
        "auth.logout",
        "authz.denied",
        "user.create",
        "user.role_change",
        "user.deactivate",
        "first_admin.provision",
    }
    assert "audit.verify" not in {action.value for action in AuditAction}
    assert "audit.rotate" not in {action.value for action in AuditAction}


def test_outcome_vocabulary_is_closed_and_exact() -> None:
    assert {outcome.value for outcome in AuditOutcome} == {
        "success",
        "denied",
        "error",
        "uncertain",
    }


def test_reason_code_vocabulary_is_closed_and_exact() -> None:
    assert {code.value for code in AuditReasonCode} == {
        "OK",
        "AWAITING_RESULT",
        "EMPLOYEE_EXISTS",
        "EMPLOYEE_NOT_FOUND",
        "EMPLOYEE_INVALID",
        "EMPLOYEE_INACTIVE",
        "NO_OPEN_SESSION",
        "CHECKOUT_COOLDOWN",
        "PERSISTENCE_UNCONFIRMED",
        "JOURNAL_CORRUPT",
        "INVALID_CREDENTIALS",
        "ACCOUNT_INACTIVE",
        "RATE_LIMITED",
        "SESSION_ABSENT",
        "SESSION_INVALID",
        "PERMISSION_DENIED",
        "DATABASE_UNAVAILABLE",
        "DATABASE_OUTCOME_UNKNOWN",
        "INTERNAL_ERROR",
        "VALIDATION_FAILED",
    }
    assert "AUDIT_WRITE_FAILED" not in {
        code.value for code in AuditReasonCode
    }


def test_source_vocabulary_is_closed_and_exact() -> None:
    assert {source.value for source in AuditSource} == {"api", "cli", "system"}


def test_target_type_vocabulary_is_closed_and_exact() -> None:
    assert {target.value for target in AuditTargetType} == {
        "employee",
        "attendance_event",
        "account",
        "session",
        "endpoint",
    }


def test_metadata_allowlist_is_exact() -> None:
    assert METADATA_ALLOWED_KEYS == frozenset(
        {
            "endpoint",
            "method",
            "http_status",
            "employee_active_after",
            "journal_event_id",
            "prior_role",
            "new_role",
            "target_user_id",
        }
    )


# ---------------------------------------------------------------------------
# Schema: construction and identifier rules
# ---------------------------------------------------------------------------


def test_valid_event_construction() -> None:
    event = make_event()
    body = event.as_body()
    assert body["schema_version"] == AUDIT_SCHEMA_VERSION
    assert body["action"] == "employee.create"
    assert body["actor_role"] == "Admin"
    assert body["target_type"] == "employee"
    assert body["source"] == "api"
    assert event.record_hash != GENESIS_PREVIOUS_HASH
    assert len(event.record_hash) == 64


def test_event_is_immutable_and_metadata_is_frozen() -> None:
    event = make_event()
    with pytest.raises(Exception):
        event.sequence = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        event.metadata["endpoint"] = "/other"  # type: ignore[index]


def test_missing_required_field_is_rejected() -> None:
    payload = make_event().as_body()
    del payload["outcome"]
    with pytest.raises(AuditSchemaError, match="missing required fields"):
        AuditEvent.from_dict(payload)


def test_unknown_field_is_rejected() -> None:
    payload = make_event().as_body()
    payload["username"] = "ada"
    with pytest.raises(AuditSchemaError, match="unknown fields"):
        AuditEvent.from_dict(payload)


def test_non_mapping_record_is_rejected() -> None:
    with pytest.raises(AuditSchemaError, match="must be a mapping"):
        AuditEvent.from_dict(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_unsupported_schema_version_is_rejected() -> None:
    for version in (0, 2, "1", True, 1.0):
        with pytest.raises(AuditSchemaError):
            replace(make_event(), schema_version=version)


@pytest.mark.parametrize("value", [0, -1, True, "1", 1.0])
def test_invalid_global_sequence_is_rejected(value: object) -> None:
    with pytest.raises(AuditSchemaError):
        make_event(sequence=value)


@pytest.mark.parametrize("value", [0, -1, True, "1"])
def test_invalid_segment_sequence_is_rejected(value: object) -> None:
    with pytest.raises(AuditSchemaError):
        make_event(segment_sequence=value)


def test_segment_sequence_may_not_exceed_global_sequence() -> None:
    with pytest.raises(AuditSchemaError, match="segment_sequence"):
        make_event(sequence=1, segment_sequence=2)


@pytest.mark.parametrize(
    "segment_id",
    [
        "2026-13",
        "2026-1",
        "26-11",
        "2026/11",
        "",
        "2026-11-01",
        "2026-11-0000",
        "2026-11-1000",
        "2026-11-00a",
        "2026-11-",
    ],
)
def test_invalid_segment_id_is_rejected(segment_id: str) -> None:
    with pytest.raises(AuditSchemaError, match="segment_id"):
        make_event(segment_id=segment_id)


def test_uuid1_is_rejected() -> None:
    with pytest.raises(AuditSchemaError, match="UUIDv4"):
        make_event(audit_id=str(uuid.uuid1()))


def test_nil_uuid_is_rejected() -> None:
    with pytest.raises(AuditSchemaError, match="UUIDv4"):
        make_event(audit_id=str(uuid.UUID(int=0)))


@pytest.mark.parametrize(
    "audit_id",
    [
        "not-a-uuid",
        AUDIT_ID.upper(),
        AUDIT_ID.replace("-", ""),
        AUDIT_ID + "0",
        123,
    ],
)
def test_invalid_audit_ids_are_rejected(audit_id: object) -> None:
    with pytest.raises(AuditSchemaError):
        make_event(audit_id=audit_id)


def test_null_audit_id_is_rejected_by_the_parser() -> None:
    payload = make_event().as_body()
    payload["audit_id"] = None
    with pytest.raises(AuditSchemaError, match="audit_id"):
        AuditEvent.from_dict(payload)


def test_uuid4_with_variant_bits_is_accepted() -> None:
    value = str(uuid.UUID(AUDIT_ID))
    assert make_event(audit_id=value).audit_id == value


# ---------------------------------------------------------------------------
# Schema: timestamps
# ---------------------------------------------------------------------------


def test_timestamp_is_normalized_to_fixed_utc_microseconds() -> None:
    event = make_event(
        timestamp=datetime(
            2026, 11, 16, 10, 30, tzinfo=timezone(timedelta(hours=1))
        )
    )
    assert event.timestamp == "2026-11-16T09:30:00.000000Z"


def test_format_timestamp_rejects_naive_and_non_datetime() -> None:
    with pytest.raises(AuditTimestampError):
        format_timestamp(datetime(2026, 11, 16, 9, 30))
    with pytest.raises(AuditTimestampError):
        format_timestamp("2026-11-16T09:30:00.000000Z")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-16T09:30:00.123456+00:00",
        "2026-11-16T09:30:00Z",
        "2026-11-16T09:30:00.123Z",
        "2026-11-16T09:30:00.1234567Z",
        "2026-11-16 09:30:00.123456Z",
        "2026-11-16T09:30:00.123456Z ",
        "2026-02-30T09:30:00.123456Z",
        "2026-11-16T24:00:00.000000Z",
        "2026-11-16T09:30:00.123456",
    ],
)
def test_invalid_timestamp_text_is_rejected(text: str) -> None:
    with pytest.raises(AuditSchemaError):
        make_event(timestamp=text)


def test_timestamp_round_trips_through_parse() -> None:
    event = make_event()
    assert format_timestamp(parse_timestamp(event.timestamp)) == event.timestamp


# ---------------------------------------------------------------------------
# Schema: enums and nullable pairs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["audit.verify", "audit.rotate", "employee"])
def test_unknown_action_value_is_rejected(value: str) -> None:
    payload = make_event().as_body()
    payload["action"] = value
    with pytest.raises(AuditSchemaError, match="approved vocabulary"):
        AuditEvent.from_dict(payload)


def test_raw_string_action_must_be_converted() -> None:
    with pytest.raises(AuditSchemaError, match="AuditAction"):
        make_event(action="employee.create")


@pytest.mark.parametrize("value", ["ok", "SUCCESS", "unknown"])
def test_unknown_outcome_value_is_rejected(value: str) -> None:
    payload = make_event().as_body()
    payload["outcome"] = value
    with pytest.raises(AuditSchemaError, match="approved vocabulary"):
        AuditEvent.from_dict(payload)


@pytest.mark.parametrize(
    "value", ["AUDIT_WRITE_FAILED", "ok", "UNKNOWN"]
)
def test_unknown_reason_code_value_is_rejected(value: str) -> None:
    payload = make_event().as_body()
    payload["reason_code"] = value
    with pytest.raises(AuditSchemaError, match="approved vocabulary"):
        AuditEvent.from_dict(payload)


@pytest.mark.parametrize("value", ["test", "API", "worker", ""])
def test_unknown_source_value_is_rejected(value: str) -> None:
    payload = make_event().as_body()
    payload["source"] = value
    with pytest.raises(AuditSchemaError):
        AuditEvent.from_dict(payload)


def test_actor_pair_must_be_both_set_or_both_null() -> None:
    with pytest.raises(AuditSchemaError, match="both be null or both be set"):
        make_event(actor_role=None)
    with pytest.raises(AuditSchemaError, match="both be null or both be set"):
        make_event(actor_user_id=None)


def test_null_actor_is_only_valid_for_login_or_logout() -> None:
    with pytest.raises(AuditSchemaError, match="null actor"):
        make_event(actor_user_id=None, actor_role=None)


def test_unknown_actor_role_is_rejected() -> None:
    payload = make_event().as_body()
    payload["actor_role"] = "Superuser"
    with pytest.raises(AuditSchemaError, match="approved roles"):
        AuditEvent.from_dict(payload)


def test_target_pair_must_be_both_set_or_both_null() -> None:
    with pytest.raises(AuditSchemaError, match="target_id"):
        make_event(target_id=None)
    with pytest.raises(AuditSchemaError, match="target_type"):
        make_event(target_type=None)


def test_login_failure_uses_account_type_and_null_target_id() -> None:
    event = make_event(
        action=AuditAction.AUTH_LOGIN,
        outcome=AuditOutcome.DENIED,
        reason_code=AuditReasonCode.INVALID_CREDENTIALS,
        actor_user_id=None,
        actor_role=None,
        target_type=AuditTargetType.ACCOUNT,
        target_id=None,
        correlation_id=None,
    )
    assert event.target_id is None
    assert event.as_body()["target_type"] == "account"
    assert "username" not in event.to_json_line()


def test_null_target_id_is_only_valid_for_login_failures() -> None:
    with pytest.raises(AuditSchemaError, match="target_id may be null"):
        make_event(
            action=AuditAction.AUTH_LOGIN,
            outcome=AuditOutcome.SUCCESS,
            reason_code=AuditReasonCode.OK,
            target_type=AuditTargetType.EMPLOYEE,
            target_id=None,
            correlation_id=None,
        )
    with pytest.raises(AuditSchemaError, match="target_id may be null"):
        make_event(target_type=AuditTargetType.ACCOUNT, target_id=None)


def test_target_id_length_and_control_characters_are_bounded() -> None:
    with pytest.raises(AuditSchemaError, match="at most 256"):
        make_event(target_id="E" * 257)
    with pytest.raises(AuditSchemaError, match="control characters"):
        make_event(target_id="E1\nE2")
    assert make_event(target_id="E" * 256).target_id == "E" * 256


# ---------------------------------------------------------------------------
# Schema: correlation, request ids and outcome coherence
# ---------------------------------------------------------------------------


def test_invalid_correlation_id_is_rejected() -> None:
    with pytest.raises(AuditSchemaError, match="correlation_id"):
        make_event(correlation_id="intent-1")


def test_invalid_request_id_is_rejected() -> None:
    with pytest.raises(AuditSchemaError, match="request_id"):
        make_event(request_id="0b1d2c3d")


def test_standalone_events_must_not_correlate() -> None:
    with pytest.raises(AuditSchemaError, match="standalone events"):
        make_event(
            action=AuditAction.AUTH_LOGIN,
            correlation_id=INTENT_ID,
        )


def test_mutation_records_without_correlation_must_be_intents() -> None:
    intent = make_event(
        outcome=AuditOutcome.UNCERTAIN,
        reason_code=AuditReasonCode.AWAITING_RESULT,
        correlation_id=None,
    )
    assert intent.correlation_id is None

    with pytest.raises(AuditSchemaError, match="intent record"):
        make_event(correlation_id=None)


def test_success_requires_reason_code_ok() -> None:
    with pytest.raises(AuditSchemaError, match="success requires"):
        make_event(reason_code=AuditReasonCode.INTERNAL_ERROR)


def test_reason_code_ok_requires_success() -> None:
    with pytest.raises(AuditSchemaError, match="OK requires outcome success"):
        make_event(outcome=AuditOutcome.ERROR)


@pytest.mark.parametrize(
    ("outcome", "reason_code", "correlation_id", "action", "target"),
    [
        (
            AuditOutcome.SUCCESS,
            AuditReasonCode.OK,
            INTENT_ID,
            AuditAction.EMPLOYEE_UPDATE,
            AuditTargetType.EMPLOYEE,
        ),
        (
            AuditOutcome.DENIED,
            AuditReasonCode.PERMISSION_DENIED,
            INTENT_ID,
            AuditAction.EMPLOYEE_DEACTIVATE,
            AuditTargetType.EMPLOYEE,
        ),
        (
            AuditOutcome.ERROR,
            AuditReasonCode.EMPLOYEE_NOT_FOUND,
            INTENT_ID,
            AuditAction.ATTENDANCE_CHECKOUT,
            AuditTargetType.ATTENDANCE_EVENT,
        ),
        (
            AuditOutcome.UNCERTAIN,
            AuditReasonCode.DATABASE_OUTCOME_UNKNOWN,
            INTENT_ID,
            AuditAction.EMPLOYEE_CREATE,
            AuditTargetType.EMPLOYEE,
        ),
    ],
)
def test_valid_outcome_reason_and_target_combinations(
    outcome: AuditOutcome,
    reason_code: AuditReasonCode,
    correlation_id: str,
    action: AuditAction,
    target: AuditTargetType,
) -> None:
    event = make_event(
        action=action,
        outcome=outcome,
        reason_code=reason_code,
        correlation_id=correlation_id,
        target_type=target,
    )
    assert event.outcome is outcome
    assert event.reason_code is reason_code


# ---------------------------------------------------------------------------
# Schema: metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["username", "password", "password_hash", "token", "cookie", "body", "url"],
)
def test_non_allowlisted_metadata_keys_are_rejected(key: str) -> None:
    with pytest.raises(AuditMetadataError, match="allowlist"):
        make_event(metadata={key: "value"})


@pytest.mark.parametrize(
    "value",
    [
        {"nested": "value"},
        ["a", "b"],
        None,
        1.5,
        float("nan"),
        float("inf"),
        object(),
    ],
)
def test_unsupported_metadata_values_are_rejected(value: object) -> None:
    with pytest.raises(AuditMetadataError):
        make_event(metadata={"journal_event_id": value})


def test_metadata_key_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = frozenset({f"k{index}" for index in range(17)})
    monkeypatch.setattr(audit_schema, "METADATA_ALLOWED_KEYS", keys)
    with pytest.raises(AuditMetadataError, match="at most 16"):
        validate_metadata({key: "value" for key in keys})


def test_metadata_serialized_size_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = frozenset({f"k{index}" for index in range(10)})
    monkeypatch.setattr(audit_schema, "METADATA_ALLOWED_KEYS", keys)
    with pytest.raises(AuditMetadataError, match="at most 1024 bytes"):
        validate_metadata({key: "x" * 200 for key in keys})


def test_metadata_string_values_are_bounded() -> None:
    with pytest.raises(AuditMetadataError, match="at most 256"):
        make_event(metadata={"journal_event_id": "x" * 257})


def test_record_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_schema, "MAX_RECORD_BYTES", 300)
    with pytest.raises(AuditSchemaError, match="at most 300 bytes"):
        make_event()


def test_endpoint_must_be_a_route_template() -> None:
    for endpoint in (
        "https://example.test/api/v1/employees",
        "/api/v1/employees?page=2",
        "/api/v1/employees#fragment",
        "api/v1/employees",
        "/api/v1/employees E1",
    ):
        with pytest.raises(AuditMetadataError):
            make_event(metadata={"endpoint": endpoint})
    assert (
        make_event(
            metadata={"endpoint": "/api/v1/employees/{employee_id}/deactivate"}
        ).metadata["endpoint"]
        == "/api/v1/employees/{employee_id}/deactivate"
    )


def test_method_and_status_metadata_rules() -> None:
    with pytest.raises(AuditMetadataError, match="uppercase HTTP method"):
        make_event(metadata={"method": "post"})
    with pytest.raises(AuditMetadataError, match="between 100 and 599"):
        make_event(metadata={"http_status": 600})
    with pytest.raises(AuditMetadataError, match="must be an int"):
        make_event(metadata={"http_status": True})
    assert make_event(metadata={"http_status": 204}).metadata["http_status"] == 204


def test_boolean_and_role_metadata_rules() -> None:
    with pytest.raises(AuditMetadataError, match="must be a bool"):
        make_event(metadata={"employee_active_after": "true"})
    with pytest.raises(AuditMetadataError, match="approved role names"):
        make_event(metadata={"prior_role": "Superuser"})
    metadata = make_event(
        metadata={
            "employee_active_after": False,
            "prior_role": "Operator",
            "new_role": "HR Manager",
        }
    ).metadata
    assert metadata["employee_active_after"] is False
    assert metadata["prior_role"] == "Operator"


def test_empty_metadata_is_valid() -> None:
    assert dict(make_event(metadata={}).metadata) == {}


# ---------------------------------------------------------------------------
# Schema: hash fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "0" * 63,
        "0" * 65,
        "A" * 64,
        "g" * 64,
        "",
        123,
        None,
    ],
)
def test_invalid_previous_hash_format_is_rejected(value: object) -> None:
    with pytest.raises(AuditSchemaError):
        make_event(previous_hash=value)


def test_invalid_record_hash_format_is_rejected() -> None:
    payload = make_event().as_body()
    payload["record_hash"] = "NOT-A-HASH"
    with pytest.raises(AuditHashFormatError):
        AuditEvent.from_dict(payload)


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------


def test_canonical_output_is_independent_of_insertion_order() -> None:
    first = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
    second = {"c": {"y": 2, "z": 1}, "a": 2, "b": 1}
    assert canonical_json_text(first) == canonical_json_text(second)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert canonical_json_text(first) == '{"a":2,"b":1,"c":{"y":2,"z":1}}'


def test_canonical_output_preserves_unicode_utf8() -> None:
    text = canonical_json_text({"endpoint": "/api/v1/café"})
    assert "café" in text
    assert "\\u" not in text
    assert canonical_json_bytes({"endpoint": "/api/v1/café"}).endswith(
        "café\"}".encode("utf-8")
    )


def test_canonical_output_has_stable_separators_and_no_newline() -> None:
    text = canonical_json_text({"a": 1, "b": [1, 2]})
    assert text == '{"a":1,"b":[1,2]}'
    assert "\n" not in text


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_serialization_rejects_non_finite_floats(value: float) -> None:
    with pytest.raises(AuditCanonicalizationError):
        canonical_json_bytes({"value": value})


def test_canonical_serialization_rejects_unsupported_objects() -> None:
    with pytest.raises(AuditCanonicalizationError):
        canonical_json_bytes({"value": object()})
    with pytest.raises(AuditCanonicalizationError):
        canonical_json_bytes({"value": {1, 2}})


def test_canonical_serialization_rejects_non_string_keys() -> None:
    with pytest.raises(AuditCanonicalizationError, match="string mapping keys"):
        canonical_json_bytes({1: "value"})


def test_canonical_line_matches_body() -> None:
    event = make_event()
    line = event.to_json_line()
    assert json.loads(line) == event.as_body()
    assert line == canonical_json_text(event.as_body())
    assert "\n" not in line


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def test_known_answer_hash_vector() -> None:
    assert make_event().record_hash == DEFAULT_EVENT_HASH


def test_same_event_produces_same_hash() -> None:
    assert make_event().record_hash == make_event().record_hash


@pytest.mark.parametrize(
    "overrides",
    [
        {"sequence": 2, "segment_sequence": 2},
        {"timestamp": datetime(2026, 11, 16, 9, 30, 1, tzinfo=UTC)},
        {"target_id": "E2"},
        {"action": AuditAction.EMPLOYEE_UPDATE},
        {"actor_role": Role.AUDITOR},
        {"metadata": {"endpoint": "/api/v1/employees", "method": "PUT"}},
        {"previous_hash": "1" * 64},
        {"request_id": None},
    ],
)
def test_any_field_change_changes_the_hash(overrides: dict[str, object]) -> None:
    baseline = make_event()
    changed = make_event(**overrides)
    assert changed.record_hash != baseline.record_hash


def test_record_hash_is_excluded_from_its_own_hash_input() -> None:
    event = make_event()
    body = event.as_body()
    body["record_hash"] = "f" * 64
    assert compute_record_hash(body) == event.record_hash
    body.pop("record_hash")
    assert compute_record_hash(body) == event.record_hash


def test_hash_covers_the_previous_hash_link() -> None:
    first = make_event()
    linked = make_event(sequence=2, segment_sequence=2, previous_hash=first.record_hash)
    unlinked = make_event(sequence=2, segment_sequence=2, previous_hash="0" * 63 + "1")
    assert linked.record_hash != unlinked.record_hash


def test_verify_record_hash_accepts_a_matching_digest() -> None:
    event = make_event()
    assert verify_record_hash(event) == event.record_hash


def test_verify_record_hash_rejects_an_incorrect_digest() -> None:
    event = make_event()
    tampered = event.as_body()
    tampered["record_hash"] = "0" * 64
    with pytest.raises(AuditHashMismatchError):
        verify_record_hash(tampered)


def test_digest_comparison_uses_compare_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    real = audit_schema._digests_equal

    def spy(left: str, right: str) -> bool:
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr(audit_schema, "_digests_equal", spy)
    event = make_event()
    verify_record_hash(event)
    assert calls == [(event.record_hash, event.record_hash)]


def test_hash_mismatch_error_carries_position_and_sequence() -> None:
    event = make_event()
    body = event.as_body()
    body["record_hash"] = "0" * 64
    with pytest.raises(AuditHashMismatchError) as caught:
        verify_record_hash(body, position=7)
    assert caught.value.position == 7
    assert caught.value.sequence == 1
    assert event.record_hash not in str(caught.value)


# ---------------------------------------------------------------------------
# Chain verification
# ---------------------------------------------------------------------------


def test_empty_chain_is_valid_and_explicit() -> None:
    summary = verify_chain([])
    assert summary.record_count == 0
    assert summary.first_global_sequence is None
    assert summary.last_global_sequence is None
    assert summary.last_record_hash is None
    assert summary.segment_ids == ()


def test_valid_single_record_chain() -> None:
    record = make_event()
    summary = verify_chain([record])
    assert summary.record_count == 1
    assert summary.last_global_sequence == 1
    assert summary.last_record_hash == record.record_hash
    assert summary.segment_ids == ("2026-11-001",)


def test_valid_multi_record_chain() -> None:
    records = build_chain(["2026-11-001"] * 5)
    summary = verify_chain(records)
    assert summary.record_count == 5
    assert summary.first_global_sequence == 1
    assert summary.last_global_sequence == 5


def test_valid_chain_spanning_two_segments() -> None:
    records = build_chain(["2026-11-001", "2026-11-001", "2026-12-001", "2026-12-001"])
    summary = verify_chain(records)
    assert summary.record_count == 4
    assert summary.segment_ids == ("2026-11-001", "2026-12-001")
    assert records[2].previous_hash == records[1].record_hash
    assert records[2].segment_sequence == 1


def test_first_record_must_use_genesis() -> None:
    record = make_event(previous_hash="1" * 64)
    with pytest.raises(AuditPreviousHashMismatchError) as caught:
        verify_chain([record])
    assert caught.value.position == 0


def test_single_record_after_genesis_must_start_at_sequence_one() -> None:
    record = make_event(sequence=2, segment_sequence=2, previous_hash="0" * 64)
    with pytest.raises(AuditSequenceError):
        verify_chain([record])


def test_later_segment_must_not_reset_to_genesis() -> None:
    records = build_chain(["2026-11-001", "2026-11-001", "2026-12-001"])
    tampered = [
        records[0],
        records[1],
        replace(
            records[2], previous_hash=GENESIS_PREVIOUS_HASH
        ),
    ]
    with pytest.raises(AuditPreviousHashMismatchError) as caught:
        verify_chain(tampered)
    assert caught.value.position == 2


def test_later_segment_must_link_to_previous_segment_hash() -> None:
    records = build_chain(["2026-11-001", "2026-12-001"])
    tampered = [records[0], replace(
        records[1], previous_hash="a" * 64
    )]
    with pytest.raises(AuditPreviousHashMismatchError):
        verify_chain(tampered)


def test_global_sequence_must_be_contiguous_across_segments() -> None:
    records = build_chain(["2026-11-001", "2026-12-001"])
    tampered = [
        records[0],
        replace(
            records[1], sequence=3, segment_sequence=1
        ),
    ]
    with pytest.raises(AuditSequenceError) as caught:
        verify_chain(tampered)
    assert caught.value.position == 1


def test_segment_sequence_must_be_contiguous_within_a_segment() -> None:
    records = build_chain(["2026-11-001", "2026-11-001", "2026-11-001"])
    # 1, 2 then 2 again: still a valid record, but not a contiguous position.
    tampered = [
        records[0],
        records[1],
        replace(records[2], segment_sequence=2),
    ]
    with pytest.raises(AuditSequenceError, match="segment_sequence"):
        verify_chain(tampered)


def test_new_segment_must_start_at_segment_sequence_one() -> None:
    records = build_chain(["2026-11-001", "2026-12-001"])
    tampered = [
        records[0],
        replace(
            records[1], segment_sequence=2
        ),
    ]
    with pytest.raises(AuditSegmentError):
        verify_chain(tampered)


def test_segment_ids_must_not_repeat() -> None:
    records = build_chain(["2026-11-001", "2026-12-001", "2026-11-001"])
    with pytest.raises(AuditSegmentError, match="must not repeat"):
        verify_chain(records)


def test_repeated_segment_id_is_rejected_even_when_hashes_and_sequences_are_valid() -> None:
    records = build_chain(["2026-11-001", "2026-12-001", "2026-11-001"])
    # Every record's digest is valid and the sequence/link chain is continuous;
    # only the reused segment id makes the chain invalid.
    for record in records:
        verify_record_hash(record)
    assert [record.sequence for record in records] == [1, 2, 3]
    with pytest.raises(AuditSegmentError, match="must not repeat"):
        verify_chain(records)


def test_two_segments_allocated_in_the_same_month() -> None:
    records = build_chain(
        ["2026-11-001", "2026-11-001", "2026-11-002", "2026-11-002"]
    )
    summary = verify_chain(records)
    assert summary.record_count == 4
    assert summary.segment_ids == ("2026-11-001", "2026-11-002")
    assert records[2].segment_sequence == 1
    assert records[3].segment_sequence == 2


def test_same_month_segment_boundary_links_to_predecessor_hash() -> None:
    records = build_chain(["2026-11-001", "2026-11-002"])
    assert records[1].previous_hash == records[0].record_hash
    assert verify_chain(records).last_record_hash == records[1].record_hash


def test_segment_local_sequence_restarts_at_one_at_a_new_segment() -> None:
    records = build_chain(["2026-11-001", "2026-11-001", "2026-11-002"])
    assert [record.segment_sequence for record in records] == [1, 2, 1]
    assert [record.sequence for record in records] == [1, 2, 3]
    assert verify_chain(records).record_count == 3


def test_zero_ordinal_is_rejected() -> None:
    with pytest.raises(AuditSchemaError, match="ordinal"):
        make_event(segment_id="2026-11-000")


def test_four_digit_ordinal_is_rejected() -> None:
    with pytest.raises(AuditSchemaError, match="segment_id"):
        make_event(segment_id="2026-11-1000")


def test_maximum_ordinal_is_accepted() -> None:
    assert make_event(segment_id="2026-11-999").segment_id == "2026-11-999"
    assert make_event(segment_id="2026-01-001").segment_id == "2026-01-001"


def test_month_only_segment_id_is_rejected() -> None:
    with pytest.raises(AuditSchemaError, match="segment_id"):
        make_event(segment_id="2026-11")


def test_non_lexically_ordered_segment_labels_are_accepted() -> None:
    # ``2026-11-003`` sorts before ``2026-12-001``; order comes from the hash
    # links and sequences, never from the label.
    records = build_chain(["2026-12-001", "2026-11-003"])
    summary = verify_chain(records)
    assert summary.segment_ids == ("2026-12-001", "2026-11-003")
    assert records[1].previous_hash == records[0].record_hash
    assert records[1].segment_sequence == 1


def test_timestamp_month_may_differ_from_segment_allocation_month() -> None:
    event = make_event(
        segment_id="2026-11-002",
        timestamp=datetime(2026, 12, 1, 0, 0, 0, tzinfo=UTC),
    )
    assert event.timestamp.startswith("2026-12-01T")
    assert event.segment_id == "2026-11-002"
    assert verify_chain([event]).record_count == 1


def test_wrong_previous_hash_is_rejected() -> None:
    records = build_chain(["2026-11-001", "2026-11-001"])
    tampered = [
        records[0],
        replace(records[1], previous_hash="b" * 64),
    ]
    with pytest.raises(AuditPreviousHashMismatchError):
        verify_chain(tampered)


def test_tampered_event_is_detected() -> None:
    records = build_chain(["2026-11-001", "2026-11-001"])
    body = records[1].as_body()
    body["target_id"] = "E999"
    with pytest.raises(AuditHashMismatchError) as caught:
        verify_chain([records[0], body])
    assert caught.value.position == 1


def test_tampered_record_hash_is_detected() -> None:
    records = build_chain(["2026-11-001", "2026-11-001"])
    tampered = [
        records[0],
        replace(records[1], record_hash="0" * 64),
    ]
    with pytest.raises(AuditHashMismatchError) as caught:
        verify_chain(tampered)
    assert caught.value.position == 1


def test_missing_record_is_detected() -> None:
    records = build_chain(["2026-11-001"] * 3)
    with pytest.raises(AuditPreviousHashMismatchError):
        verify_chain([records[0], records[2]])


def test_duplicate_sequence_is_detected() -> None:
    records = build_chain(["2026-11-001"] * 3)
    duplicate = make_event(
        sequence=2,
        segment_sequence=2,
        previous_hash=records[1].record_hash,
        audit_id=str(uuid.uuid4()),
    )
    with pytest.raises(AuditSequenceError):
        verify_chain([records[0], records[1], duplicate])


def test_out_of_order_records_are_detected() -> None:
    records = build_chain(["2026-11-001", "2026-11-001"])
    with pytest.raises(AuditChainError):
        verify_chain([records[1], records[0]])


def test_malformed_record_is_reported_with_position() -> None:
    with pytest.raises(AuditSchemaError) as caught:
        verify_chain([{"schema_version": AUDIT_SCHEMA_VERSION}])
    assert caught.value.position == 0


def test_non_record_entry_is_rejected() -> None:
    with pytest.raises(AuditSchemaError, match="must be AuditEvent"):
        verify_chain([None])  # type: ignore[list-item]


def test_verification_does_not_mutate_input() -> None:
    records = build_chain(["2026-11-001", "2026-11-001"])
    bodies = [record.as_body() for record in records]
    snapshot = copy.deepcopy(bodies)
    verify_chain(bodies)
    assert bodies == snapshot
    for original, record in zip(bodies, records):
        assert record.to_json_line() == canonical_json_text(original)


def test_verification_accepts_parsed_mappings() -> None:
    records = build_chain(["2026-11-001", "2026-12-001"])
    parsed = [AuditEvent.from_dict(json.loads(record.to_json_line())) for record in records]
    assert verify_chain(parsed).record_count == 2


def test_from_dict_round_trip_preserves_record_and_digest() -> None:
    event = make_event()
    parsed = AuditEvent.from_dict(json.loads(event.to_json_line()))
    assert parsed == event
    assert parsed.record_hash == compute_record_hash(parsed)
    assert parsed.to_json_line() == event.to_json_line()
