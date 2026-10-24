"""Hardware-free tests for the read-only attendance report CLI
(P2-4 — ``tools/attendance_report.py``).

Deterministic: journals live in ``pytest`` temporary directories and
contain synthetic records only; no camera, no biometric components, no
real attendance data, nothing under the real ``data/`` tree. Every
journal-touching test asserts the journal bytes are unchanged by the
run (the CLI is read-only by contract).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime

import pytest

from src.attendance_store import (
    ATTENDANCE_JOURNAL_SCHEMA_VERSION,
    JOURNAL_FILENAME,
)

_TOOL_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "tools"
    / "attendance_report.py"
)


def _load_cli_module():
    spec = importlib.util.spec_from_file_location(
        "attendance_report", _TOOL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli_module()

OURNAL_DIR = "attendance"


def _journal_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / OURNAL_DIR / JOURNAL_FILENAME


def _record(
    employee_id: str = "EMP-001",
    event_type: str = "check_in",
    occurred_at: str | None = None,
    event_id: str = "ev-0001",
) -> str:
    if occurred_at is None:
        occurred_at = "2026-10-23T08:59:30+00:00"
    return json.dumps(
        {
            "schema_version": ATTENDANCE_JOURNAL_SCHEMA_VERSION,
            "event_id": event_id,
            "employee_id": employee_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
        }
    )


def _write_journal(
    tmp_path: pathlib.Path, lines: list[str]
) -> pathlib.Path:
    _journal_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + ("\n" if lines else "")
    _journal_path(tmp_path).write_text(text, encoding="utf-8")
    return _journal_path(tmp_path)


def _read_bytes(tmp_path: pathlib.Path) -> bytes:
    return _journal_path(tmp_path).read_bytes()


def _run(
    argv: list[str], tmp_path: pathlib.Path
) -> tuple[int, str, str]:
    """Invoke the CLI main() capturing stdout/stderr streams."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main([*argv, "--data-dir", str(tmp_path)])
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Empty journals (exit 0 — never an error)
# ---------------------------------------------------------------------------


class TestEmptyJournal:
    def test_missing_journal_is_empty_success(self, tmp_path) -> None:
        code, out, err = _run([], tmp_path)
        assert code == 0
        assert f"No attendance events found in {_journal_path(tmp_path)}" in out
        assert "EMP-001" not in out
        assert err == ""

    def test_zero_byte_journal_is_empty_success(self, tmp_path) -> None:
        _write_journal(tmp_path, [])
        code, out, err = _run([], tmp_path)
        assert code == 0
        assert "No attendance events found" in out

    def test_no_match_after_filters_is_empty_success(self, tmp_path) -> None:
        _write_journal(tmp_path, [_record(employee_id="EMP-001")])
        code, out, err = _run(["--employee", "EMP-404"], tmp_path)
        assert code == 0
        assert "No attendance events matched the given filters." in out
        assert "ev-0001" not in out


# ---------------------------------------------------------------------------
# Listing: default chronological order, tie-break, --journal-order
# ---------------------------------------------------------------------------


class TestListing:
    def test_single_event_row(self, tmp_path) -> None:
        _write_journal(tmp_path, [_record()])
        code, out, err = _run([], tmp_path)
        assert code == 0
        assert "TIME (UTC)" in out and "EVENT_ID" in out
        assert "2026-10-23 08:59:30Z" in out
        assert "check_in" in out and "EMP-001" in out and "ev-0001" in out
        assert "1 events (1 employees)" in out

    def test_multiple_events_count_and_distinct_employees(
        self, tmp_path
    ) -> None:
        _write_journal(
            tmp_path,
            [
                _record(employee_id="EMP-001", event_id="ev-0001"),
                _record(employee_id="EMP-002", event_id="ev-0002"),
            ],
        )
        code, out, _err = _run([], tmp_path)
        assert code == 0
        assert "2 events (2 employees)" in out

    def test_default_chronological_order_with_journal_position_tiebreak(
        self, tmp_path
    ) -> None:
        # Journal contains NEW, OLD, OLD2 where OLD and OLD2 share a
        # timestamp: chronological default sorts OLD, OLD2, NEW (ties
        # resolved by original journal position).
        _write_journal(
            tmp_path,
            [
                _record(event_id="ev-new", occurred_at="2026-10-23T09:00:00+00:00"),
                _record(event_id="ev-old", occurred_at="2026-10-23T08:00:00+00:00"),
                _record(event_id="ev-old2", occurred_at="2026-10-23T08:00:00+00:00"),
            ],
        )
        code, out, _err = _run([], tmp_path)
        assert code == 0
        body = out.split("filters:")[0]
        assert body.index("ev-old") < body.index("ev-old2") < body.index("ev-new")

    def test_journal_order_preserves_append_order(self, tmp_path) -> None:
        _write_journal(
            tmp_path,
            [
                _record(event_id="ev-new", occurred_at="2026-10-23T09:00:00+00:00"),
                _record(event_id="ev-old", occurred_at="2026-10-23T08:00:00+00:00"),
            ],
        )
        code, out, _err = _run(["--journal-order"], tmp_path)
        assert code == 0
        body = out.split("filters:")[0]
        assert body.index("ev-new") < body.index("ev-old")
        assert "order=journal" in out


# ---------------------------------------------------------------------------
# Employee filtering (verbatim; never normalized)
# ---------------------------------------------------------------------------


class TestEmployeeFilter:
    def test_verbatim_match_and_repeatable(self, tmp_path) -> None:
        _write_journal(
            tmp_path,
            [
                _record(employee_id="007", event_id="ev-1"),
                _record(employee_id="EMP-002", event_id="ev-2"),
                _record(employee_id="007", event_id="ev-3"),
            ],
        )
        code, out, _err = _run(
            ["--employee", "007", "--employee", "EMP-002"], tmp_path
        )
        assert code == 0
        assert "employee=007,EMP-002" in out
        assert "ev-1" in out and "ev-2" in out and "ev-3" in out

    def test_filter_excludes_others(self, tmp_path) -> None:
        _write_journal(
            tmp_path,
            [
                _record(employee_id="EMP-001", event_id="ev-1"),
                _record(employee_id="EMP-002", event_id="ev-2"),
            ],
        )
        code, out, _err = _run(["--employee", "EMP-001"], tmp_path)
        assert code == 0
        assert "ev-1" in out and "ev-2" not in out
        assert "1 events (1 employees)" in out


# ---------------------------------------------------------------------------
# Half-open UTC date-filter boundaries (incl. fractional seconds)
# ---------------------------------------------------------------------------


class TestDateBoundaries:
    def test_since_includes_exact_midnight(self, tmp_path) -> None:
        _write_journal(
            tmp_path,
            [
                _record(employee_id="EMP-001", event_id="ev-pre",
                        occurred_at="2026-10-22T23:59:59.500000+00:00"),
                _record(event_id="ev-mid", occurred_at="2026-10-23T00:00:00+00:00"),
                _record(event_id="ev-later", occurred_at="2026-10-23T09:00:00+00:00"),
            ],
        )
        code, out, _err = _run(["--since", "2026-10-23"], tmp_path)
        assert code == 0
        assert "ev-mid" in out and "ev-later" in out and "ev-pre" not in out

    def test_until_covers_fractional_seconds_of_whole_day(
        self, tmp_path
    ) -> None:
        _write_journal(
            tmp_path,
            [
                _record(event_id="ev-in", occurred_at="2026-10-23T23:59:59.999999+00:00"),
                _record(event_id="ev-next", occurred_at="2026-10-24T00:00:00+00:00"),
            ],
        )
        code, out, _err = _run(["--until", "2026-10-23"], tmp_path)
        assert code == 0
        assert "ev-in" in out and "ev-next" not in out

    def test_day_includes_last_microsecond_excludes_next_midnight(
        self, tmp_path
    ) -> None:
        _write_journal(
            tmp_path,
            [
                _record(event_id="ev-1", occurred_at="2026-10-23T00:00:00+00:00"),
                _record(event_id="ev-2", occurred_at="2026-10-23T23:59:59.999999+00:00"),
                _record(event_id="ev-3", occurred_at="2026-10-24T00:00:00+00:00"),
            ],
        )
        code, out, _err = _run(["--day", "2026-10-23"], tmp_path)
        assert code == 0
        assert "ev-1" in out and "ev-2" in out and "ev-3" not in out


# ---------------------------------------------------------------------------
# Summary (deterministic: day order, then verbatim employee ID)
# ---------------------------------------------------------------------------


class TestSummary:
    def test_summary_counts_per_day_and_employee(self, tmp_path) -> None:
        _write_journal(
            tmp_path,
            [
                _record("EMP-002", event_id="ev-1", occurred_at="2026-10-24T08:00:00+00:00"),
                _record("EMP-001", event_id="ev-2", occurred_at="2026-10-23T08:00:00+00:00"),
                _record("EMP-001", event_id="ev-3", occurred_at="2026-10-23T09:00:00+00:00"),
            ],
        )
        code, out, _err = _run(["--summary"], tmp_path)
        assert code == 0
        assert "DAY EMPLOYEE CHECK_INS" in out
        lines = out.splitlines()
        day1 = next(line for line in lines if line.startswith("2026-10-23"))
        day2 = next(line for line in lines if line.startswith("2026-10-24"))
        assert day1 == "2026-10-23 EMP-001 2"
        assert day2 == "2026-10-24 EMP-002 1"
        assert lines.index(day1) < lines.index(day2)

    def test_summary_respects_filters(self, tmp_path) -> None:
        _write_journal(
            tmp_path,
            [
                _record("EMP-001", event_id="ev-1", occurred_at="2026-10-23T08:00:00+00:00"),
                _record("EMP-002", event_id="ev-2", occurred_at="2026-10-23T08:00:00+00:00"),
            ],
        )
        code, out, _err = _run(
            ["--summary", "--employee", "EMP-002"], tmp_path
        )
        assert code == 0
        assert "EMP-001" not in out
        assert "2026-10-23 EMP-002 1" in out


# ---------------------------------------------------------------------------
# Torn trailing record: rows print, warning on stderr, exit 0
# ---------------------------------------------------------------------------


class TestTornTail:
    def test_torn_tail_warns_and_keeps_preceding_rows(
        self, tmp_path
    ) -> None:
        _write_journal(tmp_path, [_record()])
        journal = _journal_path(tmp_path)
        before = journal.read_bytes()
        torn = _record(event_id="ev-torn", occurred_at="2026-10-23T10:00:00+00:00")[:-15]
        journal.write_bytes(before + torn.encode("utf-8"))
        code, out, err = _run([], tmp_path)
        assert code == 0
        assert "incomplete trailing record" in err
        assert "ev-torn" not in out
        assert "ev-0001" in out  # valid preceding event still displayed
        assert journal.read_bytes() == before + torn.encode("utf-8")


# ---------------------------------------------------------------------------
# Journal read failures: exit 2, typed stderr, NO event rows
# ---------------------------------------------------------------------------


class TestLoadFailures:
    def test_corrupt_journal_returns_2_no_rows(self, tmp_path) -> None:
        _write_journal(tmp_path, ['{"broken json'])
        code, out, err = _run([], tmp_path)
        assert code == 2
        assert "journal error" in err
        assert "EVENT_ID" not in out and "EMP-001" not in out

    def test_corruption_after_valid_line_returns_2(self, tmp_path) -> None:
        _write_journal(
            tmp_path,
            [
                _record(),
                '{\"schema_version\": 1, \"event_id\": \"\"}',
            ],
        )
        code, out, err = _run([], tmp_path)
        assert code == 2
        assert "journal error" in err
        assert "ev-0001" not in out

    def test_unsupported_schema_returns_2(self, tmp_path) -> None:
        payload = json.loads(_record())
        payload["schema_version"] = 99
        _write_journal(tmp_path, [json.dumps(payload)])
        code, out, err = _run([], tmp_path)
        assert code == 2
        assert "schema version" in err

    def test_journal_bytes_unchanged_after_error(self, tmp_path) -> None:
        _write_journal(tmp_path, ['{"broken json'])
        before = _read_bytes(tmp_path)
        _run([], tmp_path)
        assert _read_bytes(tmp_path) == before


# ---------------------------------------------------------------------------
# Invalid arguments / dates: exit 3, journal untouched
# ---------------------------------------------------------------------------


class TestInvalidArguments:
    def test_malformed_date_shape_returns_3(self, tmp_path) -> None:
        code, _out, err = _run(["--since", "2026-13-45"], tmp_path)
        assert code == 3
        assert "attendance-report: error:" in err

    def test_non_canonical_date_shape_returns_3(self, tmp_path) -> None:
        code, _out, err = _run(["--day", "2026-10-2"], tmp_path)
        assert code == 3

    def test_impossible_calendar_date_returns_3(self, tmp_path) -> None:
        code, _out, err = _run(["--since", "2026-02-30"], tmp_path)
        assert code == 3

    def test_day_with_since_returns_3(self, tmp_path) -> None:
        code, _out, err = _run(
            ["--day", "2026-10-23", "--since", "2026-10-22"], tmp_path
        )
        assert code == 3
        assert "cannot be combined" in err

    def test_day_with_until_returns_3(self, tmp_path) -> None:
        code, _out, err = _run(
            ["--day", "2026-10-23", "--until", "2026-10-24"], tmp_path
        )
        assert code == 3

    def test_empty_interval_returns_3(self, tmp_path) -> None:
        code, _out, err = _run(
            ["--since", "2026-10-25", "--until", "2026-10-23"], tmp_path
        )
        assert code == 3

    def test_invalid_arguments_never_touch_the_journal(self, tmp_path) -> None:
        _write_journal(tmp_path, [_record()])
        before = _read_bytes(tmp_path)
        code, _out, _err = _run(["--since", "not-a-date"], tmp_path)
        assert code == 3
        assert _read_bytes(tmp_path) == before

    def test_help_prints_and_returns_0(self, tmp_path, capsys) -> None:
        code = cli.main(["--help"])
        assert code == 0
        captured = capsys.readouterr()
        assert "usage:" in captured.out and "--journal-order" in captured.out

    def test_exit_contract_documented_values_only(self, tmp_path) -> None:
        # Spot-check the constants to pin the documented contract.
        assert cli._EXIT_OK == 0
        assert cli._EXIT_JOURNAL_ERROR == 2
        assert cli._EXIT_USAGE_ERROR == 3


# ---------------------------------------------------------------------------
# Read-only guarantee across success paths
# ---------------------------------------------------------------------------


class TestReadOnly:
    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["--journal-order"],
            ["--summary"],
            ["--employee", "EMP-001"],
            ["--day", "2026-10-23"],
        ],
    )
    def test_journal_bytes_identical_after_replay(self, tmp_path, argv) -> None:
        _write_journal(
            tmp_path,
            [_record(), _record("EMP-002", event_id="ev-2", occurred_at="2026-10-24T08:00:00+00:00")],
        )
        before = _read_bytes(tmp_path)
        code, _out, _err = _run(list(argv), tmp_path)
        assert code == 0
        assert _read_bytes(tmp_path) == before


# ---------------------------------------------------------------------------
# Import hygiene: no camera/biometric components, no default data dir I/O
# ---------------------------------------------------------------------------


class TestImportHygiene:
    def test_import_loads_no_biometric_modules_and_creates_no_data_dir(
        self, tmp_path
    ) -> None:
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, r'{root}')\n"
                    "import importlib.util\n"
                    "spec = importlib.util.spec_from_file_location("
                    "'attendance_report', r'{tool}')\n"
                    "module = importlib.util.module_from_spec(spec)\n"
                    "spec.loader.exec_module(module)\n"
                    "loaded = sorted(m for m in sys.modules if m in {{\n"
                    "    'cv2', 'src.camera', 'src.face_detection',\n"
                    "    'src.face_recognition', 'src.enrollment',\n"
                    "    'src.face_embedding', 'src.embedding_storage',\n"
                    "}})\n"
                    "print('LOADED:', loaded)\n"
                ).format(root=_TOOL_PATH.parents[1], tool=_TOOL_PATH),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd="/",
            env={**os.environ, "DATA_DIR": str(tmp_path / "probe-data")},
        )
        assert probe.returncode == 0, probe.stderr
        assert "LOADED: []" in probe.stdout
        # Import must not create the data directory tree at all.
        assert not (tmp_path / "probe-data").exists()

    def test_import_in_clean_subprocess_reports_no_side_effects(
        self, tmp_path
    ) -> None:
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, r'{root}')\n"
                    "from pathlib import Path\n"
                    "import importlib.util\n"
                    "spec = importlib.util.spec_from_file_location("
                    "'attendance_report', r'{tool}')\n"
                    "module = importlib.util.module_from_spec(spec)\n"
                    "spec.loader.exec_module(module)\n"
                    "print('IMPORT_OK')\n"
                ).format(root=_TOOL_PATH.parents[1], tool=_TOOL_PATH),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd="/",
        )
        assert probe.returncode == 0, probe.stderr
        assert "IMPORT_OK" in probe.stdout


# ---------------------------------------------------------------------------
# Execution hygiene: running the report never initializes camera or
# model/biometric components (no capture, no detector/recognizer built)
# ---------------------------------------------------------------------------


class TestNoInitialization:
    def test_full_report_run_never_initializes_camera_or_model(
        self, tmp_path, monkeypatch
    ) -> None:
        """The CLI does import the store chain (to reuse its loader),
        but executing a full report must not construct a camera or a
        detection/recognition model — proven by turning every such
        constructor into a hard failure.
        """
        import cv2

        def boom(*args, **kwargs):
            raise AssertionError(
                "camera/model initialization attempted by the CLI"
            )

        monkeypatch.setattr(cv2, "VideoCapture", boom)
        for name in ("FaceDetectorYN", "FaceRecognizerSF"):
            if hasattr(cv2, name):
                monkeypatch.setattr(cv2, name, boom)
        try:
            import onnxruntime

            monkeypatch.setattr(onnxruntime, "InferenceSession", boom)
        except ImportError:
            pass

        _write_journal(tmp_path, [_record()])
        code, out, _err = _run([], tmp_path)
        assert code == 0
        assert "ev-0001" in out
