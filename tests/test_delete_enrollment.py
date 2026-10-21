"""Focused tests for ``tools/delete_enrollment.py`` (Step 18).

Every artifact is synthetic and confined to each test's temporary
directory: the embedding record pair is created through the *real*
``EmbeddingStorageService.save`` (synthetic vectors — never real
biometrics), and manifest/sample files carry inert placeholder bytes.
No camera is opened and no real ``data/`` directory is ever touched;
the tool's final-verification storage is the real service pointed at
the temporary root (or an injected double for the failure scenarios).

The tool module is loaded from its file path (same ``importlib``
pattern as the other tool-CLI tests) because ``tools/`` scripts are
not importable packages.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from src.config import AppSettings
from src.embedding_storage import EmbeddingStorageService

_TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "delete_enrollment.py"

spec = importlib.util.spec_from_file_location("delete_enrollment", _TOOL_PATH)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

ID = "007"
NEIGHBOR_ID = "EMP-0042"
NAME = "Delete Test Employee"


@pytest.fixture()
def data(tmp_path: Path) -> AppSettings:
    """AppSettings over an isolated temporary data root."""
    return AppSettings(data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


def _synthetic_vector(dim: int = 8, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vector = rng.standard_normal(dim).astype(np.float32)
    vector[0] = 1.0
    return vector


def _fabricate_enrollment(settings: AppSettings, employee_id: str) -> None:
    """Create real-shaped artifacts for one employee (tmp only).

    Uses the real storage service (real record naming/layout) and
    inert placeholder bytes for manifest/samples — the deletion tool
    never reads file contents.
    """
    EmbeddingStorageService(settings=settings).save(
        employee_id, _synthetic_vector(seed=abs(hash(employee_id)) % 2**32)
    )

    dir_name = cli.safe_dir_name(employee_id)
    employee_dir = settings.employees_dir / dir_name
    employee_dir.mkdir(parents=True)
    (employee_dir / cli.MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "format_version": 1,
                "employee": {"employee_id": employee_id, "full_name": NAME},
            }
        ),
        encoding="utf-8",
    )
    samples_dir = settings.face_samples_dir / dir_name
    samples_dir.mkdir(parents=True)
    (samples_dir / "sample_001.png").write_bytes(b"png-placeholder")
    (samples_dir / "sample_002.png").write_bytes(b"png-placeholder")


def _artifact_file_list(settings: AppSettings) -> list[str]:
    """Every real path currently inside the three data roots."""
    roots = [
        settings.embeddings_dir,
        settings.employees_dir,
        settings.face_samples_dir,
    ]
    out: list[str] = []
    for root in roots:
        if root.exists():
            out.extend(
                str(p.relative_to(root.parent)) for p in root.rglob("*") if p.is_file()
            )
    return sorted(out)


def _run(settings, *, employee_id: str = ID, execute: bool, confirm: bool,
         storage_factory: Any = None) -> tuple[int, list[str]]:
    out: list[str] = []
    code = cli.run_enrollment_deletion(
        employee_id,
        execute=execute,
        confirm_delete=confirm,
        settings=settings,
        storage_factory=storage_factory,
        out=out.append,
    )
    return code, out


# ---------------------------------------------------------------------------
# 1. Invalid / malicious employee IDs — nothing happens at all
# ---------------------------------------------------------------------------


class TestInvalidIds:
    @pytest.mark.parametrize(
        "bad_id",
        ["", "   ", ".", "..", "../escape", "a/b", r"a\\b", "..\\.."],
    )
    def test_rejected_before_any_filesystem_work(
        self, data: AppSettings, bad_id: str
    ) -> None:
        code, out = _run(data, employee_id=bad_id, execute=True, confirm=True)
        assert code == 1
        assert any("invalid employee ID" in line for line in out)
        # real (empty) roots untouched
        assert _artifact_file_list(data) == []


# ---------------------------------------------------------------------------
# 2. Nonexistent enrollment — refused, no changes
# ---------------------------------------------------------------------------


class TestNonexistentEnrollment:
    def test_refused_with_no_changes(self, data: AppSettings) -> None:
        code, out = _run(data, execute=True, confirm=True)
        assert code == 1
        assert any("nothing to delete" in line for line in out)
        assert _artifact_file_list(data) == []


# ---------------------------------------------------------------------------
# 3. Preview mode — full report, zero changes
# ---------------------------------------------------------------------------


class TestPreviewMakesNoChanges:
    def test_report_lists_artifacts_and_changes_nothing(
        self, data: AppSettings
    ) -> None:
        _fabricate_enrollment(data, ID)
        before = _artifact_file_list(data)
        code, out = _run(data, execute=False, confirm=False)
        assert code == 0
        assert any("PREVIEW" in line for line in out)
        assert any("sample PNG file(s)" in line for line in out)
        assert _artifact_file_list(data) == before  # byte-identical state


# ---------------------------------------------------------------------------
# 4. Missing individual artifacts — tolerated, others still removed
# ---------------------------------------------------------------------------


class TestMissingIndividualArtifacts:
    def test_embedding_only(self, data: AppSettings) -> None:
        EmbeddingStorageService(settings=data).save(
            ID, _synthetic_vector(seed=7)
        )
        code, _ = _run(data, execute=True, confirm=True)
        assert code == 0
        assert EmbeddingStorageService(settings=data).list_employee_ids() == ()

    def test_samples_only(self, data: AppSettings) -> None:
        samples_dir = data.face_samples_dir / "007"
        samples_dir.mkdir(parents=True)
        (samples_dir / "sample_001.png").write_bytes(b"png-placeholder")
        code, _ = _run(data, execute=True, confirm=True)
        assert code == 0
        assert not samples_dir.exists()


# ---------------------------------------------------------------------------
# 5. Confirmation declined — --execute alone is refused
# ---------------------------------------------------------------------------


class TestConfirmationDeclined:
    def test_execute_without_confirm_changes_nothing(
        self, data: AppSettings
    ) -> None:
        _fabricate_enrollment(data, ID)
        before = _artifact_file_list(data)
        code, out = _run(data, execute=True, confirm=False)
        assert code == 2
        assert any("confirmation refused" in line for line in out)
        assert _artifact_file_list(data) == before


# ---------------------------------------------------------------------------
# 6. Successful deletion of all enrollment artifacts
# ---------------------------------------------------------------------------


class TestSuccessfulFullDeletion:
    def test_all_groups_gone_and_no_longer_discoverable(
        self, data: AppSettings
    ) -> None:
        _fabricate_enrollment(data, ID)
        code, out = _run(data, execute=True, confirm=True)
        assert code == 0
        assert any("success" in line for line in out)
        storage = EmbeddingStorageService(settings=data)
        assert storage.list_employee_ids() == ()  # recognition candidate gone
        dir_name = cli.safe_dir_name(ID)
        assert not (data.employees_dir / dir_name).exists()      # manifest gone
        assert not (data.face_samples_dir / dir_name).exists()   # samples gone
        assert not (data.embeddings_dir / f"{dir_name}.npz").is_file()
        assert not (data.embeddings_dir / f"{dir_name}.meta.json").is_file()


# ---------------------------------------------------------------------------
# 7. Deletion/storage failure — honest partial-failure reporting
# ---------------------------------------------------------------------------


class TestDeletionFailure:
    @pytest.mark.parametrize(
        ("error_kind", "expected_phrase"),
        [
            ("OSError", "deletion failed"),
            ("RuntimeError", "unexpected deletion failure"),
        ],
    )
    def test_mid_deletion_failure_reports_what_remains(
        self, data: AppSettings, monkeypatch, error_kind: str,
        expected_phrase: str,
    ) -> None:
        _fabricate_enrollment(data, ID)
        real_rmtree = cli.shutil.rmtree

        def failing_rmtree(path: object, **_kw: Any) -> None:
            # Employee-manifest removal succeeds; sample removal fails.
            if Path(path).name == "007" and str(path).replace("\\", "/").endswith(
                "face_samples/007"
            ):
                if error_kind == "OSError":
                    raise OSError("disk trouble")
                raise RuntimeError("surprise")
            real_rmtree(path, **_kw)

        monkeypatch.setattr(cli.shutil, "rmtree", failing_rmtree)
        code, out = _run(data, execute=True, confirm=True)
        assert code == 1
        text = "".join(out)
        assert expected_phrase in text
        assert "face_samples/007" in text or "007" in text  # the failing path
        assert "Remaining artifact groups: samples" in text  # honest leftover
        # the embedding + manifest pairs were actually removed before failing
        dir_name = cli.safe_dir_name(ID)
        assert (data.employees_dir / dir_name).exists() is False
        assert not (data.embeddings_dir / f"{dir_name}.npz").is_file()


# ---------------------------------------------------------------------------
# 8. Final candidate-verification failure — loud, never success
# ---------------------------------------------------------------------------


class TestFinalVerificationFailure:
    def test_id_still_discoverable_after_removal_fails_loudly(
        self, data: AppSettings
    ) -> None:
        _fabricate_enrollment(data, ID)

        class _StubbornStorage:
            def list_employee_ids(self) -> tuple[str, ...]:
                return (ID,)  # pretends the ID still exists

        code, out = _run(
            data, execute=True, confirm=True,
            storage_factory=lambda: _StubbornStorage(),
        )
        assert code == 1
        assert "final candidate verification failed" in "".join(out)
        assert "success" not in "".join(out)


# ---------------------------------------------------------------------------
# 9. Isolation — neighbors and shared files are never touched
# ---------------------------------------------------------------------------


class TestIsolation:
    def test_neighbor_and_model_files_untouched(self, data: AppSettings) -> None:
        _fabricate_enrollment(data, ID)
        _fabricate_enrollment(data, NEIGHBOR_ID)
        models_dir = data.data_dir.parent / "models"
        models_dir.mkdir()
        model_file = models_dir / "sface.onnx"
        model_file.write_bytes(b"model-bytes")  # shared artifact, never deleted

        neighbor_before = _artifact_paths_of(NEIGHBOR_ID, data)

        code, _ = _run(data, execute=True, confirm=True)
        assert code == 0
        assert neighbor_before == _artifact_paths_of(NEIGHBOR_ID, data)
        assert model_file.is_file()  # shared model untouched

        storage = EmbeddingStorageService(settings=data)
        assert storage.list_employee_ids() == (NEIGHBOR_ID,)  # neighbor remains


def _artifact_paths_of(employee_id: str, settings: AppSettings) -> set[str]:
    dir_name = cli.safe_dir_name(employee_id)
    paths: set[str] = {
        str(settings.employees_dir / dir_name / cli.MANIFEST_FILENAME),
        str(settings.face_samples_dir / dir_name / "sample_001.png"),
        str(settings.face_samples_dir / dir_name / "sample_002.png"),
        str(settings.embeddings_dir / f"{dir_name}.npz"),
        str(settings.embeddings_dir / f"{dir_name}.meta.json"),
    }
    return paths


# ---------------------------------------------------------------------------
# 10. Success only when the target is provably no longer discoverable
# ---------------------------------------------------------------------------


class TestSuccessRequiresAbsence:
    def test_real_storage_absence_is_the_success_condition(
        self, data: AppSettings
    ) -> None:
        _fabricate_enrollment(data, ID)
        storage = EmbeddingStorageService(settings=data)
        assert storage.list_employee_ids() == (ID,)  # precondition
        code, out = _run(data, execute=True, confirm=True)
        assert code == 0
        assert storage.list_employee_ids() == ()  # the recognizer agrees
        # and success was claimed only after the verification step:
        assert any("no longer a recognition candidate" in line for line in out)

    def test_leftover_file_group_blocks_success(
        self, data: AppSettings, monkeypatch
    ) -> None:
        """A group that re-materializes must fail the final verification."""
        _fabricate_enrollment(data, ID)
        real_rmtree = cli.shutil.rmtree

        def resurrecting_rmtree(path: object, **_kw: Any) -> None:
            # Simulates another process recreating a removed artifact:
            # the tool must catch this in the literal-file check (not
            # just via the candidate list).
            real_rmtree(path, **_kw)
            if Path(path).name == "007" and "employees" in str(path):
                (data.employees_dir / "007").mkdir()

        monkeypatch.setattr(cli.shutil, "rmtree", resurrecting_rmtree)
        code, out = _run(data, execute=True, confirm=True)
        assert code == 1
        assert "final artifact verification failed" in "".join(out)
        assert "success" not in "".join(out)
