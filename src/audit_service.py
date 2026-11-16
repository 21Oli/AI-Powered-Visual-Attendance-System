"""Audit service layer for Phase 5 (P5.5.4).

The service is the **only** component that turns an application decision into an
audit record. It sits between business code and
:class:`~src.audit_writer.AuditJournalWriter`:

* it binds the actor (``user_id`` + ``role`` only) and the server-generated
  request ID to every event it emits,
* it enforces the approved action / outcome / reason-code matrix from
  ``docs/AUDIT_JOURNAL_DESIGN.md`` §16.7 *before* anything is written, so an
  impossible event can never be constructed,
* it owns the intent/result protocol of §10: a durable intent record must exist
  before a mutation is attempted, every result correlates with its intent's
  ``audit_id``, and the three outcomes (confirmed applied, confirmed not
  applied, unknown) stay distinct,
* it maps writer failures onto the two approved fail-closed semantics —
  :class:`AuditUnavailableError` (``AUDIT_UNAVAILABLE``: the operation must not
  be attempted) and :class:`AuditResultUncertainError`
  (``AUDIT_RESULT_UNCERTAIN``: the mutation may have applied and has no recorded
  result),
* it provides :meth:`AuditService.record_best_effort` for login, logout and
  permission-denial events, which logs a sanitized warning and never changes the
  caller's response.

What it does not do
-------------------

No HTTP route, request parsing, cookie handling, database access, business
mutation, manifest/rotation, CLI, restricted-mode wiring or frontend work. It
never writes the journal itself: every record goes through
``AuditJournalWriter.append`` so the schema, chain, durability and poisoning
guarantees of P5.5.2/P5.5.3 cannot be bypassed. HTTP status mapping is P5.5.5;
this module exposes only stable machine codes
(:data:`AUDIT_UNAVAILABLE_CODE`, :data:`AUDIT_RESULT_UNCERTAIN_CODE`).

Safety and privacy
------------------

Callers can supply only what the approved schema allows: action, outcome,
reason code, target identifier, request ID and allowlisted metadata. Actor
identity is projected from a validated principal (``user_id`` and ``role``
only); usernames, passwords, hashes, session tokens, cookies, DSNs, request
bodies, biometric values and free text have no parameter and no metadata key.
Login failures always use ``target_type="account"`` with ``target_id=null``
(owner decision 1), and the service refuses any attempt to attach a target id to
them. Error messages are fixed, sanitized sentences: no path, OS text, record
contents, identifiers or exception messages.

Deferred to later milestones: route integration and HTTP status mapping
(P5.5.5), the read-only verifier (P5.5.6), rotation/archive/manifest (P5.5.7).
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Callable, Final, Mapping

from src.audit_schema import (
    METADATA_ALLOWED_KEYS,
    AuditAction,
    AuditError,
    AuditEvent,
    AuditOutcome,
    AuditReasonCode,
    AuditSchemaError,
    AuditSource,
    AuditTargetType,
)
from src.audit_writer import AuditJournalError, AuditJournalWriter
from src.clock import Clock, utc_now
from src.user import Role

__all__ = [
    "AUDIT_RESULT_UNCERTAIN_CODE",
    "AUDIT_UNAVAILABLE_CODE",
    "AuditActor",
    "AuditContext",
    "AuditPolicyError",
    "AuditResultUncertainError",
    "AuditService",
    "AuditServiceError",
    "AuditUnavailableError",
    "MUTATION_ACTIONS",
    "MutationIntent",
]

#: Stable machine code for "the operation was not attempted because it could
#: not be audited" (design §10.1). HTTP mapping is P5.5.5.
AUDIT_UNAVAILABLE_CODE: Final[str] = "AUDIT_UNAVAILABLE"
#: Stable machine code for "the mutation may have applied but its result could
#: not be recorded" (design §10.1). HTTP mapping is P5.5.5.
AUDIT_RESULT_UNCERTAIN_CODE: Final[str] = "AUDIT_RESULT_UNCERTAIN"

#: The four mutation actions, in the order used by the design record.
MUTATION_ACTIONS: Final[tuple[AuditAction, ...]] = (
    AuditAction.EMPLOYEE_CREATE,
    AuditAction.EMPLOYEE_UPDATE,
    AuditAction.EMPLOYEE_DEACTIVATE,
    AuditAction.ATTENDANCE_CHECKOUT,
)

_STANDALONE_ACTIONS: Final[frozenset[AuditAction]] = frozenset(
    {AuditAction.AUTH_LOGIN, AuditAction.AUTH_LOGOUT, AuditAction.AUTHZ_DENIED}
)

# Fixed, sanitized messages (design §10.1). They never carry a path, an OS
# message, record contents, identifiers or exception text.
_MSG_UNAVAILABLE = (
    "The operation was not attempted because it could not be audited."
)
_MSG_RESULT_UNCERTAIN = (
    "The operation may have been applied, but its audit result could not be "
    "recorded. Do not retry blindly."
)
_MSG_POLICY = "the audit event is not permitted"

_logger = logging.getLogger(__name__)


class AuditServiceError(Exception):
    """Base class for service-level audit failures.

    Messages are fixed and sanitized and never describe the underlying writer
    state, paths or record contents.
    """


class AuditPolicyError(AuditServiceError):
    """The requested event violates the approved policy.

    Raised before anything is written: an unsupported action/outcome/reason
    combination, an invalid actor/target pairing, a disallowed metadata key, a
    result without a matching intent, or a second result for the same intent.
    """


class AuditUnavailableError(AuditServiceError):
    """The operation was not attempted because it could not be audited.

    Raised when the writer is unavailable, poisoned or refusing (for example
    ``AuditJournalPoisonedError``) while writing an intent or a standalone
    event. No business operation may run after this failure. The stable code is
    :data:`AUDIT_UNAVAILABLE_CODE`; HTTP mapping is P5.5.5.
    """

    code: Final[str] = AUDIT_UNAVAILABLE_CODE

    def __init__(self, writer_error: str | None = None) -> None:
        super().__init__(_MSG_UNAVAILABLE)
        #: Sanitized writer exception class name (for log classification only;
        #: never part of the message, the code or a client response).
        self.writer_error = writer_error


class AuditResultUncertainError(AuditServiceError):
    """The mutation may have applied but its audit result could not be recorded.

    Raised when writing a *result* record fails after a mutation that was
    confirmed applied or whose outcome is unknown. It never means the business
    operation failed, is never retried automatically and never produces a
    compensating write. The stable code is :data:`AUDIT_RESULT_UNCERTAIN_CODE`.
    """

    code: Final[str] = AUDIT_RESULT_UNCERTAIN_CODE

    def __init__(self, writer_error: str | None = None) -> None:
        super().__init__(_MSG_RESULT_UNCERTAIN)
        #: Sanitized writer exception class name (log classification only).
        self.writer_error = writer_error


@dataclass(frozen=True, slots=True)
class AuditActor:
    """Credential-free actor identity bound to an audit event.

    Only the two fields the schema accepts are retained. Built from a validated
    principal (``user_id`` and ``role``) — never from a username, request body,
    token or database row.
    """

    user_id: str
    role: Role

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise AuditPolicyError(_MSG_POLICY)
        if not isinstance(self.role, Role):
            raise AuditPolicyError(_MSG_POLICY)

    @classmethod
    def from_principal(cls, principal: object) -> "AuditActor":
        """Project a validated principal onto the two audit fields.

        Accepts any object exposing ``user_id`` and ``role`` (in production an
        ``AuthenticatedPrincipal``); the username is deliberately not read, so
        it cannot reach an audit record through this boundary.
        """
        user_id = getattr(principal, "user_id", None)
        role = getattr(principal, "role", None)
        if not isinstance(user_id, str) or not user_id.strip():
            raise AuditPolicyError(_MSG_POLICY)
        if not isinstance(role, Role):
            raise AuditPolicyError(_MSG_POLICY)
        return cls(user_id=user_id, role=role)


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Request-scoped audit context: the actor and the server request ID.

    ``request_id`` is generated by the application (a UUIDv4), never taken from
    a client header or body; ``actor`` is ``None`` only for pre-session
    ``auth.login``/``auth.logout`` events. The schema re-validates both.
    """

    actor: AuditActor | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class MutationIntent:
    """A durably written intent record plus the facts its result must match.

    Issued only by :meth:`AuditService.begin_mutation`; :meth:`AuditService`
    rejects any intent object it did not issue, so a result cannot be invented.
    """

    event: AuditEvent
    action: AuditAction
    target_id: str
    actor: AuditActor
    source: AuditSource

    @property
    def audit_id(self) -> str:
        return self.event.audit_id


# ---------------------------------------------------------------------------
# Approved policy (docs/AUDIT_JOURNAL_DESIGN.md §16.7)
# ---------------------------------------------------------------------------

_ResultKey = tuple[AuditOutcome, AuditReasonCode]

_MUTATION_RESULTS: Final[dict[AuditAction, frozenset[_ResultKey]]] = {
    AuditAction.EMPLOYEE_CREATE: frozenset(
        {
            (AuditOutcome.SUCCESS, AuditReasonCode.OK),
            (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_EXISTS),
            (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_INVALID),
            (AuditOutcome.ERROR, AuditReasonCode.VALIDATION_FAILED),
            (AuditOutcome.ERROR, AuditReasonCode.DATABASE_UNAVAILABLE),
            (AuditOutcome.ERROR, AuditReasonCode.INTERNAL_ERROR),
            (
                AuditOutcome.UNCERTAIN,
                AuditReasonCode.DATABASE_OUTCOME_UNKNOWN,
            ),
        }
    ),
    AuditAction.EMPLOYEE_UPDATE: frozenset(
        {
            (AuditOutcome.SUCCESS, AuditReasonCode.OK),
            (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_NOT_FOUND),
            (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_INVALID),
            (AuditOutcome.ERROR, AuditReasonCode.VALIDATION_FAILED),
            (AuditOutcome.ERROR, AuditReasonCode.DATABASE_UNAVAILABLE),
            (AuditOutcome.ERROR, AuditReasonCode.INTERNAL_ERROR),
            (
                AuditOutcome.UNCERTAIN,
                AuditReasonCode.DATABASE_OUTCOME_UNKNOWN,
            ),
        }
    ),
    AuditAction.EMPLOYEE_DEACTIVATE: frozenset(
        {
            (AuditOutcome.SUCCESS, AuditReasonCode.OK),
            (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_NOT_FOUND),
            (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_INACTIVE),
            (AuditOutcome.ERROR, AuditReasonCode.VALIDATION_FAILED),
            (AuditOutcome.ERROR, AuditReasonCode.DATABASE_UNAVAILABLE),
            (AuditOutcome.ERROR, AuditReasonCode.INTERNAL_ERROR),
            (
                AuditOutcome.UNCERTAIN,
                AuditReasonCode.DATABASE_OUTCOME_UNKNOWN,
            ),
        }
    ),
    AuditAction.ATTENDANCE_CHECKOUT: frozenset(
        {
            (AuditOutcome.SUCCESS, AuditReasonCode.OK),
            (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_NOT_FOUND),
            (AuditOutcome.ERROR, AuditReasonCode.EMPLOYEE_INACTIVE),
            (AuditOutcome.ERROR, AuditReasonCode.NO_OPEN_SESSION),
            (AuditOutcome.ERROR, AuditReasonCode.CHECKOUT_COOLDOWN),
            (AuditOutcome.ERROR, AuditReasonCode.VALIDATION_FAILED),
            (AuditOutcome.ERROR, AuditReasonCode.DATABASE_UNAVAILABLE),
            (AuditOutcome.ERROR, AuditReasonCode.INTERNAL_ERROR),
            (
                AuditOutcome.UNCERTAIN,
                AuditReasonCode.DATABASE_OUTCOME_UNKNOWN,
            ),
            (AuditOutcome.UNCERTAIN, AuditReasonCode.PERSISTENCE_UNCONFIRMED),
        }
    ),
}

_LOGIN_RESULTS: Final[frozenset[_ResultKey]] = frozenset(
    {
        (AuditOutcome.SUCCESS, AuditReasonCode.OK),
        (AuditOutcome.DENIED, AuditReasonCode.INVALID_CREDENTIALS),
        (AuditOutcome.DENIED, AuditReasonCode.ACCOUNT_INACTIVE),
        (AuditOutcome.DENIED, AuditReasonCode.RATE_LIMITED),
        (AuditOutcome.ERROR, AuditReasonCode.DATABASE_UNAVAILABLE),
        (AuditOutcome.ERROR, AuditReasonCode.INTERNAL_ERROR),
        (AuditOutcome.UNCERTAIN, AuditReasonCode.DATABASE_OUTCOME_UNKNOWN),
    }
)

_LOGOUT_RESULTS: Final[frozenset[_ResultKey]] = frozenset(
    {
        (AuditOutcome.SUCCESS, AuditReasonCode.OK),
        (AuditOutcome.ERROR, AuditReasonCode.SESSION_INVALID),
        (AuditOutcome.ERROR, AuditReasonCode.DATABASE_UNAVAILABLE),
        (AuditOutcome.ERROR, AuditReasonCode.INTERNAL_ERROR),
        (AuditOutcome.UNCERTAIN, AuditReasonCode.DATABASE_OUTCOME_UNKNOWN),
    }
)

_DENIED_RESULTS: Final[frozenset[_ResultKey]] = frozenset(
    {(AuditOutcome.DENIED, AuditReasonCode.PERMISSION_DENIED)}
)

#: Reason codes that may never appear in a record this service emits.
_NEVER_EMITTED: Final[frozenset[AuditReasonCode]] = frozenset(
    {AuditReasonCode.JOURNAL_CORRUPT}
)

_DENIED_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {"endpoint", "method", "http_status"}
)

_HTTP_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)


def _validate_metadata(
    metadata: Mapping[str, str | int | bool] | None,
    *,
    allowed: frozenset[str] | None = None,
) -> dict[str, str | int | bool]:
    """Return a plain, validated copy of caller metadata.

    Keys outside the approved allowlist (or outside ``allowed``) are refused
    here so the caller receives :class:`AuditPolicyError` rather than a raw
    schema error. Value types and size bounds are enforced again by
    ``audit_schema`` when the record is built, so nothing can bypass it.
    """
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise AuditPolicyError(_MSG_POLICY)
    keys = frozenset(metadata.keys())
    if allowed is not None:
        if not keys <= allowed:
            raise AuditPolicyError(_MSG_POLICY)
    elif not keys <= METADATA_ALLOWED_KEYS:
        raise AuditPolicyError(_MSG_POLICY)
    copied: dict[str, str | int | bool] = {}
    for key, value in metadata.items():
        if not isinstance(key, str):
            raise AuditPolicyError(_MSG_POLICY)
        if not isinstance(value, (str, int, bool)) or value is None:
            raise AuditPolicyError(_MSG_POLICY)
        copied[key] = value
    return copied


def _validate_uuid4(value: object) -> str:
    """Return ``value`` when it is a canonical UUIDv4 string."""
    if not isinstance(value, str):
        raise AuditPolicyError(_MSG_POLICY)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise AuditPolicyError(_MSG_POLICY) from None
    if parsed.version != 4 or str(parsed) != value:
        raise AuditPolicyError(_MSG_POLICY)
    return value


class AuditService:
    """Policy-checking, context-binding front end for the audit writer.

    One instance per process is expected (matching the single-writer writer);
    instances are safe to use from multiple threads because ``append`` calls are
    serialized by the writer's process lock and the issued-intent registry is
    guarded by a lock here.

    Parameters
    ----------
    writer:
        An initialized :class:`~src.audit_writer.AuditJournalWriter` (or a
        compatible object exposing ``append``). Never a path: the service cannot
        write the JSONL file directly.
    clock:
        Clock seam used for every event timestamp (default: ``utc_now``).
    audit_id_factory:
        UUIDv4 factory seam for deterministic tests (default: ``uuid.uuid4``).
    """

    def __init__(
        self,
        writer: AuditJournalWriter,
        *,
        clock: Clock = utc_now,
        audit_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._writer = writer
        self._clock: Clock = clock
        self._audit_id_factory = audit_id_factory
        self._intents: dict[str, MutationIntent] = {}
        self._results_recorded: set[str] = set()
        self._registry_lock = threading.Lock()

    # -- context ------------------------------------------------------------

    @staticmethod
    def context(
        principal: object | None,
        *,
        request_id: str | None = None,
    ) -> AuditContext:
        """Build a context from a validated principal and request ID.

        ``principal`` may be ``None`` for pre-session events. The request ID is
        validated as a UUIDv4 here; callers must generate it server-side and
        must never forward a client-supplied value.
        """
        actor = None if principal is None else AuditActor.from_principal(principal)
        if request_id is not None:
            _validate_uuid4(request_id)
        return AuditContext(actor=actor, request_id=request_id)

    # -- mutations ----------------------------------------------------------

    def begin_mutation(
        self,
        *,
        action: AuditAction,
        target_id: str,
        context: AuditContext,
        metadata: Mapping[str, str | int | bool] | None = None,
        source: AuditSource = AuditSource.API,
    ) -> MutationIntent:
        """Durably record the intent of a mutation before it is attempted.

        Writes ``outcome=uncertain``, ``reason_code=AWAITING_RESULT`` with the
        actor, action and target, and returns an immutable
        :class:`MutationIntent` whose ``audit_id`` the result must correlate to.
        If this raises :class:`AuditUnavailableError`, the mutation must not run.

        Raises :class:`AuditPolicyError` for an unknown/standalone action, a
        missing target or actor, invalid metadata or a non-API/CLI/system
        source.
        """
        if not isinstance(action, AuditAction) or action not in MUTATION_ACTIONS:
            raise AuditPolicyError(_MSG_POLICY)
        if context.actor is None:
            raise AuditPolicyError(_MSG_POLICY)
        target = self._require_target_id(target_id)
        clean_metadata = _validate_metadata(metadata)
        source = self._require_source(source)

        event = self._append_or_unavailable(
            action=action,
            outcome=AuditOutcome.UNCERTAIN,
            reason_code=AuditReasonCode.AWAITING_RESULT,
            source=source,
            actor=context.actor,
            target_type=AuditTargetType.EMPLOYEE,
            target_id=target,
            correlation_id=None,
            request_id=context.request_id,
            metadata=clean_metadata,
        )
        intent = MutationIntent(
            event=event,
            action=action,
            target_id=target,
            actor=context.actor,
            source=source,
        )
        with self._registry_lock:
            self._intents[event.audit_id] = intent
        return intent

    def record_result(
        self,
        intent: MutationIntent,
        *,
        outcome: AuditOutcome,
        reason_code: AuditReasonCode,
        metadata: Mapping[str, str | int | bool] | None = None,
    ) -> AuditEvent:
        """Record a mutation result correlated with its intent.

        The pair ``(outcome, reason_code)`` must be in the approved matrix for
        the intent's action; ``outcome=uncertain`` keeps an unknown result
        distinct from success and failure. A result can never be written
        without the intent that this service issued, and each intent accepts at
        most one result.
        """
        issued = self._require_intent(intent)
        pair = self._require_mutation_result(issued.action, outcome, reason_code)
        clean_metadata = _validate_metadata(metadata)

        with self._registry_lock:
            if issued.audit_id in self._results_recorded:
                raise AuditPolicyError(_MSG_POLICY)
            # Reserve the result slot so two threads cannot both write it; it
            # stays reserved if the append fails, because a failed append may
            # have reached disk and must never be retried.
            self._results_recorded.add(issued.audit_id)

        resolved_outcome, resolved_reason = pair
        try:
            event = self._append(
                action=issued.action,
                outcome=resolved_outcome,
                reason_code=resolved_reason,
                source=issued.source,
                actor=issued.actor,
                target_type=AuditTargetType.EMPLOYEE,
                target_id=issued.target_id,
                correlation_id=issued.audit_id,
                request_id=issued.event.request_id,
                metadata=clean_metadata,
            )
        except AuditJournalError as exc:
            # Keep the slot reserved: the failed append may have reached disk,
            # so a second result for this intent must never be attempted.
            raise AuditResultUncertainError(type(exc).__name__) from None
        except AuditSchemaError:
            # Nothing was written, so the intent is still usable.
            with self._registry_lock:
                self._results_recorded.discard(issued.audit_id)
            raise AuditPolicyError(_MSG_POLICY) from None
        return event

    def record_applied(
        self,
        intent: MutationIntent,
        *,
        metadata: Mapping[str, str | int | bool] | None = None,
    ) -> AuditEvent:
        """Record a **confirmed applied** mutation (``success`` / ``OK``)."""
        return self.record_result(
            intent,
            outcome=AuditOutcome.SUCCESS,
            reason_code=AuditReasonCode.OK,
            metadata=metadata,
        )

    def record_not_applied(
        self,
        intent: MutationIntent,
        reason_code: AuditReasonCode,
        *,
        metadata: Mapping[str, str | int | bool] | None = None,
    ) -> AuditEvent | None:
        """Record a **confirmed not applied** mutation (``error`` / reason).

        Evidence must prove no effect (validation, business refusal, unique
        constraint, connection never opened). A failed result write here is
        logged and returns ``None``: the business outcome is known and the
        caller's original error response stands (design §10 step 6). It never
        becomes :class:`AuditResultUncertainError`.
        """
        try:
            return self.record_result(
                intent,
                outcome=AuditOutcome.ERROR,
                reason_code=reason_code,
                metadata=metadata,
            )
        except AuditResultUncertainError:
            _logger.warning(
                "audit result write failed after a confirmed-not-applied "
                "mutation (action=%s): result not recorded",
                intent.action.value,
            )
            return None

    def record_unknown(
        self,
        intent: MutationIntent,
        *,
        reason_code: AuditReasonCode = AuditReasonCode.DATABASE_OUTCOME_UNKNOWN,
        metadata: Mapping[str, str | int | bool] | None = None,
    ) -> AuditEvent:
        """Record an **unknown** mutation outcome (``uncertain`` / reason).

        Unknown is neither success nor failure and must never be reported as
        either. Only ``DATABASE_OUTCOME_UNKNOWN`` (any mutation) and
        ``PERSISTENCE_UNCONFIRMED`` (attendance checkout) are accepted.
        """
        return self.record_result(
            intent,
            outcome=AuditOutcome.UNCERTAIN,
            reason_code=reason_code,
            metadata=metadata,
        )

    # -- standalone events --------------------------------------------------

    def record_login(
        self,
        *,
        outcome: AuditOutcome,
        reason_code: AuditReasonCode,
        context: AuditContext,
        metadata: Mapping[str, str | int | bool] | None = None,
        source: AuditSource = AuditSource.API,
    ) -> AuditEvent:
        """Record one ``auth.login`` outcome (best-effort at the route layer).

        A successful login requires an authenticated actor and records
        ``target_type="account"`` with that actor's user id. Every failure is
        pre-session: the actor must be absent and the target must be
        ``account`` with ``target_id=None`` (owner decision 1 — no username is
        ever retained).
        """
        pair = self._require_login_result(outcome, reason_code)
        source = self._require_source(source)
        clean_metadata = _validate_metadata(metadata)
        resolved_outcome, resolved_reason = pair

        if resolved_outcome is AuditOutcome.SUCCESS:
            if context.actor is None:
                raise AuditPolicyError(_MSG_POLICY)
            actor: AuditActor | None = context.actor
            target_id: str | None = context.actor.user_id
            target_type: AuditTargetType | None = AuditTargetType.ACCOUNT
        else:
            if context.actor is not None:
                raise AuditPolicyError(_MSG_POLICY)
            actor = None
            target_type = AuditTargetType.ACCOUNT
            target_id = None

        return self._append_or_unavailable(
            action=AuditAction.AUTH_LOGIN,
            outcome=resolved_outcome,
            reason_code=resolved_reason,
            source=source,
            actor=actor,
            target_type=target_type,
            target_id=target_id,
            correlation_id=None,
            request_id=context.request_id,
            metadata=clean_metadata,
        )

    def record_logout(
        self,
        *,
        outcome: AuditOutcome,
        reason_code: AuditReasonCode,
        context: AuditContext,
        metadata: Mapping[str, str | int | bool] | None = None,
        source: AuditSource = AuditSource.API,
    ) -> AuditEvent:
        """Record one ``auth.logout`` outcome (best-effort at the route layer).

        An authenticated logout records ``account`` + the actor's user id. A
        cookie that resolves to no valid session is pre-session
        (``SESSION_INVALID``, null actor, null target). A request with no
        session cookie produces **no** event and must not call this method.
        """
        pair = self._require_logout_result(outcome, reason_code)
        source = self._require_source(source)
        clean_metadata = _validate_metadata(metadata)
        resolved_outcome, resolved_reason = pair

        if resolved_outcome is AuditOutcome.SUCCESS and context.actor is None:
            raise AuditPolicyError(_MSG_POLICY)
        if resolved_reason is AuditReasonCode.SESSION_INVALID:
            # A cookie that resolves to no valid session is pre-session.
            if context.actor is not None:
                raise AuditPolicyError(_MSG_POLICY)
            actor = None
            target_type = None
            target_id = None
        elif context.actor is not None:
            actor = context.actor
            target_type = AuditTargetType.ACCOUNT
            target_id = context.actor.user_id
        else:
            actor = None
            target_type = None
            target_id = None

        return self._append_or_unavailable(
            action=AuditAction.AUTH_LOGOUT,
            outcome=resolved_outcome,
            reason_code=resolved_reason,
            source=source,
            actor=actor,
            target_type=target_type,
            target_id=target_id,
            correlation_id=None,
            request_id=context.request_id,
            metadata=clean_metadata,
        )

    def record_denied(
        self,
        *,
        endpoint: str,
        method: str,
        context: AuditContext,
        source: AuditSource = AuditSource.API,
    ) -> AuditEvent:
        """Record one ``authz.denied`` event (best-effort at the route layer).

        The actor and endpoint/method are required; the target stays null and
        the metadata is built here (never caller-supplied), so a query string,
        path parameter, header or body cannot reach the record. ``endpoint``
        must be a route template (``/api/v1/...`` with optional ``{name}``
        segments); a raw path containing a concrete identifier is not
        detectable here, so routes must pass the template only.
        """
        if context.actor is None:
            raise AuditPolicyError(_MSG_POLICY)
        if not isinstance(endpoint, str) or not endpoint.startswith("/"):
            raise AuditPolicyError(_MSG_POLICY)
        if "?" in endpoint or "#" in endpoint:
            raise AuditPolicyError(_MSG_POLICY)
        if method not in _HTTP_METHODS:
            raise AuditPolicyError(_MSG_POLICY)
        source = self._require_source(source)

        metadata = _validate_metadata(
            {
                "endpoint": endpoint,
                "method": method,
                "http_status": 403,
            },
            allowed=_DENIED_METADATA_KEYS,
        )
        return self._append_or_unavailable(
            action=AuditAction.AUTHZ_DENIED,
            outcome=AuditOutcome.DENIED,
            reason_code=AuditReasonCode.PERMISSION_DENIED,
            source=source,
            actor=context.actor,
            target_type=None,
            target_id=None,
            correlation_id=None,
            request_id=context.request_id,
            metadata=metadata,
        )

    # -- best-effort wrapper ------------------------------------------------

    def record_best_effort(
        self,
        emit: Callable[[], AuditEvent],
        *,
        action: AuditAction,
        request_id: str | None = None,
    ) -> AuditEvent | None:
        """Run a standalone-event emitter without changing the caller's flow.

        Only ``auth.login``, ``auth.logout`` and ``authz.denied`` are allowed.
        Any audit failure is swallowed after one sanitized ``WARNING`` log
        carrying the action, the request ID and the error class — never the
        event contents, actor identity, metadata values or exception text. The
        caller's response is unchanged (design §8).
        """
        if action not in _STANDALONE_ACTIONS:
            raise AuditPolicyError(_MSG_POLICY)
        try:
            return emit()
        except (AuditJournalError, AuditUnavailableError) as exc:
            _logger.warning(
                "audit best-effort write failed (action=%s, request_id=%s): %s",
                action.value,
                request_id if request_id is not None else "-",
                getattr(exc, "writer_error", None) or type(exc).__name__,
            )
            return None

    # -- internals ----------------------------------------------------------

    def _require_intent(self, intent: object) -> MutationIntent:
        if not isinstance(intent, MutationIntent):
            raise AuditPolicyError(_MSG_POLICY)
        with self._registry_lock:
            issued = self._intents.get(intent.event.audit_id)
        if issued is None or issued is not intent:
            raise AuditPolicyError(_MSG_POLICY)
        return issued

    def _append_or_unavailable(
        self,
        *,
        action: AuditAction,
        outcome: AuditOutcome,
        reason_code: AuditReasonCode,
        source: AuditSource,
        actor: AuditActor | None,
        target_type: AuditTargetType | None,
        target_id: str | None,
        correlation_id: str | None,
        request_id: str | None,
        metadata: Mapping[str, str | int | bool],
    ) -> AuditEvent:
        try:
            return self._append(
                action=action,
                outcome=outcome,
                reason_code=reason_code,
                source=source,
                actor=actor,
                target_type=target_type,
                target_id=target_id,
                correlation_id=correlation_id,
                request_id=request_id,
                metadata=metadata,
            )
        except AuditJournalError as exc:
            raise AuditUnavailableError(type(exc).__name__) from None
        except AuditSchemaError:
            raise AuditPolicyError(_MSG_POLICY) from None

    def _append(
        self,
        *,
        action: AuditAction,
        outcome: AuditOutcome,
        reason_code: AuditReasonCode,
        source: AuditSource,
        actor: AuditActor | None,
        target_type: AuditTargetType | None,
        target_id: str | None,
        correlation_id: str | None,
        request_id: str | None,
        metadata: Mapping[str, str | int | bool],
    ) -> AuditEvent:
        return self._writer.append(
            action=action,
            outcome=outcome,
            reason_code=reason_code,
            source=source,
            actor_user_id=None if actor is None else actor.user_id,
            actor_role=None if actor is None else actor.role,
            target_type=target_type,
            target_id=target_id,
            correlation_id=correlation_id,
            request_id=request_id,
            metadata=dict(metadata),
            timestamp=self._clock(),
            audit_id=self._audit_id_factory(),
        )

    @staticmethod
    def _require_target_id(target_id: object) -> str:
        if not isinstance(target_id, str) or not target_id:
            raise AuditPolicyError(_MSG_POLICY)
        if len(target_id) > 256:
            raise AuditPolicyError(_MSG_POLICY)
        return target_id

    @staticmethod
    def _require_source(source: object) -> AuditSource:
        if not isinstance(source, AuditSource):
            raise AuditPolicyError(_MSG_POLICY)
        return source

    @staticmethod
    def _require_mutation_result(
        action: AuditAction,
        outcome: object,
        reason_code: object,
    ) -> _ResultKey:
        if not isinstance(outcome, AuditOutcome) or not isinstance(
            reason_code, AuditReasonCode
        ):
            raise AuditPolicyError(_MSG_POLICY)
        if reason_code in _NEVER_EMITTED:
            raise AuditPolicyError(_MSG_POLICY)
        pair = (outcome, reason_code)
        if pair not in _MUTATION_RESULTS[action]:
            raise AuditPolicyError(_MSG_POLICY)
        return pair

    @staticmethod
    def _require_login_result(
        outcome: object, reason_code: object
    ) -> _ResultKey:
        if not isinstance(outcome, AuditOutcome) or not isinstance(
            reason_code, AuditReasonCode
        ):
            raise AuditPolicyError(_MSG_POLICY)
        if reason_code in _NEVER_EMITTED:
            raise AuditPolicyError(_MSG_POLICY)
        pair = (outcome, reason_code)
        if pair not in _LOGIN_RESULTS:
            raise AuditPolicyError(_MSG_POLICY)
        return pair

    @staticmethod
    def _require_logout_result(
        outcome: object, reason_code: object
    ) -> _ResultKey:
        if not isinstance(outcome, AuditOutcome) or not isinstance(
            reason_code, AuditReasonCode
        ):
            raise AuditPolicyError(_MSG_POLICY)
        if reason_code in _NEVER_EMITTED:
            raise AuditPolicyError(_MSG_POLICY)
        pair = (outcome, reason_code)
        if pair not in _LOGOUT_RESULTS:
            raise AuditPolicyError(_MSG_POLICY)
        return pair
