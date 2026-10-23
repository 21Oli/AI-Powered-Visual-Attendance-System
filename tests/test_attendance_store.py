"""Hardware-free tests for the durable attendance event store (Phase 2,
P2-1 — ``src/attendance_store.py``).

All tests are deterministic: fixed UTC-aware datetimes, no sleeps, no
camera, no model files, no real biometric data — journals live in
``pytest`` temporary directories and contain synthetic event records
only.
"""

from __future__ import annotations

import json
import os
from dataclasses import is_dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.attendance import AttendanceEvent, EventType
from src.attendance_store import (
    ATTENDANCE_JOURNAL_SCHEMA_VERSION,
    JOURNAL_FILENAME,
    AttendanceEventStore,
    AttendanceStoreError,
    AttendanceStoreInputError,
    AttendanceStorePersistenceError,
    CorruptAttendanceJournalError,
    JournalLoadResult,
    UnsupportedJournalSchemaVersionError,
)
from src.config import AppSettings

_T0 = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)

_EVENT_ID_A = "ev-0001"
_EVENT_ID_B = "ev-0002"
_EVENT_ID_C = "ev-0003"
_EMPLOYEE = "EMP-001"
_EMPLOYEE_VERBATIM = "007"


def _make_store(tmp_path: Path) -> AttendanceEventStore:
    return AttendanceEventStore(settings=AppSettings(data_dir=tmp_path))


def _event(
    event_id: str = _EVENT_ID_A,
    employee_id: str = _EMPLOYEE,
    event_type: EventType = EventType.CHECK_IN,
    occurred_at: datetime | None = None,
) -> AttendanceEvent:
    return AttendanceEvent(
        event_id=event_id,
        employee_id=employee_id,
        event_type=event_type,
        occurred_at=occurred_at or _T0,
    )


def _write_journal(tmp_path: Path, text: str) -> None:
    journal_dir = tmp_path / "attendance"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / JOURNAL_FILENAME).write_text(text, encoding="utf-8")


def _read_journal(tmp_path: Path) -> str:
    return (tmp_path / "attendance" / JOURNAL_FILENAME).read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Construction and paths
# ---------------------------------------------------------------------------


class TestConstructionAndPaths:
    def test_rejects_non_settings(self) -> None:
        with pytest.raises(AttendanceStoreInputError):
            AttendanceEventStore(settings="not settings")  # type: ignore[arg-type]

    def test_construction_has_no_filesystem_side_effects(
        self, tmp_path: Path
    ) -> None:
        store = _make_store(tmp_path)
        assert store.journal_path == tmp_path / "attendance" / JOURNAL_FILENAME
        assert not (tmp_path / "attendance").exists()

    def test_load_missing_journal_is_empty_history(self, tmp_path: Path) -> None:
        result = _make_store(tmp_path).load_events()
        assert result.events == ()
        assert result.incomplete_trailing_record is False

    def test_load_zero_byte_journal_is_empty_history(
        self, tmp_path: Path
    ) -> None:
        _write_journal(tmp_path, "")
        result = _make_store(tmp_path).load_events()
        assert result.events == ()
        assert result.incomplete_trailing_record is False

    def test_load_journal_path_directory_is_empty_history(
        self, tmp_path: Path
    ) -> None:
        # Documented boundary: a directory at the journal path is not a
        # journal file; loading normalizes it to the empty history
        # exactly like a missing file (and appending raises typed —
        # pinned in TestAppendFailures).
        (tmp_path / "attendance" / JOURNAL_FILENAME).mkdir(parents=True)
        result = _make_store(tmp_path).load_events()
        assert result.events == ()
        assert result.incomplete_trailing_record is False


# ---------------------------------------------------------------------------
# Append basics and round-trip
# ---------------------------------------------------------------------------


class TestAppendAndRoundTrip:
    def test_first_append_creates_directory_and_journal(
        self, tmp_path: Path
    ) -> None:
        store = _make_store(tmp_path)
        returned = store.append(_event())
        assert returned == tmp_path / "attendance" / JOURNAL_FILENAME
        assert (tmp_path / "attendance").is_dir()
        lines = _read_journal(tmp_path).splitlines()
        assert len(lines) == 1

    def test_multiple_appends_preserve_order(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        for index, event_id in enumerate(
            (_EVENT_ID_A, _EVENT_ID_B, _EVENT_ID_C), start=1
        ):
            store.append(
                _event(
                    event_id=event_id,
                    occurred_at=_T0 + timedelta(seconds=index),
                )
            )
        result = store.load_events()
        assert [event.event_id for event in result.events] == [
            _EVENT_ID_A,
            _EVENT_ID_B,
            _EVENT_ID_C,
        ]
        assert result.incomplete_trailing_record is False

    @pytest.mark.parametrize("event_type", list(EventType))
    def test_roundtrip_each_event_type(
        self, tmp_path: Path, event_type: EventType
    ) -> None:
        store = _make_store(tmp_path)
        store.append(_event(event_type=event_type))
        (loaded,) = store.load_events().events
        assert loaded.event_type is event_type
        assert loaded.event_id == _EVENT_ID_A
        assert loaded.employee_id == _EMPLOYEE

    def test_roundtrip_preserves_ids_and_timestamp_verbatim(
        self, tmp_path: Path
    ) -> None:
        store = _make_store(tmp_path)
        instant = datetime(2026, 10, 22, 13, 45, 12, 345000, tzinfo=UTC)
        store.append(
            _event(
                event_id="f9c11a2b",
                employee_id=_EMPLOYEE_VERBATIM,
                occurred_at=instant,
            )
        )
        (loaded,) = store.load_events().events
        assert loaded.event_id == "f9c11a2b"
        assert loaded.employee_id == _EMPLOYEE_VERBATIM
        assert loaded.occurred_at == instant

    def test_roundtrip_normalizes_offset_timestamp_to_utc(
        self, tmp_path: Path
    ) -> None:
        equally_utc = timezone(timedelta(hours=2))
        store = _make_store(tmp_path)
        store.append(
            _event(occurred_at=datetime(2026, 10, 22, 14, 0, tzinfo=equally_utc))
        )
        (loaded,) = store.load_events().events
        assert loaded.occurred_at == datetime(
            2026, 10, 22, 12, 0, tzinfo=UTC
        )

    def test_load_is_read_only(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.append(_event())
        before = (tmp_path / "attendance" / JOURNAL_FILENAME).read_bytes()
        store.load_events()
        after = (tmp_path / "attendance" / JOURNAL_FILENAME).read_bytes()
        assert before == after


# ---------------------------------------------------------------------------
# Rejected appends and write/repair failures
# ---------------------------------------------------------------------------


class TestAppendFailures:
    def test_append_rejects_non_event_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        store = _make_store(tmp_path)
        with pytest.raises(AttendanceStoreInputError):
            store.append({"event_id": "nope"})  # type: ignore[arg-type]
        assert not (tmp_path / "attendance").exists()

    def test_append_wraps_fsync_failure_as_persistence_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _make_store(tmp_path)
        store.append(_event())
        real_fsync = os.fsync

        calls = {"n": 0}

        def failing_fsync(fd: int) -> None:
            calls["n"] += 1
            raise OSError("sync lost")

        monkeypatch.setattr(os, "fsync", failing_fsync)
        with pytest.raises(AttendanceStorePersistenceError):
            store.append(
                _event(event_id=_EVENT_ID_B, occurred_at=_T0 + timedelta(1))
            )
        assert calls["n"] == 1
        monkeypatch.setattr(os, "fsync", real_fsync)
        # Honesty note from the module docstring, pinned by test: the
        # write succeeded before fsync, so the record *is* present even
        # though the caller was told it is unconfirmed — and the older
        # confirmed records still load.
        result = store.load_events()
        assert result.incomplete_trailing_record is False
        assert [event.event_id for event in result.events] == [
            _EVENT_ID_A,
            _EVENT_ID_B,
        ]

    def test_append_wraps_directory_creation_failure(
        self, tmp_path: Path
    ) -> None:
        # The attendance "directory" exists as a regular file, so the
        # lazy mkdir fails in a real (unmocked) environment.
        (tmp_path / "attendance").write_text("not a dir", encoding="utf-8")
        store = _make_store(tmp_path)
        with pytest.raises(AttendanceStorePersistenceError):
            store.append(_event())

    @pytest.mark.parametrize("field", ["employee_id", "event_id"])
    def test_append_rejects_lone_surrogate_id_with_typed_error(
        self, tmp_path: Path, field: str
    ) -> None:
        # A lone surrogate passes frozen event construction (any
        # non-empty str) but cannot be encoded as UTF-8 JSON Lines. The
        # store must raise the documented typed *input* error, preserve
        # the encoding failure as the cause, and leave the journal —
        # including previously persisted records — byte-identical (no
        # partial data may reach the file).
        store = _make_store(tmp_path)
        store.append(_event())
        journal = tmp_path / "attendance" / JOURNAL_FILENAME
        before = journal.read_bytes()
        kwargs = {field: "a\udcffb"}
        target = _event(
            event_id=kwargs.get("event_id", _EVENT_ID_B),
            employee_id=kwargs.get("employee_id", _EMPLOYEE),
        )
        with pytest.raises(AttendanceStoreInputError) as excinfo:
            store.append(target)
        assert "UTF-8 journal encoding" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, UnicodeEncodeError)
        assert journal.read_bytes() == before
        # The journal still loads cleanly with exactly the prior record.
        result = store.load_events()
        assert [event.event_id for event in result.events] == [_EVENT_ID_A]
        assert result.incomplete_trailing_record is False

    def test_append_to_journal_path_directory_raises_typed_error(
        self, tmp_path: Path
    ) -> None:
        # A directory at the journal path is not a journal (documented
        # boundary): appends fail typed, nothing is written, and the
        # directory is left untouched.
        journal = tmp_path / "attendance" / JOURNAL_FILENAME
        journal.mkdir(parents=True)
        store = _make_store(tmp_path)
        with pytest.raises(AttendanceStorePersistenceError):
            store.append(_event())
        assert journal.is_dir()


# ---------------------------------------------------------------------------
# Privacy contract
# ---------------------------------------------------------------------------


_ALLOWED_KEY_NAMES = {
    "schema_version",
    "event_id",
    "employee_id",
    "event_type",
    "occurred_at",
}


class TestPrivacyContract:
    def test_serialized_records_carry_exactly_the_documented_keys(
        self, tmp_path: Path
    ) -> None:
        store = _make_store(tmp_path)
        store.append(_event())
        records = [
            json.loads(line)
            for line in _read_journal(tmp_path).splitlines()
        ]
        assert records
        for record in records:
            assert set(record) == _ALLOWED_KEY_NAMES
            assert record["schema_version"] == ATTENDANCE_JOURNAL_SCHEMA_VERSION
        # The schema-version constant and file pin the on-disk contract.
        assert ATTENDANCE_JOURNAL_SCHEMA_VERSION == 1
        assert JOURNAL_FILENAME == "events.jsonl"


# ---------------------------------------------------------------------------
# Load validation (untrusted stored lines)
# ---------------------------------------------------------------------------


def _valid_record(event_id: str = _EVENT_ID_A, **overrides: object) -> str:
    payload = {
        "schema_version": ATTENDANCE_JOURNAL_SCHEMA_VERSION,
        "event_id": event_id,
        "employee_id": _EMPLOYEE,
        "event_type": EventType.CHECK_IN.value,
        "occurred_at": _T0.isoformat(),
    }
    payload.update(overrides)  # type: ignore[arg-type]
    return json.dumps(payload)


class TestLoadValidation:
    def test_malformed_json_raises_with_line_number(
        self, tmp_path: Path
    ) -> None:
        _write_journal(
            tmp_path,
            _valid_record(_EVENT_ID_A) + "\nnot json at all\n",
        )
        with pytest.raises(CorruptAttendanceJournalError) as excinfo:
            _make_store(tmp_path).load_events()
        assert "line 2" in str(excinfo.value)

    def test_mid_file_corruption_raises_even_with_torn_tail(
        self, tmp_path: Path
    ) -> None:
        _write_journal(
            tmp_path,
            _valid_record(_EVENT_ID_A)
            + "\n{broken\n"
            + _valid_record(_EVENT_ID_C),
        )
        with pytest.raises(CorruptAttendanceJournalError) as excinfo:
            _make_store(tmp_path).load_events()
        assert "line 2" in str(excinfo.value)

    def test_empty_line_mid_journal_raises(self, tmp_path: Path) -> None:
        _write_journal(tmp_path, _valid_record(_EVENT_ID_A) + "\n\n")
        with pytest.raises(CorruptAttendanceJournalError) as excinfo:
            _make_store(tmp_path).load_events()
        assert "line 2" in str(excinfo.value)

    def test_unknown_schema_version_raises_typed_error(
        self, tmp_path: Path
    ) -> None:
        _write_journal(tmp_path, _valid_record(schema_version=2) + "\n")
        with pytest.raises(UnsupportedJournalSchemaVersionError):
            _make_store(tmp_path).load_events()

    @pytest.mark.parametrize("version", ["1", True, None, 1.0])
    def test_schema_version_of_invalid_type_raises(
        self, tmp_path: Path, version: object
    ) -> None:
        _write_journal(tmp_path, _valid_record(schema_version=version) + "\n")
        with pytest.raises(CorruptAttendanceJournalError):
            _make_store(tmp_path).load_events()

    def test_missing_key_raises(self, tmp_path: Path) -> None:
        record = json.loads(_valid_record())
        del record["occurred_at"]
        _write_journal(tmp_path, json.dumps(record) + "\n")
        with pytest.raises(CorruptAttendanceJournalError) as excinfo:
            _make_store(tmp_path).load_events()
        assert "occurred_at" in str(excinfo.value)

    def test_unknown_extra_key_raises(self, tmp_path: Path) -> None:
        # Privacy envelope: an extra key is refused even when innocuous.
        _write_journal(
            tmp_path, _valid_record(similarity_score=0.99) + "\n"
        )
        with pytest.raises(CorruptAttendanceJournalError) as excinfo:
            _make_store(tmp_path).load_events()
        assert "similarity_score" in str(excinfo.value)

    def test_unknown_event_type_raises(self, tmp_path: Path) -> None:
        _write_journal(tmp_path, _valid_record(event_type="check_out") + "\n")
        with pytest.raises(CorruptAttendanceJournalError):
            _make_store(tmp_path).load_events()

    def test_event_type_of_wrong_type_raises(self, tmp_path: Path) -> None:
        _write_journal(tmp_path, _valid_record(event_type=7) + "\n")
        with pytest.raises(CorruptAttendanceJournalError):
            _make_store(tmp_path).load_events()

    def test_naive_timestamp_raises(self, tmp_path: Path) -> None:
        naive = _T0.replace(tzinfo=None).isoformat()
        _write_journal(tmp_path, _valid_record(occurred_at=naive) + "\n")
        with pytest.raises(CorruptAttendanceJournalError):
            _make_store(tmp_path).load_events()

    def test_unparseable_timestamp_raises(self, tmp_path: Path) -> None:
        _write_journal(tmp_path, _valid_record(occurred_at="not-a-date") + "\n")
        with pytest.raises(CorruptAttendanceJournalError):
            _make_store(tmp_path).load_events()

    @pytest.mark.parametrize("field", ["event_id", "employee_id"])
    def test_empty_identity_fields_raise(
        self, tmp_path: Path, field: str
    ) -> None:
        _write_journal(tmp_path, _valid_record(**{field: ""}) + "\n")
        with pytest.raises(CorruptAttendanceJournalError):
            _make_store(tmp_path).load_events()

    @pytest.mark.parametrize("field", ["event_id", "employee_id"])
    def test_identity_fields_of_wrong_type_raise(
        self, tmp_path: Path, field: str
    ) -> None:
        _write_journal(tmp_path, _valid_record(**{field: 42}) + "\n")
        with pytest.raises(CorruptAttendanceJournalError):
            _make_store(tmp_path).load_events()

    def test_non_object_record_raises(self, tmp_path: Path) -> None:
        _write_journal(tmp_path, "[1, 2, 3]\n")
        with pytest.raises(CorruptAttendanceJournalError):
            _make_store(tmp_path).load_events()

    def test_non_utf8_journal_raises(self, tmp_path: Path) -> None:
        journal_dir = tmp_path / "attendance"
        journal_dir.mkdir(parents=True)
        (journal_dir / JOURNAL_FILENAME).write_bytes(b"\xff\xfe\x00")
        with pytest.raises(CorruptAttendanceJournalError):
            _make_store(tmp_path).load_events()

    def test_frozen_load_result_carries_explicit_flags(self) -> None:
        assert is_dataclass(JournalLoadResult)
        result = JournalLoadResult(events=(), incomplete_trailing_record=False)
        with pytest.raises((AttributeError, TypeError)):
            result.events = ()  # frozen: assignment must fail
        assert result.incomplete_trailing_record is False


# ---------------------------------------------------------------------------
# Incomplete trailing line (torn tail): load leniency is visible,
# append repair is salvage-or-truncate
# ---------------------------------------------------------------------------


class TestIncompleteTrailingFragment:
    def test_load_excludes_torn_fragment_and_reports_it(
        self, tmp_path: Path
    ) -> None:
        _write_journal(
            tmp_path, _valid_record(_EVENT_ID_A) + "\n" + '{"partial rec'
        )
        result = _make_store(tmp_path).load_events()
        assert [event.event_id for event in result.events] == [_EVENT_ID_A]
        assert result.incomplete_trailing_record is True

    def test_load_reports_no_incomplete_when_journal_ends_with_newline(
        self, tmp_path: Path
    ) -> None:
        _write_journal(tmp_path, _valid_record(_EVENT_ID_A) + "\n")
        result = _make_store(tmp_path).load_events()
        assert result.incomplete_trailing_record is False

    def test_load_valid_record_missing_only_newline_is_excluded_and_flagged(
        self, tmp_path: Path
    ) -> None:
        # The load-side boundary of the salvage policy: a trailing
        # fragment that is a *fully valid* record (missing only its
        # newline) is still excluded from the events and reported —
        # the leniency boundary is the newline, never the content.
        _write_journal(
            tmp_path,
            _valid_record(_EVENT_ID_A) + "\n" + _valid_record(_EVENT_ID_B),
        )
        result = _make_store(tmp_path).load_events()
        assert [event.event_id for event in result.events] == [_EVENT_ID_A]
        assert result.incomplete_trailing_record is True

    def test_append_salvages_complete_fragment_missing_only_newline(
        self, tmp_path: Path
    ) -> None:
        # A crash between the complete write and its newline/fsync: the
        # trailing record is fully valid without its newline — repair
        # must preserve it, not truncate it.
        torn_tail = (
            _valid_record(_EVENT_ID_A)
            + "\n"
            + _valid_record(_EVENT_ID_B)  # complete record, no newline
        )
        _write_journal(tmp_path, torn_tail)
        store = _make_store(tmp_path)
        store.append(_event(event_id=_EVENT_ID_C, occurred_at=_T0))
        result = store.load_events()
        assert [event.event_id for event in result.events] == [
            _EVENT_ID_A,
            _EVENT_ID_B,
            _EVENT_ID_C,
        ]
        assert result.incomplete_trailing_record is False

    def test_append_truncates_unsalvageable_fragment(
        self, tmp_path: Path
    ) -> None:
        _write_journal(
            tmp_path, _valid_record(_EVENT_ID_A) + "\n" + '{"event_id": "to'
        )
        store = _make_store(tmp_path)
        store.append(_event(event_id=_EVENT_ID_C, occurred_at=_T0))
        result = store.load_events()
        assert [event.event_id for event in result.events] == [
            _EVENT_ID_A,
            _EVENT_ID_C,
        ]
        assert result.incomplete_trailing_record is False
        journal_text = _read_journal(tmp_path)
        assert "torn" not in journal_text  # the fragment text is gone

    def test_failed_truncate_repair_leaves_journal_unchanged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_journal(tmp_path, _valid_record(_EVENT_ID_A) + "\n" + "queue")
        journal_file = tmp_path / "attendance" / JOURNAL_FILENAME
        before = journal_file.read_bytes()
        real_replace = os.replace

        def failing_replace(src: object, dst: object) -> None:
            raise OSError("replace fails")

        monkeypatch.setattr(os, "replace", failing_replace)
        store = _make_store(tmp_path)
        with pytest.raises(AttendanceStorePersistenceError) as excinfo:
            store.append(_event(event_id=_EVENT_ID_C, occurred_at=_T0))
        assert "left as found" in str(excinfo.value)
        monkeypatch.setattr(os, "replace", real_replace)
        # Journal bytes unchanged; torn tail still flagged, not silent.
        assert journal_file.read_bytes() == before
        result = store.load_events()
        assert result.incomplete_trailing_record is True
        assert [event.event_id for event in result.events] == [_EVENT_ID_A]
        # No staging remains after the failed repair attempt.
        assert not [
            p for p in journal_file.parent.iterdir() if p.suffix == ".tmp"
        ]

    def test_append_after_garbage_fragment_keeps_new_record_intact(
        self, tmp_path: Path
    ) -> None:
        # The torn fragment must never corrupt a subsequently appended
        # record by concatenation.
        _write_journal(tmp_path, '{"torn": "fra')
        store = _make_store(tmp_path)
        store.append(_event(event_id=_EVENT_ID_A, occurred_at=_T0))
        result = store.load_events()
        assert [event.event_id for event in result.events] == [_EVENT_ID_A]
        assert result.incomplete_trailing_record is False


# ---------------------------------------------------------------------------
# Error hierarchy (typed, actionable)
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    @pytest.mark.parametrize(
        "error_class",
        [
            AttendanceStoreError,
            AttendanceStoreInputError,
            AttendanceStorePersistenceError,
            CorruptAttendanceJournalError,
            UnsupportedJournalSchemaVersionError,
        ],
    )
    def test_errors_derive_from_the_store_base(
        self, error_class: type[Exception]
    ) -> None:
        assert error_class.__name__ and issubclass(error_class, Exception)

    def test_suberrors_are_store_errors(self) -> None:
        for subclass in (
            AttendanceStoreInputError,
            AttendanceStorePersistenceError,
            CorruptAttendanceJournalError,
        ):
            assert issubclass(subclass, AttendanceStoreError)

    def test_unsupported_version_is_specific_corruption(self) -> None:
        assert issubclass(
            UnsupportedJournalSchemaVersionError,
            CorruptAttendanceJournalError,
        )
