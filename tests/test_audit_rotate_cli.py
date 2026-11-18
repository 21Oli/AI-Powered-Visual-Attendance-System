"""Tests for the operator rotation CLI (P5.5.7 stage C/D).

Every installation is synthetic and lives under ``tmp_path``; the real
``data/audit`` directory is never touched.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import src.audit_rotation as rotation_module
import tools.audit_rotate as cli
from src.audit_layout import AuditLayout
from src.audit_rotation import RotationIntegrityError, RotationRecoveryRequiredError
from tests.audit_support import install, install_events, write_events, build_events

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _journal_only(root: Path, count: int = 2) -> AuditLayout:
    install_events(
        root, segments=[], active=("2026-11-001", count), manifest=False
    )
    return AuditLayout.for_directory(root / "audit")


def _run(capsys, *args: str) -> tuple[int, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _dir(root: Path) -> str:
    return str(root / "audit")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_status_of_a_new_installation(tmp_path: Path, capsys) -> None:
    _journal_only(tmp_path, 2)
    code, output = _run(capsys, "--status", "--audit-dir", _dir(tmp_path))
    assert code == cli.EXIT_OK
    assert "Audit installation: OK" in output
    assert "Manifest entries: 0" in output
    assert "Active records: 2" in output


def test_status_after_rotation(tmp_path: Path, capsys) -> None:
    _journal_only(tmp_path, 2)
    cli.main(["--audit-dir", _dir(tmp_path)])
    capsys.readouterr()
    code, output = _run(capsys, "--status", "--audit-dir", _dir(tmp_path))
    assert code == cli.EXIT_OK
    assert "Manifest entries: 1" in output
    assert "Archived segments: 1" in output
    assert "Sealed records: 2" in output
    assert "Active segment: none" in output


def test_status_reports_integrity_failure(tmp_path: Path, capsys) -> None:
    installation = install(tmp_path, sealed=[2])
    installation.archive_path(0).write_bytes(b"{broken\n")
    code, output = _run(capsys, "--status", "--audit-dir", _dir(tmp_path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert "Audit installation: FAIL" in output
    assert "Category:" in output


def test_status_reports_recovery_required(tmp_path: Path, capsys) -> None:
    installation = install(tmp_path, sealed=[2])
    installation.archive_path(0).unlink()
    code, output = _run(capsys, "--status", "--audit-dir", _dir(tmp_path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    installation = install(tmp_path, sealed=[2], active=0)
    installation.layout.journal.unlink()
    code, output = _run(capsys, "--status", "--audit-dir", _dir(tmp_path))
    assert code == cli.EXIT_RECOVERY_REQUIRED


def test_status_uses_the_journal_option(tmp_path: Path, capsys) -> None:
    layout = _journal_only(tmp_path, 1)
    code, output = _run(capsys, "--status", "--journal", str(layout.journal))
    assert code == cli.EXIT_OK
    assert "Active records: 1" in output


# ---------------------------------------------------------------------------
# Dry run and rotation
# ---------------------------------------------------------------------------


def test_dry_run_reports_the_plan_without_changes(tmp_path: Path, capsys) -> None:
    layout = _journal_only(tmp_path, 3)
    before = layout.journal.read_bytes()
    code, output = _run(capsys, "--dry-run", "--audit-dir", _dir(tmp_path))
    assert code == cli.EXIT_OK
    assert "DRY RUN" in output
    assert "Segment to seal: 2026-11-001" in output
    assert "Archive target: archive/audit-1-3.jsonl" in output
    assert "Next segment: 2026-11-002" in output
    assert layout.journal.read_bytes() == before
    assert not layout.manifest.exists()


def test_rotation_seals_and_reports(tmp_path: Path, capsys) -> None:
    layout = _journal_only(tmp_path, 2)
    code, output = _run(capsys, "--audit-dir", _dir(tmp_path))
    assert code == cli.EXIT_OK
    assert "Audit rotation: SEALED" in output
    assert "Archive: archive/audit-1-2.jsonl" in output
    assert layout.archive_path(1, 2).is_file()
    assert layout.manifest.is_file()
    assert layout.journal.read_bytes() == b""


def test_rotation_refuses_an_empty_journal(tmp_path: Path, capsys) -> None:
    install_events(tmp_path, segments=[], active=None, manifest=False)
    code, output = _run(capsys, "--audit-dir", _dir(tmp_path))
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert "NOTHING_TO_SEAL" in output


def test_rotation_sanitizes_unexpected_failures(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_only(tmp_path, 1)

    def boom(self, **kwargs):  # noqa: ANN001
        raise RuntimeError("C:/secret/path exploded")

    monkeypatch.setattr(rotation_module.RotationOperations, "rotate", boom)
    code, output = _run(capsys, "--audit-dir", _dir(tmp_path))
    assert code == cli.EXIT_INTERNAL_ERROR
    assert "C:/secret" not in output
    assert "RuntimeError" not in output


def test_rotation_reports_recovery_required(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal_only(tmp_path, 1)

    def boom(self, **kwargs):  # noqa: ANN001
        raise RotationRecoveryRequiredError(
            "seal missing", category="SEAL_NOT_RECORDED"
        )

    monkeypatch.setattr(rotation_module.RotationOperations, "rotate", boom)
    code, output = _run(capsys, "--audit-dir", _dir(tmp_path))
    assert code == cli.EXIT_RECOVERY_REQUIRED
    assert "RECOVERY REQUIRED" in output
    assert "SEAL_NOT_RECORDED" in output
    assert "runbook" in output.lower()


# ---------------------------------------------------------------------------
# Recovery commands
# ---------------------------------------------------------------------------


def test_register_existing_command(tmp_path: Path, capsys) -> None:
    layout = _journal_only(tmp_path, 2)
    manifest_store_error = rotation_module.ManifestStoreError

    original = rotation_module.ManifestStore.append

    def failing(self, **kwargs):  # noqa: ANN001
        raise manifest_store_error("write failed")

    rotation_module.ManifestStore.append = failing
    try:
        code, _ = _run(capsys, "--audit-dir", _dir(tmp_path))
        assert code == cli.EXIT_RECOVERY_REQUIRED
    finally:
        rotation_module.ManifestStore.append = original

    code, output = _run(
        capsys, "--register-existing", "--audit-dir", _dir(tmp_path)
    )
    assert code == cli.EXIT_OK
    assert "Audit rotation: REGISTERED" in output
    assert "Segment: 2026-11-001" in output
    assert len(layout.manifest.read_text(encoding="utf-8").splitlines()) == 1


def test_create_active_command(tmp_path: Path, capsys) -> None:
    installation = install(tmp_path, sealed=[2], active=0)
    installation.layout.journal.unlink()
    code, output = _run(capsys, "--create-active", "--audit-dir", _dir(tmp_path))
    assert code == cli.EXIT_OK
    assert "Audit recovery: ACTIVE CREATED" in output
    assert "Next segment: 2026-11-002" in output
    assert installation.layout.journal.read_bytes() == b""


def test_retention_command(tmp_path: Path, capsys) -> None:
    installation = install(tmp_path, sealed=[1])
    code, output = _run(
        capsys,
        "--retention",
        "2026-11-001",
        "--audit-dir",
        _dir(tmp_path),
    )
    assert code == cli.EXIT_OK
    assert "Audit retention: RECORDED" in output
    assert "archived_offsite" in output
    assert installation.archive_path(0).is_file()


def test_retention_refuses_unknown_segment(tmp_path: Path, capsys) -> None:
    install(tmp_path, sealed=[1])
    code, output = _run(
        capsys, "--retention", "2026-11-009", "--audit-dir", _dir(tmp_path)
    )
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert "UNKNOWN_SEGMENT" in output


# ---------------------------------------------------------------------------
# Usage, defaults and entry points
# ---------------------------------------------------------------------------


def test_conflicting_actions_are_a_usage_error(tmp_path: Path, capsys) -> None:
    code, output = _run(
        capsys, "--status", "--dry-run", "--audit-dir", _dir(tmp_path)
    )
    assert code == cli.EXIT_USAGE_ERROR
    assert "INVALID_ARGUMENTS" in output


def test_unknown_option_is_a_usage_error(capsys) -> None:
    code, output = _run(capsys, "--nope")
    assert code == cli.EXIT_USAGE_ERROR
    assert "INVALID_ARGUMENTS" in output


def test_default_directory_is_project_data_audit() -> None:
    assert cli.DEFAULT_AUDIT_DIRECTORY == (
        PROJECT_ROOT / "data" / "audit"
    )


def test_exit_codes_are_stable() -> None:
    assert cli.EXIT_OK == 0
    assert cli.EXIT_INTEGRITY_FAILURE == 1
    assert cli.EXIT_UNAVAILABLE == 2
    assert cli.EXIT_USAGE_ERROR == 3
    assert cli.EXIT_INTERNAL_ERROR == 4
    assert cli.EXIT_RECOVERY_REQUIRED == 5


def test_module_and_script_entry_points_run(tmp_path: Path) -> None:
    _journal_only(tmp_path, 1)
    commands = (
        [sys.executable, "-m", "tools.audit_rotate"],
        [sys.executable, str(PROJECT_ROOT / "tools" / "audit_rotate.py")],
    )
    for command in commands:
        completed = subprocess.run(
            [*command, "--status", "--audit-dir", _dir(tmp_path)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "Audit installation: OK" in completed.stdout


def test_missing_destination_fails_closed(tmp_path: Path, capsys) -> None:
    code, output = _run(capsys, "--dry-run", "--audit-dir", _dir(tmp_path))
    assert code in {cli.EXIT_UNAVAILABLE, cli.EXIT_INTEGRITY_FAILURE}
    assert "Audit operation: FAIL" in output
    assert "UNAVAILABLE" in output
    assert not (tmp_path / "audit").exists()


def test_output_never_contains_record_contents(tmp_path: Path, capsys) -> None:
    layout = _journal_only(tmp_path, 1)
    write_events(layout.journal, build_events([("2026-11-001", 1)]))
    code, output = _run(capsys, "--status", "--audit-dir", _dir(tmp_path))
    assert code == cli.EXIT_OK
    assert "00000001-1111-4111-8111-111111111111" not in output
    assert "record_hash" not in output
