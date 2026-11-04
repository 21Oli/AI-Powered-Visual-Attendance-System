"""Focused offline tests for the Step 1.5c-2 employee-metadata importer.

Everything here is synthetic: JSONL fixtures are built in-string or in a
temporary directory, the repository is an in-memory fake, and no
PostgreSQL connection, DSN, enrollment artifact, face sample, embedding,
or attendance journal is read. The importer CLI is loaded from its file
path (the project's existing ``importlib`` pattern).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src import db_employee_repository, employee_import
from src.config import AppSettings
from src.employee import Employee
from src.employee_export import EXPORT_FILENAME, EXPORT_SCHEMA_VERSION
from src.employee_import import (
    IMPORT_SCHEMA_VERSION,
    EmployeeRepositoryLike,
    ImportFileError,
    ImportInputError,
    ImportRecord,
    ImportReportError,
    ImportReportExistsError,
    RecordRejection,
    parse_artifact,
    read_artifact,
    report_payload,
    run_import,
    write_report,
)
from src.employee_repository import EmployeeAlreadyExists

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TOOL_PATH = PROJECT_ROOT / "tools" / "import_employee_metadata.py"
spec = importlib.util.spec_from_file_location(
    "import_employee_metadata", _TOOL_PATH
)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

NAME = "Ada Lovelace"
CREATED_AT = "2026-10-31T09:12:44.123456+00:00"
CREATED_DT = datetime.fromisoformat(CREATED_AT)
KEY_ORDER = [
    "schema_version",
    "employee_id",
    "full_name",
    "is_active",
    "created_at",
]


# ---------------------------------------------------------------------------
# Fakes and synthetic artifacts
# ---------------------------------------------------------------------------


class FakeRepository:
    """In-memory repository that records every call and forbids mutations."""

    def __init__(self, employees: tuple[Employee, ...] = ()) -> None:
        self.rows: dict[str, Employee] = {
            employee.employee_id: employee for employee in employees
        }
        self.calls: list[tuple[str, str]] = []
        self.forbidden_calls: list[str] = []
        self.race_ids: set[str] = set()
        self.fail_on_get: BaseException | None = None
        self.fail_on_create: BaseException | None = None

    # -- approved surface ---------------------------------------------------
    def get(self, employee_id: str) -> Employee | None:
        self.calls.append(("get", employee_id))
        if self.fail_on_get is not None:
            raise self.fail_on_get
        return self.rows.get(employee_id.strip())

    def create(self, employee: Employee) -> Employee:
        self.calls.append(("create", employee.employee_id))
        if self.fail_on_create is not None:
            raise self.fail_on_create
        if employee.employee_id in self.rows or (
            employee.employee_id in self.race_ids
        ):
            raise EmployeeAlreadyExists("duplicate employee id")
        self.rows[employee.employee_id] = employee
        return employee

    # -- operations the importer must never touch --------------------------
    def update_full_name(self, *args: object, **kwargs: object) -> None:
        self.forbidden_calls.append("update_full_name")
        raise AssertionError("importer must never update a name")

    def deactivate(self, *args: object, **kwargs: object) -> None:
        self.forbidden_calls.append("deactivate")
        raise AssertionError("importer must never deactivate an employee")

    def list(self, *args: object, **kwargs: object) -> None:
        self.forbidden_calls.append("list")
        raise AssertionError("importer must never list employees")


def _employee(
    employee_id: str = "E-001",
    *,
    full_name: str = NAME,
    is_active: bool = True,
    created_at: datetime = CREATED_DT,
) -> Employee:
    return Employee(
        employee_id=employee_id,
        full_name=full_name,
        is_active=is_active,
        created_at=created_at,
    )


def _record_line(
    employee_id: object = "E-001",
    *,
    full_name: object = NAME,
    is_active: object = True,
    created_at: object = CREATED_AT,
    schema_version: object = IMPORT_SCHEMA_VERSION,
    **extra: object,
) -> str:
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "employee_id": employee_id,
        "full_name": full_name,
        "is_active": is_active,
        "created_at": created_at,
    }
    payload.update(extra)
    return json.dumps(payload)


def _artifact(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _write_artifact(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "employee_metadata.jsonl"
    path.write_text(text, encoding="utf-8")
    return path


def _reasons(parsed: employee_import.ParsedArtifact) -> list[RecordRejection]:
    return [item.reason for item in parsed.rejected]


# ---------------------------------------------------------------------------
# 1. Strict record validation
# ---------------------------------------------------------------------------


class TestRecordValidation:
    def test_valid_record_is_parsed(self) -> None:
        parsed = parse_artifact(_artifact(_record_line()))
        assert parsed.line_count == 1
        assert parsed.rejected == ()
        (record,) = parsed.records
        assert record.employee_id == "E-001"
        assert record.full_name == NAME
        assert record.is_active is True
        assert record.created_at == CREATED_DT

    def test_payload_uses_exactly_the_five_keys(self) -> None:
        # The importer's expected key set is exactly the exporter's contract.
        assert IMPORT_SCHEMA_VERSION == EXPORT_SCHEMA_VERSION == 1
        assert employee_import._RECORD_KEYS == frozenset(KEY_ORDER)
        record = parse_artifact(_artifact(_record_line())).records[0]
        assert list(record.to_employee().to_dict()) == [
            "employee_id",
            "full_name",
            "is_active",
            "created_at",
        ]

    def test_timestamp_with_z_suffix_preserved(self) -> None:
        parsed = parse_artifact(
            _artifact(_record_line(created_at="2026-10-31T09:12:44Z"))
        )
        assert parsed.records[0].created_at == datetime(
            2026, 10, 31, 9, 12, 44, tzinfo=UTC
        )

    def test_records_are_sorted_by_employee_id(self) -> None:
        parsed = parse_artifact(
            _artifact(
                _record_line("M-300"),
                _record_line("A-100"),
                _record_line("007"),
            )
        )
        assert [record.employee_id for record in parsed.records] == [
            "007",
            "A-100",
            "M-300",
        ]

    @pytest.mark.parametrize("raw", ["{broken", "", "null", "[1, 2]", "42"])
    def test_malformed_json_rejected(self, raw: str) -> None:
        parsed = parse_artifact(_artifact(raw))
        assert _reasons(parsed) == [RecordRejection.MALFORMED_JSON]
        assert parsed.records == ()

    def test_blank_line_rejected_but_other_lines_continue(self) -> None:
        parsed = parse_artifact(
            _artifact(_record_line(), "", _record_line("E-002"))
        )
        assert _reasons(parsed) == [RecordRejection.MALFORMED_JSON]
        assert parsed.rejected[0].line == 2
        assert [record.employee_id for record in parsed.records] == [
            "E-001",
            "E-002",
        ]

    @pytest.mark.parametrize("bad_version", [2, 0, "1", True, None, 1.0, 100])
    def test_unsupported_schema_version_rejected(
        self, bad_version: object
    ) -> None:
        parsed = parse_artifact(
            _artifact(_record_line(schema_version=bad_version))
        )
        assert _reasons(parsed) == [
            RecordRejection.UNSUPPORTED_SCHEMA_VERSION
        ]

    @pytest.mark.parametrize(
        "missing", ["schema_version", "employee_id", "full_name", "is_active", "created_at"]
    )
    def test_missing_key_rejected(self, missing: str) -> None:
        payload = json.loads(_record_line())
        del payload[missing]
        parsed = parse_artifact(_artifact(json.dumps(payload)))
        assert _reasons(parsed) == [RecordRejection.MISSING_KEYS]

    def test_extra_key_rejected(self) -> None:
        parsed = parse_artifact(
            _artifact(_record_line(nickname="Ada"))
        )
        assert _reasons(parsed) == [RecordRejection.UNEXPECTED_KEYS]

    @pytest.mark.parametrize("bad_id", ["", "   ", None, 7, ["E-001"]])
    def test_invalid_employee_id_rejected(self, bad_id: object) -> None:
        parsed = parse_artifact(_artifact(_record_line(bad_id)))
        assert _reasons(parsed) == [RecordRejection.INVALID_EMPLOYEE_ID]

    def test_id_normalization_rejected(self) -> None:
        parsed = parse_artifact(_artifact(_record_line(" E-001")))
        assert _reasons(parsed) == [RecordRejection.ID_NORMALIZATION_CHANGED]

    @pytest.mark.parametrize("bad_name", ["Ada  Lovelace", " Ada", "Ada "])
    def test_name_normalization_rejected(self, bad_name: str) -> None:
        parsed = parse_artifact(_artifact(_record_line(full_name=bad_name)))
        assert _reasons(parsed) == [
            RecordRejection.NAME_NORMALIZATION_CHANGED
        ]

    @pytest.mark.parametrize("bad_name", ["", "   ", None, 42, ["Ada"]])
    def test_invalid_full_name_rejected(self, bad_name: object) -> None:
        parsed = parse_artifact(_artifact(_record_line(full_name=bad_name)))
        assert _reasons(parsed) == [RecordRejection.INVALID_FULL_NAME]

    @pytest.mark.parametrize("bad_active", ["true", 1, 0, None, [True]])
    def test_invalid_is_active_rejected(self, bad_active: object) -> None:
        parsed = parse_artifact(_artifact(_record_line(is_active=bad_active)))
        assert _reasons(parsed) == [RecordRejection.INVALID_IS_ACTIVE]

    @pytest.mark.parametrize(
        "bad_timestamp",
        [
            "not-a-date",
            "2026-10-31T09:12:44",  # naive
            "2026-10-31",
            "",
            "   ",
            None,
            1735632000,
        ],
    )
    def test_invalid_created_at_rejected(
        self, bad_timestamp: object
    ) -> None:
        parsed = parse_artifact(
            _artifact(_record_line(created_at=bad_timestamp))
        )
        assert _reasons(parsed) == [RecordRejection.INVALID_CREATED_AT]

    def test_rejection_reports_line_and_id_without_contents(self) -> None:
        parsed = parse_artifact(
            _artifact(_record_line("E-001", is_active="yes"))
        )
        (item,) = parsed.rejected
        assert item.line == 1
        assert item.employee_id == "E-001"
        assert item.reason is RecordRejection.INVALID_IS_ACTIVE


# ---------------------------------------------------------------------------
# 2. Duplicate identity
# ---------------------------------------------------------------------------


class TestDuplicates:
    def test_every_duplicate_occurrence_is_rejected(self) -> None:
        parsed = parse_artifact(
            _artifact(
                _record_line("E-001"),
                _record_line("E-001", full_name="Grace Hopper"),
                _record_line("E-002"),
            )
        )
        assert [record.employee_id for record in parsed.records] == ["E-002"]
        duplicate_lines = [
            item.line
            for item in parsed.rejected
            if item.reason is RecordRejection.DUPLICATE_EMPLOYEE_ID
        ]
        assert duplicate_lines == [1, 2]

    def test_duplicate_ids_are_never_imported(self) -> None:
        parsed = parse_artifact(
            _artifact(_record_line("E-001"), _record_line("E-001"))
        )
        repository = FakeRepository()
        report = run_import(parsed, repository=repository, apply=True)
        assert report.created == ()
        assert repository.calls == []
        assert len(report.rejected) == 2


# ---------------------------------------------------------------------------
# 3. Artifact reading
# ---------------------------------------------------------------------------


class TestReadArtifact:
    def test_reads_and_parses_a_file(self, tmp_path: Path) -> None:
        path = _write_artifact(tmp_path, _artifact(_record_line()))
        parsed = read_artifact(path)
        assert parsed.line_count == 1
        assert parsed.records[0].employee_id == "E-001"

    def test_missing_file_raises_typed_error(self, tmp_path: Path) -> None:
        with pytest.raises(ImportFileError):
            read_artifact(tmp_path / "absent.jsonl")

    def test_non_utf8_file_raises_typed_error(self, tmp_path: Path) -> None:
        path = tmp_path / "artifact.jsonl"
        path.write_bytes(b"\xff\xfe\x00binary")
        with pytest.raises(ImportFileError):
            read_artifact(path)

    def test_empty_file_is_a_clean_empty_artifact(
        self, tmp_path: Path
    ) -> None:
        parsed = read_artifact(_write_artifact(tmp_path, ""))
        assert parsed.line_count == 0
        assert parsed.records == ()
        assert parsed.rejected == ()


# ---------------------------------------------------------------------------
# 4. Classification and write policy
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_validate_only_needs_no_repository(self) -> None:
        parsed = parse_artifact(_artifact(_record_line()))
        report = run_import(parsed)
        assert report.mode == "validate-only"
        assert report.results == ()
        assert report.clean is True

    def test_validate_only_still_reports_rejections(self) -> None:
        parsed = parse_artifact(_artifact("{broken"))
        report = run_import(parsed)
        assert report.mode == "validate-only"
        assert report.clean is False
        assert len(report.rejected) == 1

    def test_dry_run_reports_would_create_without_writing(self) -> None:
        parsed = parse_artifact(_artifact(_record_line()))
        repository = FakeRepository()
        report = run_import(parsed, repository=repository)
        assert report.mode == "dry-run"
        assert report.would_create == ("E-001",)
        assert report.created == ()
        assert repository.rows == {}
        assert [call[0] for call in repository.calls] == ["get"]
        assert report.clean is True

    def test_identical_employee_is_skipped(self) -> None:
        parsed = parse_artifact(_artifact(_record_line()))
        repository = FakeRepository((_employee(),))
        report = run_import(parsed, repository=repository, apply=True)
        assert report.identical == ("E-001",)
        assert report.created == ()
        assert [call[0] for call in repository.calls] == ["get"]
        assert repository.forbidden_calls == []

    def test_apply_creates_missing_employee(self) -> None:
        parsed = parse_artifact(_artifact(_record_line()))
        repository = FakeRepository()
        report = run_import(parsed, repository=repository, apply=True)
        assert report.mode == "apply"
        assert report.created == ("E-001",)
        assert report.clean is True
        stored = repository.rows["E-001"]
        assert stored.full_name == NAME
        assert stored.is_active is True
        assert stored.created_at == CREATED_DT

    def test_apply_preserves_supplied_created_at(self) -> None:
        parsed = parse_artifact(
            _artifact(_record_line(created_at="2020-01-02T03:04:05+02:00"))
        )
        repository = FakeRepository()
        run_import(parsed, repository=repository, apply=True)
        stored = repository.rows["E-001"]
        # Same instant as the artifact timestamp; never import time.
        assert stored.created_at.astimezone(UTC) == datetime(
            2020, 1, 2, 1, 4, 5, tzinfo=UTC
        )

    @pytest.mark.parametrize(
        ("existing", "field"),
        [
            ({"full_name": "Grace Hopper"}, "full_name"),
            ({"is_active": False}, "is_active"),
            (
                {"created_at": datetime(2020, 1, 1, tzinfo=UTC)},
                "created_at",
            ),
        ],
    )
    def test_field_differences_are_conflicts_without_mutation(
        self, existing: dict[str, Any], field: str
    ) -> None:
        parsed = parse_artifact(_artifact(_record_line()))
        repository = FakeRepository((_employee(**existing),))
        report = run_import(parsed, repository=repository, apply=True)
        (conflict,) = report.conflicts
        assert conflict.employee_id == "E-001"
        assert conflict.fields == (field,)
        assert report.created == ()
        assert [call[0] for call in repository.calls] == ["get"]
        assert repository.forbidden_calls == []
        assert repository.rows["E-001"] == _employee(**existing)

    def test_apply_without_repository_is_refused(self) -> None:
        parsed = parse_artifact(_artifact(_record_line()))
        with pytest.raises(ImportInputError):
            run_import(parsed, apply=True)

    def test_existing_employee_id_race_is_a_conflict(self) -> None:
        parsed = parse_artifact(_artifact(_record_line()))
        repository = FakeRepository()

        def stale_get(employee_id: str) -> Employee | None:
            # Another runner inserts between our read and our create.
            repository.calls.append(("get", employee_id))
            repository.race_ids.add(employee_id)
            return None

        repository.get = stale_get  # type: ignore[method-assign]
        report = run_import(parsed, repository=repository, apply=True)
        (conflict,) = report.conflicts
        assert conflict.fields == ("employee_id",)
        assert report.created == ()
        assert [call[0] for call in repository.calls].count("create") == 1
        assert [call[0] for call in repository.calls].count("get") == 1

    def test_operational_error_stops_the_run(self) -> None:
        parsed = parse_artifact(
            _artifact(_record_line("E-001"), _record_line("E-002"))
        )
        repository = FakeRepository()
        repository.fail_on_get = OSError("connection dropped")
        report = run_import(parsed, repository=repository, apply=True)
        assert report.stopped is True
        assert report.error_type == "OSError"
        assert report.created == ()
        assert report.clean is False
        assert report.unprocessed == 2
        assert [call[0] for call in repository.calls] == ["get"]

    def test_failure_after_a_successful_create_reports_what_happened(
        self,
    ) -> None:
        parsed = parse_artifact(
            _artifact(_record_line("E-001"), _record_line("E-002"))
        )
        repository = FakeRepository()
        calls = {"n": 0}
        original_get = repository.get

        def flaky_get(employee_id: str) -> Employee | None:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("driver exploded with secret detail")
            return original_get(employee_id)

        repository.get = flaky_get  # type: ignore[method-assign]
        report = run_import(parsed, repository=repository, apply=True)
        assert report.created == ("E-001",)
        assert report.stopped is True
        assert report.error_type == "RuntimeError"
        assert report.unprocessed == 1
        assert "secret detail" not in json.dumps(report_payload(report))

    def test_processing_order_is_deterministic(self) -> None:
        parsed = parse_artifact(
            _artifact(
                _record_line("M-300"),
                _record_line("A-100"),
                _record_line("007"),
            )
        )
        repository = FakeRepository()
        run_import(parsed, repository=repository, apply=True)
        assert [
            call[1] for call in repository.calls if call[0] == "get"
        ] == ["007", "A-100", "M-300"]

    def test_importer_never_updates_or_deactivates(self) -> None:
        parsed = parse_artifact(
            _artifact(_record_line("E-001"), _record_line("E-002"))
        )
        repository = FakeRepository(
            (
                _employee("E-001"),
                _employee("E-002", full_name="Grace Hopper"),
            )
        )
        run_import(parsed, repository=repository, apply=True)
        assert repository.forbidden_calls == []


# ---------------------------------------------------------------------------
# 5. Report payload
# ---------------------------------------------------------------------------


class TestReport:
    def test_payload_contains_counts_and_reason_codes_only(self) -> None:
        parsed = parse_artifact(
            _artifact(
                _record_line("E-001"),
                _record_line("E-002", is_active="yes"),
                _record_line("E-003", full_name="Grace Hopper"),
            )
        )
        repository = FakeRepository((_employee("E-003"),))
        report = run_import(parsed, repository=repository, apply=True)
        payload = report_payload(report)
        assert payload["mode"] == "apply"
        assert payload["line_count"] == 3
        assert payload["valid_records"] == 2
        assert payload["counts"] == {
            "created": 1,
            "would_create": 0,
            "identical": 0,
            "conflict": 1,
            "rejected": 1,
            "unprocessed": 0,
        }
        assert payload["conflicts"] == [
            {"employee_id": "E-003", "fields": ["full_name"]}
        ]
        assert payload["rejected"] == [
            {"line": 2, "employee_id": "E-002", "reason": "invalid_is_active"}
        ]
        serialized = json.dumps(payload)
        assert NAME not in serialized
        assert "Grace Hopper" not in serialized
        assert CREATED_AT not in serialized
        assert "2026-10-31" not in serialized

    def test_write_report_creates_file(self, tmp_path: Path) -> None:
        parsed = parse_artifact(_artifact(_record_line()))
        payload = report_payload(run_import(parsed))
        target = tmp_path / "reports" / "import.json"
        written = write_report(payload, target)
        assert written == target
        assert json.loads(target.read_text(encoding="utf-8"))["mode"] == (
            "validate-only"
        )

    def test_existing_report_is_not_overwritten(self, tmp_path: Path) -> None:
        parsed = parse_artifact(_artifact(_record_line()))
        payload = report_payload(run_import(parsed))
        target = tmp_path / "import.json"
        target.write_text("previous\n", encoding="utf-8")
        with pytest.raises(ImportReportExistsError):
            write_report(payload, target)
        assert target.read_text(encoding="utf-8") == "previous\n"

    def test_explicit_overwrite_replaces_report(self, tmp_path: Path) -> None:
        parsed = parse_artifact(_artifact(_record_line()))
        payload = report_payload(run_import(parsed))
        target = tmp_path / "import.json"
        target.write_text("previous\n", encoding="utf-8")
        write_report(payload, target, overwrite=True)
        assert "previous" not in target.read_text(encoding="utf-8")

    def test_write_failure_leaves_no_partial_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "import.json"

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("simulated replace failure")

        monkeypatch.setattr(employee_import.os, "replace", boom)
        with pytest.raises(ImportReportError):
            write_report({"schema_version": 1}, target)
        assert not target.exists()
        assert [
            path
            for path in tmp_path.iterdir()
            if path.name.startswith(".")
        ] == []


# ---------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------


def _run_cli(
    tmp_path: Path,
    *,
    text: str | None = None,
    apply: bool = False,
    validate_only: bool = False,
    repository: FakeRepository | None = None,
    report_path: Path | None = None,
    overwrite_report: bool = False,
    input_path: Path | None = None,
) -> tuple[int, list[str]]:
    path = input_path or _write_artifact(
        tmp_path, text if text is not None else _artifact(_record_line())
    )
    out: list[str] = []
    code = cli.run_import_cli(
        input_path=path,
        apply=apply,
        validate_only=validate_only,
        report_path=report_path,
        overwrite_report=overwrite_report,
        repository=repository,
        out=out.append,
    )
    return code, out


class TestCli:
    def test_validate_only_clean_run(self, tmp_path: Path) -> None:
        code, out = _run_cli(tmp_path, validate_only=True)
        assert code == 0
        joined = "\n".join(out)
        assert "validation passed for 1 record(s)" in joined
        assert "nothing was imported" in joined

    def test_validate_only_with_invalid_record_exits_one(
        self, tmp_path: Path
    ) -> None:
        code, out = _run_cli(
            tmp_path,
            text=_artifact("{broken"),
            validate_only=True,
        )
        assert code == 1
        joined = "\n".join(out)
        assert "reason=malformed_json" in joined
        assert "nothing was imported" in joined

    def test_runner_refuses_repository_mode_without_a_repository(
        self, tmp_path: Path
    ) -> None:
        # Defensive library contract: the CLI always supplies one now (the
        # production wiring or an injected factory), so a bare runner call
        # with no repository must still refuse instead of guessing.
        code, out = _run_cli(tmp_path, repository=None)
        assert code == 2
        joined = "\n".join(out)
        assert "no repository was supplied" in joined
        assert "Nothing was read from or written to a database" in joined

    def test_runner_refuses_apply_without_a_repository(self, tmp_path: Path) -> None:
        code, out = _run_cli(tmp_path, apply=True)
        assert code == 2
        assert any("no repository was supplied" in line for line in out)

    def test_apply_and_validate_only_are_mutually_exclusive(
        self, tmp_path: Path
    ) -> None:
        code, out = _run_cli(
            tmp_path, apply=True, validate_only=True, repository=FakeRepository()
        )
        assert code == 2
        assert any("mutually exclusive" in line for line in out)

    def test_missing_input_file_exits_two(self, tmp_path: Path) -> None:
        code, out = _run_cli(
            tmp_path,
            validate_only=True,
            input_path=tmp_path / "absent.jsonl",
        )
        assert code == 2
        assert any("does not exist" in line for line in out)

    def test_dry_run_clean_exits_zero(self, tmp_path: Path) -> None:
        repository = FakeRepository()
        code, out = _run_cli(tmp_path, repository=repository)
        assert code == 0
        assert any("would_create=1" in line for line in out)
        assert repository.rows == {}

    def test_apply_creates_rows(self, tmp_path: Path) -> None:
        repository = FakeRepository()
        code, out = _run_cli(tmp_path, apply=True, repository=repository)
        assert code == 0
        assert set(repository.rows) == {"E-001"}
        assert any("1 employee row(s)" in line for line in out)

    def test_conflicts_exit_one_and_are_not_resolved(
        self, tmp_path: Path
    ) -> None:
        repository = FakeRepository((_employee(full_name="Grace Hopper"),))
        code, out = _run_cli(tmp_path, apply=True, repository=repository)
        assert code == 1
        joined = "\n".join(out)
        assert "conflict id=E-001 fields=full_name (not updated)" in joined
        assert "never overwritten" in joined
        assert repository.rows["E-001"].full_name == "Grace Hopper"

    def test_output_contains_no_names_or_timestamps(
        self, tmp_path: Path
    ) -> None:
        code, out = _run_cli(tmp_path, validate_only=True)
        assert code == 0
        joined = "\n".join(out)
        for forbidden in (NAME, CREATED_AT, "2026-10-31"):
            assert forbidden not in joined

    def test_report_is_written(self, tmp_path: Path) -> None:
        report_path = tmp_path / "reports" / "import.json"
        code, out = _run_cli(
            tmp_path,
            validate_only=True,
            report_path=report_path,
        )
        assert code == 0
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        assert payload["mode"] == "validate-only"
        assert NAME not in json.dumps(payload)
        assert any("report written" in line for line in out)

    def test_existing_report_is_refused_before_any_work(
        self, tmp_path: Path
    ) -> None:
        report_path = tmp_path / "import.json"
        report_path.write_text("previous\n", encoding="utf-8")
        repository = FakeRepository()
        code, out = _run_cli(
            tmp_path,
            apply=True,
            repository=repository,
            report_path=report_path,
        )
        assert code == 2
        assert repository.calls == []
        assert report_path.read_text(encoding="utf-8") == "previous\n"
        assert any("--overwrite-report" in line for line in out)

    def test_overwrite_report_flag_replaces_file(
        self, tmp_path: Path
    ) -> None:
        report_path = tmp_path / "import.json"
        report_path.write_text("previous\n", encoding="utf-8")
        code, _ = _run_cli(
            tmp_path,
            validate_only=True,
            report_path=report_path,
            overwrite_report=True,
        )
        assert code == 0
        assert "previous" not in report_path.read_text(encoding="utf-8")

    def test_stopped_run_exits_one_and_names_only_the_type(
        self, tmp_path: Path
    ) -> None:
        repository = FakeRepository()
        repository.fail_on_get = RuntimeError(
            "psycopg detail with a secret host"
        )
        code, out = _run_cli(tmp_path, apply=True, repository=repository)
        assert code == 1
        joined = "\n".join(out)
        assert "RuntimeError" in joined
        assert "secret host" not in joined
        assert "stopped safely" in joined

    def test_main_uses_injected_factory_and_settings(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        settings = AppSettings(
            data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
        )
        settings.exports_dir.mkdir(parents=True)
        (settings.exports_dir / EXPORT_FILENAME).write_text(
            _artifact(_record_line()), encoding="utf-8"
        )
        repository = FakeRepository()
        code = cli.main(
            ["--apply"],
            settings=settings,
            repository_factory=lambda: repository,
        )
        captured = capsys.readouterr()
        assert code == 0
        assert set(repository.rows) == {"E-001"}
        assert NAME not in captured.out

    def test_main_validate_only_without_factory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        settings = AppSettings(
            data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
        )
        settings.exports_dir.mkdir(parents=True)
        (settings.exports_dir / EXPORT_FILENAME).write_text(
            _artifact(_record_line()), encoding="utf-8"
        )
        code = cli.main(["--validate-only"], settings=settings)
        captured = capsys.readouterr()
        assert code == 0
        assert "validation passed" in captured.out


# ---------------------------------------------------------------------------
# 7. Contract hygiene
# ---------------------------------------------------------------------------


class TestContractHygiene:
    def test_module_imports_no_database_driver(self) -> None:
        source = Path(employee_import.__file__).read_text(encoding="utf-8")
        assert "import psycopg" not in source
        assert "from src.db" not in source
        assert "import src.db" not in source

    def test_report_schema_version_is_one(self) -> None:
        assert employee_import.IMPORT_REPORT_SCHEMA_VERSION == 1

    def test_fake_satisfies_the_narrow_protocol(self) -> None:
        assert isinstance(FakeRepository(), EmployeeRepositoryLike)

    def test_import_record_builds_a_model_instance(self) -> None:
        record = ImportRecord(
            employee_id="E-001",
            full_name=NAME,
            is_active=True,
            created_at=CREATED_DT,
        )
        employee = record.to_employee()
        assert isinstance(employee, Employee)
        assert employee.created_at == CREATED_DT


# ---------------------------------------------------------------------------
# 8. Production wiring selection (Step 1.5c-3a)
# ---------------------------------------------------------------------------


def _settings_with_artifact(tmp_path: Path) -> AppSettings:
    """Settings whose export artifact exists — nothing else is created."""
    settings = AppSettings(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
    )
    settings.exports_dir.mkdir(parents=True)
    (settings.exports_dir / EXPORT_FILENAME).write_text(
        _artifact(_record_line()), encoding="utf-8"
    )
    return settings


class TestProductionWiringSelection:
    """The CLI builds the real wiring only for repository-backed modes.

    The production factory is replaced here, so no test in this file ever
    constructs the psycopg-backed wiring or touches a DSN.
    """

    def test_validate_only_never_builds_the_wiring(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def refuse(*, apply: bool) -> None:
            raise AssertionError("--validate-only must not build the wiring")

        monkeypatch.setattr(cli, "_default_repository_factory", refuse)
        code = cli.main(
            ["--validate-only"], settings=_settings_with_artifact(tmp_path)
        )
        assert code == 0
        assert "validation passed" in capsys.readouterr().out

    def test_dry_run_builds_a_read_only_wiring(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        requested: list[bool] = []
        repository = FakeRepository()

        def factory(*, apply: bool) -> Any:
            requested.append(apply)
            return lambda: repository

        monkeypatch.setattr(cli, "_default_repository_factory", factory)
        code = cli.main([], settings=_settings_with_artifact(tmp_path))
        assert code == 0
        # apply=False → the wiring is asked for a read-only repository.
        assert requested == [False]
        assert repository.calls == [("get", "E-001")]
        assert repository.rows == {}
        capsys.readouterr()

    def test_apply_builds_a_writable_wiring(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        requested: list[bool] = []
        repository = FakeRepository()

        def factory(*, apply: bool) -> Any:
            requested.append(apply)
            return lambda: repository

        monkeypatch.setattr(cli, "_default_repository_factory", factory)
        code = cli.main(["--apply"], settings=_settings_with_artifact(tmp_path))
        assert code == 0
        assert requested == [True]
        assert set(repository.rows) == {"E-001"}
        capsys.readouterr()

    def test_injected_factory_wins_over_the_production_wiring(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def refuse(*, apply: bool) -> None:
            raise AssertionError("an injected factory must win")

        monkeypatch.setattr(cli, "_default_repository_factory", refuse)
        repository = FakeRepository()
        code = cli.main(
            [],
            settings=_settings_with_artifact(tmp_path),
            repository_factory=lambda: repository,
        )
        assert code == 0
        assert repository.calls == [("get", "E-001")]
        capsys.readouterr()

    def test_default_factory_maps_the_mode_to_read_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[dict[str, Any]] = []

        def fake_open(**kwargs: Any) -> object:
            captured.append(kwargs)
            return object()

        monkeypatch.setattr(
            db_employee_repository, "open_employee_repository", fake_open
        )
        cli._default_repository_factory(apply=False)()
        cli._default_repository_factory(apply=True)()

        assert captured == [{"read_only": True}, {"read_only": False}]

    def test_importing_the_cli_does_not_import_the_database_wiring(self) -> None:
        code = (
            "import importlib.util, sys;"
            f"sys.path.insert(0, r'{PROJECT_ROOT}');"
            "spec = importlib.util.spec_from_file_location("
            f"'cli_probe', r'{_TOOL_PATH}');"
            "module = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(module);"
            "print('src.db_employee_repository' in sys.modules);"
            "print('psycopg' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["False", "False"]
