"""Manifest-aware verifier tests (P5.5.7 stage F).

The original active-journal tests stay in ``test_audit_verify.py``; this file
covers the manifest, archive, coverage-mode and recovery-required behaviour.
Every installation is synthetic and lives under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tools.audit_verify as cli
from tests.audit_support import build_events, install, install_events, write_events


def _run(capsys, path: Path) -> tuple[int, str]:
    code = cli.main(["--journal", str(path)])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _fingerprint(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# PASS paths
# ---------------------------------------------------------------------------


def test_full_history_with_active_segment_passes(
    tmp_path: Path, capsys
) -> None:
    installation = install(tmp_path, sealed=[2, 3], active=1)
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_OK
    assert "verification: PASS" in output
    assert "Manifest entries: 2" in output
    assert "Archived segments: 2" in output
    assert "Records verified: 6" in output
    assert "Global sequence: 1..6" in output
    assert "Segments: 3" in output
    assert "First segment: 2026-11-001" in output
    assert "Last segment: 2026-11-003" in output
    assert "Coverage modes: full-record-chain, segment-manifest-continuity" in output
    assert "Segments not content-verified: (none)" in output
    assert "Final manifest hash:" in output


def test_empty_active_after_seal_passes(tmp_path: Path, capsys) -> None:
    installation = install(tmp_path, sealed=[2], active=0)
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_OK
    assert "Records verified: 2" in output
    assert "none (empty active journal)" in output


def test_active_only_installation_reports_full_record_chain(
    tmp_path: Path, capsys
) -> None:
    installation = install_events(
        tmp_path, segments=[], active=("2026-11-001", 2), manifest=False
    )
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_OK
    assert "Manifest entries: 0" in output
    assert "Coverage modes: full-record-chain" in output


def test_anchor_only_coverage_is_reported_honestly(
    tmp_path: Path, capsys
) -> None:
    installation = install(tmp_path, sealed=[2], retention=[0], active=1)
    installation.archive_path(0).unlink()
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_OK
    assert "Anchor-only segments: 2026-11-001" in output
    assert "Segments not content-verified: 2026-11-001" in output
    assert "anchor-only, segment-manifest-continuity" in output
    assert "full-record-chain" not in output
    assert "NOT" in output
    assert "not a full historical verification" in output


def test_restored_archive_after_retention_is_content_verified(
    tmp_path: Path, capsys
) -> None:
    installation = install(tmp_path, sealed=[2], retention=[0])
    installation.archive_path(0).unlink()
    verify_code, _ = _run(capsys, installation.layout.journal)
    assert verify_code == cli.EXIT_OK
    write_events(installation.archive_path(0), installation.segments[0][1])
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_OK
    assert "Anchor-only segments: (none)" in output
    assert "full-record-chain" in output


def test_empty_installation_passes_as_an_empty_chain(
    tmp_path: Path, capsys
) -> None:
    install_events(tmp_path, segments=[], active=None, manifest=False)
    layout_journal = tmp_path / "audit" / "audit.jsonl"
    code, output = _run(capsys, layout_journal)
    assert code == cli.EXIT_OK
    assert "valid empty chain" in output
    assert "Manifest entries: 0" in output


# ---------------------------------------------------------------------------
# FAIL paths and exit codes
# ---------------------------------------------------------------------------


def test_missing_archive_without_retention_is_an_integrity_failure(
    tmp_path: Path, capsys
) -> None:
    installation = install(tmp_path, sealed=[2])
    installation.archive_path(0).unlink()
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert "ARCHIVE_MISSING" in output
    assert "Segment: 2026-11-001" in output


def test_unregistered_archive_requires_recovery(tmp_path: Path, capsys) -> None:
    installation = install(tmp_path, sealed=[1], active=1)
    write_events(
        installation.layout.archive_path(3, 4),
        build_events([("2026-11-004", 2)]),
    )
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_RECOVERY_REQUIRED
    assert "ARCHIVE_UNREGISTERED" in output
    assert "runbook" in output.lower()
    assert "PASS" not in output


def test_archives_without_a_manifest_require_recovery(
    tmp_path: Path, capsys
) -> None:
    install(tmp_path, sealed=[1], manifest=False)
    code, output = _run(capsys, tmp_path / "audit" / "audit.jsonl")
    assert code == cli.EXIT_RECOVERY_REQUIRED
    assert "ARCHIVE_UNREGISTERED" in output


def test_torn_manifest_requires_recovery(tmp_path: Path, capsys) -> None:
    installation = install(tmp_path, sealed=[1])
    raw = installation.layout.manifest.read_bytes()
    installation.layout.manifest.write_bytes(raw[:-2])
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_RECOVERY_REQUIRED
    assert "MANIFEST_TORN_TAIL" in output
    assert "Line: 1" in output


def test_corrupt_manifest_is_an_integrity_failure(
    tmp_path: Path, capsys
) -> None:
    installation = install(tmp_path, sealed=[1])
    installation.layout.manifest.write_bytes(b"not json\n")
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert "MANIFEST_CORRUPT" in output


def test_duplicate_manifest_seal_is_an_integrity_failure(
    tmp_path: Path, capsys
) -> None:
    from dataclasses import replace

    from src.audit_manifest import compute_entry_hash

    installation = install(tmp_path, sealed=[1])
    entry = installation.manifest[0]
    duplicate = replace(
        entry,
        manifest_sequence=2,
        previous_manifest_hash=entry.entry_hash,
    )
    body = duplicate.as_body()
    body["entry_hash"] = "0" * 64
    object.__setattr__(duplicate, "entry_hash", compute_entry_hash(body))
    installation.layout.manifest.write_bytes(
        entry.to_json_line().encode("utf-8")
        + b"\n"
        + duplicate.to_json_line().encode("utf-8")
        + b"\n"
    )
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert "MANIFEST_DUPLICATE" in output


def test_missing_active_journal_after_a_seal_requires_recovery(
    tmp_path: Path, capsys
) -> None:
    installation = install(tmp_path, sealed=[2], active=0)
    installation.layout.journal.unlink()
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_RECOVERY_REQUIRED
    assert "ACTIVE_SEGMENT_MISSING" in output
    assert "--create-active" in output


def test_torn_archive_is_an_integrity_failure(tmp_path: Path, capsys) -> None:
    installation = install(tmp_path, sealed=[2])
    path = installation.archive_path(0)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert "ARCHIVE_TORN_TAIL" in output


def test_archive_count_mismatch_is_an_integrity_failure(
    tmp_path: Path, capsys
) -> None:
    installation = install(tmp_path, sealed=[3])
    write_events(installation.archive_path(0), installation.segments[0][1][:2])
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert "ARCHIVE_COUNT_MISMATCH" in output


def test_active_journal_must_continue_the_sealed_chain(
    tmp_path: Path, capsys
) -> None:
    installation = install(tmp_path, sealed=[2], active=1)
    write_events(installation.layout.journal, build_events([("2026-11-002", 1)]))
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert "BROKEN_PREDECESSOR" in output


def test_unexpected_archive_entry_is_an_integrity_failure(
    tmp_path: Path, capsys
) -> None:
    installation = install(tmp_path, sealed=[1])
    (installation.archive_directory / "notes.txt").write_text("x", encoding="utf-8")
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert "ARCHIVE_UNEXPECTED_FILE" in output


def test_missing_journal_without_history_is_unavailable(
    tmp_path: Path, capsys
) -> None:
    code, output = _run(capsys, tmp_path / "audit" / "audit.jsonl")
    assert code == cli.EXIT_UNAVAILABLE
    assert "JOURNAL_MISSING" in output
    assert not (tmp_path / "audit").exists()


# ---------------------------------------------------------------------------
# Read-only guarantees and sanitization
# ---------------------------------------------------------------------------


def test_successful_verification_modifies_nothing(tmp_path: Path, capsys) -> None:
    installation = install(tmp_path, sealed=[2], active=1)
    before = _fingerprint(tmp_path)
    code, _ = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_OK
    assert _fingerprint(tmp_path) == before


def test_failed_verification_modifies_nothing(tmp_path: Path, capsys) -> None:
    installation = install(tmp_path, sealed=[2])
    installation.archive_path(0).unlink()
    before = _fingerprint(tmp_path)
    code, _ = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert _fingerprint(tmp_path) == before


def test_verifier_never_creates_a_lock_file(tmp_path: Path, capsys) -> None:
    installation = install(tmp_path, sealed=[1], active=1)
    _run(capsys, installation.layout.journal)
    assert not installation.layout.rotation_lock.exists()


def test_archive_failure_output_is_sanitized(tmp_path: Path, capsys) -> None:
    installation = install(tmp_path, sealed=[1])
    path = installation.archive_path(0)
    body = installation.segments[0][1][0].as_body()
    body["audit_id"] = "alice-secret"
    path.write_bytes(
        __import__("json").dumps(body).encode("utf-8") + b"\n"
    )
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert "alice-secret" not in output
    assert "record_hash" not in output
    assert "Traceback" not in output


def test_manifest_failure_output_is_sanitized(tmp_path: Path, capsys) -> None:
    installation = install(tmp_path, sealed=[1])
    installation.layout.manifest.write_bytes(b'{"secret-token": "abc"}\n')
    code, output = _run(capsys, installation.layout.journal)
    assert code == cli.EXIT_INTEGRITY_FAILURE
    assert "secret-token" not in output
    assert "abc" not in output


def test_recovery_exit_code_is_distinct_and_stable() -> None:
    assert cli.EXIT_RECOVERY_REQUIRED == 5
    assert cli.EXIT_RECOVERY_REQUIRED not in {
        cli.EXIT_OK,
        cli.EXIT_INTEGRITY_FAILURE,
        cli.EXIT_UNAVAILABLE,
        cli.EXIT_USAGE_ERROR,
        cli.EXIT_INTERNAL_ERROR,
    }


def test_all_exit_categories_have_explanations() -> None:
    for category in cli._EXPLANATIONS:
        assert cli._EXPLANATIONS[category]
