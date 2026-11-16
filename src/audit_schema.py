"""Audit event schema and hash-chain primitives for Phase 5 (P5.5.2).

This module implements **only** the pure, in-memory half of the P5.5 audit
journal described by ``docs/AUDIT_JOURNAL_DESIGN.md``:

* the closed vocabularies (actions, outcomes, reason codes, sources, target
  types) and the single event record model with its nullability rules,
* the metadata allowlist together with its scalar-only rule and size bounds,
* one deterministic canonical-JSON serializer,
* SHA-256 record hashing over that canonical form, and
* hash-chain verification primitives (genesis, per-record digest,
  previous-hash linkage, global and segment-local sequence continuity).

It deliberately contains **no I/O**: no file creation, no append, no
``flush``/``os.fsync``, no locking, no writer state machine, no manifest, no
rotation, no HTTP handler, no CLI and no database access. Those arrive in
P5.5.3–P5.5.7. Everything here is a pure function or an immutable value, so it
can be reused unchanged by the writer and by the read-only verifier.

Record model and what the constructor guarantees
------------------------------------------------

:class:`AuditEvent` is a frozen dataclass whose ``__post_init__`` enforces
every per-field rule from the design record: supported schema version,
positive global and segment-local sequences, canonical lowercase UUIDv4
identifiers, the fixed ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` timestamp form, the
actor and target nullable-pair rules, the closed vocabularies, the metadata
allowlist and bounds, lowercase 64-hex hashes, and the 8 KiB record bound.

The constructor validates the *format* of ``record_hash`` but does not
recompute it. That is deliberate: a record read back from a journal whose
digest was tampered with must still be representable so that verification can
report :class:`AuditHashMismatchError` and the position of the defect. Digest
consistency is a verification property (:func:`verify_record_hash`,
:func:`verify_chain`), not a construction invariant. :meth:`AuditEvent.create`
always computes the digest for you and is the constructor new code should use.

Canonical serialization
-----------------------

``canonical_json_bytes(value)`` is the single serializer::

    json.dumps(value, ensure_ascii=False, sort_keys=True,
               separators=(",", ":"), allow_nan=False).encode("utf-8")

No trailing newline is added; the writer owns line framing. The input is the
complete record body **including** ``previous_hash`` and **excluding**
``record_hash``, so a record's digest covers its predecessor link. Non-string
mapping keys, non-finite floats, NaN/infinity and unsupported objects are
rejected with :class:`AuditCanonicalizationError` rather than silently
coerced. Timestamps are normalized before serialization
(:func:`format_timestamp`), so one logical record has exactly one digest.

Chain rules
-----------

The first-ever record uses ``previous_hash = "0" * 64``; every later record
links to its immediate predecessor's ``record_hash``, including the first
record of a later segment (rotation never resets the chain). ``sequence`` is
global and contiguous from 1; ``segment_sequence`` restarts at 1 in each new
segment and is contiguous inside it.

``segment_id`` is ``YYYY-MM-NNN``: the UTC month in which the segment was
**allocated** plus a ``001``–``999`` allocation ordinal. It is unique across
the whole history (the same id may not reappear — a sealed segment cannot be
reopened), and any number of segments may be allocated in one calendar month.
The identifier is a label, not an ordering claim: verification never compares
segment ids to infer order. Chronological order is established by the
predecessor hash links and the global sequence numbers; seal order is
established later by the manifest (P5.5.7). A record's timestamp is likewise
independent of its segment's allocation month — event time is an event fact,
not a segment label.

Verification never writes, repairs, normalizes or mutates its input, and its
errors carry only a position, a global sequence and a stable message; they
never echo record contents (no target ids, metadata values, usernames, tokens
or hashes beyond the fact of a mismatch).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Final, Iterable, Mapping

from src.user import Role

__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "GENESIS_PREVIOUS_HASH",
    "HASH_HEX_LENGTH",
    "MAX_METADATA_BYTES",
    "MAX_METADATA_KEYS",
    "MAX_RECORD_BYTES",
    "MAX_TARGET_ID_LENGTH",
    "METADATA_ALLOWED_KEYS",
    "AuditAction",
    "AuditCanonicalizationError",
    "AuditChainError",
    "AuditError",
    "AuditEvent",
    "AuditHashFormatError",
    "AuditHashMismatchError",
    "AuditMetadataError",
    "AuditOutcome",
    "AuditPreviousHashMismatchError",
    "AuditReasonCode",
    "AuditSchemaError",
    "AuditSegmentError",
    "AuditSequenceError",
    "AuditSource",
    "AuditTargetType",
    "AuditTimestampError",
    "ChainVerification",
    "canonical_json_bytes",
    "canonical_json_text",
    "compute_record_hash",
    "format_timestamp",
    "parse_timestamp",
    "validate_metadata",
    "verify_chain",
    "verify_record_hash",
]

# ---------------------------------------------------------------------------
# Approved constants
# ---------------------------------------------------------------------------

#: The only event schema version P5.5 writes or accepts.
AUDIT_SCHEMA_VERSION: Final[int] = 1

#: Genesis predecessor hash: 64 zeroes, never anything derived.
GENESIS_PREVIOUS_HASH: Final[str] = "0" * 64

#: Length of a lowercase hexadecimal SHA-256 digest.
HASH_HEX_LENGTH: Final[int] = 64

#: Maximum canonical size of one complete record, in bytes (8 KiB).
MAX_RECORD_BYTES: Final[int] = 8192

#: Maximum number of metadata keys.
MAX_METADATA_KEYS: Final[int] = 16

#: Maximum canonical size of the serialized metadata object, in bytes (1 KiB).
MAX_METADATA_BYTES: Final[int] = 1024

#: Maximum length of ``target_id`` in characters.
MAX_TARGET_ID_LENGTH: Final[int] = 256

#: The metadata keys a record may carry. Nothing else is ever accepted.
METADATA_ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "endpoint",
        "method",
        "http_status",
        "employee_active_after",
        "journal_event_id",
        "prior_role",
        "new_role",
    }
)

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"T(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"\.(?P<microsecond>\d{6})Z$"
)
# ASCII digits only: a segment id is an opaque allocation label, so
# non-ASCII numerals must never be accepted as a year or ordinal.
_SEGMENT_PATTERN = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])-([0-9]{3})$")
_ENDPOINT_PATTERN = re.compile(r"^/[A-Za-z0-9._~{}/\-]*$")
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")

_HTTP_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)
_ROLE_VALUES: Final[frozenset[str]] = frozenset(role.value for role in Role)

#: Maximum length of a free-form identifier carried in a single field.
_MAX_IDENTIFIER_LENGTH: Final[int] = 256
#: Maximum length of an allowlisted string metadata value.
_MAX_METADATA_STRING_LENGTH: Final[int] = 256

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------


class AuditAction(str, Enum):
    """The approved audit actions. There is no other action, ever."""

    EMPLOYEE_CREATE = "employee.create"
    EMPLOYEE_UPDATE = "employee.update"
    EMPLOYEE_DEACTIVATE = "employee.deactivate"
    ATTENDANCE_CHECKOUT = "attendance.checkout"
    AUTH_LOGIN = "auth.login"
    AUTH_LOGOUT = "auth.logout"
    AUTHZ_DENIED = "authz.denied"


class AuditOutcome(str, Enum):
    """The approved outcome vocabulary."""

    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"
    UNCERTAIN = "uncertain"


class AuditReasonCode(str, Enum):
    """The approved reason-code vocabulary.

    ``AUDIT_WRITE_FAILED`` is deliberately absent (it cannot be recorded in a
    journal that just failed); failed audit writes are log-only.
    """

    OK = "OK"
    AWAITING_RESULT = "AWAITING_RESULT"
    EMPLOYEE_EXISTS = "EMPLOYEE_EXISTS"
    EMPLOYEE_NOT_FOUND = "EMPLOYEE_NOT_FOUND"
    EMPLOYEE_INVALID = "EMPLOYEE_INVALID"
    EMPLOYEE_INACTIVE = "EMPLOYEE_INACTIVE"
    NO_OPEN_SESSION = "NO_OPEN_SESSION"
    CHECKOUT_COOLDOWN = "CHECKOUT_COOLDOWN"
    PERSISTENCE_UNCONFIRMED = "PERSISTENCE_UNCONFIRMED"
    JOURNAL_CORRUPT = "JOURNAL_CORRUPT"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE"
    RATE_LIMITED = "RATE_LIMITED"
    SESSION_ABSENT = "SESSION_ABSENT"
    SESSION_INVALID = "SESSION_INVALID"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    DATABASE_OUTCOME_UNKNOWN = "DATABASE_OUTCOME_UNKNOWN"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    VALIDATION_FAILED = "VALIDATION_FAILED"


class AuditSource(str, Enum):
    """Where the audited operation originated. No ``test`` source exists."""

    API = "api"
    CLI = "cli"
    SYSTEM = "system"


class AuditTargetType(str, Enum):
    """The approved target types."""

    EMPLOYEE = "employee"
    ATTENDANCE_EVENT = "attendance_event"
    ACCOUNT = "account"
    SESSION = "session"
    ENDPOINT = "endpoint"


#: Standalone events: they never carry a correlation to an intent record.
_STANDALONE_ACTIONS: Final[frozenset[AuditAction]] = frozenset(
    {AuditAction.AUTH_LOGIN, AuditAction.AUTH_LOGOUT, AuditAction.AUTHZ_DENIED}
)

# Actions with a null actor are limited to pre-session login/logout events.
_NULL_ACTOR_ACTIONS: Final[frozenset[AuditAction]] = frozenset(
    {AuditAction.AUTH_LOGIN, AuditAction.AUTH_LOGOUT}
)

#: Reason codes that describe a deliberately not-applied operation.
_UNCERTAIN_REASON_CODES: Final[frozenset[AuditReasonCode]] = frozenset(
    {
        AuditReasonCode.AWAITING_RESULT,
        AuditReasonCode.DATABASE_OUTCOME_UNKNOWN,
        AuditReasonCode.PERSISTENCE_UNCONFIRMED,
    }
)

#: Every event field, in documentation order. Unknown or missing is invalid.
_EVENT_FIELDS: Final[tuple[str, ...]] = (
    "schema_version",
    "sequence",
    "segment_id",
    "segment_sequence",
    "audit_id",
    "timestamp",
    "actor_user_id",
    "actor_role",
    "action",
    "target_type",
    "target_id",
    "outcome",
    "reason_code",
    "correlation_id",
    "request_id",
    "source",
    "metadata",
    "previous_hash",
    "record_hash",
)

_EVENT_FIELD_SET: Final[frozenset[str]] = frozenset(_EVENT_FIELDS)

# ---------------------------------------------------------------------------
# Typed errors (safe for logs; never rendered to a client)
# ---------------------------------------------------------------------------


class AuditError(ValueError):
    """Base class for every audit schema/hash/chain failure.

    Messages name the failing field, rule, position or sequence only. They
    never include record contents, metadata values, identifiers or secrets,
    and ``repr`` adds nothing beyond the attributes below.
    """

    def __init__(
        self,
        message: str,
        *,
        position: int | None = None,
        sequence: int | None = None,
    ) -> None:
        super().__init__(message)
        self.position = position
        self.sequence = sequence


class AuditSchemaError(AuditError):
    """An event is missing a field, has an unknown one, or violates a rule."""


class AuditTimestampError(AuditSchemaError):
    """A timestamp is not in the approved fixed UTC microsecond form."""


class AuditMetadataError(AuditSchemaError):
    """Metadata violates the allowlist, scalar rule or size bounds."""


class AuditHashFormatError(AuditSchemaError):
    """A hash field is not a lowercase 64-character hexadecimal SHA-256."""


class AuditCanonicalizationError(AuditError):
    """A value cannot be serialized canonically (unsupported or non-finite)."""


class AuditHashMismatchError(AuditError):
    """A record's stored ``record_hash`` does not match its computed hash."""


class AuditChainError(AuditError):
    """Base class for chain-level defects (link, sequence or segment)."""


class AuditPreviousHashMismatchError(AuditChainError):
    """A record does not link to its expected predecessor hash."""


class AuditSequenceError(AuditChainError):
    """Global or segment-local sequence numbers are not contiguous."""


class AuditSegmentError(AuditChainError):
    """Segment identity, ordering or boundary rules are violated."""


# ---------------------------------------------------------------------------
# Field validators
# ---------------------------------------------------------------------------


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuditSchemaError(
            f"{name} must be an int, got {type(value).__name__}"
        )
    return value


def _require_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise AuditSchemaError(
            f"{name} must be a str, got {type(value).__name__}"
        )
    return value


def _require_identifier(
    value: object, name: str, *, max_length: int = _MAX_IDENTIFIER_LENGTH
) -> str:
    text = _require_str(value, name)
    if not text:
        raise AuditSchemaError(f"{name} must be non-empty")
    if len(text) > max_length:
        raise AuditSchemaError(
            f"{name} must be at most {max_length} characters"
        )
    if _CONTROL_PATTERN.search(text):
        raise AuditSchemaError(f"{name} must not contain control characters")
    return text


def _require_uuid4(value: object, name: str) -> str:
    """Return ``value`` if it is a canonical lowercase UUIDv4 string."""
    text = _require_str(value, name)
    parsed: uuid.UUID | None
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError, TypeError):
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != text:
        raise AuditSchemaError(
            f"{name} must be a canonical lowercase UUIDv4 string"
        )
    return text


def _require_hash(value: object, name: str) -> str:
    text = _require_str(value, name)
    if _HASH_PATTERN.fullmatch(text) is None:
        raise AuditHashFormatError(
            f"{name} must be a lowercase 64-character hexadecimal SHA-256 "
            "digest"
        )
    return text


def _require_member(value: object, enum_type: type[Enum], name: str) -> Any:
    if not isinstance(value, enum_type):
        hint = ""
        if isinstance(value, str):
            hint = f"; use {enum_type.__name__}(value) to convert a string"
        raise AuditSchemaError(
            f"{name} must be a {enum_type.__name__} member, got "
            f"{type(value).__name__}{hint}"
        )
    return value


def format_timestamp(moment: datetime) -> str:
    """Render ``moment`` as the approved fixed UTC microsecond string.

    A naive datetime is refused (an ambiguous local time must never enter the
    audit trail); an aware non-UTC value is converted to UTC first. The result
    always ends in ``Z`` and always carries exactly six fractional digits, so
    one instant has exactly one representation and one digest.
    """
    if not isinstance(moment, datetime):
        raise AuditTimestampError(
            f"timestamp must be a datetime, got {type(moment).__name__}"
        )
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise AuditTimestampError("timestamp must be timezone-aware (UTC)")
    utc = moment.astimezone(UTC)
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}"
        f"T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}"
        f".{utc.microsecond:06d}Z"
    )


def parse_timestamp(text: object) -> datetime:
    """Parse the approved fixed timestamp form into an aware UTC datetime.

    ``+00:00`` offsets, missing microseconds, other precision, trailing data
    and impossible calendar values are all refused.
    """
    value = _require_str(text, "timestamp")
    match = _TIMESTAMP_PATTERN.fullmatch(value)
    if match is None:
        raise AuditTimestampError(
            "timestamp must use YYYY-MM-DDTHH:MM:SS.ffffffZ (UTC)"
        )
    try:
        return datetime(
            int(match["year"]),
            int(match["month"]),
            int(match["day"]),
            int(match["hour"]),
            int(match["minute"]),
            int(match["second"]),
            int(match["microsecond"]),
            tzinfo=UTC,
        )
    except ValueError:
        raise AuditTimestampError(
            "timestamp is not a valid UTC calendar instant"
        ) from None


def _metadata_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise AuditMetadataError(
            f"{name} must be a str, got {type(value).__name__}"
        )
    return value


def _metadata_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuditMetadataError(
            f"{name} must be an int, got {type(value).__name__}"
        )
    return value


def _validate_endpoint(value: object) -> str:
    text = _metadata_str(value, "metadata[endpoint]")
    if len(text) > _MAX_METADATA_STRING_LENGTH:
        raise AuditMetadataError(
            "metadata[endpoint] must be a route template of at most "
            f"{_MAX_METADATA_STRING_LENGTH} characters"
        )
    if _ENDPOINT_PATTERN.fullmatch(text) is None:
        raise AuditMetadataError(
            "metadata[endpoint] must be a route template beginning with '/' "
            "and must not contain a query string, fragment or scheme"
        )
    return text


def _validate_method(value: object) -> str:
    text = _metadata_str(value, "metadata[method]")
    if text not in _HTTP_METHODS:
        raise AuditMetadataError(
            "metadata[method] must be an uppercase HTTP method"
        )
    return text


def _validate_http_status(value: object) -> int:
    status = _metadata_int(value, "metadata[http_status]")
    if not 100 <= status <= 599:
        raise AuditMetadataError(
            "metadata[http_status] must be between 100 and 599"
        )
    return status


def _validate_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise AuditMetadataError(
            f"{name} must be a bool, got {type(value).__name__}"
        )
    return value


def _validate_short_identifier(value: object, name: str) -> str:
    text = _metadata_str(value, name)
    if not text:
        raise AuditMetadataError(f"{name} must be non-empty")
    if len(text) > _MAX_METADATA_STRING_LENGTH:
        raise AuditMetadataError(
            f"{name} must be at most {_MAX_METADATA_STRING_LENGTH} characters"
        )
    if _CONTROL_PATTERN.search(text):
        raise AuditMetadataError(
            f"{name} must not contain control characters"
        )
    return text


def _validate_role_value(value: object, name: str) -> str:
    text = _metadata_str(value, name)
    if text not in _ROLE_VALUES:
        raise AuditMetadataError(
            f"{name} must be one of the approved role names"
        )
    return text


def _validate_generic_scalar(key: str, value: object) -> str | int | bool:
    """Fallback rule for allowed keys without a specific validator.

    Only reachable if the allowlist is extended; keeps the scalar-only rule
    (no floats, no nesting, no arrays) regardless of key.
    """
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        text = _metadata_str(value, f"metadata[{key}]")
        if not text:
            raise AuditMetadataError(f"metadata[{key}] must be non-empty")
        if len(text) > _MAX_METADATA_STRING_LENGTH:
            raise AuditMetadataError(
                f"metadata[{key}] must be at most "
                f"{_MAX_METADATA_STRING_LENGTH} characters"
            )
        if _CONTROL_PATTERN.search(text):
            raise AuditMetadataError(
                f"metadata[{key}] must not contain control characters"
            )
        return text
    if isinstance(value, float):
        raise AuditMetadataError(
            f"metadata[{key}] must not be a floating-point value"
        )
    raise AuditMetadataError(
        f"metadata[{key}] must be a str, int or bool, got "
        f"{type(value).__name__}"
    )


_METADATA_RULES: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "endpoint": lambda value: _validate_endpoint(value),
        "method": lambda value: _validate_method(value),
        "http_status": lambda value: _validate_http_status(value),
        "employee_active_after": lambda value: _validate_bool(
            value, "metadata[employee_active_after]"
        ),
        "journal_event_id": lambda value: _validate_short_identifier(
            value, "metadata[journal_event_id]"
        ),
        "prior_role": lambda value: _validate_role_value(
            value, "metadata[prior_role]"
        ),
        "new_role": lambda value: _validate_role_value(
            value, "metadata[new_role]"
        ),
    }
)


def validate_metadata(value: object) -> Mapping[str, str | int | bool]:
    """Validate metadata against the allowlist and bounds; return a frozen copy.

    Rules: the value must be a mapping; at most :data:`MAX_METADATA_KEYS`
    keys; every key must be a string from :data:`METADATA_ALLOWED_KEYS`; every
    value must be a ``str``, ``int`` or ``bool`` (never a float, ``None``, a
    nested mapping or an array), bounded and control-character free; the
    canonical serialized object must be at most :data:`MAX_METADATA_BYTES`.
    """
    if not isinstance(value, Mapping):
        raise AuditMetadataError(
            f"metadata must be a mapping, got {type(value).__name__}"
        )
    if len(value) > MAX_METADATA_KEYS:
        raise AuditMetadataError(
            f"metadata must carry at most {MAX_METADATA_KEYS} keys"
        )
    frozen: dict[str, str | int | bool] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise AuditMetadataError("metadata keys must be str")
        if key not in METADATA_ALLOWED_KEYS:
            raise AuditMetadataError(
                f"metadata key {key[:32]!r} is not in the allowlist"
            )
        rule = _METADATA_RULES.get(key)
        frozen[key] = (
            rule(item) if rule is not None else _validate_generic_scalar(key, item)
        )
    try:
        encoded = canonical_json_bytes(frozen)
    except AuditCanonicalizationError:
        raise AuditMetadataError(
            "metadata contains a value that cannot be serialized"
        ) from None
    if len(encoded) > MAX_METADATA_BYTES:
        raise AuditMetadataError(
            f"serialized metadata must be at most {MAX_METADATA_BYTES} bytes"
        )
    return MappingProxyType(frozen)


# ---------------------------------------------------------------------------
# Canonical serialization and hashing
# ---------------------------------------------------------------------------


def _require_string_keys(value: Mapping[Any, Any]) -> None:
    for key in value:
        if not isinstance(key, str):
            raise AuditCanonicalizationError(
                "canonical JSON requires string mapping keys"
            )


def canonical_json_text(value: object) -> str:
    """Return the deterministic canonical JSON text for ``value``.

    Keys are sorted, separators are exactly ``","`` and ``":"`` (no spaces),
    non-ASCII characters are preserved (``ensure_ascii=False``), no newline is
    appended, and NaN/Infinity are refused (``allow_nan=False``). Unsupported
    objects and non-string mapping keys raise
    :class:`AuditCanonicalizationError` instead of being coerced.
    """
    if isinstance(value, Mapping):
        _require_string_keys(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise AuditCanonicalizationError(
            "value cannot be serialized with allow_nan=False"
        ) from None


def canonical_json_bytes(value: object) -> bytes:
    """Return :func:`canonical_json_text` encoded as UTF-8 bytes."""
    return canonical_json_text(value).encode("utf-8")


def _digests_equal(left: str, right: str) -> bool:
    """Constant-time digest comparison (monkeypatched in tests)."""
    return hmac.compare_digest(left, right)


def _hash_body(body: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def compute_record_hash(source: "AuditEvent | Mapping[str, object]") -> str:
    """Return the lowercase SHA-256 hex digest of a record body.

    The hash input is every field **except** ``record_hash``; a supplied
    ``record_hash`` is ignored (and may be omitted entirely), so the value is
    stable no matter what digest a record currently claims. A mapping is
    validated as a complete event first (an :class:`AuditEvent` is already
    validated by construction).
    """
    if isinstance(source, AuditEvent):
        body = source.as_body()
    elif isinstance(source, Mapping):
        candidate = dict(source)
        candidate.setdefault("record_hash", GENESIS_PREVIOUS_HASH)
        body = AuditEvent.from_dict(candidate).as_body()
    else:
        raise AuditSchemaError(
            "record must be an AuditEvent or a mapping, got "
            f"{type(source).__name__}"
        )
    body.pop("record_hash", None)
    return _hash_body(body)


def verify_record_hash(
    source: "AuditEvent | Mapping[str, object]",
    *,
    position: int | None = None,
) -> str:
    """Raise :class:`AuditHashMismatchError` unless the digest matches.

    Returns the recomputed digest (needed by chain verification to advance the
    expected predecessor).
    """
    event = source if isinstance(source, AuditEvent) else AuditEvent.from_dict(source)
    calculated = compute_record_hash(event)
    if not _digests_equal(calculated, event.record_hash):
        raise AuditHashMismatchError(
            "record_hash does not match the canonical record body",
            position=position,
            sequence=event.sequence,
        )
    return calculated


# ---------------------------------------------------------------------------
# Event record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One immutable audit record.

    See the module docstring and ``docs/AUDIT_JOURNAL_DESIGN.md`` §2 for the
    field rules. Construction validates every field; :meth:`create` also
    computes ``record_hash``. Prefer :meth:`create` for new records and
    :meth:`from_dict` for parsed ones.
    """

    schema_version: int
    sequence: int
    segment_id: str
    segment_sequence: int
    audit_id: str
    timestamp: str
    action: AuditAction
    outcome: AuditOutcome
    reason_code: AuditReasonCode
    source: AuditSource
    metadata: Mapping[str, str | int | bool]
    previous_hash: str
    record_hash: str
    actor_user_id: str | None = None
    actor_role: Role | None = None
    target_type: AuditTargetType | None = None
    target_id: str | None = None
    correlation_id: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        # -- schema version --------------------------------------------------
        version = _require_int(self.schema_version, "schema_version")
        if version != AUDIT_SCHEMA_VERSION:
            raise AuditSchemaError(
                f"unsupported schema_version {version}; "
                f"only {AUDIT_SCHEMA_VERSION} is accepted"
            )

        # -- sequences and segment ------------------------------------------
        sequence = _require_int(self.sequence, "sequence")
        if sequence < 1:
            raise AuditSchemaError("sequence must be a positive integer")
        segment_sequence = _require_int(
            self.segment_sequence, "segment_sequence"
        )
        if segment_sequence < 1:
            raise AuditSchemaError(
                "segment_sequence must be a positive integer"
            )
        if segment_sequence > sequence:
            raise AuditSchemaError(
                "segment_sequence must not exceed the global sequence"
            )
        segment_id = _require_str(self.segment_id, "segment_id")
        segment_match = _SEGMENT_PATTERN.fullmatch(segment_id)
        if segment_match is None:
            raise AuditSchemaError(
                "segment_id must use YYYY-MM-NNN: a UTC allocation month "
                "and a three-digit ordinal"
            )
        if segment_match.group(3) == "000":
            raise AuditSchemaError(
                "segment_id ordinal must be between 001 and 999"
            )

        # -- identifiers and timestamp --------------------------------------
        object.__setattr__(
            self, "audit_id", _require_uuid4(self.audit_id, "audit_id")
        )
        object.__setattr__(
            self,
            "timestamp",
            format_timestamp(parse_timestamp(self.timestamp)),
        )

        # -- vocabulary ------------------------------------------------------
        object.__setattr__(
            self, "action", _require_member(self.action, AuditAction, "action")
        )
        object.__setattr__(
            self,
            "outcome",
            _require_member(self.outcome, AuditOutcome, "outcome"),
        )
        object.__setattr__(
            self,
            "reason_code",
            _require_member(
                self.reason_code, AuditReasonCode, "reason_code"
            ),
        )
        object.__setattr__(
            self, "source", _require_member(self.source, AuditSource, "source")
        )

        # -- actor pair ------------------------------------------------------
        if (self.actor_user_id is None) != (self.actor_role is None):
            raise AuditSchemaError(
                "actor_user_id and actor_role must both be null or both be set"
            )
        if self.actor_user_id is None:
            if self.action not in _NULL_ACTOR_ACTIONS:
                raise AuditSchemaError(
                    "a null actor is only valid for auth.login or auth.logout"
                )
        else:
            object.__setattr__(
                self,
                "actor_user_id",
                _require_identifier(self.actor_user_id, "actor_user_id"),
            )
            object.__setattr__(
                self,
                "actor_role",
                _require_member(self.actor_role, Role, "actor_role"),
            )

        # -- target pair (with the approved login-failure exception) --------
        if self.target_type is None:
            if self.target_id is not None:
                raise AuditSchemaError(
                    "target_id must be null when target_type is null"
                )
        else:
            object.__setattr__(
                self,
                "target_type",
                _require_member(
                    self.target_type, AuditTargetType, "target_type"
                ),
            )
            if self.target_id is None:
                # Owner decision: failed logins record the account *kind* and
                # never the username, so type-without-id is valid only here.
                if not (
                    self.target_type is AuditTargetType.ACCOUNT
                    and self.action is AuditAction.AUTH_LOGIN
                ):
                    raise AuditSchemaError(
                        "target_id may be null only for an auth.login event "
                        "with target_type='account'"
                    )
            else:
                target_id = _require_identifier(
                    self.target_id,
                    "target_id",
                    max_length=MAX_TARGET_ID_LENGTH,
                )
                object.__setattr__(self, "target_id", target_id)

        # -- correlation and request identifiers ----------------------------
        if self.correlation_id is not None:
            object.__setattr__(
                self,
                "correlation_id",
                _require_uuid4(self.correlation_id, "correlation_id"),
            )
        if self.request_id is not None:
            object.__setattr__(
                self, "request_id", _require_uuid4(self.request_id, "request_id")
            )

        # -- outcome / reason coherence -------------------------------------
        if self.reason_code is AuditReasonCode.OK:
            if self.outcome is not AuditOutcome.SUCCESS:
                raise AuditSchemaError(
                    "reason_code OK requires outcome success"
                )
        if self.outcome is AuditOutcome.SUCCESS:
            if self.reason_code is not AuditReasonCode.OK:
                raise AuditSchemaError(
                    "outcome success requires reason_code OK"
                )
        if self.correlation_id is None:
            if self.action in _STANDALONE_ACTIONS:
                pass  # Standalone events never correlate.
            elif self.reason_code is not AuditReasonCode.AWAITING_RESULT:
                raise AuditSchemaError(
                    "a mutation record without correlation_id must be the "
                    "intent record (reason_code AWAITING_RESULT)"
                )
        elif self.action in _STANDALONE_ACTIONS:
            raise AuditSchemaError(
                "standalone events must not carry a correlation_id"
            )

        # -- metadata and hashes ---------------------------------------------
        object.__setattr__(self, "metadata", validate_metadata(self.metadata))
        object.__setattr__(
            self,
            "previous_hash",
            _require_hash(self.previous_hash, "previous_hash"),
        )
        object.__setattr__(
            self, "record_hash", _require_hash(self.record_hash, "record_hash")
        )

        # -- record size ------------------------------------------------------
        if len(canonical_json_bytes(self.as_body())) > MAX_RECORD_BYTES:
            raise AuditSchemaError(
                f"record must be at most {MAX_RECORD_BYTES} bytes when "
                "serialized canonically"
            )

    # -- construction helpers -------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        segment_id: str,
        segment_sequence: int,
        action: AuditAction,
        outcome: AuditOutcome,
        reason_code: AuditReasonCode,
        source: AuditSource,
        previous_hash: str,
        timestamp: datetime,
        actor_user_id: str | None = None,
        actor_role: Role | None = None,
        target_type: AuditTargetType | None = None,
        target_id: str | None = None,
        correlation_id: str | None = None,
        request_id: str | None = None,
        metadata: Mapping[str, str | int | bool] | None = None,
        audit_id: str | None = None,
    ) -> "AuditEvent":
        """Build a validated record and compute its ``record_hash``.

        ``timestamp`` must be a timezone-aware ``datetime`` (the application
        clock seam supplies it); ``audit_id`` defaults to a fresh UUIDv4. All
        other values are validated by the constructor.
        """
        draft = cls(
            schema_version=AUDIT_SCHEMA_VERSION,
            sequence=sequence,
            segment_id=segment_id,
            segment_sequence=segment_sequence,
            audit_id=audit_id if audit_id is not None else str(uuid.uuid4()),
            timestamp=format_timestamp(timestamp),
            action=action,
            outcome=outcome,
            reason_code=reason_code,
            source=source,
            metadata={} if metadata is None else metadata,
            previous_hash=previous_hash,
            record_hash=GENESIS_PREVIOUS_HASH,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            target_type=target_type,
            target_id=target_id,
            correlation_id=correlation_id,
            request_id=request_id,
        )
        body = draft.as_body()
        body.pop("record_hash")
        return replace(draft, record_hash=_hash_body(body))

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "AuditEvent":
        """Validate and convert a parsed JSON object into an :class:`AuditEvent`.

        Unknown and missing fields are refused rather than ignored, enums are
        converted from their exact string values, and every field rule is
        applied. Error messages never echo the supplied values.
        """
        if not isinstance(payload, Mapping):
            raise AuditSchemaError(
                f"record must be a mapping, got {type(payload).__name__}"
            )
        keys = frozenset(payload.keys())
        unknown = keys - _EVENT_FIELD_SET
        missing = _EVENT_FIELD_SET - keys
        if unknown:
            shown = sorted(str(key)[:32] for key in unknown)[:5]
            raise AuditSchemaError(
                "record carries unknown fields: " + ", ".join(shown)
            )
        if missing:
            raise AuditSchemaError(
                "record is missing required fields: "
                + ", ".join(sorted(missing))
            )
        return cls(
            schema_version=payload["schema_version"],  # type: ignore[arg-type]
            sequence=payload["sequence"],  # type: ignore[arg-type]
            segment_id=payload["segment_id"],  # type: ignore[arg-type]
            segment_sequence=payload["segment_sequence"],  # type: ignore[arg-type]
            audit_id=payload["audit_id"],  # type: ignore[arg-type]
            timestamp=payload["timestamp"],  # type: ignore[arg-type]
            actor_user_id=payload["actor_user_id"],  # type: ignore[arg-type]
            actor_role=_role_or_none(payload["actor_role"]),
            action=_action(payload["action"]),
            target_type=_target_type_or_none(payload["target_type"]),
            target_id=payload["target_id"],  # type: ignore[arg-type]
            outcome=_outcome(payload["outcome"]),
            reason_code=_reason_code(payload["reason_code"]),
            correlation_id=payload["correlation_id"],  # type: ignore[arg-type]
            request_id=payload["request_id"],  # type: ignore[arg-type]
            source=_source(payload["source"]),
            metadata=payload["metadata"],  # type: ignore[arg-type]
            previous_hash=payload["previous_hash"],  # type: ignore[arg-type]
            record_hash=payload["record_hash"],  # type: ignore[arg-type]
        )

    # -- export ---------------------------------------------------------------

    def as_body(self) -> dict[str, object]:
        """Return the JSON body: every field, enums as their string values."""
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "segment_id": self.segment_id,
            "segment_sequence": self.segment_sequence,
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "actor_user_id": self.actor_user_id,
            "actor_role": None if self.actor_role is None else self.actor_role.value,
            "action": self.action.value,
            "target_type": (
                None if self.target_type is None else self.target_type.value
            ),
            "target_id": self.target_id,
            "outcome": self.outcome.value,
            "reason_code": self.reason_code.value,
            "correlation_id": self.correlation_id,
            "request_id": self.request_id,
            "source": self.source.value,
            "metadata": dict(self.metadata),
            "previous_hash": self.previous_hash,
            "record_hash": self.record_hash,
        }

    def to_json_line(self) -> str:
        """Return the canonical single-line JSON text (no trailing newline)."""
        return canonical_json_text(self.as_body())


def _action(value: object) -> AuditAction:
    if not isinstance(value, str):
        raise AuditSchemaError(
            f"action must be a str, got {type(value).__name__}"
        )
    try:
        return AuditAction(value)
    except ValueError:
        raise AuditSchemaError("action is not in the approved vocabulary") from None


def _outcome(value: object) -> AuditOutcome:
    if not isinstance(value, str):
        raise AuditSchemaError(
            f"outcome must be a str, got {type(value).__name__}"
        )
    try:
        return AuditOutcome(value)
    except ValueError:
        raise AuditSchemaError(
            "outcome is not in the approved vocabulary"
        ) from None


def _reason_code(value: object) -> AuditReasonCode:
    if not isinstance(value, str):
        raise AuditSchemaError(
            f"reason_code must be a str, got {type(value).__name__}"
        )
    try:
        return AuditReasonCode(value)
    except ValueError:
        raise AuditSchemaError(
            "reason_code is not in the approved vocabulary"
        ) from None


def _source(value: object) -> AuditSource:
    if not isinstance(value, str):
        raise AuditSchemaError(
            f"source must be a str, got {type(value).__name__}"
        )
    try:
        return AuditSource(value)
    except ValueError:
        raise AuditSchemaError(
            "source must be exactly api, cli or system"
        ) from None


def _role_or_none(value: object) -> Role | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuditSchemaError(
            f"actor_role must be a str or null, got {type(value).__name__}"
        )
    try:
        return Role(value)
    except ValueError:
        raise AuditSchemaError(
            "actor_role is not one of the approved roles"
        ) from None


def _target_type_or_none(value: object) -> AuditTargetType | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuditSchemaError(
            f"target_type must be a str or null, got {type(value).__name__}"
        )
    try:
        return AuditTargetType(value)
    except ValueError:
        raise AuditSchemaError(
            "target_type is not in the approved vocabulary"
        ) from None


# ---------------------------------------------------------------------------
# Chain verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainVerification:
    """Summary of a successfully verified record chain."""

    record_count: int
    first_global_sequence: int | None
    last_global_sequence: int | None
    last_record_hash: str | None
    segment_ids: tuple[str, ...]


def _coerce_record(
    item: object, position: int
) -> AuditEvent:
    if isinstance(item, AuditEvent):
        return item
    if isinstance(item, Mapping):
        try:
            return AuditEvent.from_dict(item)
        except AuditSchemaError as error:
            raise type(error)(
                str(error), position=position
            ) from None
    raise AuditSchemaError(
        "chain entries must be AuditEvent values or parsed record mappings",
        position=position,
    )


def verify_chain(
    records: Iterable["AuditEvent | Mapping[str, object]"],
    *,
    expected_previous_hash: str = GENESIS_PREVIOUS_HASH,
    expected_next_sequence: int = 1,
) -> ChainVerification:
    """Verify a complete contiguous record chain and summarise it.

    Checks, in order, for every record: it parses into a valid
    :class:`AuditEvent`; its ``previous_hash`` equals the expected predecessor
    hash; its global ``sequence`` equals the expected number; its
    ``segment_sequence`` continues its segment (or is 1 in a new segment); its
    segment id has not appeared earlier in this chain; and its ``record_hash``
    matches the canonical body.

    Segment ids are **not** compared lexically: ``YYYY-MM-NNN`` is a unique
    allocation label, not a chronological proof. Order comes from the hash
    links and the global sequence; the manifest adds seal order in P5.5.7.

    The first record must link to ``expected_previous_hash`` (genesis by
    default). A later segment's first record therefore links to the previous
    segment's final hash — the chain never resets. The two keyword parameters
    are the extension seam for P5.5.6/P5.5.7 anchor verification; their
    defaults describe a full chain that starts at genesis.

    An empty input is valid and returns a zero-record summary. Nothing is
    written, repaired, normalised or mutated: mappings are parsed into new
    immutable values and the caller's objects are left untouched.
    """
    expected_hash = _require_hash(expected_previous_hash, "expected_previous_hash")
    expected_sequence = _require_int(
        expected_next_sequence, "expected_next_sequence"
    )
    if expected_sequence < 1:
        raise AuditSchemaError("expected_next_sequence must be positive")

    previous: AuditEvent | None = None
    seen_segments: list[str] = []
    record_count = 0

    for position, item in enumerate(records):
        record = _coerce_record(item, position)

        if record.previous_hash != expected_hash:
            raise AuditPreviousHashMismatchError(
                "record does not link to the expected predecessor hash",
                position=position,
                sequence=record.sequence,
            )
        if record.sequence != expected_sequence:
            raise AuditSequenceError(
                "global sequence is not contiguous",
                position=position,
                sequence=record.sequence,
            )

        if previous is None:
            if record.segment_sequence != 1:
                raise AuditSegmentError(
                    "the first record of a segment must have "
                    "segment_sequence 1",
                    position=position,
                    sequence=record.sequence,
                )
            seen_segments.append(record.segment_id)
        elif record.segment_id == previous.segment_id:
            if record.segment_sequence != previous.segment_sequence + 1:
                raise AuditSequenceError(
                    "segment_sequence is not contiguous",
                    position=position,
                    sequence=record.sequence,
                )
        else:
            if record.segment_id in seen_segments:
                raise AuditSegmentError(
                    "segment ids must not repeat; a sealed segment cannot be "
                    "reopened",
                    position=position,
                    sequence=record.sequence,
                )
            if record.segment_sequence != 1:
                raise AuditSegmentError(
                    "the first record of a segment must have "
                    "segment_sequence 1",
                    position=position,
                    sequence=record.sequence,
                )
            seen_segments.append(record.segment_id)

        verify_record_hash(record, position=position)

        expected_hash = record.record_hash
        expected_sequence += 1
        record_count += 1
        previous = record

    if previous is None:
        return ChainVerification(
            record_count=0,
            first_global_sequence=None,
            last_global_sequence=None,
            last_record_hash=None,
            segment_ids=(),
        )
    return ChainVerification(
        record_count=record_count,
        first_global_sequence=expected_next_sequence,
        last_global_sequence=previous.sequence,
        last_record_hash=previous.record_hash,
        segment_ids=tuple(seen_segments),
    )
