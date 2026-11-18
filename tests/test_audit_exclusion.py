"""P5.5.8.2 audit exclusion-sweep harness (offline, temporary directories only).

The sweep proves the approved P5.5 exclusion rule on the **real** implementation:
synthetic records are produced through the actual writer, service, manifest,
rotation and CLI paths, and every resulting artifact — active journal bytes,
manifest bytes, archived segment bytes, verifier/rotation CLI stdout and stderr,
captured application logs and sanitized exception messages — is scanned for
forbidden material.

Nothing here contacts PostgreSQL, reads ``.env``, opens ``data/audit``, handles
real credentials or inspects biometric data. Every value below is a synthetic
``sentinel-*`` token that cannot be mistaken for a real secret.

The scanner is deliberately test-only (design §12 "Exclusion"; plan §9.5
"Integration"): it is given explicit artifacts, never walks the repository, never
inspects the environment, and its failure messages name the artifact and the
forbidden category but never the matched value.

Encoding is not a hiding place. A raw byte search is defeated by JSON escaping
(a quote becomes ``\\"``, a backslash ``\\\\``, and under ``ensure_ascii=True``
every non-ASCII character ``\\uXXXX``), so every artifact is searched in its raw
form, as decoded text, and after one and two unescape passes, in UTF-8 and
UTF-16-LE, and every JSON string leaf value is walked and unescaped through the
same bounded passes so that an escaped value inside a JSON string — including a
string nested inside another — is still seen. The key pass does the same, so
``"\\u0075sername":`` is still seen.
"""

from __future__ import annotations

import codecs
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable, Iterator, Sequence

import pytest

import src.audit_rotation as rotation_module
import tools.audit_rotate as rotate_cli
import tools.audit_verify as verify_cli
from src.audit_layout import AuditLayout
from src.audit_manifest_store import ManifestCorruptError, ManifestStore
from src.audit_rotation import RotationOperations, RotationUnavailableError
from src.audit_schema import (
    METADATA_ALLOWED_KEYS,
    AuditAction,
    AuditEvent,
    AuditOutcome,
    AuditReasonCode,
    AuditTargetType,
    canonical_json_bytes,
)
from src.audit_service import (
    AuditContext,
    AuditResultUncertainError,
    AuditService,
    AuditUnavailableError,
)
from src.audit_writer import AuditJournalUnavailableError, AuditJournalWriter
from src.user import Role
from tests.audit_support import install_events

BASE_TS = datetime(2026, 11, 16, 9, 30, 0, 123456, tzinfo=UTC)
REQUEST_ID = "0b1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"

#: The exact event field set, restated independently from ``audit_schema`` so a
#: record cannot silently start carrying an extra field.
EVENT_FIELDS = frozenset(
    {
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
    }
)

#: The accepted 16-field manifest schema (design §9.2), restated independently.
MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "manifest_sequence",
        "entry_type",
        "segment_id",
        "disposition",
        "segment_file",
        "first_global_sequence",
        "last_global_sequence",
        "record_count",
        "first_previous_hash",
        "last_record_hash",
        "sealed_at",
        "tool",
        "tool_version",
        "previous_manifest_hash",
        "entry_hash",
    }
)


# ---------------------------------------------------------------------------
# Forbidden information: synthetic sentinels
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sentinel:
    """One forbidden-information category and a synthetic value for it."""

    category: str
    value: str


PASSWORD = Sentinel("password", "sentinel-password-4f21c8bd")
#: A password sentinel whose characters JSON escapes as ``\\uXXXX`` under
#: ``ensure_ascii=True`` (any tool that re-serializes a record) **and** is
#: literal in the canonical journal, which keeps ``ensure_ascii=False``.
PASSWORD_UNICODE = Sentinel("password", "sentinel-p\u00e4ssw\u00f6rd-3a9f")
PASSWORD_HASH = Sentinel("password hash", "sentinel-hash-9d3e7a15")
SESSION_TOKEN = Sentinel("session token", "sentinel-token-1c6b0e94")
#: A token sentinel containing a quote, which JSON always escapes as ``\\"``,
#: so even canonical JSON cannot be scanned for its literal bytes.
SESSION_TOKEN_QUOTED = Sentinel("session token", 'sentinel-"token"-e41c')
TOKEN_DIGEST = Sentinel("token digest", "sentinel-digest-5a2f83d0")
COOKIE_NAME = Sentinel("cookie name", "sentinel_cookie_name")
COOKIE_VALUE = Sentinel("cookie value", "sentinel-cookie-7b41d2e6")
DSN = Sentinel(
    "database dsn",
    "sentinel-dsn-postgresql://sentinel-user:sentinel-pass@sentinel-host:5432/sentinel-db",
)
API_KEY = Sentinel("api key", "sentinel-api-key-2e8f6407")
SECRET_ENV = Sentinel("secret environment value", "sentinel-env-value-63ac91f5")
EMBEDDING = Sentinel("face embedding", "sentinel-embedding-b83c4671")
IMAGE_PATH = Sentinel("image path", "sentinel-image-path/face-0001.png")
FRAME_PAYLOAD = Sentinel("camera frame", "sentinel-frame-payload-0d5a72c9")
REQUEST_BODY = Sentinel("request body", "sentinel-request-body-a41790ce")
PERSONAL_DATA = Sentinel("personal data", "sentinel-personal-data-8be205d7")
USERNAME = Sentinel("login username", "sentinel-username-alice")

SENTINELS: tuple[Sentinel, ...] = (
    PASSWORD,
    PASSWORD_UNICODE,
    PASSWORD_HASH,
    SESSION_TOKEN,
    SESSION_TOKEN_QUOTED,
    TOKEN_DIGEST,
    COOKIE_NAME,
    COOKIE_VALUE,
    DSN,
    API_KEY,
    SECRET_ENV,
    EMBEDDING,
    IMAGE_PATH,
    FRAME_PAYLOAD,
    REQUEST_BODY,
    PERSONAL_DATA,
    USERNAME,
)

#: Key names that must never appear in a structured audit artifact, even with a
#: benign value: the allowlist is what keeps future fields safe.
FORBIDDEN_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "passwd",
        "secret",
        "token",
        "session_token",
        "token_digest",
        "cookie",
        "dsn",
        "conninfo",
        "authorization",
        "api_key",
        "embedding",
        "face",
        "face_embedding",
        "image",
        "image_path",
        "photo",
        "camera",
        "frame",
        "body",
        "request_body",
        "username",
        "login_name",
    }
)


# ---------------------------------------------------------------------------
# Scanner (test-only; explicit artifacts, no recursion, no environment)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Artifact:
    """A named piece of captured output or file content to scan."""

    name: str
    content: bytes | str


@dataclass(frozen=True, slots=True)
class Leak:
    """A forbidden category found in a named artifact (never the value)."""

    artifact: str
    category: str

    def __str__(self) -> str:
        return f"{self.artifact}/{self.category}"


def _as_bytes(content: bytes | str) -> bytes:
    if isinstance(content, bytes):
        return content
    return content.encode("utf-8", "surrogatepass")


_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}

_ESCAPE_RE = re.compile(r"\\(u[0-9a-fA-F]{4}|.)")
_KEY_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')

#: How many times escaped text is unescaped before searching. Two passes cover
#: a JSON string nested inside another JSON string, which is the deepest
#: realistic encoding of a leaked value.
_MAX_UNESCAPE_PASSES = 2


def _decode_text(raw: bytes) -> str:
    """Decode by BOM where present, otherwise UTF-8 with replacement."""
    if raw.startswith(codecs.BOM_UTF16_LE):
        return raw[len(codecs.BOM_UTF16_LE) :].decode("utf-16-le", "replace")
    if raw.startswith(codecs.BOM_UTF16_BE):
        return raw[len(codecs.BOM_UTF16_BE) :].decode("utf-16-be", "replace")
    return raw.decode("utf-8", "replace")


def _unescape(text: str) -> str:
    """Apply one JSON string-escape pass (``\\uXXXX`` and the short escapes)."""

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.startswith("u"):
            try:
                return chr(int(token[1:], 16))
            except ValueError:  # pragma: no cover - regex guarantees 4 hex digits
                return match.group(0)
        return _SIMPLE_ESCAPES.get(token, match.group(0))

    return _ESCAPE_RE.sub(_replace, text)


def _text_views(content: bytes | str) -> list[str]:
    """Decoded text, then one and two unescape passes.

    This is the one bounded unescape strategy shared by the value scanner and
    the key scanner, so the two can never drift apart. ``_MAX_UNESCAPE_PASSES``
    is two passes, i.e. three views: as stored, once unescaped (a nested JSON
    string) and twice unescaped (a JSON string nested inside another one).
    """
    text = _decode_text(_as_bytes(content))
    views: list[str] = []
    for _ in range(_MAX_UNESCAPE_PASSES + 1):
        views.append(text)
        text = _unescape(text)
    return views


def _views(content: bytes | str) -> list[bytes]:
    """Every representation a sentinel could take inside ``content``.

    A raw byte search alone is defeatable by encoding: JSON escapes a quote as
    ``\\"``, a backslash as ``\\\\`` and, under ``ensure_ascii=True``, every
    non-ASCII character as ``\\uXXXX``. The views therefore include the raw
    UTF-8 bytes and every :func:`_text_views` pass, each as UTF-8 and
    UTF-16-LE. Undecodable binary content stays scannable because the raw view
    is compared as bytes.
    """
    views: list[bytes] = [_as_bytes(content).lower()]
    for text in _text_views(content):
        views.append(text.encode("utf-8", "surrogatepass").lower())
        views.append(text.encode("utf-16-le", "surrogatepass").lower())
    return views


def _walk_values(payload: object) -> Iterator[str]:
    """Yield every JSON string leaf value, fully unescaped.

    This is the value-scanner counterpart to :func:`_walk_keys`: it reaches
    sentinel values that survive only as escaped JSON string content, including
    a JSON string nested inside another JSON string, without relying on the raw
    artifact bytes containing the literal sentinel.
    """
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from _walk_values(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_values(item)


def scan(artifacts: Iterable[Artifact]) -> tuple[Leak, ...]:
    """Scan explicit artifacts for every synthetic sentinel.

    Each artifact is searched in every representation from :func:`_views` and
    in every JSON string leaf value produced by :func:`_walk_values` and
    unescaped through :func:`_text_views`, so a sentinel cannot hide behind
    UTF-16 re-encoding, JSON string escaping, a re-serialization with
    ``ensure_ascii=True``, or a JSON string nested inside another one.
    """
    leaks: list[Leak] = []
    for artifact in artifacts:
        found: set[tuple[str, str]] = set()
        # Raw-form search: every representation the artifact could take.
        views = _views(artifact.content)
        for sentinel in SENTINELS:
            needles = (
                sentinel.value.encode("utf-8").lower(),
                sentinel.value.encode("utf-16-le").lower(),
            )
            if any(
                needle in view for view in views for needle in needles
            ):
                found.add((artifact.name, sentinel.category))
        # Parsed-value search: walk every JSON string leaf value and search its
        # bounded unescape views, mirroring the key scanner's strategy so the two
        # can never drift apart.
        for value in _walk_values(artifact.content):
            for text_view in _text_views(value):
                view_bytes = text_view.encode("utf-8", "surrogatepass").lower()
                view_utf16 = text_view.encode("utf-16-le", "surrogatepass").lower()
                for sentinel in SENTINELS:
                    needles = (
                        sentinel.value.encode("utf-8").lower(),
                        sentinel.value.encode("utf-16-le").lower(),
                    )
                    if any(needle in v for needle in needles for v in (view_bytes, view_utf16)):
                        found.add((artifact.name, sentinel.category))
        for artifact_name, category in sorted(found):
            leak = Leak(artifact_name, category)
            if leak not in leaks:
                leaks.append(leak)
    return tuple(leaks)


def _json_objects(content: bytes | str) -> Iterator[object]:
    """Yield every JSON value that appears one-per-line in ``content``."""
    text = (
        content.decode("utf-8", "replace")
        if isinstance(content, bytes)
        else content
    )
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith(("{", "[")):
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _walk_keys(payload: object) -> Iterator[str]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(key, str):
                yield key
            yield from _walk_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_keys(item)


def scan_keys(artifacts: Iterable[Artifact]) -> tuple[Leak, ...]:
    """Scan structured artifacts for forbidden key names.

    Parsed JSON is authoritative, and a text pass runs over the same bounded
    unescape views as the value scanner (:func:`_text_views`), so a key written
    as ``"\\u0075sername":``, a key nested inside an escaped JSON string, or a
    key in a torn line are all caught — without JSON parsing having to succeed.
    """
    leaks: list[Leak] = []
    for artifact in artifacts:
        found: set[str] = set()
        for payload in _json_objects(artifact.content):
            for key in _walk_keys(payload):
                if key.lower() in FORBIDDEN_KEYS:
                    found.add(f"key:{key}")
        for text_view in _text_views(artifact.content):
            for match in _KEY_RE.finditer(text_view):
                key = match.group(1)
                if key.lower() in FORBIDDEN_KEYS:
                    found.add(f"key:{key}")
        for category in sorted(found):
            leak = Leak(artifact.name, category)
            if leak not in leaks:
                leaks.append(leak)
    return tuple(leaks)


def violations(artifacts: Sequence[Artifact]) -> tuple[Leak, ...]:
    """Every value-level and key-level violation across the artifacts."""
    return scan(artifacts) + scan_keys(artifacts)


def assert_no_leaks(artifacts: Sequence[Artifact]) -> None:
    """Fail with artifact names and categories only — never the value."""
    found = violations(artifacts)
    if found:
        listed = ", ".join(str(leak) for leak in found)
        raise AssertionError(f"audit exclusion violation(s): {listed}")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class StepClock:
    """Strictly increasing synthetic clock shared by writer and service."""

    def __init__(self, start: datetime = BASE_TS) -> None:
        self._now = start
        self._step = 0

    def __call__(self) -> datetime:
        self._step += 1
        return self._now + timedelta(microseconds=self._step)


def audit_ids(start: int = 1) -> Callable[[], str]:
    counter = count(start)
    return lambda: str(uuid.UUID(int=next(counter), version=4))


def principal(
    user_id: str = "user-1",
    role: Role = Role.ADMIN,
    username: str = USERNAME.value,
) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, role=role, username=username)


def request_context() -> tuple[AuditContext, AuditContext]:
    """An authenticated context and a pre-session one."""
    return (
        AuditService.context(principal(), request_id=REQUEST_ID),
        AuditService.context(None, request_id=REQUEST_ID),
    )


def build_service(
    root: Path, *, start: datetime = BASE_TS, first_id: int = 1
) -> tuple[AuditService, AuditJournalWriter, StepClock]:
    clock = StepClock(start)
    writer = AuditJournalWriter(root / "audit" / "audit.jsonl", clock=clock)
    service = AuditService(
        writer, clock=clock, audit_id_factory=audit_ids(first_id)
    )
    return service, writer, clock


def record_representative_events(service: AuditService) -> list[AuditEvent]:
    """One of each event shape the approved policy allows an application to emit."""
    actor_context, anonymous_context = request_context()
    events: list[AuditEvent] = []

    created = service.begin_mutation(
        action=AuditAction.EMPLOYEE_CREATE,
        target_id="E-1",
        context=actor_context,
        metadata={"endpoint": "/api/v1/employees", "method": "POST"},
    )
    events.append(created.event)
    events.append(service.record_applied(created))

    checkout = service.begin_mutation(
        action=AuditAction.ATTENDANCE_CHECKOUT,
        target_id="E-2",
        context=actor_context,
    )
    events.append(checkout.event)
    failed_result = service.record_not_applied(
        checkout, AuditReasonCode.NO_OPEN_SESSION
    )
    assert failed_result is not None
    events.append(failed_result)

    events.append(
        service.record_login(
            outcome=AuditOutcome.SUCCESS,
            reason_code=AuditReasonCode.OK,
            context=actor_context,
        )
    )
    events.append(
        service.record_login(
            outcome=AuditOutcome.DENIED,
            reason_code=AuditReasonCode.INVALID_CREDENTIALS,
            context=anonymous_context,
        )
    )
    events.append(
        service.record_logout(
            outcome=AuditOutcome.SUCCESS,
            reason_code=AuditReasonCode.OK,
            context=actor_context,
        )
    )
    events.append(
        service.record_denied(
            endpoint="/api/v1/employees",
            method="POST",
            context=actor_context,
        )
    )
    return events


def journal_path(root: Path) -> Path:
    return AuditLayout.for_directory(root / "audit").journal


def journal_artifacts(root: Path) -> list[Artifact]:
    return [Artifact("active-journal", journal_path(root).read_bytes())]


def parsed_journal(root: Path) -> list[AuditEvent]:
    return [
        AuditEvent.from_dict(json.loads(line))
        for line in journal_path(root).read_text(encoding="utf-8").splitlines()
    ]


class LeakyWriter:
    """Writer stand-in whose failure carries a deliberately leaky message."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    def append(self, **kwargs: object) -> AuditEvent:
        self.calls += 1
        raise self._exc


class FailAfterWriter:
    """Pass-through writer that fails once its call budget is spent."""

    def __init__(
        self, delegate: AuditJournalWriter, *, fail_from: int, exc: BaseException
    ) -> None:
        self._delegate = delegate
        self._fail_from = fail_from
        self._exc = exc
        self.calls = 0

    def append(self, **kwargs: object) -> AuditEvent:
        self.calls += 1
        if self.calls >= self._fail_from:
            raise self._exc
        return self._delegate.append(**kwargs)  # type: ignore[arg-type]


def leaky_service(exc: BaseException) -> AuditService:
    return AuditService(
        LeakyWriter(exc),  # type: ignore[arg-type]
        clock=StepClock(),
        audit_id_factory=audit_ids(),
    )


def _rotate_service_journal(
    root: Path,
) -> tuple[AuditLayout, RotationOperations]:
    """Write representative records, then seal them through the real rotation."""
    service, _writer, clock = build_service(root)
    record_representative_events(service)
    layout = AuditLayout.for_directory(root / "audit")
    # A synthetic installation is laid out exactly as ``install()`` builds it,
    # which includes the archive directory that rotation renames into.
    layout.archive_directory.mkdir(parents=True, exist_ok=True)
    operations = RotationOperations(layout, clock=clock)
    operations.rotate()
    return layout, operations


def _sealed_segment_id(layout: AuditLayout) -> str:
    entries = [
        json.loads(line)
        for line in layout.manifest.read_text(encoding="utf-8").splitlines()
    ]
    sealed = [entry for entry in entries if entry["entry_type"] == "segment_sealed"]
    assert sealed
    return sealed[0]["segment_id"]


# ---------------------------------------------------------------------------
# A. Positive control: the scanner actually detects forbidden material
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentinel",
    SENTINELS,
    ids=[
        f"{index}-{sentinel.category}"
        for index, sentinel in enumerate(SENTINELS)
    ],
)
def test_scanner_detects_every_forbidden_category(sentinel: Sentinel) -> None:
    artifacts = [
        Artifact("clean", "audit journal verification: PASS"),
        Artifact("planted", f'{{"note": "{sentinel.value}"}}'),
    ]
    leaks = scan(artifacts)
    assert Leak("planted", sentinel.category) in leaks
    assert all(leak.artifact == "planted" for leak in leaks)


def test_scanner_detects_sentinel_in_binary_content() -> None:
    planted = b"\xff\xfe\x00garbage" + SESSION_TOKEN.value.encode("utf-16-le")
    leaks = scan([Artifact("binary", planted)])
    assert Leak("binary", SESSION_TOKEN.category) in leaks


def test_raw_byte_search_is_defeated_by_json_escaping() -> None:
    """Regression: the escaped forms are exactly what a raw scan cannot see."""
    payload = canonical_json_bytes(
        {"journal_event_id": SESSION_TOKEN_QUOTED.value}
    )
    assert SESSION_TOKEN_QUOTED.value.encode("utf-8") not in payload
    assert SESSION_TOKEN_QUOTED.value.encode("utf-16-le") not in payload
    assert Leak("journal", SESSION_TOKEN_QUOTED.category) in scan(
        [Artifact("journal", payload)]
    )


def test_scanner_detects_ensure_ascii_encoded_sentinel() -> None:
    escaped = json.dumps({"note": PASSWORD_UNICODE.value}).encode("utf-8")
    assert PASSWORD_UNICODE.value.encode("utf-8") not in escaped
    assert b"\\u00e4" in escaped
    assert Leak("response", PASSWORD_UNICODE.category) in scan(
        [Artifact("response", escaped)]
    )


def test_scanner_detects_double_escaped_sentinel() -> None:
    nested = json.dumps({"note": json.dumps(PASSWORD_UNICODE.value)}).encode()
    assert PASSWORD_UNICODE.value.encode("utf-8") not in nested
    assert Leak("log", PASSWORD_UNICODE.category) in scan(
        [Artifact("log", nested)]
    )


def test_scanner_detects_escaped_key_in_a_torn_line() -> None:
    planted = b'{"\\u0075sername":"x"'
    assert Leak("journal", "key:username") in scan_keys(
        [Artifact("journal", planted)]
    )


def nested_key_json(key: str) -> str:
    """A JSON object whose forbidden key needs one unescape pass to appear."""
    inner = json.dumps({key: "x"}, separators=(",", ":"))
    return json.dumps({"payload": inner}, separators=(",", ":"))


def double_escaped_key_json(key: str) -> str:
    """A JSON object whose forbidden key needs two unescape passes to appear."""
    once = nested_key_json(key)
    return json.dumps({"payload": once}, separators=(",", ":"))


def test_scan_keys_detects_a_single_escaped_key() -> None:
    payload = nested_key_json("username")
    assert '"username"' not in payload
    assert '\\"username\\"' in payload
    assert scan_keys([Artifact("journal", payload)]) == (
        Leak("journal", "key:username"),
    )


def test_scan_keys_detects_a_double_escaped_key() -> None:
    payload = double_escaped_key_json("username")
    assert '"username"' not in payload
    assert scan_keys([Artifact("journal", payload)]) == (
        Leak("journal", "key:username"),
    )


def test_scan_keys_detects_a_double_escaped_key_in_a_torn_line() -> None:
    torn = double_escaped_key_json("session_token")[:-1]
    assert '"session_token"' not in torn
    with pytest.raises(ValueError):
        json.loads(torn)
    assert scan_keys([Artifact("journal", torn)]) == (
        Leak("journal", "key:session_token"),
    )


def test_scan_keys_accepts_clean_nested_json() -> None:
    payload = json.dumps(
        {"payload": json.dumps({"action": "auth.login", "target_id": None})}
    )
    assert scan_keys([Artifact("journal", payload)]) == ()
    assert violations([Artifact("journal", payload)]) == ()


def test_key_violation_message_names_artifact_and_key_but_not_the_value() -> None:
    payload = json.dumps({"username": "opaque-value-1"})
    with pytest.raises(AssertionError) as caught:
        assert_no_leaks([Artifact("active-journal", payload)])
    message = str(caught.value)
    assert "active-journal" in message
    assert "key:username" in message
    assert "opaque-value-1" not in message


def test_scanner_reports_no_leak_for_clean_artifacts() -> None:
    clean_event = json.dumps(
        {
            "action": "auth.login",
            "outcome": "denied",
            "reason_code": "INVALID_CREDENTIALS",
            "target_type": "account",
            "target_id": None,
        }
    )
    assert scan([Artifact("journal", clean_event)]) == ()
    assert scan_keys([Artifact("journal", clean_event)]) == ()
    assert_no_leaks([Artifact("journal", clean_event)])


def test_scanner_failure_names_artifact_and_category_but_not_the_value() -> None:
    for sentinel in SENTINELS:
        artifacts = [Artifact("active-journal", sentinel.value)]
        with pytest.raises(AssertionError) as caught:
            assert_no_leaks(artifacts)
        message = str(caught.value)
        assert "active-journal" in message
        assert sentinel.category in message
        assert sentinel.value not in message


def test_scanner_detects_forbidden_keys_without_echoing_values() -> None:
    planted = json.dumps({"username": "not-a-secret", "target_id": "E-1"})
    leaks = scan_keys([Artifact("journal", planted)])
    assert Leak("journal", "key:username") in leaks
    with pytest.raises(AssertionError) as caught:
        assert_no_leaks([Artifact("journal", planted)])
    assert "not-a-secret" not in str(caught.value)


def test_scanner_reports_each_artifact_and_category_once() -> None:
    content = f"{PASSWORD.value} {PASSWORD.value}"
    leaks = scan([Artifact("one", content), Artifact("two", content)])
    assert leaks == (
        Leak("one", PASSWORD.category),
        Leak("two", PASSWORD.category),
    )


def test_scan_values_detects_a_sentinel_inside_an_escaped_json_string() -> None:
    """Regression: a sentinel whose literal contains a double-quote is JSON-
    escaped as part of a string value, so the raw artifact bytes do not contain
    the literal sentinel. The value scanner must still see it by walking parsed
    JSON string leaf values (the value-scanner counterpart to the key scanner's
    JSON walk + _text_views pass).

    ``SESSION_TOKEN_QUOTED`` is the existing sentinel whose literal contains a
    quote, so this is the value-scanner equivalent of
    ``test_scan_keys_detects_a_double_escaped_key``.
    """
    sentinel = SESSION_TOKEN_QUOTED
    # Single-escaped: the sentinel appears as a top-level JSON string value.
    single = json.dumps({"note": sentinel.value}).encode()
    assert sentinel.value.encode() not in single, (
        "raw artifact must not contain the literal escaped value"
    )
    assert sentinel.value.encode("utf-16-le") not in single
    assert Leak("single", sentinel.category) in scan(
        [Artifact("single", single)]
    ), "escaped JSON string value must be scanned"

    # Double-escaped: the sentinel is itself JSON-encoded and placed as a
    # string value — the deepest realistic encoding the harness documents.
    double = json.dumps({"payload": json.dumps(sentinel.value)}).encode()
    assert sentinel.value.encode() not in double
    assert Leak("double", sentinel.category) in scan(
        [Artifact("double", double)]
    ), "JSON string nested inside another must be scanned"

    # ensure_ascii re-serialization of the already-escaped value.
    ensure = json.dumps(
        {"payload": json.dumps(sentinel.value)}, ensure_ascii=True
    ).encode()
    assert sentinel.value.encode() not in ensure
    assert Leak("ensure", sentinel.category) in scan(
        [Artifact("ensure", ensure)]
    ), "ensure_ascii re-serialization must be scanned"


def test_scan_values_does_not_flag_a_sentinel_missing_from_values() -> None:
    payload = json.dumps({"note": "sentinel-absent-0000"})
    assert scan([Artifact("clean", payload)]) == ()


# ---------------------------------------------------------------------------
# B. Active journal sweep
# ---------------------------------------------------------------------------


def test_active_journal_carries_only_the_accepted_event_field_set(
    tmp_path: Path,
) -> None:
    service, _writer, _clock = build_service(tmp_path)
    record_representative_events(service)

    records = [
        json.loads(line)
        for line in journal_path(tmp_path).read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 8
    for record in records:
        assert set(record) == EVENT_FIELDS
        assert set(record["metadata"]) <= METADATA_ALLOWED_KEYS


def test_login_failure_retains_no_username(tmp_path: Path) -> None:
    service, _writer, _clock = build_service(tmp_path)
    record_representative_events(service)

    failures = [
        record
        for record in parsed_journal(tmp_path)
        if record.action is AuditAction.AUTH_LOGIN
        and record.outcome is AuditOutcome.DENIED
    ]
    assert len(failures) == 1
    failure = failures[0]
    assert failure.target_type is AuditTargetType.ACCOUNT
    assert failure.target_id is None
    assert failure.actor_user_id is None
    assert USERNAME.value not in journal_path(tmp_path).read_text(
        encoding="utf-8"
    )
    assert_no_leaks(journal_artifacts(tmp_path))


def test_active_journal_sweep_finds_no_forbidden_material(tmp_path: Path) -> None:
    service, _writer, _clock = build_service(tmp_path)
    record_representative_events(service)

    raw = journal_path(tmp_path).read_bytes()
    assert raw.count(b"\n") == 8
    assert_no_leaks(journal_artifacts(tmp_path))


def test_denied_event_metadata_is_the_route_template_only(tmp_path: Path) -> None:
    service, _writer, _clock = build_service(tmp_path)
    record_representative_events(service)

    denied = [
        record
        for record in parsed_journal(tmp_path)
        if record.action is AuditAction.AUTHZ_DENIED
    ]
    assert len(denied) == 1
    assert denied[0].metadata == {
        "endpoint": "/api/v1/employees",
        "method": "POST",
        "http_status": 403,
    }
    assert "?" not in str(denied[0].metadata["endpoint"])
    assert_no_leaks(journal_artifacts(tmp_path))


# ---------------------------------------------------------------------------
# C. Manifest sweep
# ---------------------------------------------------------------------------


def test_manifest_keeps_the_accepted_sixteen_field_schema(tmp_path: Path) -> None:
    layout, _operations = _rotate_service_journal(tmp_path)

    lines = layout.manifest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert set(json.loads(lines[0])) == MANIFEST_FIELDS
    assert len(MANIFEST_FIELDS) == 16


def test_manifest_bytes_and_fields_carry_no_forbidden_material(
    tmp_path: Path,
) -> None:
    layout, operations = _rotate_service_journal(tmp_path)
    operations.record_retention(_sealed_segment_id(layout))

    manifest = layout.manifest.read_bytes()
    assert manifest.count(b"\n") == 2
    assert_no_leaks([Artifact("manifest", manifest)])


def test_manifest_store_reports_corruption_without_echoing_contents(
    tmp_path: Path,
) -> None:
    layout, _operations = _rotate_service_journal(tmp_path)
    layout.manifest.write_bytes(layout.manifest.read_bytes() + DSN.value.encode())

    with pytest.raises(ManifestCorruptError) as caught:
        ManifestStore(layout).load()

    message = str(caught.value)
    assert "sentinel" not in message.lower()
    assert_no_leaks([Artifact("manifest-error", message)])


# ---------------------------------------------------------------------------
# D. Archive sweep
# ---------------------------------------------------------------------------


def test_archived_segment_sweep_finds_no_forbidden_material(tmp_path: Path) -> None:
    layout, _operations = _rotate_service_journal(tmp_path)
    archives = sorted(layout.archive_directory.glob("*.jsonl"))
    assert len(archives) == 1

    raw = archives[0].read_bytes()
    records = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    assert len(records) == 8
    for record in records:
        assert set(record) == EVENT_FIELDS
        assert set(record["metadata"]) <= METADATA_ALLOWED_KEYS

    assert_no_leaks([Artifact("archive", raw)])


def test_rotation_cli_output_does_not_echo_record_contents(
    tmp_path: Path, capsys
) -> None:
    install_events(
        tmp_path,
        segments=[],
        active=("2026-11-001", 4),
        manifest=False,
    )
    directory = str(tmp_path / "audit")

    assert rotate_cli.main(["--status", "--audit-dir", directory]) == (
        rotate_cli.EXIT_OK
    )
    status = capsys.readouterr()
    assert rotate_cli.main(["--audit-dir", directory]) == rotate_cli.EXIT_OK
    rotated = capsys.readouterr()

    assert "Audit installation: OK" in status.out
    assert "Audit rotation: SEALED" in rotated.out
    assert_no_leaks(
        [
            Artifact("rotate-status-stdout", status.out),
            Artifact("rotate-status-stderr", status.err),
            Artifact("rotate-stdout", rotated.out),
            Artifact("rotate-stderr", rotated.err),
        ]
    )


# ---------------------------------------------------------------------------
# E. CLI stdout/stderr sweep
# ---------------------------------------------------------------------------


def test_verifier_pass_output_has_no_record_bodies(tmp_path: Path, capsys) -> None:
    service, _writer, _clock = build_service(tmp_path)
    events = record_representative_events(service)
    path = journal_path(tmp_path)
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line
    ]

    code = verify_cli.main(["--journal", str(path)])
    captured = capsys.readouterr()

    assert code == verify_cli.EXIT_OK
    assert "Audit journal verification: PASS" in captured.out
    output = captured.out + captured.err
    for line in lines:
        assert line not in output
    for event in events[:-1]:
        assert event.audit_id not in output
        assert event.record_hash not in output
    assert_no_leaks(
        [
            Artifact("verify-stdout", captured.out),
            Artifact("verify-stderr", captured.err),
        ]
    )


def test_verifier_failure_output_is_explanation_only(tmp_path: Path, capsys) -> None:
    service, _writer, _clock = build_service(tmp_path)
    record_representative_events(service)
    path = journal_path(tmp_path)
    original = path.read_text(encoding="utf-8")

    records = [json.loads(line) for line in original.splitlines()]
    records[0]["record_hash"] = "00" * 32
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    code = verify_cli.main(["--journal", str(path)])
    captured = capsys.readouterr()

    assert code == verify_cli.EXIT_INTEGRITY_FAILURE
    assert "Audit journal verification: FAIL" in captured.out
    assert "Category:" in captured.out
    for line in original.splitlines():
        if line:
            assert line not in (captured.out + captured.err)
    assert_no_leaks(
        [
            Artifact("verify-fail-stdout", captured.out),
            Artifact("verify-fail-stderr", captured.err),
        ]
    )


def test_verifier_stdout_and_stderr_are_scanned_separately(
    tmp_path: Path, capsys
) -> None:
    installation = install_events(
        tmp_path,
        segments=[("2026-11-001", 3)],
        active=("2026-11-002", 2),
    )
    installation.layout.manifest.unlink()

    code = verify_cli.main(["--journal", str(installation.layout.journal)])
    captured = capsys.readouterr()

    assert code == verify_cli.EXIT_RECOVERY_REQUIRED
    assert captured.out
    assert_no_leaks(
        [
            Artifact("verify-recovery-stdout", captured.out),
            Artifact("verify-recovery-stderr", captured.err),
        ]
    )


def test_rotation_cli_failure_output_has_no_contents(tmp_path: Path, capsys) -> None:
    installation = install_events(
        tmp_path,
        segments=[("2026-11-001", 3)],
        active=("2026-11-002", 1),
    )
    installation.archive_path(0).write_bytes(b"{torn\n")

    code = rotate_cli.main(["--status", "--audit-dir", str(tmp_path / "audit")])
    captured = capsys.readouterr()

    assert code == rotate_cli.EXIT_INTEGRITY_FAILURE
    assert "Audit installation: FAIL" in captured.out
    assert "{torn" not in (captured.out + captured.err)
    assert_no_leaks(
        [
            Artifact("rotate-fail-stdout", captured.out),
            Artifact("rotate-fail-stderr", captured.err),
        ]
    )


# ---------------------------------------------------------------------------
# F. Logging and exception sweep
# ---------------------------------------------------------------------------


def test_unavailable_error_never_carries_the_writer_message() -> None:
    service = leaky_service(
        AuditJournalUnavailableError(
            f"cannot open {DSN.value} for {USERNAME.value}"
        )
    )

    with pytest.raises(AuditUnavailableError) as caught:
        service.begin_mutation(
            action=AuditAction.EMPLOYEE_CREATE,
            target_id="E-1",
            context=request_context()[0],
        )

    message = str(caught.value)
    assert message == (
        "The operation was not attempted because it could not be audited."
    )
    assert caught.value.writer_error == "AuditJournalUnavailableError"
    assert caught.value.code == "AUDIT_UNAVAILABLE"
    assert_no_leaks([Artifact("unavailable-error", message)])


def test_result_uncertain_error_never_carries_the_writer_message(
    tmp_path: Path,
) -> None:
    clock = StepClock()
    delegate = AuditJournalWriter(journal_path(tmp_path), clock=clock)
    writer = FailAfterWriter(
        delegate,
        fail_from=2,
        exc=AuditJournalUnavailableError(
            f"append failed for {PASSWORD_HASH.value}"
        ),
    )
    service = AuditService(
        writer,  # type: ignore[arg-type]
        clock=clock,
        audit_id_factory=audit_ids(),
    )
    intent = service.begin_mutation(
        action=AuditAction.EMPLOYEE_CREATE,
        target_id="E-1",
        context=request_context()[0],
    )

    with pytest.raises(AuditResultUncertainError) as caught:
        service.record_applied(intent)

    message = str(caught.value)
    assert "could not be recorded" in message
    assert caught.value.writer_error == "AuditJournalUnavailableError"
    assert caught.value.code == "AUDIT_RESULT_UNCERTAIN"
    assert_no_leaks([Artifact("result-uncertain-error", message)])


def test_best_effort_warning_is_sanitized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = leaky_service(
        AuditJournalUnavailableError(f"append failed: {SESSION_TOKEN.value}")
    )

    with caplog.at_level(logging.WARNING):
        result = service.record_best_effort(
            lambda: service.record_login(
                outcome=AuditOutcome.SUCCESS,
                reason_code=AuditReasonCode.OK,
                context=request_context()[0],
            ),
            action=AuditAction.AUTH_LOGIN,
            request_id=REQUEST_ID,
        )

    assert result is None
    warnings = "\n".join(record.getMessage() for record in caplog.records)
    assert "audit best-effort write failed" in warnings
    assert "auth.login" in warnings
    assert "user-1" not in warnings
    assert_no_leaks([Artifact("best-effort-warning", warnings)])


def test_confirmed_not_applied_failure_warns_without_contents(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = StepClock()
    delegate = AuditJournalWriter(journal_path(tmp_path), clock=clock)
    writer = FailAfterWriter(
        delegate,
        fail_from=2,
        exc=AuditJournalUnavailableError(
            f"append failed: {REQUEST_BODY.value}"
        ),
    )
    service = AuditService(
        writer,  # type: ignore[arg-type]
        clock=clock,
        audit_id_factory=audit_ids(),
    )
    intent = service.begin_mutation(
        action=AuditAction.ATTENDANCE_CHECKOUT,
        target_id="E-1",
        context=request_context()[0],
    )

    with caplog.at_level(logging.WARNING):
        result = service.record_not_applied(
            intent, AuditReasonCode.NO_OPEN_SESSION
        )

    assert result is None
    warnings = "\n".join(record.getMessage() for record in caplog.records)
    assert "confirmed-not-applied" in warnings
    assert_no_leaks([Artifact("not-applied-warning", warnings)])


def test_rotation_failure_message_is_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_events(
        tmp_path, segments=[], active=("2026-11-001", 3), manifest=False
    )
    layout = AuditLayout.for_directory(tmp_path / "audit")

    def failing_replace(source: Path, target: Path) -> None:
        raise OSError(f"cannot rename {IMAGE_PATH.value}")

    monkeypatch.setattr(rotation_module, "_replace_file", failing_replace)

    with pytest.raises(RotationUnavailableError) as caught:
        RotationOperations(layout, clock=StepClock()).rotate()

    message = str(caught.value)
    assert "sentinel" not in message.lower()
    assert "OSError" not in message
    assert_no_leaks([Artifact("rotation-error", message)])


# ---------------------------------------------------------------------------
# G. Complete artifact sweep
# ---------------------------------------------------------------------------


def test_complete_artifact_sweep_is_clean(
    tmp_path: Path, capsys, caplog: pytest.LogCaptureFixture
) -> None:
    """Every artifact class at once: journal, manifest, archive, CLIs, logs, errors."""
    layout, operations = _rotate_service_journal(tmp_path)
    operations.record_retention(_sealed_segment_id(layout))

    # The sealed journal is empty again; append through the real writer so the
    # active journal is part of the same installation.
    service, _writer, _clock = build_service(
        tmp_path, start=BASE_TS + timedelta(seconds=60), first_id=100
    )
    record_representative_events(service)

    verify_code = verify_cli.main(["--journal", str(layout.journal)])
    verify_captured = capsys.readouterr()
    status_code = rotate_cli.main(
        ["--status", "--audit-dir", str(layout.directory)]
    )
    status_captured = capsys.readouterr()

    with caplog.at_level(logging.WARNING):
        leaky = leaky_service(
            AuditJournalUnavailableError(
                f"append failed: {COOKIE_VALUE.value}"
            )
        )
        leaky.record_best_effort(
            lambda: leaky.record_login(
                outcome=AuditOutcome.SUCCESS,
                reason_code=AuditReasonCode.OK,
                context=request_context()[0],
            ),
            action=AuditAction.AUTH_LOGIN,
            request_id=REQUEST_ID,
        )
        with pytest.raises(AuditUnavailableError) as caught:
            leaky.begin_mutation(
                action=AuditAction.EMPLOYEE_UPDATE,
                target_id="E-1",
                context=request_context()[0],
            )

    logs = "\n".join(record.getMessage() for record in caplog.records)
    archives = sorted(layout.archive_directory.glob("*.jsonl"))
    assert len(archives) == 1

    artifacts = [
        Artifact("active-journal", layout.journal.read_bytes()),
        Artifact("manifest", layout.manifest.read_bytes()),
        Artifact("archive", archives[0].read_bytes()),
        Artifact("verify-stdout", verify_captured.out),
        Artifact("verify-stderr", verify_captured.err),
        Artifact("rotate-stdout", status_captured.out),
        Artifact("rotate-stderr", status_captured.err),
        Artifact("application-log", logs),
        Artifact("sanitized-error", str(caught.value)),
    ]

    assert verify_code == verify_cli.EXIT_OK
    assert status_code == rotate_cli.EXIT_OK
    assert all(
        artifact.content
        for artifact in artifacts
        if not artifact.name.endswith("stderr")
    )
    assert len(artifacts) == 9
    assert_no_leaks(artifacts)
