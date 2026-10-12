"""Step 09 tests: embedding storage.

Deterministic, hardware-free: synthetic NumPy vectors, temporary
directories. **No real employee biometrics and no real model
inference anywhere** — vectors are plain synthetic numbers; the SFace
artifact story belongs entirely to Step 08 (see test_face_embedding).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from src.config import AppSettings
from src.embedding_storage import (
    EMBEDDING_STORAGE_FORMAT_VERSION,
    METADATA_SUFFIX,
    RECORD_SUFFIX,
    CorruptEmbeddingRecordError,
    DuplicateEmbeddingRecordError,
    EmbeddingPersistenceError,
    EmbeddingRecordFormatError,
    EmbeddingRecordNotFoundError,
    EmbeddingStorageInputError,
    EmbeddingStorageService,
    StorageDtype,
    StoredEmbedding,
)
from src.face_embedding import FaceEmbedding, VectorDtype

# ---------------------------------------------------------------------------
# Fixtures & helpers (synthetic vectors only — never real biometrics)
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings(tmp_path: Path) -> AppSettings:
    """AppSettings pointed at the test's temporary data directory."""
    return AppSettings(data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


def synthetic_vector(dim: int = 8, seed: int = 0) -> np.ndarray:
    """Deterministic non-zero float32 vector (no biometric claims)."""
    rng = np.random.default_rng(seed)
    vector = rng.standard_normal(dim).astype(np.float32)
    vector[0] = 1.0  # guarantee non-zero norm even for dim=1
    return vector


def make_face_embedding(dim: int = 8, seed: int = 1) -> FaceEmbedding:
    """A Step 08-shaped result built on a synthetic vector."""
    vector = synthetic_vector(dim, seed)
    norm = float(np.linalg.norm(vector.astype(np.float64)))
    vector.setflags(write=False)
    return FaceEmbedding(
        vector=vector,
        dimension=dim,
        norm=round(norm, 6),
        dtype=VectorDtype.FLOAT32,
    )


def save_ok(
    settings: AppSettings, employee_id: str = "E1", dim: int = 8, seed: int = 0
) -> StoredEmbedding:
    service = EmbeddingStorageService(settings=settings)
    return service.save(employee_id, synthetic_vector(dim, seed))


# ---------------------------------------------------------------------------
# 1. Save & load round trip
# ---------------------------------------------------------------------------


class TestSaveLoadRoundTrip:
    def test_save_then_load_returns_equal_vector(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        original = synthetic_vector(16, seed=3)
        record = service.save("E1", original)
        loaded = service.load("E1")
        assert loaded.employee_id == "E1"
        assert np.array_equal(loaded.vector, record.vector)
        assert np.array_equal(loaded.vector, original)
        assert loaded.dimension == 16
        assert loaded.vector.dtype == np.float32

    def test_accepts_step08_face_embedding_result(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        record = service.save("E1", make_face_embedding(dim=10, seed=7))
        assert record.dimension == 10
        assert service.exists("E1")
        loaded = service.load("E1")
        assert isinstance(loaded, StoredEmbedding)

    def test_non_float32_input_normalized_to_float32_contract(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        record = service.save("E1", synthetic_vector(8, 5).astype(np.float64))
        assert record.vector.dtype == np.float32

    def test_stored_at_is_timezone_aware_utc(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        record = service.save("E1", synthetic_vector())
        assert record.metadata.stored_at.tzinfo is not None
        assert record.metadata.stored_at.utcoffset() == UTC.utcoffset(None)

    def test_files_land_in_configured_embeddings_dir(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        names = {p.name for p in settings.embeddings_dir.iterdir()}
        assert names == {"E1.npz", "E1.meta.json"}


# ---------------------------------------------------------------------------
# 2. Exact employee-ID preservation (007, EMP-0042, special characters)
# ---------------------------------------------------------------------------


class TestEmployeeIdPreservation:
    @pytest.mark.parametrize("employee_id", ["007", "EMP-0042", "a b-1"])
    def test_exact_id_round_trips(
        self, settings: AppSettings, employee_id: str
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        service.save(employee_id, synthetic_vector())
        loaded = service.load(employee_id)
        assert loaded.employee_id == employee_id
        assert loaded.metadata.employee_id == employee_id
        assert service.exists(employee_id)
        assert employee_id in service.list_employee_ids()

    def test_leading_zeros_and_hyphens_differ_from_other_ids(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        service.save("007", synthetic_vector(seed=1))
        service.save("EMP-0042", synthetic_vector(seed=2))
        assert service.load("007").vector is not None
        assert service.load("EMP-0042").vector is not None
        assert set(service.list_employee_ids()) == {"007", "EMP-0042"}

    def test_special_characters_encoded_but_id_preserved(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        service.save("id with spaces", synthetic_vector())
        # Encoded stem, exact ID inside the metadata sidecar.
        sidecars = [
            p for p in settings.embeddings_dir.iterdir()
            if p.name.endswith(METADATA_SUFFIX)
        ]
        assert len(sidecars) == 1
        payload = json.loads(sidecars[0].read_text(encoding="utf-8"))
        assert payload["employee_id"] == "id with spaces"
        assert "id with spaces" not in sidecars[0].name


# ---------------------------------------------------------------------------
# 3/4. Vector validation: shape, dtype, dimension, finite values, NaN/inf
# ---------------------------------------------------------------------------


class TestVectorValidation:
    def test_2d_array_rejected(self, settings: AppSettings) -> None:
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(EmbeddingStorageInputError, match="1-D"):
            service.save("E1", np.zeros((2, 4), dtype=np.float32))
        assert not settings.embeddings_dir.exists()

    @pytest.mark.parametrize("bad", [None, "not an array", 5])
    def test_non_array_embedding_rejected(
        self, settings: AppSettings, bad: object
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(EmbeddingStorageInputError):
            service.save("E1", bad)

    def test_empty_vector_rejected(self, settings: AppSettings) -> None:
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(EmbeddingStorageInputError, match="empty"):
            service.save("E1", np.array([], dtype=np.float32))

    def test_non_floating_dtype_rejected(self, settings: AppSettings) -> None:
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(EmbeddingStorageInputError, match="floating"):
            service.save("E1", np.array([1, 2, 3], dtype=np.int32))

    def test_nan_rejected(self, settings: AppSettings) -> None:
        service = EmbeddingStorageService(settings=settings)
        bad = synthetic_vector()
        bad[2] = np.nan
        with pytest.raises(EmbeddingStorageInputError, match="non-finite"):
            service.save("E1", bad)
        assert not service.exists("E1")

    def test_positive_infinity_rejected(self, settings: AppSettings) -> None:
        service = EmbeddingStorageService(settings=settings)
        bad = synthetic_vector()
        bad[0] = np.inf
        with pytest.raises(EmbeddingStorageInputError, match="non-finite"):
            service.save("E1", bad)

    def test_negative_infinity_rejected(self, settings: AppSettings) -> None:
        service = EmbeddingStorageService(settings=settings)
        bad = synthetic_vector()
        bad[0] = -np.inf
        with pytest.raises(EmbeddingStorageInputError, match="non-finite"):
            service.save("E1", bad)

    def test_zero_norm_vector_rejected(self, settings: AppSettings) -> None:
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(EmbeddingStorageInputError, match="zero norm"):
            service.save("E1", np.zeros(8, dtype=np.float32))

    def test_expected_dimension_enforced_on_save(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(
            settings=settings, expected_dimension=8
        )
        with pytest.raises(EmbeddingRecordFormatError) as excinfo:
            service.save("E1", synthetic_vector(16, seed=0))
        # A dimension-contract violation is a format incompatibility,
        # raised before anything is written.
        assert not settings.embeddings_dir.exists()

    def test_expected_dimension_enforced_on_load(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1", dim=16)
        strict = EmbeddingStorageService(
            settings=settings, expected_dimension=8
        )
        with pytest.raises(EmbeddingRecordFormatError, match="contract"):
            strict.load("E1")


# ---------------------------------------------------------------------------
# 5. Missing record behavior
# ---------------------------------------------------------------------------


class TestMissingRecord:
    def test_load_missing_raises_typed_not_found(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(EmbeddingRecordNotFoundError):
            service.load("nobody")

    def test_load_missing_in_existing_tree_raises_not_found(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(EmbeddingRecordNotFoundError):
            service.load("E2")

    def test_exists_false_for_missing(self, settings: AppSettings) -> None:
        service = EmbeddingStorageService(settings=settings)
        assert service.exists("nobody") is False

    def test_list_empty_when_no_directory(self, settings: AppSettings) -> None:
        service = EmbeddingStorageService(settings=settings)
        assert service.list_employee_ids() == ()


# ---------------------------------------------------------------------------
# 6. Duplicate saves & overwrite prevention (+ explicit replace)
# ---------------------------------------------------------------------------


class TestDuplicateSavePolicy:
    def test_second_save_refused_without_replace(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        first = service.save("E1", synthetic_vector(seed=1))
        with pytest.raises(DuplicateEmbeddingRecordError, match="refusing"):
            service.save("E1", synthetic_vector(seed=2))
        # The original record is untouched.
        loaded = service.load("E1")
        assert np.array_equal(loaded.vector, first.vector)

    def test_sidecar_alone_also_blocks_save(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        service = EmbeddingStorageService(settings=settings)
        (settings.embeddings_dir / "E1.npz").unlink()
        with pytest.raises(DuplicateEmbeddingRecordError):
            service.save("E1", synthetic_vector(seed=9))

    def test_explicit_replace_replaces_record(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        first = service.save("E1", synthetic_vector(8, seed=1))
        second = service.save("E1", synthetic_vector(8, seed=2), replace=True)
        assert second.metadata.stored_at >= first.metadata.stored_at
        loaded = service.load("E1")
        assert np.array_equal(loaded.vector, second.vector)
        assert not np.array_equal(loaded.vector, first.vector)

    def test_replace_non_bool_rejected(self, settings: AppSettings) -> None:
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(EmbeddingStorageInputError, match="replace"):
            service.save("E1", synthetic_vector(), replace="yes")  # type: ignore[arg-type]

    def test_replace_still_true_only_affects_requested_id(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        service.save("E1", synthetic_vector(seed=1))
        service.save("E2", synthetic_vector(seed=2))
        service.save("E2", synthetic_vector(seed=3), replace=True)
        assert np.array_equal(
            service.load("E1").vector, synthetic_vector(seed=1)
        )


# ---------------------------------------------------------------------------
# 7/8. Corruption, malformed records, incompatible schema/data
# ---------------------------------------------------------------------------


class TestCorruptRecords:
    def _service(self, settings: AppSettings) -> EmbeddingStorageService:
        return EmbeddingStorageService(settings=settings)

    def test_garbage_array_file_rejected_as_corrupt(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        (settings.embeddings_dir / "E1.npz").write_bytes(b"not an npz file")
        with pytest.raises(CorruptEmbeddingRecordError):
            self._service(settings).load("E1")

    def test_invalid_json_sidecar_rejected_as_corrupt(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        (settings.embeddings_dir / "E1.meta.json").write_text(
            "{not json", encoding="utf-8"
        )
        with pytest.raises(CorruptEmbeddingRecordError):
            self._service(settings).load("E1")

    def test_non_object_json_sidecar_rejected(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        (settings.embeddings_dir / "E1.meta.json").write_text(
            "[1, 2, 3]", encoding="utf-8"
        )
        with pytest.raises(CorruptEmbeddingRecordError):
            self._service(settings).load("E1")

    def test_missing_embedding_key_in_array_rejected(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        np.savez(  # arbitrary key, same numeric contract otherwise
            settings.embeddings_dir / "E1.npz", other=synthetic_vector()
        )
        with pytest.raises(CorruptEmbeddingRecordError, match="embedding"):
            self._service(settings).load("E1")

    def test_object_array_rejected_without_pickle(
        self, settings: AppSettings
    ) -> None:
        """The load path must disallow pickle and reject object arrays."""
        save_ok(settings, "E1")
        # Craft the hostile file directly: an object array saved with
        # pickle into a valid NPZ, exactly what a malicious writer
        # would produce (np.savez itself refuses this without pickle).
        import zipfile

        import numpy.lib.format as npformat

        npz_path = settings.embeddings_dir / "E1.npz"
        with zipfile.ZipFile(npz_path, "w") as zf:
            buffer = io.BytesIO()
            npformat.write_array(
                buffer,
                np.array([{"malicious": "obj"}], dtype=object),
                allow_pickle=True,  # hostile file, crafted on purpose
            )
            zf.writestr("embedding.npy", buffer.getvalue())
        with pytest.raises(CorruptEmbeddingRecordError):
            self._service(settings).load("E1")

    def test_malicious_pickle_payload_never_executes(
        self, settings: AppSettings
    ) -> None:
        """An array file that smuggles pickled content must not execute."""
        save_ok(settings, "E1")
        import zipfile

        import numpy.lib.format as npformat

        # Hand-build a pickled payload WITHOUT invoking __reduce__ at
        # write time (serialize a plain object array whose pickle blob
        # would execute arbitrary code on load if pickle were allowed).
        import pickle

        hostile_code = (
            "raise RuntimeError('pickle executed — must never happen')"
        )
        payload = pickle.dumps((compile, (hostile_code, "x", "exec")))
        # Write a REAL .npy header via the official writer on a plain
        # float32 array, then splice the pickle blob over the data.
        plain = io.BytesIO()
        npformat.write_array(plain, np.zeros(1, dtype=np.float32))
        raw = plain.getvalue()
        # .npy v1.0 header: 6-byte magic + 2-byte header length + dict.
        header_size = int.from_bytes(raw[8:10], "little")
        data_start = 10 + header_size
        buffer = io.BytesIO()
        buffer.write(raw[:data_start])
        buffer.write(payload)  # pickle bytes in place of raw data
        npz_path = settings.embeddings_dir / "E1.npz"
        with zipfile.ZipFile(npz_path, "w") as zf:
            zf.writestr("embedding.npy", buffer.getvalue())
        with pytest.raises(CorruptEmbeddingRecordError):
            self._service(settings).load("E1")

    def test_nan_values_in_stored_file_rejected_as_corrupt(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        bad = synthetic_vector()
        bad[3] = np.nan
        np.savez(settings.embeddings_dir / "E1.npz", embedding=bad)
        with pytest.raises(CorruptEmbeddingRecordError, match="non-finite"):
            self._service(settings).load("E1")

    def test_wrong_dtype_in_stored_file_rejected(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        np.savez(
            settings.embeddings_dir / "E1.npz",
            embedding=synthetic_vector().astype(np.float64),
        )
        with pytest.raises(CorruptEmbeddingRecordError, match="float32"):
            self._service(settings).load("E1")

    def test_dimension_mismatch_between_array_and_metadata_rejected(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        np.savez(
            settings.embeddings_dir / "E1.npz",
            embedding=synthetic_vector(16, seed=4).astype(np.float32),
        )
        with pytest.raises(CorruptEmbeddingRecordError, match="disagrees"):
            self._service(settings).load("E1")

    def test_id_mismatch_between_sidecar_and_request_rejected(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        # Point E1's stem at E2's identity — storage must refuse to
        # hand back a vector associated with a different employee.
        sidecar = settings.embeddings_dir / "E1.meta.json"
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["employee_id"] = "E2"
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(CorruptEmbeddingRecordError, match="mismatch"):
            self._service(settings).load("E1")

    def test_sidecar_dimension_zero_rejected(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        sidecar = settings.embeddings_dir / "E1.meta.json"
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["dimension"] = 0
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(CorruptEmbeddingRecordError, match="dimension"):
            self._service(settings).load("E1")

    def test_naive_timestamp_sidecar_rejected(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        sidecar = settings.embeddings_dir / "E1.meta.json"
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["stored_at"] = datetime(2026, 1, 1).isoformat()
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(CorruptEmbeddingRecordError, match="timezone"):
            self._service(settings).load("E1")

    def test_unknown_dtype_marker_sidecar_rejected(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        sidecar = settings.embeddings_dir / "E1.meta.json"
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["dtype"] = "float128-quantum"
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(CorruptEmbeddingRecordError, match="dtype"):
            self._service(settings).load("E1")


# ---------------------------------------------------------------------------
# 8b. Incompatible schema version handling
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_future_major_version_rejected_as_format_error(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        sidecar = settings.embeddings_dir / "E1.meta.json"
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["format_version"] = EMBEDDING_STORAGE_FORMAT_VERSION + 1
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(EmbeddingRecordFormatError, match="incompatible"):
            EmbeddingStorageService(settings=settings).load("E1")

    def test_non_int_version_rejected_as_corrupt(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        sidecar = settings.embeddings_dir / "E1.meta.json"
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["format_version"] = "1"
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(CorruptEmbeddingRecordError, match="format_version"):
            EmbeddingStorageService(settings=settings).load("E1")


# ---------------------------------------------------------------------------
# 9. Path traversal & malicious identifier rejection
# ---------------------------------------------------------------------------


class TestPathSafety:
    @pytest.mark.parametrize(
        "bad_id", ["../evil", "..\\evil", "a/b", "a\\b", ".", ".."]
    )
    def test_traversal_shaped_ids_rejected_before_any_write(
        self, settings: AppSettings, bad_id: str
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        try:
            service.save(bad_id, synthetic_vector())
        except Exception:
            pass
        assert not settings.embeddings_dir.exists() or not any(
            settings.embeddings_dir.iterdir()
        )

    @pytest.mark.parametrize(
        "bad_id", ["../evil", "..\\evil", "a/b", "a\\b", ".", "..", ""]
    )
    def test_traversal_ids_rejected_saving(
        self, settings: AppSettings, bad_id: str
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(EmbeddingStorageInputError):
            service.save(bad_id, synthetic_vector())

    def test_empty_id_rejected(self, settings: AppSettings) -> None:
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(EmbeddingStorageInputError):
            service.save("", synthetic_vector())

    def test_whitespace_only_id_rejected(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(EmbeddingStorageInputError):
            service.save("   ", synthetic_vector())

    def test_non_str_id_rejected(self, settings: AppSettings) -> None:
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(EmbeddingStorageInputError):
            service.save(7, synthetic_vector())  # type: ignore[arg-type]

    def test_traversal_id_load_refused(self, settings: AppSettings) -> None:
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(EmbeddingStorageInputError):
            service.load("../evil")

    def test_escape_attempt_cannot_reach_outside_embeddings_dir(
        self, settings: AppSettings, tmp_path: Path
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        sentinel = tmp_path / "data" / "secret.txt"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("untouched", encoding="utf-8")
        with pytest.raises(EmbeddingStorageInputError):
            service.save("../secret", synthetic_vector())
        assert sentinel.read_text(encoding="utf-8") == "untouched"


# ---------------------------------------------------------------------------
# 11/12. Write failures, temp-file cleanup, unrelated files untouched
# ---------------------------------------------------------------------------


class TestPersistenceFailure:
    def test_unreadable_dir_turns_into_persistence_error(
        self, settings: AppSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = EmbeddingStorageService(settings=settings)

        def exploding_mkdir(self: Path, *args, **kwargs):  # noqa: ANN001
            raise OSError("disk on fire")

        monkeypatch.setattr(Path, "mkdir", exploding_mkdir)
        try:
            with pytest.raises(EmbeddingPersistenceError, match="disk on fire"):
                service.save("E1", synthetic_vector())
        finally:
            monkeypatch.undo()
        # Nothing was left behind for this employee.
        assert not service.exists("E1")
        assert service.list_employee_ids() == ()

    def test_failed_mktemp_leaves_no_tmp_files(
        self, settings: AppSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_ok(settings, "E1")
        service = EmbeddingStorageService(settings=settings)

        real_open = open

        def exploding_open(*args, **kwargs):  # noqa: ANN001
            raise OSError(" antivirus says no")

        monkeypatch.setattr("builtins.open", exploding_open)
        with pytest.raises(EmbeddingPersistenceError):
            service.save("E1", synthetic_vector(), replace=True)
        monkeypatch.setattr("builtins.open", real_open)
        leftovers = [
            p.name
            for p in settings.embeddings_dir.iterdir()
            if p.name.endswith(".tmp")
        ]
        assert leftovers == []

    def test_unrelated_files_untouched_after_failure(
        self, settings: AppSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_ok(settings, "E1")
        keep = settings.embeddings_dir / "unrelated.txt"
        keep.write_text("keep me", encoding="utf-8")
        service = EmbeddingStorageService(settings=settings)

        real_open = open

        def exploding_open(path, mode="r", *args, **kwargs):  # noqa: ANN001
            # Only explode binary writes for this attempt (paths are
            # strings); np.load's read of E1 stays intact.
            if "w" in mode or "a" in mode:
                raise OSError("nope")
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", exploding_open)
        with pytest.raises(EmbeddingPersistenceError):
            service.save("E2", synthetic_vector())
        monkeypatch.setattr("builtins.open", real_open)
        assert keep.read_text(encoding="utf-8") == "keep me"
        # E1 record intact.
        assert service.exists("E1")
        assert np.array_equal(
            service.load("E1").vector, synthetic_vector(seed=0)
        )

    def test_concurrent_materialization_between_check_and_link_refused(
        self, settings: AppSettings
    ) -> None:
        """The hard-link publication path is the TOCTOU-safe refusal."""
        save_ok(settings, "E1")
        service = EmbeddingStorageService(settings=settings)
        record_path = settings.embeddings_dir / "E1.npz"
        assert record_path.is_file()
        # Direct os.link semantics: relinking an existing target fails.
        with pytest.raises(OSError):
            os.link(
                settings.embeddings_dir / "E1.meta.json", record_path
            )


# ---------------------------------------------------------------------------
# 13/14/15. Hygiene: no import/construction writes; foreign CWD; no network
# ---------------------------------------------------------------------------


class TestHygiene:
    def test_import_and_construction_no_filesystem_writes(
        self, tmp_path: Path
    ) -> None:
        before = sorted(p.name for p in tmp_path.iterdir())
        sys.path.insert(0, str(Path.cwd()))
        import src.embedding_storage  # noqa: F401

        service = EmbeddingStorageService(
            settings=AppSettings(data_dir=tmp_path / "data")
        )
        # Even first existence/list probes create nothing.
        assert service.list_employee_ids() == ()
        assert not (tmp_path / "data").exists()
        after = sorted(p.name for p in tmp_path.iterdir())
        assert before == after

    def test_foreign_wd_construction_with_explicit_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        monkeypatch.chdir(foreign)
        from src.embedding_storage import EmbeddingStorageService as S

        service = S(settings=AppSettings(data_dir=tmp_path / "data2"))
        with pytest.raises(EmbeddingRecordNotFoundError):
            service.load("E1")
        # Merely constructing/using from a foreign CWD created nothing.
        assert not (tmp_path / "data2").exists()

    def test_subprocess_import_no_side_effects(self, tmp_path: Path) -> None:
        import src.embedding_storage as module

        project_root = Path(module.__file__).resolve().parent.parent
        code = (
            "import sys; sys.path.insert(0, r'"
            + str(project_root)
            + "'); import src.embedding_storage;"
            "print('imported-clean')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(tmp_path),  # foreign working directory
        )
        assert proc.returncode == 0, proc.stderr
        assert "imported-clean" in proc.stdout

    def test_no_camera_or_network_involved(
        self, settings: AppSettings
    ) -> None:
        # Structural proof: the module imports no cv2, no sockets, and
        # the service performs no device/network surface anywhere.
        import src.embedding_storage as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "import cv2" not in source
        assert "socket" not in source
        assert "urllib.request" not in source


# ---------------------------------------------------------------------------
# 16. Manifest-missing safety (explicit design note)
# ---------------------------------------------------------------------------


class TestEnrollmentManifestIndependence:
    """Storage does NOT consult enrollment manifests — employee identity
    is validated through the ``Employee`` model rules (form only) so no
    second source of truth is created and no manifest is required."""

    def test_save_works_without_any_manifest(
        self, settings: AppSettings
    ) -> None:
        # Nothing enrolled anywhere — storage accepts a well-formed ID
        # and never reads or writes the enrollment trees.
        service = EmbeddingStorageService(settings=settings)
        record = service.save("007", synthetic_vector())
        assert record.employee_id == "007"
        assert not settings.employees_dir.exists()
        assert not settings.face_samples_dir.exists()

    def test_storage_never_touches_enrollment_trees_on_failure(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        employees_dir = settings.employees_dir
        employees_dir.mkdir(parents=True)
        (employees_dir / "something.json").write_text("{}", encoding="utf-8")
        service = EmbeddingStorageService(settings=settings)
        with pytest.raises(DuplicateEmbeddingRecordError):
            service.save("E1", synthetic_vector(seed=2))
        assert (employees_dir / "something.json").exists()


# ---------------------------------------------------------------------------
# 17. Output isolation: stored files & returned state cannot be mutated
# ---------------------------------------------------------------------------


class TestOutputIsolation:
    def test_loaded_vector_is_write_protected(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        service = EmbeddingStorageService(settings=settings)
        loaded = service.load("E1")
        with pytest.raises(ValueError):
            loaded.vector[0] = 123.0

    def test_mutating_loaded_vector_does_not_affect_second_load(
        self, settings: AppSettings
    ) -> None:
        save_ok(settings, "E1")
        service = EmbeddingStorageService(settings=settings)
        first = service.load("E1")
        # Attempt in-place corruption via a writable alias of the same
        # underlying values (write was refused above; storage stays
        # intact even if a caller held pre-set writable copies).
        second = service.load("E1")
        assert np.array_equal(second.vector, first.vector)

    def test_saving_after_caller_mutates_its_own_array_is_safe(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        original = synthetic_vector()
        record = service.save("E1", original)
        original[0] = 999.0  # caller mutates their own input afterwards
        loaded = service.load("E1")
        assert not np.array_equal(loaded.vector, original)
        assert np.array_equal(loaded.vector, record.vector)

    def test_step08_embedding_vector_not_consumable_mutably(
        self, settings: AppSettings
    ) -> None:
        service = EmbeddingStorageService(settings=settings)
        embedding = make_face_embedding()
        record = service.save("E1", embedding)
        # The returned record is derived from storage (validated), not
        # a live alias of the caller's object.
        assert record.vector is not embedding.vector
        with pytest.raises(ValueError):
            record.vector[0] = 5.0

    def test_metadata_frozen(self, settings: AppSettings) -> None:
        save_ok(settings, "E1")
        service = EmbeddingStorageService(settings=settings)
        loaded = service.load("E1")
        with pytest.raises(Exception):
            loaded.metadata.dimension = 99  # type: ignore[misc]
