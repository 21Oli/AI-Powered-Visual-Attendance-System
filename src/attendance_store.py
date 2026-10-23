"""Durable attendance-event store for the AI-Powered Visual Attendance
System (Phase 2 — P2-1).

Persists existing, already-validated attendance events — exactly the
:class:`~src.attendance.AttendanceEvent` values the Step 13 engine
produces — as **JSON Lines** under ``<DATA_DIR>/attendance/
events.jsonl`` and loads them back, in append order, after full
validation. This store is **storage only**: it performs no face
processing, no recognition, temporal verification, engine logic, or
camera work, and it is not wired into the live demo in this step. The
directory is created lazily inside a real append (never at import or
construction time), following the same explicit-side-effect convention
as the Step 09 embedding storage.

Storage format (single decision)
--------------------------------

One UTF-8 JSON object per line, fields exactly:

.. code-block:: json

    {"schema_version": 1, "event_id": "…", "employee_id": "…",
     "event_type": "check_in", "occurred_at": "2026-10-22T12:00:00+00:00"}

* ``schema_version`` — this on-disk journal schema (1 here; any other
  value is a typed load error, never a silent reinterpretation).
* ``event_id`` / ``employee_id`` — verbatim from the event (``"007"``
  stays ``"007"``).
* ``event_type`` — the :class:`~src.attendance.EventType` value.
* ``occurred_at`` — the event's UTC-normalized timestamp in ISO 8601.

**Privacy contract (enforced):** these five keys are the *only* keys a
record may carry. Loading rejects records with extra keys (whether
biometric, diagnostic, or merely future) and appending is the inverse:
only those five fields are ever serialized. No face images, crops,
embeddings, similarity scores, unknown-person observations, or
diagnostics are written or read by this store.

Append policy (safe append, not rewrite-per-event)
---------------------------------------------------

* ``append()`` opens the journal in ``"a"`` (O_APPEND) mode, writes the
  encoded record plus a newline, then ``flush()`` + ``os.fsync()`` of
  the file. The rest of the journal is never rewritten for a normal
  append — the whole-list rewrite cost would grow with journal size and
  was explicitly not chosen.
* A record counts as persisted **only after** ``fsync`` returns; any
  failure (creating the directory, appending, flushing, syncing) raises
  a typed :class:`AttendanceStorePersistenceError` and the caller must
  treat the event as *unconfirmed*.
* Honesty note (tested up to the injectable failure points): the
  journal may still physically contain an appended line if the OS lost
  durability after a partial flush — ``append`` cannot retract buffered
  data, so the persistence error does not guarantee the counterfactual
  "record is absent". There is also no process file locking: concurrent
  appends from multiple processes have interleave semantics decided by
  the OS, not by this store.

Torn-tail recovery (documented, never silent mid-file discards)
---------------------------------------------------------------

An interrupted append can leave an incomplete final line (no trailing
newline). The policy, applied in one place:

* **Loading** treats a torn final fragment leniently but *visibly*: it
  is excluded from the returned events and the load reports
  ``incomplete_trailing_record=True``. Corruption anywhere else — a
  malformed line, unknown schema version, invalid field, extra key —
  **always raises** :class:`CorruptAttendanceJournalError` with the
  exact line number; mid-file corruption is never silently skipped.
* **Appending** over a torn tail repairs it before the new record, in
  one of two document-and-tested ways:
  1. *Salvage*: if the fragment alone parses as a fully valid event
     record (a crash between a complete write and its newline/fsync),
     the repair is a pure append of the missing newline — the record is
     preserved intact.
  2. *Truncate*: otherwise the fragment is discarded by rewriting the
     journal with its complete-line prefix through the staged-temp-file
     → ``fsync`` → atomic ``os.replace`` pattern (the Step 09
     precedent), which keeps the repair crash-safe: a crash mid-repair
     leaves either the old bytes or the repaired prefix, never a
     mixture. A failed repair raises and leaves the journal unchanged;
     the append is not attempted.
  This is the **only** code path that rewrites the journal, it touches
  only the trailing fragment, and its failure semantics are tested.
* A missing journal file is the normal empty history (not corruption):
  loading returns no events. An empty-but-present or mid-file-empty
  line is *not* treated as an empty history — it is corruption.

Validation policy (write and load alike)
----------------------------------------

Records are trusted input neither on write nor load. Loading re-derives
a real :class:`~src.attendance.AttendanceEvent` through its own frozen
constructor (timezone-aware UTC timestamps, non-empty verbatim IDs),
after schema checks: exact key set, integer (non-bool) schema version,
known ``EventType`` value, parseable ISO-8601 aware timestamp. Naive
timestamps, missing fields, unknown event types, wrong types, and
duplicate/extra keys are typed load errors — never silently coerced,
repaired, or dropped.

Known limitations (never overclaimed)
--------------------------------------

* Appends are single-record durable (fsync) but this store is not a
  cross-process exactly-once mechanism: it neither locks the file
  against concurrent appenders nor de-duplicates ``event_id`` (a retry
  after an uncertain failure can produce two identical-event-id lines;
  the composition layer owns retry policy).
* ``os.fsync`` establishes durability of the file, not of the directory
  entry itself; the rename-publication path performs no directory
  fsync. This matches the existing storage precedent on the supported
  environments.
* No query/audit API yet (Phase 2 plan: a later report CLI reads the
  same journal read-only); no live-mode wiring in this step.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.attendance import AttendanceEvent, AttendanceInputError, EventType
from src.config import SETTINGS, AppSettings
from src.logging_setup import get_logger

__all__ = [
    "ATTENDANCE_JOURNAL_SCHEMA_VERSION",
    "JOURNAL_FILENAME",
    "AttendanceStoreError",
    "AttendanceStoreInputError",
    "AttendanceStorePersistenceError",
    "CorruptAttendanceJournalError",
    "UnsupportedJournalSchemaVersionError",
    "JournalLoadResult",
    "AttendanceEventStore",
]

_logger = get_logger(__name__)

#: On-disk journal schema version. Bump only with a documented migration.
ATTENDANCE_JOURNAL_SCHEMA_VERSION: int = 1

#: Journal filename under the derived attendance directory.
JOURNAL_FILENAME: str = "events.jsonl"

#: The exact keys one journal record may carry — the privacy envelope.
_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"schema_version", "event_id", "employee_id", "event_type", "occurred_at"}
)


# ---------------------------------------------------------------------------
# Errors (typed, actionable; loading never degrades to silently empty)
# ---------------------------------------------------------------------------


class AttendanceStoreError(Exception):
    """Base class for attendance-store errors."""


class AttendanceStoreInputError(AttendanceStoreError, ValueError):
    """Raised when an append/construct request violates the input contract."""


class AttendanceStorePersistenceError(AttendanceStoreError):
    """Raised when writing (appending, repairing, syncing) fails.

    The event involved must be treated as **unconfirmed**: ``fsync``
    did not complete, and the store cannot promise the record is
    absent (buffer integration is outside its control) — retries are
    the composition layer's decision.
    """


class CorruptAttendanceJournalError(AttendanceStoreError):
    """Raised when the journal is unreadable or a record fails validation.

    The message names the exact journal line. Loading never silently
    discards such records; the only leniency ever applied is to the
    incomplete trailing fragment (documented, and reported on the load
    result, not silently).
    """


class UnsupportedJournalSchemaVersionError(CorruptAttendanceJournalError):
    """Raised when a record's ``schema_version`` is not the supported one."""


# ---------------------------------------------------------------------------
# Load result (frozen; explicit incomplete-tail reporting)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JournalLoadResult:
    """Outcome of loading the journal (explicit, never ambiguous).

    Attributes:
        events: All validated events, in journal (append) order. An
            incomplete trailing fragment is **not** among them.
        incomplete_trailing_record: ``True`` when the journal ended
            without a newline and the torn fragment was excluded as
            documented — never silently.
    """

    events: tuple[AttendanceEvent, ...]
    incomplete_trailing_record: bool = False


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class AttendanceEventStore:
    """Append-only JSON Lines journal over :class:`AttendanceEvent`.

    Args:
        settings: :class:`~src.config.AppSettings` providing
            ``attendance_dir`` (``<DATA_DIR>/attendance`` by composition
            with the project's data-directory convention). Construction
            performs no filesystem side effects; the directory is
            created lazily by the first real ``append``.

    Raises:
        AttendanceStoreInputError: ``settings`` is not an
            ``AppSettings``.
    """

    def __init__(self, settings: AppSettings = SETTINGS) -> None:
        if not isinstance(settings, AppSettings):
            raise AttendanceStoreInputError(
                "settings must be an AppSettings, got "
                f"{type(settings).__name__}"
            )
        self._settings = settings

    # ------------------------------------------------------------------
    # Path (derived, never invented)
    # ------------------------------------------------------------------

    @property
    def journal_path(self) -> Path:
        """Absolute journal path for the configured data directory."""
        return self._settings.attendance_dir / JOURNAL_FILENAME

    # ------------------------------------------------------------------
    # Append: validate → (torn-tail repair, if needed) → O_APPEND + fsync
    # ------------------------------------------------------------------

    def append(self, event: AttendanceEvent) -> Path:
        """Persist one already-validated event; return the journal path.

        The event object is validated here (must be a real
        ``AttendanceEvent``; its frozen constructor already enforced the
        timestamp/ID contract). Exactly the five schema keys are
        serialized. The record is durable (fsync completed) when this
        returns without raising.

        Raises:
            AttendanceStoreInputError: ``event`` is not an
                ``AttendanceEvent``, or the event's fields cannot be
                represented in the store's UTF-8 journal encoding (for
                example an ID carrying a lone Unicode surrogate — such
                strings pass event construction but cannot be encoded).
                In both cases nothing is written and nothing is
                repaired.
            AttendanceStorePersistenceError: directory creation, torn-tail
                repair, the append write, flush, or fsync failed (see the
                module docstring for the unconfirmed-record honesty note).
        """
        if not isinstance(event, AttendanceEvent):
            raise AttendanceStoreInputError(
                "event must be an AttendanceEvent, got "
                f"{type(event).__name__}"
            )
        payload = {
            "schema_version": ATTENDANCE_JOURNAL_SCHEMA_VERSION,
            "event_id": event.event_id,
            "employee_id": event.employee_id,
            "event_type": event.event_type.value,
            "occurred_at": event.occurred_at.isoformat(),
        }
        line = json.dumps(payload, ensure_ascii=False)

        journal = self.journal_path
        try:
            journal.parent.mkdir(parents=True, exist_ok=True)
            self._repair_trailing_fragment(journal)
            with open(journal, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except AttendanceStoreError:
            raise
        except UnicodeEncodeError as exc:
            # Invalid input, not persistence: the event passed frozen
            # event construction, but its fields cannot be represented
            # in the store's UTF-8 journal encoding (e.g. an ID carrying
            # a lone surrogate — Python encodes before buffering, so no
            # partial bytes reached the journal). Raise the documented
            # typed input error with the original cause preserved; never
            # swallow, never mislabel as an I/O failure.
            raise AttendanceStoreInputError(
                "event fields are not representable in the UTF-8 journal "
                f"encoding for event {event.event_id!r}: {exc}"
            ) from exc
        except OSError as exc:  # noqa: BLE001 - wrap filesystem failure
            raise AttendanceStorePersistenceError(
                f"attendance journal append failed for event "
                f"{event.event_id!r}: {exc}"
            ) from exc
        _logger.info(
            "attendance event persisted (journal lines bumped: id=%s, "
            "type=%s)",
            event.event_id,
            event.event_type.value,
        )
        return journal

    # ------------------------------------------------------------------
    # Load: treat stored lines as untrusted input, fail loudly mid-file
    # ------------------------------------------------------------------

    def load_events(self) -> JournalLoadResult:
        """Load all validated events in append order.

        A missing journal file is the normalized empty history (no
        events). Any complete line that fails JSON/schema/event
        validation raises with its exact line number — mid-file
        corruption is never silently skipped. An incomplete trailing
        fragment (a torn final line after an interrupted append) is
        excluded leniently *and reported* on the result.

        Raises:
            CorruptAttendanceJournalError: the journal is unreadable, or
                any complete line fails validation (including unknown
                schema versions — the typed subclass names it).
            UnsupportedJournalSchemaVersionError: a record carries a
                future/unknown ``schema_version``.

        Documented boundary: a *directory* (or any non-regular file) at
        the journal path is not a journal — loading normalizes it to
        the empty history exactly like a missing file; a later append
        raises the typed persistence error (the path cannot be opened)
        without writing anything.
        """
        journal = self.journal_path
        if not journal.is_file():
            # Nothing recorded yet is history-normal, not corruption.
            return JournalLoadResult(events=(), incomplete_trailing_record=False)
        try:
            text = journal.read_text(encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 - unreadable journal is corrupt
            raise CorruptAttendanceJournalError(
                f"attendance journal {journal} is unreadable: {exc}"
            ) from exc
        except UnicodeDecodeError as exc:
            raise CorruptAttendanceJournalError(
                f"attendance journal {journal} is not valid UTF-8: {exc}"
            ) from exc

        lines = text.split("\n")
        incomplete_trailing = False
        if text and not text.endswith("\n"):
            # Torn final fragment after an interrupted append: a
            # complete final line that only lacks its newline is still
            # "the line", but distinguishing is unnecessary here — the
            # trailing non-newline fragment is uniformly excluded and
            # reported; append() salvages it when it is a valid record.
            incomplete_trailing = True
            lines = lines[:-1]
        elif lines and lines[-1] == "":
            # Normal trailing newline produced one empty split element.
            lines = lines[:-1]

        events: list[AttendanceEvent] = []
        for lineno, raw in enumerate(lines, start=1):
            events.append(self._parse_line(raw, lineno))
        _logger.info(
            "attendance journal loaded: %d events (incomplete_trailing=%s)",
            len(events),
            incomplete_trailing,
        )
        return JournalLoadResult(
            events=tuple(events), incomplete_trailing_record=incomplete_trailing
        )

    # ------------------------------------------------------------------
    # Internal: torn-tail repair (salvage-by-append or truncate-prefix)
    # ------------------------------------------------------------------

    def _repair_trailing_fragment(self, journal: Path) -> bool:
        """Repair a torn trailing fragment, if any; return whether repaired.

        * A complete trailing record that merely lacks its newline is
          salvaged by appending the newline (pure append; the record is
          preserved) — returns ``True``.
        * A torn partial record is cut at the last newline and the
          complete-line prefix is republished via staged temp file →
          fsync → atomic ``os.replace`` — returns ``True``.
        * An intact journal (ends with a newline, or does not exist)
          needs nothing — returns ``False`` (the normal fast path; no
          rewrite is ever performed).

        Raises:
            AttendanceStorePersistenceError: inspection or repair I/O
                failed; the journal is left as found.
        """
        try:
            if not journal.is_file():
                return False
            size = journal.stat().st_size
            if size == 0:
                return False
            with open(journal, "rb") as handle:
                handle.seek(-1, os.SEEK_END)
                last_byte = handle.read(1)
            if last_byte == b"\n":
                return False
            data = journal.read_bytes()
        except OSError as exc:  # noqa: BLE001 - wrap inspection failure
            raise AttendanceStorePersistenceError(
                f"attendance journal torn-tail inspection failed: {exc}"
            ) from exc

        cut = data.rfind(b"\n")
        fragment = data[cut + 1 :] if cut != -1 else data

        # Salvage: the fragment alone is a fully valid event record →
        # the "repair" is a one-byte append completing it. Nothing of
        # the record is discarded this way.
        if self._fragment_is_salvageable(journal, fragment):
            try:
                with open(journal, "a", encoding="utf-8") as handle:
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:  # noqa: BLE001
                raise AttendanceStorePersistenceError(
                    f"attendance journal torn-tail salvage failed: {exc}"
                ) from exc
            _logger.info(
                "attendance journal torn tail salvaged (complete record "
                "missing only its newline)"
            )
            return True

        # Truncate: rewrite only the complete-line prefix through the
        # staged-temp-file → fsync → atomic os.replace pattern so a
        # crash mid-repair leaves either the old bytes or the repaired
        # prefix, never a mixture. This is the store's only rewrite path.
        prefix = data[: cut + 1] if cut != -1 else b""
        repair_tmp = (
            journal.parent / f"events.repair-{uuid.uuid4().hex[:12]}.tmp"
        )
        try:
            repair_tmp.parent.mkdir(parents=True, exist_ok=True)
            with open(repair_tmp, "wb") as handle:
                handle.write(prefix)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(repair_tmp, journal)
        except OSError as exc:  # noqa: BLE001
            repair_tmp.unlink(missing_ok=True)
            raise AttendanceStorePersistenceError(
                f"attendance journal torn-tail repair failed (journal "
                f"left as found): {exc}"
            ) from exc
        finally:
            # Publish remnants (published ones no longer exist).
            repair_tmp.unlink(missing_ok=True)
        _logger.info(
            "attendance journal torn tail truncated (incomplete trailing "
            "fragment discarded by documented repair)"
        )
        return True

    def _fragment_is_salvageable(self, journal: Path, fragment: bytes) -> bool:
        """True when the trailing fragment is a fully valid event record."""
        try:
            text = fragment.decode("utf-8")
        except UnicodeDecodeError:
            return False
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return False
        try:
            self._parse_payload(payload)
        except AttendanceStoreError:
            return False
        return True

    # ------------------------------------------------------------------
    # Internal: one-line → one-event validation (write=load contract)
    # ------------------------------------------------------------------

    def _parse_line(self, raw: str, lineno: int) -> AttendanceEvent:
        """Parse and validate one complete journal line (untrusted input)."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CorruptAttendanceJournalError(
                f"attendance journal line {lineno} is not valid JSON: {exc}"
            ) from exc
        try:
            return self._parse_payload(payload)
        except UnsupportedJournalSchemaVersionError:
            raise UnsupportedJournalSchemaVersionError(
                f"attendance journal line {lineno} carries an unsupported "
                f"schema version; supported version is "
                f"{ATTENDANCE_JOURNAL_SCHEMA_VERSION} "
                f"(record detail: {_describe_schema_problem(payload)})"
            ) from None
        except AttendanceStoreError as exc:
            raise CorruptAttendanceJournalError(
                f"attendance journal line {lineno} fails validation: {exc}"
            ) from exc

    def _parse_payload(self, payload: object) -> AttendanceEvent:
        """Validate one record payload against the exact store contract."""
        if not isinstance(payload, dict):
            raise CorruptAttendanceJournalError(
                f"record must be a JSON object, got {type(payload).__name__}"
            )
        keys = set(payload)
        unknown = keys - _ALLOWED_KEYS
        missing = _ALLOWED_KEYS - keys
        if unknown:
            # Enforced privacy envelope: no field outside the five
            # documented ones is ever accepted, whatever it carries.
            raise CorruptAttendanceJournalError(
                "record carries keys outside the documented schema "
                f"(refusing the record, not extra data): "
                f"{', '.join(sorted(unknown))}"
            )
        if missing:
            raise CorruptAttendanceJournalError(
                f"record is missing required keys: "
                f"{', '.join(sorted(missing))}"
            )

        version = payload["schema_version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise CorruptAttendanceJournalError(
                "record 'schema_version' must be an int (bool rejected), "
                f"got {version!r}"
            )
        if version != ATTENDANCE_JOURNAL_SCHEMA_VERSION:
            raise UnsupportedJournalSchemaVersionError(
                f"record 'schema_version' {version!r} is not the supported "
                f"version {ATTENDANCE_JOURNAL_SCHEMA_VERSION}"
            )

        event_id = payload["event_id"]
        if not isinstance(event_id, str) or not event_id:
            raise CorruptAttendanceJournalError(
                f"record 'event_id' must be a non-empty str, got "
                f"{type(event_id).__name__}"
            )
        employee_id = payload["employee_id"]
        if not isinstance(employee_id, str) or not employee_id:
            raise CorruptAttendanceJournalError(
                f"record 'employee_id' must be a non-empty str, got "
                f"{type(employee_id).__name__}"
            )

        event_type_raw = payload["event_type"]
        if not isinstance(event_type_raw, str):
            raise CorruptAttendanceJournalError(
                f"record 'event_type' must be a str EventType value, got "
                f"{type(event_type_raw).__name__}"
            )
        try:
            event_type = EventType(event_type_raw)
        except ValueError as exc:
            raise CorruptAttendanceJournalError(
                f"record 'event_type' {event_type_raw!r} is not a known "
                "EventType value (unknown event types are never mapped "
                "to a best guess)"
            ) from exc

        occurred_at_raw = payload["occurred_at"]
        if not isinstance(occurred_at_raw, str):
            raise CorruptAttendanceJournalError(
                f"record 'occurred_at' must be an ISO-8601 str, got "
                f"{type(occurred_at_raw).__name__}"
            )
        try:
            occurred_at = datetime.fromisoformat(occurred_at_raw)
        except ValueError as exc:
            raise CorruptAttendanceJournalError(
                f"record 'occurred_at' {occurred_at_raw!r} is not a "
                "parseable ISO-8601 datetime"
            ) from exc
        # The frozen AttendanceEvent constructor re-enforces the full
        # event contract (naive timestamps rejected, UTC normalization,
        # non-empty verbatim IDs) — the journal never relaxes it.
        try:
            return AttendanceEvent(
                event_id=event_id,
                employee_id=employee_id,
                event_type=event_type,
                occurred_at=occurred_at,
            )
        except AttendanceInputError as exc:
            raise CorruptAttendanceJournalError(
                f"record fields violate the AttendanceEvent contract: {exc}"
            ) from exc


def _describe_schema_problem(payload: object) -> str:
    """Short, non-biometric description used for schema-version errors."""
    if isinstance(payload, dict):
        version = payload.get("schema_version")
        return f"record with {len(payload)} keys, schema_version={version!r}"
    return f"payload of type {type(payload).__name__}"
