"""P5.5.4 audit service tests (offline, temporary directories only).

Every test writes into ``tmp_path`` through a real ``AuditJournalWriter``; the
real ``data/audit`` directory is never touched. No test connects to PostgreSQL,
reads ``.env``, or handles passwords, tokens, camera frames, biometric data or
employee images.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterator

import pytest

import src.audit_writer as writer_module
from src.audit_schema import (
    METADATA_ALLOWED_KEYS,
    AuditAction,
    AuditEvent,
    AuditOutcome,
    AuditReasonCode,
    AuditSource,
    AuditTargetType,
    verify_chain,
)
from src.audit_service import (
    AUDIT_RESULT_UNCERTAIN_CODE,
    AUDIT_UNAVAILABLE_CODE,
    AuditActor,
    AuditContext,
    AuditPolicyError,
    AuditResultUncertainError,
    AuditService,
    AuditUnavailableError,
    MUTATION_ACTIONS,
    MutationIntent,
)
from src.audit_writer import (
    AuditJournalContendedError,
    AuditJournalPoisonedError,
    AuditJournalWriter,
)
from src.user import Role

BASE_TS = datetime(2026, 11, 16, 9, 30, 0, 123456, tzinfo=UTC)
REQUEST_ID = "0b1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"

# The approved P5.5.4 matrix, restated independently from design §16.7 so the
# implementation cannot silently drift from the approved table.
EXPECTED_MUTATION_RESULTS: dict[AuditAction, set[tuple[AuditOutcome, AuditReasonCode]]] = {
    AuditAction.EMPLOYEE_CREATE: {
        (AuditOutcome.SUCCESS, AuditReasonCode.OK),
        (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_EXISTS),
        (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_INVALID),
        (AuditOutcome.ERROR, AuditReasonCode.VALIDATION_FAILED),
        (AuditOutcome.ERROR, AuditReasonCode.DATABASE_UNAVAILABLE),
        (AuditOutcome.ERROR, AuditReasonCode.INTERNAL_ERROR),
        (AuditOutcome.UNCERTAIN, AuditReasonCode.DATABASE_OUTCOME_UNKNOWN),
    },
    AuditAction.EMPLOYEE_UPDATE: {
        (AuditOutcome.SUCCESS, AuditReasonCode.OK),
        (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_NOT_FOUND),
        (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_INVALID),
        (AuditOutcome.ERROR, AuditReasonCode.VALIDATION_FAILED),
        (AuditOutcome.ERROR, AuditReasonCode.DATABASE_UNAVAILABLE),
        (AuditOutcome.ERROR, AuditReasonCode.INTERNAL_ERROR),
        (AuditOutcome.UNCERTAIN, AuditReasonCode.DATABASE_OUTCOME_UNKNOWN),
    },
    AuditAction.EMPLOYEE_DEACTIVATE: {
        (AuditOutcome.SUCCESS, AuditReasonCode.OK),
        (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_NOT_FOUND),
        (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_INACTIVE),
        (AuditOutcome.ERROR, AuditReasonCode.VALIDATION_FAILED),
        (AuditOutcome.ERROR, AuditReasonCode.DATABASE_UNAVAILABLE),
        (AuditOutcome.ERROR, AuditReasonCode.INTERNAL_ERROR),
        (AuditOutcome.UNCERTAIN, AuditReasonCode.DATABASE_OUTCOME_UNKNOWN),
    },
    AuditAction.ATTENDANCE_CHECKOUT: {
        (AuditOutcome.SUCCESS, AuditReasonCode.OK),
        (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_NOT_FOUND),
        (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_INACTIVE),
        (AuditOutcome.ERROR, AuditReasonCode.NO_OPEN_SESSION),
        (AuditOutcome.ERROR, AuditReasonCode.CHECKOUT_COOLDOWN),
        (AuditOutcome.ERROR, AuditReasonCode.VALIDATION_FAILED),
        (AuditOutcome.ERROR, AuditReasonCode.DATABASE_UNAVAILABLE),
        (AuditOutcome.ERROR, AuditReasonCode.INTERNAL_ERROR),
        (AuditOutcome.UNCERTAIN, AuditReasonCode.DATABASE_OUTCOME_UNKNOWN),
        (AuditOutcome.UNCERTAIN, AuditReasonCode.PERSISTENCE_UNCONFIRMED),
    },
}

EXPECTED_LOGIN_RESULTS: set[tuple[AuditOutcome, AuditReasonCode]] = {
    (AuditOutcome.SUCCESS, AuditReasonCode.OK),
    (AuditOutcome.DENIED, AuditReasonCode.INVALID_CREDENTIALS),
    (AuditOutcome.DENIED, AuditReasonCode.ACCOUNT_INACTIVE),
    (AuditOutcome.DENIED, AuditReasonCode.RATE_LIMITED),
    (AuditOutcome.ERROR, AuditReasonCode.DATABASE_UNAVAILABLE),
    (AuditOutcome.ERROR, AuditReasonCode.INTERNAL_ERROR),
    (AuditOutcome.UNCERTAIN, AuditReasonCode.DATABASE_OUTCOME_UNKNOWN),
}

EXPECTED_LOGOUT_RESULTS: set[tuple[AuditOutcome, AuditReasonCode]] = {
    (AuditOutcome.SUCCESS, AuditReasonCode.OK),
    (AuditOutcome.ERROR, AuditReasonCode.SESSION_INVALID),
    (AuditOutcome.ERROR, AuditReasonCode.DATABASE_UNAVAILABLE),
    (AuditOutcome.ERROR, AuditReasonCode.INTERNAL_ERROR),
    (AuditOutcome.UNCERTAIN, AuditReasonCode.DATABASE_OUTCOME_UNKNOWN),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "audit.jsonl"


def clock() -> Callable[[], datetime]:
    step = 0

    def now() -> datetime:
        nonlocal step
        step += 1
        return BASE_TS + timedelta(microseconds=step)

    return now


def ids() -> "Iterator[str]":
    counter = count(1)
    return lambda: str(uuid.UUID(int=next(counter), version=4))


def principal(
    user_id: str = "user-1", role: Role = Role.ADMIN, username: str = "alice"
) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, role=role, username=username)


def build_service(
    tmp_path: Path, *, writer: object | None = None
) -> tuple[AuditService, AuditJournalWriter]:
    journal_writer = (
        writer
        if writer is not None
        else AuditJournalWriter(journal_path(tmp_path), clock=clock())
    )
    service = AuditService(
        journal_writer,  # type: ignore[arg-type]
        clock=clock(),
        audit_id_factory=ids(),
    )
    return service, journal_writer  # type: ignore[return-value]


def context(
    *,
    actor: bool = True,
    request_id: str | None = REQUEST_ID,
) -> AuditContext:
    return AuditContext(
        actor=(
            AuditActor.from_principal(principal()) if actor else None
        ),
        request_id=request_id,
    )


def events(tmp_path: Path) -> list[AuditEvent]:
    return [
        AuditEvent.from_dict(json.loads(line))
        for line in journal_path(tmp_path).read_text(encoding="utf-8").splitlines()
    ]


def intent(service: AuditService, action=AuditAction.EMPLOYEE_CREATE) -> MutationIntent:
    return service.begin_mutation(
        action=action,
        target_id="E1",
        context=context(),
    )


class RaisingWriter:
    """Minimal writer stand-in that fails exactly like the real one."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    def append(self, **kwargs: object) -> AuditEvent:
        self.calls += 1
        raise self._exc


# ---------------------------------------------------------------------------
# Context and actor binding
# ---------------------------------------------------------------------------


def test_context_projects_only_user_id_and_role() -> None:
    actor = AuditActor.from_principal(principal(username="alice"))
    assert actor.user_id == "user-1"
    assert actor.role is Role.ADMIN
    assert not hasattr(actor, "username")


def test_context_rejects_invalid_request_ids() -> None:
    for bad in ("not-a-uuid", "0b1d2e3f-4a5b-1c6d-8e7f-9a0b1c2d3e4f", None, 5):
        if bad is None:
            continue
        with pytest.raises(AuditPolicyError):
            AuditService.context(principal(), request_id=bad)  # type: ignore[arg-type]


def test_context_accepts_uuid4_request_id() -> None:
    ctx = AuditService.context(principal(), request_id=REQUEST_ID)
    assert ctx.actor is not None
    assert ctx.actor.user_id == "user-1"
    assert ctx.request_id == REQUEST_ID


def test_context_accepts_no_principal() -> None:
    ctx = AuditService.context(None, request_id=REQUEST_ID)
    assert ctx.actor is None


def test_rejects_invalid_principal() -> None:
    with pytest.raises(AuditPolicyError):
        AuditActor.from_principal(SimpleNamespace(user_id="", role=Role.ADMIN))
    with pytest.raises(AuditPolicyError):
        AuditActor.from_principal(SimpleNamespace(user_id="u1", role="Admin"))


# ---------------------------------------------------------------------------
# Intent records
# ---------------------------------------------------------------------------


def test_begin_mutation_writes_the_intent_record(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    mutation = intent(service)

    assert mutation.event.sequence == 1
    assert mutation.event.action is AuditAction.EMPLOYEE_CREATE
    assert mutation.event.outcome is AuditOutcome.UNCERTAIN
    assert mutation.event.reason_code is AuditReasonCode.AWAITING_RESULT
    assert mutation.event.correlation_id is None
    assert mutation.event.actor_user_id == "user-1"
    assert mutation.event.actor_role is Role.ADMIN
    assert mutation.event.target_type is AuditTargetType.EMPLOYEE
    assert mutation.event.target_id == "E1"
    assert mutation.event.request_id == REQUEST_ID
    assert mutation.audit_id == mutation.event.audit_id


def test_begin_mutation_rejects_standalone_actions(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    for action in (AuditAction.AUTH_LOGIN, AuditAction.AUTHZ_DENIED):
        with pytest.raises(AuditPolicyError):
            service.begin_mutation(
                action=action, target_id="E1", context=context()
            )
    assert journal_path(tmp_path).read_bytes() == b""


def test_begin_mutation_requires_actor_and_target(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    with pytest.raises(AuditPolicyError):
        service.begin_mutation(
            action=AuditAction.EMPLOYEE_CREATE,
            target_id="E1",
            context=context(actor=False),
        )
    for bad_target in ("", 5, "x" * 257):
        with pytest.raises(AuditPolicyError):
            service.begin_mutation(
                action=AuditAction.EMPLOYEE_CREATE,
                target_id=bad_target,  # type: ignore[arg-type]
                context=context(),
            )
    assert journal_path(tmp_path).read_bytes() == b""


def test_intent_and_result_preserve_request_and_correlation(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    mutation = intent(service)
    result = service.record_applied(mutation)

    assert result.correlation_id == mutation.audit_id
    assert result.request_id == mutation.event.request_id == REQUEST_ID
    assert result.action is AuditAction.EMPLOYEE_CREATE
    assert result.target_type is AuditTargetType.EMPLOYEE
    assert result.target_id == "E1"
    assert result.sequence == 2


def test_results_require_an_issued_intent(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    other_service, _ = build_service(tmp_path)
    mutation = intent(service)

    forged = MutationIntent(
        event=mutation.event,
        action=mutation.action,
        target_id=mutation.target_id,
        actor=mutation.actor,
        source=mutation.source,
    )
    with pytest.raises(AuditPolicyError):
        other_service.record_applied(mutation)
    with pytest.raises(AuditPolicyError):
        service.record_applied(forged)
    assert len(events(tmp_path)) == 1


def test_one_result_per_intent(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    mutation = intent(service)
    service.record_applied(mutation)
    before = journal_path(tmp_path).read_bytes()
    with pytest.raises(AuditPolicyError):
        service.record_not_applied(mutation, AuditReasonCode.EMPLOYEE_EXISTS)
    assert journal_path(tmp_path).read_bytes() == before


# ---------------------------------------------------------------------------
# Confirmed applied / not applied / unknown
# ---------------------------------------------------------------------------


def test_confirmed_applied_records_success_ok(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    mutation = intent(service)
    result = service.record_applied(mutation)
    assert result.outcome is AuditOutcome.SUCCESS
    assert result.reason_code is AuditReasonCode.OK
    verify_chain(events(tmp_path))


def test_confirmed_not_applied_records_error_with_reason(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    mutation = intent(service)
    result = service.record_not_applied(
        mutation, AuditReasonCode.EMPLOYEE_EXISTS
    )
    assert result is not None
    assert result.outcome is AuditOutcome.ERROR
    assert result.reason_code is AuditReasonCode.EMPLOYEE_EXISTS
    verify_chain(events(tmp_path))


def test_unknown_outcome_records_uncertain(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    mutation = intent(service, AuditAction.ATTENDANCE_CHECKOUT)
    result = service.record_unknown(mutation)
    assert result.outcome is AuditOutcome.UNCERTAIN
    assert result.reason_code is AuditReasonCode.DATABASE_OUTCOME_UNKNOWN

    second = intent(service, AuditAction.ATTENDANCE_CHECKOUT)
    persistence = service.record_unknown(
        second, reason_code=AuditReasonCode.PERSISTENCE_UNCONFIRMED
    )
    assert persistence.outcome is AuditOutcome.UNCERTAIN
    assert persistence.reason_code is AuditReasonCode.PERSISTENCE_UNCONFIRMED


def test_persistence_unconfirmed_is_checkout_only(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    for action in MUTATION_ACTIONS:
        if action is AuditAction.ATTENDANCE_CHECKOUT:
            continue
        mutation = intent(service, action)
        with pytest.raises(AuditPolicyError):
            service.record_unknown(
                mutation, reason_code=AuditReasonCode.PERSISTENCE_UNCONFIRMED
            )


def test_unknown_never_looks_applied_or_failed(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    mutation = intent(service, AuditAction.ATTENDANCE_CHECKOUT)
    with pytest.raises(AuditPolicyError):
        service.record_result(
            mutation,
            outcome=AuditOutcome.SUCCESS,
            reason_code=AuditReasonCode.DATABASE_OUTCOME_UNKNOWN,
        )
    with pytest.raises(AuditPolicyError):
        service.record_result(
            mutation,
            outcome=AuditOutcome.ERROR,
            reason_code=AuditReasonCode.DATABASE_OUTCOME_UNKNOWN,
        )


# ---------------------------------------------------------------------------
# Exhaustive matrix
# ---------------------------------------------------------------------------


def test_mutation_matrix_is_exhaustively_enforced(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    for action in MUTATION_ACTIONS:
        expected = EXPECTED_MUTATION_RESULTS[action]
        # One probe intent serves every rejected pair: a rejected result never
        # consumes the intent, so no result can slip through unnoticed.
        probe = intent(service, action)
        for outcome in AuditOutcome:
            for reason in AuditReasonCode:
                if (outcome, reason) in expected:
                    mutation = intent(service, action)
                    result = service.record_result(
                        mutation, outcome=outcome, reason_code=reason
                    )
                    assert result.outcome is outcome
                    assert result.reason_code is reason
                    assert result.correlation_id == mutation.audit_id
                else:
                    with pytest.raises(AuditPolicyError):
                        service.record_result(
                            probe, outcome=outcome, reason_code=reason
                        )
    verify_chain(events(tmp_path))


def test_login_matrix_is_exhaustively_enforced(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    for outcome in AuditOutcome:
        for reason in AuditReasonCode:
            allowed = (outcome, reason) in EXPECTED_LOGIN_RESULTS
            ctx = context(actor=outcome is AuditOutcome.SUCCESS, request_id=None)
            if allowed:
                event = service.record_login(
                    outcome=outcome, reason_code=reason, context=ctx
                )
                assert event.action is AuditAction.AUTH_LOGIN
            else:
                with pytest.raises(AuditPolicyError):
                    service.record_login(
                        outcome=outcome, reason_code=reason, context=ctx
                    )
    verify_chain(events(tmp_path))


def test_logout_matrix_is_exhaustively_enforced(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    for outcome in AuditOutcome:
        for reason in AuditReasonCode:
            allowed = (outcome, reason) in EXPECTED_LOGOUT_RESULTS
            ctx = context(
                actor=outcome is AuditOutcome.SUCCESS
                and reason is AuditReasonCode.OK,
                request_id=None,
            )
            if allowed:
                event = service.record_logout(
                    outcome=outcome, reason_code=reason, context=ctx
                )
                assert event.action is AuditAction.AUTH_LOGOUT
            else:
                with pytest.raises(AuditPolicyError):
                    service.record_logout(
                        outcome=outcome, reason_code=reason, context=ctx
                    )
    verify_chain(events(tmp_path))


def test_cross_cutting_reason_rules(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    # JOURNAL_CORRUPT is never emitted by this layer.
    mutation = intent(service)
    with pytest.raises(AuditPolicyError):
        service.record_result(
            mutation,
            outcome=AuditOutcome.ERROR,
            reason_code=AuditReasonCode.JOURNAL_CORRUPT,
        )
    # PERMISSION_DENIED belongs to authz.denied only.
    with pytest.raises(AuditPolicyError):
        service.record_result(
            mutation,
            outcome=AuditOutcome.DENIED,
            reason_code=AuditReasonCode.PERMISSION_DENIED,
        )
    # Employee codes are not login codes.
    with pytest.raises(AuditPolicyError):
        service.record_login(
            outcome=AuditOutcome.DENIED,
            reason_code=AuditReasonCode.EMPLOYEE_EXISTS,
            context=context(actor=False),
        )


# ---------------------------------------------------------------------------
# Actor / target rules
# ---------------------------------------------------------------------------


def test_login_success_uses_actor_account_target(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    event = service.record_login(
        outcome=AuditOutcome.SUCCESS,
        reason_code=AuditReasonCode.OK,
        context=context(),
    )
    assert event.target_type is AuditTargetType.ACCOUNT
    assert event.target_id == "user-1"
    assert event.actor_user_id == "user-1"


def test_login_failure_retains_no_username(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    event = service.record_login(
        outcome=AuditOutcome.DENIED,
        reason_code=AuditReasonCode.INVALID_CREDENTIALS,
        context=context(actor=False),
    )
    assert event.target_type is AuditTargetType.ACCOUNT
    assert event.target_id is None
    assert event.actor_user_id is None
    raw = journal_path(tmp_path).read_text(encoding="utf-8")
    assert "alice" not in raw


def test_login_success_requires_actor_and_failure_forbids_it(
    tmp_path: Path,
) -> None:
    service, _ = build_service(tmp_path)
    with pytest.raises(AuditPolicyError):
        service.record_login(
            outcome=AuditOutcome.SUCCESS,
            reason_code=AuditReasonCode.OK,
            context=context(actor=False),
        )
    with pytest.raises(AuditPolicyError):
        service.record_login(
            outcome=AuditOutcome.DENIED,
            reason_code=AuditReasonCode.INVALID_CREDENTIALS,
            context=context(),
        )


def test_logout_target_rules(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    authenticated = service.record_logout(
        outcome=AuditOutcome.SUCCESS,
        reason_code=AuditReasonCode.OK,
        context=context(),
    )
    assert authenticated.target_type is AuditTargetType.ACCOUNT
    assert authenticated.target_id == "user-1"

    pre_session = service.record_logout(
        outcome=AuditOutcome.ERROR,
        reason_code=AuditReasonCode.SESSION_INVALID,
        context=context(actor=False),
    )
    assert pre_session.target_type is None
    assert pre_session.target_id is None
    assert pre_session.actor_user_id is None

    with pytest.raises(AuditPolicyError):
        service.record_logout(
            outcome=AuditOutcome.SUCCESS,
            reason_code=AuditReasonCode.OK,
            context=context(actor=False),
        )
    with pytest.raises(AuditPolicyError):
        service.record_logout(
            outcome=AuditOutcome.ERROR,
            reason_code=AuditReasonCode.SESSION_INVALID,
            context=context(),
        )


def test_denied_requires_actor_and_builds_safe_metadata(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    event = service.record_denied(
        endpoint="/api/v1/employees/{employee_id}",
        method="POST",
        context=context(),
    )
    assert event.action is AuditAction.AUTHZ_DENIED
    assert event.outcome is AuditOutcome.DENIED
    assert event.reason_code is AuditReasonCode.PERMISSION_DENIED
    assert event.target_type is None and event.target_id is None
    assert dict(event.metadata) == {
        "endpoint": "/api/v1/employees/{employee_id}",
        "method": "POST",
        "http_status": 403,
    }
    with pytest.raises(AuditPolicyError):
        service.record_denied(
            endpoint="/api/v1/employees/{employee_id}",
            method="POST",
            context=context(actor=False),
        )
    for endpoint, method in (
        ("api/v1/employees", "POST"),
        ("/api/v1/employees?limit=1", "POST"),
        ("/api/v1/employees", "post"),
        ("/api/v1/employees", "TRACE"),
    ):
        with pytest.raises(AuditPolicyError):
            service.record_denied(
                endpoint=endpoint, method=method, context=context()
            )


# ---------------------------------------------------------------------------
# Metadata policy
# ---------------------------------------------------------------------------


def test_metadata_allowlist_is_enforced(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    for bad_key in (
        "password",
        "password_hash",
        "session_token",
        "cookie",
        "username",
        "face_embedding",
        "request_body",
    ):
        with pytest.raises(AuditPolicyError):
            service.begin_mutation(
                action=AuditAction.EMPLOYEE_CREATE,
                target_id="E1",
                context=context(),
                metadata={bad_key: "value"},  # type: ignore[dict-item]
            )
    with pytest.raises(AuditPolicyError):
        service.begin_mutation(
            action=AuditAction.EMPLOYEE_CREATE,
            target_id="E1",
            context=context(),
            metadata={"http_status": 1.5},  # type: ignore[dict-item]
        )
    with pytest.raises(AuditPolicyError):
        service.begin_mutation(
            action=AuditAction.EMPLOYEE_CREATE,
            target_id="E1",
            context=context(),
            metadata={"http_status": None},  # type: ignore[dict-item]
        )
    assert journal_path(tmp_path).read_bytes() == b""


def test_metadata_bounds_are_enforced_by_the_schema(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    # 257 characters exceeds the approved 256-character metadata string bound;
    # the schema rejects it and the service reports a policy violation.
    with pytest.raises(AuditPolicyError):
        service.begin_mutation(
            action=AuditAction.EMPLOYEE_CREATE,
            target_id="E1",
            context=context(),
            metadata={"endpoint": "/" + "a" * 256},
        )
    assert journal_path(tmp_path).read_bytes() == b""


def test_allowlisted_metadata_is_preserved(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    event = service.begin_mutation(
        action=AuditAction.EMPLOYEE_CREATE,
        target_id="E1",
        context=context(),
        metadata={
            "endpoint": "/api/v1/employees",
            "method": "POST",
            "http_status": 201,
            "employee_active_after": True,
        },
    )
    assert dict(event.event.metadata) == {
        "endpoint": "/api/v1/employees",
        "method": "POST",
        "http_status": 201,
        "employee_active_after": True,
    }
    assert dict(event.event.metadata).keys() <= METADATA_ALLOWED_KEYS


# ---------------------------------------------------------------------------
# Writer integration and failure semantics
# ---------------------------------------------------------------------------


def test_writer_unavailable_before_append_maps_to_audit_unavailable(
    tmp_path: Path,
) -> None:
    writer = RaisingWriter(AuditJournalPoisonedError("poisoned"))
    service, _ = build_service(tmp_path, writer=writer)
    with pytest.raises(AuditUnavailableError) as excinfo:
        intent(service)
    assert str(excinfo.value) == (
        "The operation was not attempted because it could not be audited."
    )
    assert excinfo.value.code == AUDIT_UNAVAILABLE_CODE
    assert "audit.jsonl" not in str(excinfo.value)
    assert "poisoned" not in str(excinfo.value)
    assert writer.calls == 1


def test_poisoned_real_writer_maps_to_audit_unavailable(tmp_path: Path) -> None:
    service, journal_writer = build_service(tmp_path)

    def broken_fsync(handle) -> None:
        raise OSError("injected fsync failure")

    def broken_lock(handle) -> None:
        raise AuditJournalContendedError("contended")

    original = writer_module._fsync_file
    writer_module._fsync_file = broken_fsync
    try:
        with pytest.raises(AuditUnavailableError):
            intent(service)
    finally:
        writer_module._fsync_file = original

    assert journal_writer.state.name == "POISONED"
    assert journal_writer.last_sequence == 0

    original_lock = writer_module._lock_handle
    writer_module._lock_handle = broken_lock
    try:
        with pytest.raises(AuditUnavailableError):
            intent(service)
    finally:
        writer_module._lock_handle = original_lock


def test_result_failure_after_applied_mutation_is_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, journal_writer = build_service(tmp_path)
    mutation = intent(service)

    def broken_fsync(handle) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(writer_module, "_fsync_file", broken_fsync)
    with pytest.raises(AuditResultUncertainError) as excinfo:
        service.record_applied(mutation)
    monkeypatch.undo()

    assert excinfo.value.code == AUDIT_RESULT_UNCERTAIN_CODE
    assert "may have been applied" in str(excinfo.value)
    assert "audit.jsonl" not in str(excinfo.value)
    assert journal_writer.state.name == "POISONED"
    # The intent remains the only durable record; no result was announced.
    assert journal_writer.last_sequence == 1

    # No automatic retry is possible once the ambiguous append happened.
    before = journal_path(tmp_path).read_bytes()
    with pytest.raises(AuditPolicyError):
        service.record_applied(mutation)
    assert journal_path(tmp_path).read_bytes() == before


def test_result_failure_after_unknown_outcome_is_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, journal_writer = build_service(tmp_path)
    mutation = intent(service, AuditAction.ATTENDANCE_CHECKOUT)

    def broken_write(handle, payload: bytes) -> int:
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(writer_module, "_write_line", broken_write)
    with pytest.raises(AuditResultUncertainError):
        service.record_unknown(mutation)
    monkeypatch.undo()
    assert journal_writer.state.name == "POISONED"
    assert journal_writer.last_sequence == 1


def test_confirmed_not_applied_result_failure_does_not_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, journal_writer = build_service(tmp_path)
    mutation = intent(service)

    def broken_fsync(handle) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(writer_module, "_fsync_file", broken_fsync)
    with caplog.at_level(logging.WARNING):
        result = service.record_not_applied(
            mutation, AuditReasonCode.EMPLOYEE_EXISTS
        )
    monkeypatch.undo()

    assert result is None
    assert journal_writer.state.name == "POISONED"
    assert any(
        "confirmed-not-applied" in record.getMessage()
        for record in caplog.records
    )
    assert "injected" not in caplog.text


def test_best_effort_never_raises_and_logs_sanitized(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    writer = RaisingWriter(AuditJournalPoisonedError("secret detail"))
    service, _ = build_service(tmp_path, writer=writer)
    with caplog.at_level(logging.WARNING):
        result = service.record_best_effort(
            lambda: service.record_login(
                outcome=AuditOutcome.DENIED,
                reason_code=AuditReasonCode.INVALID_CREDENTIALS,
                context=context(actor=False),
            ),
            action=AuditAction.AUTH_LOGIN,
            request_id=REQUEST_ID,
        )
    assert result is None
    assert "auth.login" in caplog.text
    assert REQUEST_ID in caplog.text
    assert "AuditJournalPoisonedError" in caplog.text
    assert "secret detail" not in caplog.text


def test_best_effort_returns_the_event_on_success(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    event = service.record_best_effort(
        lambda: service.record_denied(
            endpoint="/api/v1/employees/{employee_id}",
            method="DELETE",
            context=context(),
        ),
        action=AuditAction.AUTHZ_DENIED,
        request_id=REQUEST_ID,
    )
    assert event is not None
    assert event.action is AuditAction.AUTHZ_DENIED


def test_best_effort_rejects_mutation_actions(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    with pytest.raises(AuditPolicyError):
        service.record_best_effort(
            lambda: intent(service),
            action=AuditAction.EMPLOYEE_CREATE,
        )


def test_source_is_preserved_and_restricted(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    event = service.begin_mutation(
        action=AuditAction.EMPLOYEE_CREATE,
        target_id="E1",
        context=context(),
        source=AuditSource.CLI,
    )
    result = service.record_applied(event)
    assert event.event.source is AuditSource.CLI
    assert result.source is AuditSource.CLI
    with pytest.raises(AuditPolicyError):
        service.begin_mutation(
            action=AuditAction.EMPLOYEE_CREATE,
            target_id="E1",
            context=context(),
            source="api",  # type: ignore[arg-type]
        )


def test_no_forbidden_material_in_any_record(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    mutation = intent(service)
    service.record_applied(
        mutation,
        metadata={"endpoint": "/api/v1/employees", "method": "POST"},
    )
    service.record_login(
        outcome=AuditOutcome.DENIED,
        reason_code=AuditReasonCode.INVALID_CREDENTIALS,
        context=context(actor=False),
    )
    raw = journal_path(tmp_path).read_text(encoding="utf-8")
    for forbidden in (
        "alice",
        "password",
        "token",
        "cookie",
        "embedding",
        "camera",
        "postgres",
    ):
        assert forbidden not in raw


def test_journal_is_a_valid_chain(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    mutation = intent(service)
    service.record_applied(mutation)
    service.record_login(
        outcome=AuditOutcome.SUCCESS,
        reason_code=AuditReasonCode.OK,
        context=context(),
    )
    chain = events(tmp_path)
    verify_chain(chain)
    assert [event.sequence for event in chain] == [1, 2, 3]
