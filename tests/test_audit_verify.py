"""Tests for the P5.5.6 read-only audit journal verifier.

Every journal is synthetic and lives under ``tmp_path``; the real
``data/audit`` directory is never read or written.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import tools.audit_verify as cli
from src.audit_schema import (
    GENESIS_PREVIOUS_HASH,
    AuditAction,
    AuditEvent,
    AuditOutcome,
    AuditReasonCode,
    AuditSource,
    AuditTargetType,
    parse_timestamp,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_TIMESTAMP = "2026-11-16T12:00:00.000000Z"
_AUDIT_ID = "11111111-1111-4111-8111-111111111111"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _journal_path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "audit.jsonl"


def _event(
    sequence: int,
    *,
    segment: str = "2026-11-001",
    segment_sequence: int | None = None,
    previous_hash: str = GENESIS_PREVIOUS_HASH,
    timestamp: str = BASE_TIMESTAMP,
    audit_id: str | None = None,
    target_id: str | None = None,
) -> AuditEvent:
    return AuditEvent.create(
        sequence=sequence,
        segment_id=segment,
        segment_sequence=(
            segment_sequence if segment_sequence is not None else sequence
        ),
        previous_hash=previous_hash,
        timestamp=parse_timestamp(timestamp),
        audit_id=audit_id,
        action=AuditAction.AUTH_LOGIN,
        outcome=AuditOutcome.SUCCESS,
        reason_code=AuditReasonCode.OK,
        source=AuditSource.SYSTEM,
        target_type=AuditTargetType.ACCOUNT if target_id else None,
        target_id=target_id,
    )


def _chain(specs: list[dict]) -> list[AuditEvent]:
    """Build a connected chain from specs (sequence/segment defaults filled)."""
    events: list[AuditEvent] = []
    previous = GENESIS_PREVIOUS_HASH
    segment: str | None = None
    segment_sequence = 0
    for spec in specs:
        values = dict(spec)
        sequence = values.pop("sequence", len(events) + 1)
        current_segment = values.pop("segment", segment or "2026-11-001")
        if current_segment == segment:
            segment_sequence += 1
        else:
            segment_sequence = 1
        values.setdefault("segment_sequence", segment_sequence)
        values.setdefault("previous_hash", previous)
        event = _event(sequence, segment=current_segment, **values)
        events.append(event)
        previous = event.record_hash
        segment = current_segment
    return events


def _write_events(path: Path, events: list[AuditEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            event.to_json_line().encode("utf-8") + b"\n" for event in events
        )
    )


def _write_raw(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _valid_single(path: Path) -> AuditEvent:
    event = _event(1)
    _write_events(path, [event])
    return event


# ---------------------------------------------------------------------------
# Valid journals
# ---------------------------------------------------------------------------


def test_default_path_is_the_project_journal_and_cwd_independent() -> None:
    assert cli.DEFAULT_JOURNAL_PATH == (
        PROJECT_ROOT / "data" / "audit" / "audit.jsonl"
    )
    assert cli.DEFAULT_JOURNAL_PATH.is_absolute()


def test_valid_empty_journal_passes(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_raw(path, b"")
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_OK
    assert "verification: PASS" in output
    assert "valid empty chain" in output
    assert "Segments: 0" in output
    assert "Records verified" not in output


def test_valid_single_record_journal_passes(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    event = _valid_single(path)
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_OK
    assert "Records verified: 1" in output
    assert "Global sequence: 1..1" in output
    assert "Segments: 1" in output
    assert "First segment: 2026-11-001" in output
    assert event.record_hash in output


def test_valid_multi_record_journal_passes(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_events(path, _chain([{}, {}, {}]))
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_OK
    assert "Records verified: 3" in output
    assert "Global sequence: 1..3" in output


def test_valid_journal_spanning_segments_passes(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_events(
        path,
        _chain(
            [
                {"segment": "2026-11-001"},
                {"segment": "2026-11-001"},
                {"segment": "2026-11-002"},
                {"segment": "2026-11-002"},
            ]
        ),
    )
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_OK
    assert "Segments: 2" in output
    assert "First segment: 2026-11-001" in output
    assert "Last segment: 2026-11-002" in output


def test_same_month_segments_with_different_ordinals_pass(
    tmp_path: Path, capsys
) -> None:
    path = _journal_path(tmp_path)
    _write_events(
        path,
        _chain(
            [
                {"segment": "2026-11-001"},
                {"segment": "2026-11-002"},
                {"segment": "2026-11-003"},
            ]
        ),
    )
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_OK
    assert "Segments: 3" in output


def test_non_lexical_segment_labels_are_accepted(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_events(
        path,
        _chain(
            [
                {"segment": "2026-11-002"},
                {"segment": "2026-11-001"},
            ]
        ),
    )
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_OK
    assert "First segment: 2026-11-002" in output
    assert "Last segment: 2026-11-001" in output


def test_timestamp_month_differing_from_segment_month_passes(
    tmp_path: Path, capsys
) -> None:
    path = _journal_path(tmp_path)
    _write_events(
        path,
        _chain(
            [
                {
                    "segment": "2026-11-001",
                    "timestamp": "2026-12-01T00:00:00.000000Z",
                }
            ]
        ),
    )
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_OK
    assert "First timestamp: 2026-12-01T00:00:00.000000Z" in output


def test_default_path_is_used_when_no_argument_is_given(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    path = _journal_path(tmp_path)
    _write_events(path, _chain([{}]))
    monkeypatch.setattr(cli, "DEFAULT_JOURNAL_PATH", path)
    code, output = _run(capsys)
    assert code == cli.EXIT_OK
    assert str(path.resolve()) in output


# ---------------------------------------------------------------------------
# Structural failures
# ---------------------------------------------------------------------------


def test_malformed_json_fails(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_raw(path, b"{not-json}\n")
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.MALFORMED_JSON in output
    assert "Line: 1" in output
    assert "Next step:" in output


def test_invalid_utf8_fails(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_raw(path, b"\xff\xfe\n")
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.INVALID_UTF8 in output


def test_empty_line_fails(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    first = _event(1).to_json_line().encode("utf-8") + b"\n"
    second = _event(2).to_json_line().encode("utf-8") + b"\n"
    _write_raw(path, first + b"\n" + second)
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.EMPTY_LINE in output
    assert "Line: 2" in output


def test_missing_final_newline_is_a_torn_tail(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_raw(path, _event(1).to_json_line().encode("utf-8"))
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.TORN_TAIL in output
    assert "Line: 1" in output


def test_record_missing_a_required_field_is_a_schema_failure(
    tmp_path: Path, capsys
) -> None:
    path = _journal_path(tmp_path)
    body = _event(1).as_body()
    body.pop("record_hash")
    _write_raw(path, json.dumps(body).encode("utf-8") + b"\n")
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.SCHEMA_INVALID in output
    assert "Line: 1" in output


def test_invalid_segment_ordinal_is_a_schema_failure(
    tmp_path: Path, capsys
) -> None:
    path = _journal_path(tmp_path)
    body = _event(1).as_body()
    body["segment_id"] = "2026-11-000"
    _write_raw(path, json.dumps(body).encode("utf-8") + b"\n")
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.SCHEMA_INVALID in output


# ---------------------------------------------------------------------------
# Cryptographic and sequence failures
# ---------------------------------------------------------------------------


def test_invalid_record_digest_fails(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    tampered = replace(_event(1), record_hash="a" * 64)
    _write_events(path, [tampered])
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.HASH_MISMATCH in output
    assert "Line: 1" in output


def test_broken_predecessor_link_fails(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_events(
        path,
        _chain([{}, {"previous_hash": "b" * 64}]),
    )
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.BROKEN_PREDECESSOR in output
    assert "Line: 2" in output


def test_invalid_genesis_predecessor_fails(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_events(path, _chain([{"previous_hash": "c" * 64}]))
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.INVALID_GENESIS in output
    assert "Line: 1" in output


def test_global_sequence_gap_fails(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_events(path, _chain([{}, {"sequence": 3}]))
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.SEQUENCE_INVALID in output
    assert "Line: 2" in output


def test_incorrect_segment_local_sequence_fails(
    tmp_path: Path, capsys
) -> None:
    path = _journal_path(tmp_path)
    # A repeated local index inside one segment (1, 1, ...) is a gap in the
    # expected 1, 2, ... progression.
    _write_events(
        path,
        _chain([{}, {"segment_sequence": 1}, {}]),
    )
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.SEQUENCE_INVALID in output
    assert "Line: 2" in output


def test_global_sequence_must_begin_at_one(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_events(path, _chain([{"sequence": 2}]))
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.SEQUENCE_INVALID in output
    assert "Line: 1" in output


def test_new_segment_must_start_at_segment_sequence_one(
    tmp_path: Path, capsys
) -> None:
    path = _journal_path(tmp_path)
    _write_events(
        path,
        _chain(
            [
                {"segment": "2026-11-001"},
                {"segment": "2026-11-002", "segment_sequence": 2},
            ]
        ),
    )
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.SEGMENT_INVALID in output
    assert "Line: 2" in output


def test_repeated_segment_id_fails(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_events(
        path,
        _chain(
            [
                {"segment": "2026-11-001"},
                {"segment": "2026-11-002"},
                {"segment": "2026-11-001"},
            ]
        ),
    )
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.SEGMENT_INVALID in output
    assert "Line: 3" in output


def test_duplicate_audit_id_fails(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_events(
        path,
        _chain([{"audit_id": _AUDIT_ID}, {"audit_id": _AUDIT_ID}]),
    )
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.DUPLICATE_AUDIT_ID in output
    assert "Line: 2" in output


def test_timestamp_regression_fails(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_events(
        path,
        _chain(
            [
                {"timestamp": "2026-11-16T12:00:01.000000Z"},
                {"timestamp": "2026-11-16T12:00:00.000000Z"},
            ]
        ),
    )
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.TIMESTAMP_REGRESSION in output
    assert "Line: 2" in output


def test_timestamps_restart_at_a_new_segment(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_events(
        path,
        _chain(
            [
                {
                    "segment": "2026-11-001",
                    "timestamp": "2026-11-16T13:00:00.000000Z",
                },
                {
                    "segment": "2026-11-002",
                    "timestamp": "2026-11-16T12:00:00.000000Z",
                },
            ]
        ),
    )
    code, _ = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_OK


# ---------------------------------------------------------------------------
# Availability, usage and read-only behavior
# ---------------------------------------------------------------------------


def test_missing_journal_fails_without_creating_anything(
    tmp_path: Path, capsys
) -> None:
    path = _journal_path(tmp_path)
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_UNAVAILABLE
    assert cli.JOURNAL_MISSING in output
    assert not path.exists()
    assert not path.parent.exists()


def test_directory_instead_of_file_fails(tmp_path: Path, capsys) -> None:
    path = tmp_path / "audit"
    path.mkdir()
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_UNAVAILABLE
    assert cli.JOURNAL_NOT_REGULAR_FILE in output


def test_read_failure_is_sanitized(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    path = _journal_path(tmp_path)
    _valid_single(path)

    def denied(_path: Path):
        raise PermissionError("C:/secret/location denied")

    monkeypatch.setattr(cli, "_open_readonly", denied)
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_UNAVAILABLE
    assert cli.JOURNAL_UNREADABLE in output
    assert "C:/secret" not in output
    assert "PermissionError" not in output
    assert "Traceback" not in output


def test_invalid_cli_arguments_exit_three(tmp_path: Path, capsys) -> None:
    code, output = _run(capsys, "--unknown-option")
    assert code == cli.EXIT_USAGE_ERROR
    assert cli.INVALID_ARGUMENTS in output


def test_missing_option_value_exits_three(tmp_path: Path, capsys) -> None:
    code, output = _run(capsys, "--journal")
    assert code == cli.EXIT_USAGE_ERROR
    assert cli.INVALID_ARGUMENTS in output


def test_failure_output_never_contains_record_contents(
    tmp_path: Path, capsys
) -> None:
    path = _journal_path(tmp_path)
    tampered = replace(
        _event(1, target_id="alice-secret"), record_hash="a" * 64
    )
    _write_events(path, [tampered])
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert "alice-secret" not in output
    assert "audit_id" not in output
    assert "target_id" not in output


def test_malformed_line_is_not_echoed(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_raw(path, b'{"secret-token": "abc"}\n')
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.SCHEMA_INVALID in output
    assert "secret-token" not in output
    assert "abc" not in output


def _fingerprint(path: Path) -> tuple[bytes, int, int]:
    stat = path.stat()
    return path.read_bytes(), stat.st_size, stat.st_mtime_ns


def test_corrupt_journal_is_not_modified(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_raw(path, b"{broken\n")
    before = _fingerprint(path)
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert cli.MALFORMED_JSON in output
    assert _fingerprint(path) == before


def test_valid_journal_is_not_modified(tmp_path: Path, capsys) -> None:
    path = _journal_path(tmp_path)
    _write_events(path, _chain([{}, {}]))
    before = _fingerprint(path)
    code, _ = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_OK
    assert _fingerprint(path) == before


def test_journal_changed_during_verification_refuses_pass(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    path = _journal_path(tmp_path)
    _valid_single(path)

    def append_during_read() -> None:
        with path.open("ab") as handle:
            handle.write(b"\n")

    monkeypatch.setattr(cli, "_AFTER_READ", append_during_read)
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_UNAVAILABLE
    assert cli.JOURNAL_CHANGED in output
    assert "verification: PASS" not in output


def test_unexpected_internal_error_exits_four(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    path = _journal_path(tmp_path)
    _valid_single(path)

    def broken(_path):
        raise RuntimeError("raw internal detail")

    monkeypatch.setattr(cli, "verify_journal", broken)
    code, output = _run(capsys, "--journal", str(path))
    assert code == cli.EXIT_INTERNAL_ERROR
    assert cli.INTERNAL_ERROR in output
    assert "raw internal detail" not in output


def test_module_and_script_entry_points_run(tmp_path: Path) -> None:
    path = _journal_path(tmp_path)
    _write_events(path, _chain([{}]))
    commands = (
        [sys.executable, "-m", "tools.audit_verify"],
        [sys.executable, str(PROJECT_ROOT / "tools" / "audit_verify.py")],
    )
    for command in commands:
        completed = subprocess.run(
            [*command, "--journal", str(path)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "verification: PASS" in completed.stdout
