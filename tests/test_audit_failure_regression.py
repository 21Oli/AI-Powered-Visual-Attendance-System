"""P5.5.8.3 end-to-end failure and recovery regression (offline, ``tmp_path``).

The unit suites already prove each mechanism in isolation. This module proves the
**combinations** that only an end-to-end run can show:

* a failure during a request poisons the writer and every *later* request fails
  closed, with no new bytes and no duplicate sequence;
* a durable intent survives a failed result and stays verifiable;
* a short write is reported by the verifier and never repaired;
* a corrupt manifest or a recovery-required history puts the API into restricted
  mode without creating, replacing or repairing anything;
* each documented crash state is reproduced, reported with the documented exit
  code, resolved **only** by the documented operator command, and leaves the
  sealed history byte-identical with an unbroken sequence and hash chain.

Synthetic installations only: no PostgreSQL, no ``.env``, no ``data/audit/``, no
real attendance, employee, image or biometric data.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.audit_rotation as rotation_module
import src.audit_writer as writer_module
import tools.audit_rotate as rotate_cli
import tools.audit_verify as verify_cli
from src.audit_layout import AuditLayout
from src.audit_schema import AuditOutcome, AuditReasonCode
from src.audit_service import AuditService
from src.audit_writer import AuditJournalWriter
from src.user import Role
from tests.audit_support import install, install_events
from tests.test_api_audit import (
    API_PREFIX,
    FakeAttendance,
    FakeEmployees,
    build_app,
    call,
    create_employee,
    events,
    journal_path,
)

AUDIT_UNAVAILABLE_BODY = {
    "code": "AUDIT_UNAVAILABLE",
    "message": "The operation was not attempted because it could not be audited.",
}

#: The four mandatory audited mutations, invoked exactly as the API exposes them.
MUTATIONS = {
    "employee_create": lambda app: create_employee(app),
    "employee_update": lambda app: call(
        app,
        method="PATCH",
        path=f"{API_PREFIX}/employees/E1",
        body={"full_name": "New"},
    ),
    "employee_deactivate": lambda app: call(
        app,
        method="POST",
        path=f"{API_PREFIX}/employees/E1/deactivate",
    ),
    "attendance_checkout": lambda app: call(
        app,
        method="POST",
        path=f"{API_PREFIX}/attendance/E1/checkout",
        body={},
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def audit_dir(root: Path) -> str:
    return str(AuditLayout.for_directory(root / "audit").directory)


def snapshot(root: Path) -> dict[str, bytes]:
    """Every *history* file under the synthetic installation, by relative path.

    The advisory ``.rotation.lock`` is excluded: taking the layout lock creates
    it, which is not a change to audit history (dry-run lock behaviour is
    covered by the rotation suite).
    """
    base = root / "audit"
    return {
        str(path.relative_to(base)).replace("\\", "/"): path.read_bytes()
        for path in sorted(base.rglob("*"))
        if path.is_file() and path.name != ".rotation.lock"
    }


def verify(root: Path, capsys) -> tuple[int, str]:
    path = AuditLayout.for_directory(root / "audit").journal
    code = verify_cli.main(["--journal", str(path)])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def rotate(root: Path, capsys, *args: str) -> tuple[int, str]:
    code = rotate_cli.main([*args, "--audit-dir", audit_dir(root)])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def journal_only(root: Path, count: int = 3) -> AuditLayout:
    """A fresh installation: one active segment, no manifest, no archive."""
    install_events(
        root, segments=[], active=("2026-11-001", count), manifest=False
    )
    layout = AuditLayout.for_directory(root / "audit")
    layout.archive_directory.mkdir(parents=True, exist_ok=True)
    return layout


def fail_fsync_at(monkeypatch: pytest.MonkeyPatch, position: int) -> None:
    """Fail ``os.fsync`` on the ``position``-th writer call onwards."""
    real = writer_module._fsync_file
    calls = {"n": 0}

    def flaky(handle) -> None:  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] >= position:
            raise OSError("injected fsync failure")
        real(handle)

    monkeypatch.setattr(writer_module, "_fsync_file", flaky)


def fail_seal(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self, **kwargs):  # noqa: ANN001
        raise rotation_module.ManifestStoreError("write failed")

    monkeypatch.setattr(rotation_module.ManifestStore, "append", boom)


def fail_active_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(path: Path) -> None:
        raise OSError("cannot create")

    monkeypatch.setattr(rotation_module, "_create_empty_file", boom)


def archive_bytes(root: Path) -> bytes:
    archives = sorted(
        AuditLayout.for_directory(root / "audit").archive_directory.glob("*.jsonl")
    )
    assert len(archives) == 1
    return archives[0].read_bytes()


def append_one(root: Path) -> object:
    """Append one login event through a fresh, verified writer."""
    layout = AuditLayout.for_directory(root / "audit")
    service = AuditService(AuditJournalWriter(layout.journal))
    context = AuditService.context(
        SimpleNamespace(user_id="user-1", role=Role.ADMIN)
    )
    return service.record_login(
        outcome=AuditOutcome.SUCCESS,
        reason_code=AuditReasonCode.OK,
        context=context,
    )


# ---------------------------------------------------------------------------
# A/B. Audit append failures and the intent/result protocol, through the API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(MUTATIONS), ids=list(MUTATIONS))
def test_intent_failure_blocks_the_mutation_before_business(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """A failed intent write must stop the operation before any business call."""
    employees = FakeEmployees()
    attendance = FakeAttendance()
    fail_fsync_at(monkeypatch, position=1)
    app, *_ = build_app(tmp_path, employees=employees, attendance=attendance)

    response = MUTATIONS[name](app)
    monkeypatch.undo()

    assert response["status"] == 503
    assert response["payload"]["error"] == AUDIT_UNAVAILABLE_BODY
    # No mutating business call: target resolution (``get``) may precede the
    # intent, the mutation itself never does.
    assert all(entry[0] == "get" for entry in employees.calls)
    assert attendance.calls == []
    # The flush precedes the injected fsync failure, so the file may hold the
    # intent; it must never hold a partial record or a result.
    written = events(tmp_path)
    assert len(written) <= 1
    if written:
        assert written[0].reason_code is AuditReasonCode.AWAITING_RESULT
        assert written[0].correlation_id is None


def test_failed_intent_poisons_the_writer_for_later_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer is poisoned: a later, unrelated request still fails closed."""
    employees = FakeEmployees()
    fail_fsync_at(monkeypatch, position=1)
    app, *_ = build_app(tmp_path, employees=employees)
    first = create_employee(app)
    monkeypatch.undo()

    before = journal_path(tmp_path).read_bytes()
    second = create_employee(app)
    later = call(app, method="POST", path=f"{API_PREFIX}/employees/E1/deactivate")

    assert first["status"] == 503
    assert first["payload"]["error"]["code"] == "AUDIT_UNAVAILABLE"
    for response in (second, later):
        assert response["status"] == 503
        assert response["payload"]["error"] == AUDIT_UNAVAILABLE_BODY
    assert all(entry[0] == "get" for entry in employees.calls)
    assert journal_path(tmp_path).read_bytes() == before


def test_failed_result_leaves_a_durable_intent_and_refuses_later_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Intent survives, result does not; the intent-only journal still verifies."""
    employees = FakeEmployees()
    fail_fsync_at(monkeypatch, position=2)
    app, *_ = build_app(tmp_path, employees=employees)
    response = create_employee(app)
    monkeypatch.undo()

    assert response["status"] == 503
    assert response["payload"]["error"]["code"] == "AUDIT_RESULT_UNCERTAIN"
    assert "Do not retry blindly" in response["payload"]["error"]["message"]
    assert [entry[0] for entry in employees.calls] == ["create"]

    # The intent is durable. The unconfirmed result may also have reached the
    # file (flush precedes fsync), which is exactly why the outcome is uncertain.
    records = events(tmp_path)
    assert 1 <= len(records) <= 2
    assert records[0].reason_code is AuditReasonCode.AWAITING_RESULT
    assert records[0].correlation_id is None
    if len(records) == 2:
        assert records[1].correlation_id == records[0].audit_id
    code, output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_OK, output

    durable = journal_path(tmp_path).read_bytes()
    later = create_employee(app)
    assert later["status"] == 503
    assert journal_path(tmp_path).read_bytes() == durable
    assert len(employees.calls) == 1


def test_short_write_is_reported_by_the_verifier_and_never_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A truncated record is damage, not success, and nothing repairs it."""
    real = writer_module._write_line
    calls = {"n": 0}

    def short(handle, payload: bytes) -> int:  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] >= 2:
            return real(handle, payload[:-10])
        return real(handle, payload)

    monkeypatch.setattr(writer_module, "_write_line", short)
    app, *_ = build_app(tmp_path)
    response = create_employee(app)
    monkeypatch.undo()

    assert response["status"] == 503
    assert response["payload"]["error"]["code"] == "AUDIT_RESULT_UNCERTAIN"

    damaged = journal_path(tmp_path).read_bytes()
    assert not damaged.endswith(b"\n")
    code, output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_INTEGRITY_FAILURE
    assert "FAIL" in output
    assert journal_path(tmp_path).read_bytes() == damaged


def test_restricted_response_does_not_disclose_internals(tmp_path: Path) -> None:
    app, *_ = build_app(tmp_path)
    journal_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    journal_path(tmp_path).write_bytes(b'{"broken":true}\n')

    corrupt = journal_path(tmp_path).read_bytes()
    response = create_employee(app)
    body = json.dumps(response["payload"])

    assert response["status"] == 503
    assert response["payload"]["error"] == AUDIT_UNAVAILABLE_BODY
    assert set(response["payload"]) == {"error"}
    assert set(response["payload"]["error"]) == {"code", "message"}
    for leak in (str(tmp_path), "audit.jsonl", "Traceback", "OSError"):
        assert leak not in body
    assert journal_path(tmp_path).read_bytes() == corrupt


# ---------------------------------------------------------------------------
# C. Restricted mode entered from history problems (not just a corrupt journal)
# ---------------------------------------------------------------------------


def test_corrupt_manifest_enters_restricted_without_touching_history(
    tmp_path: Path, capsys
) -> None:
    installation = install(tmp_path, sealed=[2], active=1)
    manifest = installation.layout.manifest.read_bytes()
    installation.layout.manifest.write_bytes(manifest[:-4])
    before = snapshot(tmp_path)

    app, employees, *_ = build_app(tmp_path)
    response = create_employee(app)

    assert response["status"] == 503
    assert response["payload"]["error"] == AUDIT_UNAVAILABLE_BODY
    assert employees.calls == []
    assert app.state.audit_subsystem.state == "restricted"
    assert snapshot(tmp_path) == before
    code, _output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_RECOVERY_REQUIRED


def test_missing_active_after_a_seal_blocks_the_api_and_needs_create_active(
    tmp_path: Path, capsys
) -> None:
    installation = install(tmp_path, sealed=[2], active=0)
    installation.layout.journal.unlink()
    before = snapshot(tmp_path)

    app, employees, *_ = build_app(tmp_path)
    response = create_employee(app)

    assert response["status"] == 503
    assert employees.calls == []
    assert not installation.layout.journal.exists()
    assert snapshot(tmp_path) == before

    code, output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_RECOVERY_REQUIRED
    assert "ACTIVE_SEGMENT_MISSING" in output

    code, output = rotate(tmp_path, capsys, "--create-active")
    assert code == rotate_cli.EXIT_OK, output
    assert installation.layout.journal.read_bytes() == b""

    code, output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_OK, output


def test_unregistered_archive_blocks_the_api_and_needs_register_existing(
    tmp_path: Path, capsys
) -> None:
    installation = install_events(
        tmp_path,
        segments=[("2026-11-001", 3)],
        active=("2026-11-002", 1),
        manifest=False,
    )
    assert not installation.layout.manifest.exists()
    archived = archive_bytes(tmp_path)

    app, employees, *_ = build_app(tmp_path)
    response = create_employee(app)
    assert response["status"] == 503
    assert employees.calls == []

    code, output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_RECOVERY_REQUIRED

    code, output = rotate(tmp_path, capsys, "--register-existing")
    assert code == rotate_cli.EXIT_OK, output
    assert archive_bytes(tmp_path) == archived

    code, output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_OK, output

    app, *_ = build_app(tmp_path)
    assert create_employee(app)["status"] == 201


# ---------------------------------------------------------------------------
# D. Rotation crash states: report, recover only by command, preserve history
# ---------------------------------------------------------------------------


def test_seal_not_recorded_recovery_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    layout = journal_only(tmp_path, 3)
    original_journal = layout.journal.read_bytes()

    fail_seal(monkeypatch)
    code, output = rotate(tmp_path, capsys)
    monkeypatch.undo()

    assert code == rotate_cli.EXIT_RECOVERY_REQUIRED
    assert "SEAL_NOT_RECORDED" in output
    assert not layout.journal.exists()
    assert not layout.manifest.exists()
    archived = archive_bytes(tmp_path)
    assert archived == original_journal
    crashed = snapshot(tmp_path)

    # Nothing recovers automatically: a second report changes nothing.
    code, output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_RECOVERY_REQUIRED
    assert snapshot(tmp_path) == crashed

    code, output = rotate(tmp_path, capsys, "--register-existing")
    assert code == rotate_cli.EXIT_OK, output
    assert archive_bytes(tmp_path) == archived
    entry = json.loads(layout.manifest.read_text(encoding="utf-8"))
    assert entry["record_count"] == 3
    assert entry["last_global_sequence"] == 3

    # The active journal is still missing, so recovery is not finished yet.
    code, output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_RECOVERY_REQUIRED
    assert "ACTIVE_SEGMENT_MISSING" in output

    code, output = rotate(tmp_path, capsys, "--create-active")
    assert code == rotate_cli.EXIT_OK, output
    code, output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_OK, output
    assert archive_bytes(tmp_path) == archived


def test_active_not_created_recovery_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    layout = journal_only(tmp_path, 2)
    fail_active_creation(monkeypatch)
    code, output = rotate(tmp_path, capsys)
    monkeypatch.undo()

    assert code == rotate_cli.EXIT_RECOVERY_REQUIRED
    assert "ACTIVE_NOT_CREATED" in output
    assert not layout.journal.exists()
    archived = archive_bytes(tmp_path)
    crashed = snapshot(tmp_path)

    code, output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_RECOVERY_REQUIRED
    assert snapshot(tmp_path) == crashed

    code, output = rotate(tmp_path, capsys, "--create-active")
    assert code == rotate_cli.EXIT_OK, output
    assert archive_bytes(tmp_path) == archived

    code, output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_OK, output

    # The next append continues the sealed chain exactly.
    sealed = [
        json.loads(line)["last_record_hash"]
        for line in layout.manifest.read_text(encoding="utf-8").splitlines()
    ][0]
    event = append_one(tmp_path)
    records = [
        json.loads(line)
        for line in layout.journal.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["sequence"] == 3
    assert records[0]["segment_id"] == "2026-11-002"
    assert records[0]["segment_sequence"] == 1
    assert records[0]["previous_hash"] == sealed
    assert records[0]["record_hash"] == event.record_hash

    code, output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_OK, output
    assert "Records verified: 3" in output


def test_rotation_cli_refuses_to_register_a_tampered_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    layout = journal_only(tmp_path, 3)
    fail_seal(monkeypatch)
    rotate(tmp_path, capsys)
    monkeypatch.undo()

    path = next(layout.archive_directory.glob("*.jsonl"))
    tampered = bytearray(path.read_bytes())
    tampered[20] ^= 0x01
    path.write_bytes(bytes(tampered))
    before = snapshot(tmp_path)

    code, output = rotate(tmp_path, capsys, "--register-existing")
    assert code == rotate_cli.EXIT_INTEGRITY_FAILURE, output
    assert not layout.manifest.exists()
    assert snapshot(tmp_path) == before


def test_torn_manifest_tail_has_no_automatic_recovery_or_command(
    tmp_path: Path, capsys
) -> None:
    installation = install(tmp_path, sealed=[2], active=1)
    manifest = installation.layout.manifest.read_bytes()
    installation.layout.manifest.write_bytes(manifest[:-3])
    before = snapshot(tmp_path)

    code, _output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_RECOVERY_REQUIRED

    code, output = rotate(tmp_path, capsys, "--register-existing")
    assert code == rotate_cli.EXIT_RECOVERY_REQUIRED, output
    code, output = rotate(tmp_path, capsys, "--create-active")
    assert code == rotate_cli.EXIT_RECOVERY_REQUIRED, output
    assert "MANIFEST_TORN_TAIL" in output
    assert snapshot(tmp_path) == before


def test_rotation_preserves_archived_bytes_and_entry_hash_chain(
    tmp_path: Path, capsys
) -> None:
    """A second rotation cannot rewrite, reorder or re-seal the first archive."""
    layout = journal_only(tmp_path, 2)
    assert rotate(tmp_path, capsys)[0] == rotate_cli.EXIT_OK
    first_archive = archive_bytes(tmp_path)
    first_manifest = layout.manifest.read_bytes()

    append_one(tmp_path)
    assert rotate(tmp_path, capsys)[0] == rotate_cli.EXIT_OK

    archives = sorted(layout.archive_directory.glob("*.jsonl"))
    assert len(archives) == 2
    assert archives[0].read_bytes() == first_archive
    assert layout.manifest.read_bytes().startswith(first_manifest.rstrip(b"\n"))
    entries = [
        json.loads(line)
        for line in layout.manifest.read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["manifest_sequence"] for entry in entries] == [1, 2]
    assert entries[1]["previous_manifest_hash"] == entries[0]["entry_hash"]
    assert entries[1]["first_global_sequence"] == (
        entries[0]["last_global_sequence"] + 1
    )
    assert layout.journal.exists()

    code, output = verify(tmp_path, capsys)
    assert code == verify_cli.EXIT_OK, output


def test_recovery_does_not_create_a_second_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    layout = journal_only(tmp_path, 2)
    fail_seal(monkeypatch)
    rotate(tmp_path, capsys)
    monkeypatch.undo()

    before = sorted(path.name for path in layout.archive_directory.iterdir())
    rotate(tmp_path, capsys, "--register-existing")
    rotate(tmp_path, capsys, "--create-active")
    after = sorted(path.name for path in layout.archive_directory.iterdir())

    assert before == after
    assert len(after) == 1


def test_rotation_operations_are_not_reachable_from_the_api(
    tmp_path: Path,
) -> None:
    """The API never rotates, repairs or registers: only the operator CLI does."""
    app, *_ = build_app(tmp_path)
    assert create_employee(app)["status"] == 201
    assert len(events(tmp_path)) == 2
    layout = AuditLayout.for_directory(tmp_path / "audit")
    assert not layout.manifest.exists()
    assert not list(layout.archive_directory.glob("*.jsonl"))
