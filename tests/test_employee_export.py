"""Focused offline tests for the Step 1.5c-1 employee-metadata exporter.

Every manifest is synthetic and confined to a per-test temporary data
root: no real ``data/`` directory, enrollment artifact, face sample,
embedding, or attendance journal is read, and no database or DSN is
touched. The tool CLI is loaded from its file path (the project's
existing ``importlib`` pattern — ``tools/`` is not a package).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

from src import employee_export
from src.config import AppSettings
from src.employee_export import (
    EXPORT_FILENAME,
    EXPORT_SCHEMA_VERSION,
    ExportError,
    ExportOutputExistsError,
    ExportRejection,
    ExportRecord,
    ExportWriteError,
    collect_records,
    default_output_path,
    export_employee_metadata,
    write_export,
)
from src.enrollment import MANIFEST_FILENAME, safe_dir_name

_TOOL_PATH = (
    Path(__file__).resolve().parent.parent
    / "tools"
    / "export_employee_metadata.py"
)
spec = importlib.util.spec_from_file_location(
    "export_employee_metadata", _TOOL_PATH
)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

NAME = "Ada Lovelace"
CREATED_AT = "2026-10-31T09:12:44.123456+00:00"
EXPECTED_KEYS = [
    "schema_version",
    "employee_id",
    "full_name",
    "is_active",
    "created_at",
]


@pytest.fixture()
def data(tmp_path: Path) -> AppSettings:
    """Isolated settings: nothing outside the temporary directory."""
    return AppSettings(data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


# ---------------------------------------------------------------------------
# Synthetic manifest helpers
# ---------------------------------------------------------------------------


def _payload(
    employee_id: str = "E-001",
    *,
    full_name: object = NAME,
    is_active: object = True,
    created_at: object = CREATED_AT,
    format_version: object = 1,
) -> dict[str, Any]:
    return {
        "format_version": format_version,
        "employee": {
            "employee_id": employee_id,
            "full_name": full_name,
            "is_active": is_active,
            "created_at": created_at,
        },
        "sample_count": 3,
        "samples": [
            {
                "file": "sample_001.png",
                "captured_at": CREATED_AT,
                "quality_decision": "accept",
                "sharpness": 12.5,
                "brightness": 128.0,
            }
        ],
    }


def _manifest_path(
    settings: AppSettings, employee_id: str, *, dir_name: str | None = None
) -> Path:
    directory = settings.employees_dir / (
        dir_name if dir_name is not None else safe_dir_name(employee_id)
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory / MANIFEST_FILENAME


def _write_manifest(
    settings: AppSettings,
    employee_id: str = "E-001",
    *,
    payload: object | None = None,
    dir_name: str | None = None,
    raw_text: str | None = None,
) -> Path:
    path = _manifest_path(settings, employee_id, dir_name=dir_name)
    if raw_text is not None:
        path.write_text(raw_text, encoding="utf-8")
    else:
        path.write_text(
            json.dumps(payload if payload is not None else _payload(employee_id)),
            encoding="utf-8",
        )
    return path


def _rejections(settings: AppSettings) -> dict[str, ExportRejection]:
    collected = collect_records(settings.employees_dir)
    return {item.dir_name: item.reason for item in collected.rejected}


def _line_payloads(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _symlink_or_skip(target: Path, link: Path, *, directory: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create symlinks")


# ---------------------------------------------------------------------------
# 1. Valid export and the exact five-key schema
# ---------------------------------------------------------------------------


class TestValidExport:
    def test_exact_five_key_schema(self, data: AppSettings) -> None:
        _write_manifest(data)
        result = export_employee_metadata(data)

        assert result.written is True
        assert result.exported == 1
        assert result.scanned == 1
        assert result.rejected == ()
        assert result.output_path == data.exports_dir / EXPORT_FILENAME

        lines = result.output_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert list(payload) == EXPECTED_KEYS
        assert payload == {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "employee_id": "E-001",
            "full_name": NAME,
            "is_active": True,
            "created_at": CREATED_AT,
        }

    def test_output_is_one_object_per_line_with_trailing_newline(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data)
        export_employee_metadata(data)
        text = (data.exports_dir / EXPORT_FILENAME).read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert text.count("\n") == 1

    def test_timestamp_preserved_verbatim(self, data: AppSettings) -> None:
        _write_manifest(data)
        export_employee_metadata(data)
        (payload,) = _line_payloads(data.exports_dir / EXPORT_FILENAME)
        assert payload["created_at"] == CREATED_AT

    def test_timestamp_with_z_suffix_accepted_verbatim(
        self, data: AppSettings
    ) -> None:
        _write_manifest(
            data, payload=_payload(created_at="2026-10-31T09:12:44Z")
        )
        export_employee_metadata(data)
        (payload,) = _line_payloads(data.exports_dir / EXPORT_FILENAME)
        assert payload["created_at"] == "2026-10-31T09:12:44Z"

    def test_inactive_employee_preserved(self, data: AppSettings) -> None:
        _write_manifest(data, payload=_payload(is_active=False))
        export_employee_metadata(data)
        (payload,) = _line_payloads(data.exports_dir / EXPORT_FILENAME)
        assert payload["is_active"] is False

    def test_non_ascii_name_round_trips(self, data: AppSettings) -> None:
        _write_manifest(data, payload=_payload(full_name="Zoë Ünal"))
        export_employee_metadata(data)
        (payload,) = _line_payloads(data.exports_dir / EXPORT_FILENAME)
        assert payload["full_name"] == "Zoë Ünal"

    def test_employee_id_comes_from_manifest_not_directory_name(
        self, data: AppSettings
    ) -> None:
        # An ID containing a space encodes to a *different* directory name;
        # the exported ID must still be the exact manifest value.
        _write_manifest(data, "EMP 42")
        export_employee_metadata(data)
        (payload,) = _line_payloads(data.exports_dir / EXPORT_FILENAME)
        assert payload["employee_id"] == "EMP 42"
        assert safe_dir_name("EMP 42") == "EMP%2042"

    def test_leading_zeros_preserved(self, data: AppSettings) -> None:
        _write_manifest(data, "007")
        export_employee_metadata(data)
        (payload,) = _line_payloads(data.exports_dir / EXPORT_FILENAME)
        assert payload["employee_id"] == "007"

    def test_nothing_from_samples_is_exported(self, data: AppSettings) -> None:
        _write_manifest(data)
        export_employee_metadata(data)
        text = (data.exports_dir / EXPORT_FILENAME).read_text(encoding="utf-8")
        for forbidden in (
            "sample_count",
            "samples",
            "sample_001",
            "quality",
            "sharpness",
            "png",
            "path",
        ):
            assert forbidden not in text

    def test_multiple_employees_sorted_by_employee_id(
        self, data: AppSettings
    ) -> None:
        for employee_id in ("M-300", "A-100", "Z-900", "007"):
            _write_manifest(data, employee_id)
        result = export_employee_metadata(data)
        assert result.exported == 4
        exported = [
            payload["employee_id"]
            for payload in _line_payloads(data.exports_dir / EXPORT_FILENAME)
        ]
        assert exported == sorted(["M-300", "A-100", "Z-900", "007"])

    def test_default_output_path_is_under_exports_dir(
        self, data: AppSettings
    ) -> None:
        assert data.exports_dir == data.data_dir / "exports"
        assert default_output_path(data) == (
            data.exports_dir / EXPORT_FILENAME
        )

    def test_custom_output_path_respected(
        self, data: AppSettings, tmp_path: Path
    ) -> None:
        _write_manifest(data)
        custom = tmp_path / "somewhere" / "employees.jsonl"
        result = export_employee_metadata(data, output=custom)
        assert result.written is True
        assert result.output_path == custom
        assert custom.is_file()


# ---------------------------------------------------------------------------
# 2. Field validation and typed rejection codes
# ---------------------------------------------------------------------------


class TestFieldValidation:
    def test_employee_id_normalization_rejected(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data, payload=_payload(employee_id=" E-001"))
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.ID_NORMALIZATION_CHANGED
        }

    @pytest.mark.parametrize("bad_name", ["Ada  Lovelace", " Ada Lovelace", "Ada "])
    def test_full_name_normalization_rejected(
        self, data: AppSettings, bad_name: str
    ) -> None:
        _write_manifest(data, payload=_payload(full_name=bad_name))
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.NAME_NORMALIZATION_CHANGED
        }

    @pytest.mark.parametrize("bad_name", ["", "   ", None, 42, ["Ada"]])
    def test_invalid_full_name_rejected(
        self, data: AppSettings, bad_name: object
    ) -> None:
        _write_manifest(data, payload=_payload(full_name=bad_name))
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.INVALID_FULL_NAME
        }

    @pytest.mark.parametrize("bad_id", ["", "   ", None, 7, ["E-001"]])
    def test_invalid_employee_id_rejected(
        self, data: AppSettings, bad_id: object
    ) -> None:
        _write_manifest(data, payload=_payload(employee_id=bad_id))  # type: ignore[arg-type]
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.INVALID_EMPLOYEE_ID
        }

    @pytest.mark.parametrize("separator_id", ["a/b", "a\\b", "..", "."])
    def test_unencodable_employee_id_rejected(
        self, data: AppSettings, separator_id: str
    ) -> None:
        _write_manifest(
            data,
            payload=_payload(employee_id=separator_id),
            dir_name="SAFE-DIR",
        )
        assert _rejections(data) == {
            "SAFE-DIR": ExportRejection.INVALID_EMPLOYEE_ID
        }

    @pytest.mark.parametrize("bad_active", ["true", 1, 0, None, [True]])
    def test_invalid_is_active_rejected(
        self, data: AppSettings, bad_active: object
    ) -> None:
        _write_manifest(data, payload=_payload(is_active=bad_active))
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.INVALID_IS_ACTIVE
        }

    @pytest.mark.parametrize(
        "bad_timestamp",
        [
            "not-a-date",
            "2026-10-31T09:12:44",  # naive: no offset
            "2026-10-31",
            "",
            "   ",
            None,
            1735632000,
        ],
    )
    def test_invalid_created_at_rejected(
        self, data: AppSettings, bad_timestamp: object
    ) -> None:
        _write_manifest(data, payload=_payload(created_at=bad_timestamp))
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.INVALID_CREATED_AT
        }


# ---------------------------------------------------------------------------
# 3. Manifest envelope validation
# ---------------------------------------------------------------------------


class TestManifestEnvelope:
    @pytest.mark.parametrize("bad_version", [2, 0, "1", True, None, 1.0])
    def test_unsupported_format_version_rejected(
        self, data: AppSettings, bad_version: object
    ) -> None:
        _write_manifest(data, payload=_payload(format_version=bad_version))
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.UNSUPPORTED_FORMAT_VERSION
        }

    @pytest.mark.parametrize(
        "missing_field",
        ["employee_id", "full_name", "is_active", "created_at"],
    )
    def test_missing_employee_field_rejected(
        self, data: AppSettings, missing_field: str
    ) -> None:
        payload = _payload()
        del payload["employee"][missing_field]
        _write_manifest(data, payload=payload)
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.MISSING_FIELDS
        }

    def test_extra_employee_field_rejected(self, data: AppSettings) -> None:
        payload = _payload()
        payload["employee"]["nickname"] = "Ada"
        _write_manifest(data, payload=payload)
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.UNEXPECTED_FIELDS
        }

    def test_missing_employee_section_rejected(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data, payload={"format_version": 1, "sample_count": 0})
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.MISSING_EMPLOYEE_SECTION
        }

    def test_directory_name_mismatch_rejected(
        self, data: AppSettings
    ) -> None:
        _write_manifest(
            data, payload=_payload("E-002"), dir_name=safe_dir_name("E-001")
        )
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.DIRECTORY_MISMATCH
        }

    @pytest.mark.parametrize("raw_text", ["{broken", "", "null", "[1, 2]"])
    def test_malformed_json_rejected(
        self, data: AppSettings, raw_text: str
    ) -> None:
        _write_manifest(data, raw_text=raw_text)
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.MALFORMED_JSON
        }

    def test_non_utf8_manifest_rejected(self, data: AppSettings) -> None:
        path = _manifest_path(data, "E-001")
        path.write_bytes(b"\xff\xfe\x00binary")
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.MANIFEST_UNREADABLE
        }

    def test_directory_without_manifest_rejected(
        self, data: AppSettings
    ) -> None:
        (data.employees_dir / "ORPHAN").mkdir(parents=True)
        assert _rejections(data) == {
            "ORPHAN": ExportRejection.MANIFEST_MISSING
        }

    def test_plain_file_entry_rejected(self, data: AppSettings) -> None:
        data.employees_dir.mkdir(parents=True)
        (data.employees_dir / "stray.txt").write_text("x", encoding="utf-8")
        assert _rejections(data) == {
            "stray.txt": ExportRejection.NOT_A_DIRECTORY
        }


# ---------------------------------------------------------------------------
# 4. Ambiguous identity (duplicate employee IDs)
# ---------------------------------------------------------------------------


class TestDuplicateEmployeeIds:
    def test_split_helper_rejects_every_duplicate_claim(self) -> None:
        first = ExportRecord(
            employee_id="E-001",
            full_name=NAME,
            is_active=True,
            created_at=CREATED_AT,
        )
        second = ExportRecord(
            employee_id="E-001",
            full_name="Grace Hopper",
            is_active=False,
            created_at=CREATED_AT,
        )
        unique = ExportRecord(
            employee_id="E-002",
            full_name="Katherine Johnson",
            is_active=True,
            created_at=CREATED_AT,
        )
        records, rejected = employee_export._split_duplicate_ids(
            [("DIR-A", first), ("DIR-B", second), ("DIR-C", unique)]
        )
        assert [record.employee_id for record in records] == ["E-002"]
        assert {
            (item.dir_name, item.reason) for item in rejected
        } == {
            ("DIR-A", ExportRejection.DUPLICATE_EMPLOYEE_ID),
            ("DIR-B", ExportRejection.DUPLICATE_EMPLOYEE_ID),
        }

    def test_a_single_claim_is_exported_unchanged(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data, "E-001")
        collected = collect_records(data.employees_dir)
        assert collected.scanned == 1
        assert len(collected.records) == 1
        assert collected.rejected == ()


# ---------------------------------------------------------------------------
# 5. Symlink and path-traversal refusal
# ---------------------------------------------------------------------------


class TestSymlinkRefusal:
    def test_symlinked_employee_directory_refused(
        self, data: AppSettings, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        (outside).mkdir()
        (outside / MANIFEST_FILENAME).write_text(
            json.dumps(_payload()), encoding="utf-8"
        )
        data.employees_dir.mkdir(parents=True)
        _symlink_or_skip(
            outside,
            data.employees_dir / safe_dir_name("E-001"),
            directory=True,
        )
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.SYMLINK_REFUSED
        }

    def test_symlinked_manifest_refused(
        self, data: AppSettings, tmp_path: Path
    ) -> None:
        outside_manifest = tmp_path / "elsewhere.json"
        outside_manifest.write_text(
            json.dumps(_payload()), encoding="utf-8"
        )
        directory = data.employees_dir / safe_dir_name("E-001")
        directory.mkdir(parents=True)
        _symlink_or_skip(
            outside_manifest,
            directory / MANIFEST_FILENAME,
            directory=False,
        )
        assert _rejections(data) == {
            safe_dir_name("E-001"): ExportRejection.SYMLINK_REFUSED
        }

    def test_symlinked_manifest_root_refused(
        self, data: AppSettings, tmp_path: Path
    ) -> None:
        real_root = tmp_path / "real-employees"
        real_root.mkdir()
        (real_root / "dummy").mkdir()
        data.data_dir.mkdir(parents=True)
        _symlink_or_skip(
            real_root, data.employees_dir, directory=True
        )
        collected = collect_records(data.employees_dir)
        assert collected.records == ()
        assert collected.scanned == 1
        assert collected.rejected[0].reason is ExportRejection.SYMLINK_REFUSED

    def test_symlink_flag_on_employee_directory_refused(
        self, data: AppSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal branch, exercised where symlinks cannot be created.

        Windows without developer mode refuses ``os.symlink``; the flag is
        therefore simulated for the one entry under test, so the branch
        is still covered on every platform.
        """
        _write_manifest(data)
        real = Path.is_symlink
        target_name = safe_dir_name("E-001")

        def fake(self: Path) -> bool:
            if self.name == target_name and self.parent == data.employees_dir:
                return True
            return real(self)

        monkeypatch.setattr(Path, "is_symlink", fake)
        assert _rejections(data) == {
            target_name: ExportRejection.SYMLINK_REFUSED
        }

    def test_symlink_flag_on_manifest_refused(
        self, data: AppSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The manifest-link refusal branch (simulated, see above)."""
        _write_manifest(data)
        real = Path.is_symlink
        target_dir = safe_dir_name("E-001")

        def fake(self: Path) -> bool:
            if (
                self.name == MANIFEST_FILENAME
                and self.parent.name == target_dir
            ):
                return True
            return real(self)

        monkeypatch.setattr(Path, "is_symlink", fake)
        assert _rejections(data) == {
            target_dir: ExportRejection.SYMLINK_REFUSED
        }

    def test_employee_data_outside_the_root_is_never_read(
        self, data: AppSettings, tmp_path: Path
    ) -> None:
        outside_manifest = tmp_path / "secret.json"
        outside_manifest.write_text(
            json.dumps(_payload("E-999")), encoding="utf-8"
        )
        _write_manifest(data)
        export_employee_metadata(data)
        exported = [
            payload["employee_id"]
            for payload in _line_payloads(data.exports_dir / EXPORT_FILENAME)
        ]
        assert exported == ["E-001"]
        # the unlinked outside file is untouched
        assert json.loads(outside_manifest.read_text(encoding="utf-8"))[
            "employee"
        ]["employee_id"] == "E-999"


# ---------------------------------------------------------------------------
# 6. Output protection, atomicity, permissions
# ---------------------------------------------------------------------------


class TestOutputProtection:
    def test_existing_output_is_never_overwritten_silently(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data)
        target = data.exports_dir / EXPORT_FILENAME
        target.parent.mkdir(parents=True)
        target.write_text("previous artifact\n", encoding="utf-8")

        with pytest.raises(ExportOutputExistsError):
            export_employee_metadata(data)

        assert target.read_text(encoding="utf-8") == "previous artifact\n"

    def test_explicit_overwrite_replaces_artifact(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data)
        target = data.exports_dir / EXPORT_FILENAME
        target.parent.mkdir(parents=True)
        target.write_text("previous artifact\n", encoding="utf-8")

        result = export_employee_metadata(data, overwrite=True)

        assert result.written is True
        assert "previous artifact" not in target.read_text(encoding="utf-8")
        (payload,) = _line_payloads(target)
        assert payload["employee_id"] == "E-001"

    def test_existing_directory_target_refused_even_with_overwrite(
        self, data: AppSettings, tmp_path: Path
    ) -> None:
        _write_manifest(data)
        target = tmp_path / "artifact-dir"
        target.mkdir()
        with pytest.raises(ExportOutputExistsError):
            export_employee_metadata(data, output=target, overwrite=True)

    def test_write_failure_leaves_no_partial_artifact(
        self, data: AppSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_manifest(data)
        target = data.exports_dir / EXPORT_FILENAME

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("simulated replace failure")

        monkeypatch.setattr(employee_export.os, "replace", boom)
        with pytest.raises(ExportWriteError):
            export_employee_metadata(data)

        assert not target.exists()
        leftovers = [
            path.name
            for path in data.exports_dir.iterdir()
            if path.name.startswith(".")
        ]
        assert leftovers == []

    def test_write_failure_preserves_previous_artifact(
        self, data: AppSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_manifest(data)
        target = data.exports_dir / EXPORT_FILENAME
        target.parent.mkdir(parents=True)
        target.write_text("previous artifact\n", encoding="utf-8")

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("simulated replace failure")

        monkeypatch.setattr(employee_export.os, "replace", boom)
        with pytest.raises(ExportWriteError):
            export_employee_metadata(data, overwrite=True)
        assert target.read_text(encoding="utf-8") == "previous artifact\n"

    @pytest.mark.skipif(
        os.name != "posix", reason="POSIX permission bits only"
    )
    def test_output_permissions_are_owner_only(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data)
        result = export_employee_metadata(data)
        assert result.permissions_restricted is True
        mode = result.output_path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_write_export_returns_permission_outcome(
        self, data: AppSettings
    ) -> None:
        record = ExportRecord(
            employee_id="E-001",
            full_name=NAME,
            is_active=True,
            created_at=CREATED_AT,
        )
        applied = write_export([record], data.exports_dir / "x.jsonl")
        assert isinstance(applied, bool)


# ---------------------------------------------------------------------------
# 7. Fail-closed behavior and empty sources
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_any_rejection_blocks_the_artifact(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data, "E-001")
        _write_manifest(data, payload=_payload(is_active="yes"), dir_name="BAD")
        result = export_employee_metadata(data)
        assert result.written is False
        assert result.exported == 0
        assert result.scanned == 2
        assert not result.output_path.exists()

    def test_allow_partial_writes_only_valid_records(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data, "E-001")
        _write_manifest(data, payload=_payload(is_active="yes"), dir_name="BAD")
        result = export_employee_metadata(data, allow_partial=True)
        assert result.written is True
        assert result.exported == 1
        assert len(result.rejected) == 1
        (payload,) = _line_payloads(result.output_path)
        assert payload["employee_id"] == "E-001"

    def test_no_valid_records_writes_no_empty_artifact(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data, raw_text="{broken")
        result = export_employee_metadata(data, allow_partial=True)
        assert result.written is False
        assert result.exported == 0
        assert not result.output_path.exists()

    def test_missing_manifest_root_writes_empty_artifact(
        self, data: AppSettings
    ) -> None:
        assert not data.employees_dir.exists()
        result = export_employee_metadata(data)
        assert result.scanned == 0
        assert result.written is True
        assert result.output_path.read_text(encoding="utf-8") == ""

    def test_missing_root_is_reported_by_collection(
        self, data: AppSettings
    ) -> None:
        collected = collect_records(data.employees_dir)
        assert collected.scanned == 0
        assert collected.records == ()
        assert collected.rejected == ()

    def test_export_result_never_carries_names(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data)
        result = export_employee_metadata(data)
        assert NAME not in repr(result)


# ---------------------------------------------------------------------------
# 8. Source immutability and biometric isolation
# ---------------------------------------------------------------------------


class TestSourceIsolation:
    def test_source_manifests_are_never_modified(
        self, data: AppSettings
    ) -> None:
        path = _write_manifest(data, "E-001")
        before_bytes = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns

        export_employee_metadata(data)

        assert path.read_bytes() == before_bytes
        assert path.stat().st_mtime_ns == before_mtime

    def test_sample_and_embedding_trees_are_untouched(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data)
        samples = data.face_samples_dir / safe_dir_name("E-001")
        samples.mkdir(parents=True)
        (samples / "sample_001.png").write_bytes(b"not-a-real-png")
        embedding = data.embeddings_dir / f"{safe_dir_name('E-001')}.npz"
        data.embeddings_dir.mkdir(parents=True, exist_ok=True)
        embedding.write_bytes(b"\x00\x01binary-vector")
        before = {
            "samples": (samples / "sample_001.png").read_bytes(),
            "embedding": embedding.read_bytes(),
        }

        result = export_employee_metadata(data)

        assert result.exported == 1
        assert (samples / "sample_001.png").read_bytes() == before["samples"]
        assert embedding.read_bytes() == before["embedding"]

    def test_attendance_journal_absent_from_the_export(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data)
        export_employee_metadata(data)
        text = (data.exports_dir / EXPORT_FILENAME).read_text(encoding="utf-8")
        assert "event_id" not in text
        assert "events.jsonl" not in text


# ---------------------------------------------------------------------------
# 9. Operator CLI
# ---------------------------------------------------------------------------


def _run_cli(
    settings: AppSettings,
    *,
    output: Path | None = None,
    overwrite: bool = False,
    allow_partial: bool = False,
) -> tuple[int, list[str]]:
    out: list[str] = []
    code = cli.run_export(
        settings=settings,
        output=output,
        overwrite=overwrite,
        allow_partial=allow_partial,
        out=out.append,
    )
    return code, out


class TestCli:
    def test_success_exit_code_and_summary(self, data: AppSettings) -> None:
        _write_manifest(data)
        code, out = _run_cli(data)
        assert code == 0
        joined = "\n".join(out)
        assert "scanned 1 employee manifest entry" in joined
        assert "validated 1 record(s)" in joined
        assert str(data.exports_dir / EXPORT_FILENAME) in joined

    def test_cli_never_prints_employee_names(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data)
        code, out = _run_cli(data)
        assert code == 0
        assert all(NAME not in line for line in out)

    def test_existing_output_exit_code_two(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data)
        target = data.exports_dir / EXPORT_FILENAME
        target.parent.mkdir(parents=True)
        target.write_text("previous\n", encoding="utf-8")
        code, out = _run_cli(data)
        assert code == 2
        assert any("--overwrite" in line for line in out)
        assert target.read_text(encoding="utf-8") == "previous\n"

    def test_rejections_exit_code_one_and_no_artifact(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data, raw_text="{broken")
        code, out = _run_cli(data)
        assert code == 1
        joined = "\n".join(out)
        assert "reason=malformed_json" in joined
        assert "fail-closed" in joined
        assert not (data.exports_dir / EXPORT_FILENAME).exists()

    def test_allow_partial_exit_code_one_but_written(
        self, data: AppSettings
    ) -> None:
        _write_manifest(data, "E-001")
        _write_manifest(data, raw_text="{broken", dir_name="BAD-DIR")
        code, out = _run_cli(data, allow_partial=True)
        assert code == 1
        assert (data.exports_dir / EXPORT_FILENAME).is_file()
        assert any("partial export" in line for line in out)

    def test_nothing_to_export_exit_zero(self, data: AppSettings) -> None:
        code, out = _run_cli(data)
        assert code == 0
        joined = "\n".join(out)
        assert "nothing to export" in joined
        assert "no employee manifests" in joined
        assert (data.exports_dir / EXPORT_FILENAME).read_text(
            encoding="utf-8"
        ) == ""

    def test_cli_main_uses_injected_settings(
        self, data: AppSettings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_manifest(data)
        output = tmp_path / "cli-artifact.jsonl"
        code = cli.main(
            ["--output", str(output)], settings=data
        )
        assert code == 0
        assert output.is_file()
        captured = capsys.readouterr()
        assert NAME not in captured.out

    def test_cli_main_overwrite_flag(
        self, data: AppSettings, tmp_path: Path
    ) -> None:
        _write_manifest(data)
        output = tmp_path / "cli-artifact.jsonl"
        output.write_text("previous\n", encoding="utf-8")
        assert cli.main(["--output", str(output)], settings=data) == 2
        assert (
            cli.main(
                ["--output", str(output), "--overwrite"], settings=data
            )
            == 0
        )
        assert "previous" not in output.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 10. Contract hygiene
# ---------------------------------------------------------------------------


class TestContractHygiene:
    def test_schema_version_constant_is_one(self) -> None:
        assert EXPORT_SCHEMA_VERSION == 1
        assert EXPORT_FILENAME == "employee_metadata.jsonl"

    def test_export_record_payload_has_exactly_five_keys(self) -> None:
        record = ExportRecord(
            employee_id="E-001",
            full_name=NAME,
            is_active=True,
            created_at=CREATED_AT,
        )
        payload = record.to_payload()
        assert list(payload) == EXPECTED_KEYS
        assert set(payload) == set(EXPECTED_KEYS)

    def test_module_imports_no_database_layer(self) -> None:
        source = Path(employee_export.__file__).read_text(encoding="utf-8")
        assert "import psycopg" not in source
        assert "from src.db" not in source
        assert "import src.db" not in source

    def test_export_errors_share_one_base_class(self) -> None:
        assert issubclass(ExportOutputExistsError, ExportError)
        assert issubclass(ExportWriteError, ExportError)
