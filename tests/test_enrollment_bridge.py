"""Step 16 bridge tests: enrollment samples → one stored embedding.

Deterministic, hardware-free: synthetic NumPy crops written as PNGs,
temporary directories, faked embedders. **No real employee biometrics and
no real model inference anywhere** — crops are plain synthetic noise, in
the project's established style (mirrors tests/test_embedding_storage.py
and tests/test_enrollment.py).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from src.config import AppSettings
from src.embedding_storage import (
    DuplicateEmbeddingRecordError,
    EmbeddingPersistenceError,
    EmbeddingStorageService,
)
from src.enrollment import MANIFEST_FILENAME, safe_dir_name
from src.face_embedding import FaceEmbedding, VectorDtype
from src.enrollment_bridge import (
    EnrollmentBridgeInputError,
    NoEligibleSamplesError,
    embed_published_samples,
)

# ---------------------------------------------------------------------------
# Fixtures & synthetic helpers (never real biometrics)
# ---------------------------------------------------------------------------

DIMS = 8


@pytest.fixture()
def settings(tmp_path: Path) -> AppSettings:
    """AppSettings pointed at the test's temporary data directory."""
    return AppSettings(data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


def _write_manifest(
    settings: AppSettings,
    employee_id: str = "E1",
    *,
    is_active: bool = True,
    stored_id: str | None = None,
    format_version: int = 1,
) -> None:
    """Publish a Step 07-shaped manifest (the eligibility marker)."""
    directory = settings.employees_dir / safe_dir_name(employee_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format_version": format_version,
        "employee": {
            "employee_id": stored_id or employee_id,
            "full_name": "Test Person",
            "is_active": is_active,
            "created_at": "2026-10-19T00:00:00+00:00",
        },
        "sample_count": 3,
        "samples": [],
    }
    (directory / MANIFEST_FILENAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _write_samples(
    settings: AppSettings, employee_id: str = "E1", count: int = 3
) -> None:
    """Publish `count` synthetic PNG face crops (random noise, no identity)."""
    directory = settings.face_samples_dir / safe_dir_name(employee_id)
    directory.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    for index in range(1, count + 1):
        crop = rng.integers(0, 256, size=(40, 40, 3), dtype=np.uint8)
        ok, buffer = cv2.imencode(".png", crop)
        assert ok
        (directory / f"sample_{index:03d}.png").write_bytes(buffer.tobytes())


def _fake_embedding(vector: np.ndarray) -> FaceEmbedding:
    norm = float(np.linalg.norm(vector.astype(np.float64)))
    vector = np.array(vector, copy=True)
    vector.setflags(write=False)
    return FaceEmbedding(
        vector=vector.astype(np.float32),
        dimension=int(vector.size),
        norm=round(norm, 6),
        dtype=VectorDtype.FLOAT32,
    )


class DeterministicEmbedder:
    """Fake embedder: deterministic float32 vector derived from crop bytes."""

    def __init__(self, dimension: int = DIMS) -> None:
        self._dimension = dimension

    def embed(self, face_crop: np.ndarray) -> FaceEmbedding:
        digest = hashlib.sha256(face_crop.tobytes()).digest()
        seed = int.from_bytes(digest[:8], "big")
        rng = np.random.default_rng(seed)
        vector = rng.standard_normal(self._dimension).astype(np.float32)
        vector[0] = 1.0  # guarantee non-zero norm
        return _fake_embedding(vector)


class SequenceEmbedder:
    """Fake embedder playing back scripted per-call vectors (tests-only)."""

    def __init__(self, vectors: list[np.ndarray]) -> None:
        self._vectors = list(vectors)
        self.calls = 0

    def embed(self, _face_crop: object) -> FaceEmbedding:
        vector = self._vectors[self.calls]
        self.calls += 1
        return _fake_embedding(vector)


class FailingEmbedder:
    """Fake embedder raising a typed Step 08 error (model contract)."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def embed(self, _face_crop: object) -> FaceEmbedding:
        raise self.exc


class FlakyStorage(EmbeddingStorageService):
    """Real storage whose save() fails with the typed persistence error."""

    def save(self, *args: object, **kwargs: object) -> object:  # noqa: ANN002, ANN003
        raise EmbeddingPersistenceError("simulated persistence failure")


# ---------------------------------------------------------------------------
# 1. Successful bridge + candidate discovery
# ---------------------------------------------------------------------------


class TestSuccessfulBridge:
    def test_persists_one_embedding_and_becomes_a_candidate(
        self, settings: AppSettings
    ) -> None:
        _write_manifest(settings)
        _write_samples(settings, count=3)
        storage = EmbeddingStorageService(settings=settings)

        result = embed_published_samples(
            "E1",
            embedder=DeterministicEmbedder(),
            storage=storage,
            settings=settings,
        )

        assert result.employee_id == "E1"
        assert result.dimension == DIMS
        assert result.samples_used == 3
        record = storage.load("E1")
        assert record.dimension == DIMS
        assert storage.list_employee_ids() == ("E1",)
        stored = np.array(record.vector, copy=True)

        # Candidate discovery is exercised through the real recognizer.
        from src.face_recognition import (
            FaceRecognitionService,
            RecognitionStatus,
        )

        class QueryEmbedder:
            def embed(self, _crop: object) -> FaceEmbedding:
                return _fake_embedding(stored)

        recognizer = FaceRecognitionService(
            embedder=QueryEmbedder(),
            storage=storage,
            settings=settings,
        )
        query_crop = np.zeros((50, 50, 3), dtype=np.uint8)
        decision = recognizer.recognize(query_crop)
        assert decision.status is RecognitionStatus.MATCHED
        assert decision.employee_id == "E1"

    def test_aggregation_is_the_cosine_centroid(
        self, settings: AppSettings
    ) -> None:
        _write_manifest(settings)
        _write_samples(settings, count=2)
        # Two scripted samples on different scales: normals (3,0,…)
        # dominating-norm and (0,4,…); per-sample normalization must make
        # the centroid the unit direction of (1,0,…) + (0,1,…).
        v1 = np.zeros((DIMS,), dtype=np.float32)
        v1[0] = 3.0
        v2 = np.zeros((DIMS,), dtype=np.float32)
        v2[1] = 4.0
        storage = EmbeddingStorageService(settings=settings)

        result = embed_published_samples(
            "E1",
            embedder=SequenceEmbedder([v1, v2]),
            storage=storage,
            settings=settings,
        )

        assert result.samples_used == 2
        expected = np.zeros((DIMS,))
        expected[0], expected[1] = 0.5, 0.5
        expected /= np.linalg.norm(expected)
        stored = np.array(storage.load("E1").vector, copy=True)
        assert np.allclose(stored, expected, atol=1e-6)
        # The stored vector is unit-norm (harmless: cosine is norm-agnostic).
        assert pytest.approx(1.0, abs=1e-5) == float(
            np.linalg.norm(stored.astype(np.float64))
        )


# ---------------------------------------------------------------------------
# 2. Invalid input & eligibility markers
# ---------------------------------------------------------------------------


class TestInvalidInput:
    def test_missing_manifest_aborts_before_persistence(
        self, settings: AppSettings
    ) -> None:
        _write_samples(settings)
        storage = EmbeddingStorageService(settings=settings)
        with pytest.raises(EnrollmentBridgeInputError, match="manifest"):
            embed_published_samples(
                "E1",
                embedder=DeterministicEmbedder(),
                storage=storage,
                settings=settings,
            )
        assert storage.list_employee_ids() == ()

    def test_identity_contradiction_in_manifest_rejected(
        self, settings: AppSettings
    ) -> None:
        _write_manifest(settings, stored_id="OTHER")
        with pytest.raises(EnrollmentBridgeInputError, match="identity"):
            embed_published_samples(
                "E1",
                embedder=DeterministicEmbedder(),
                storage=EmbeddingStorageService(settings=settings),
                settings=settings,
            )

    def test_incompatible_manifest_format_version(
        self, settings: AppSettings
    ) -> None:
        _write_manifest(settings, format_version=999)
        with pytest.raises(EnrollmentBridgeInputError, match="format_version"):
            embed_published_samples(
                "E1",
                embedder=DeterministicEmbedder(),
                storage=EmbeddingStorageService(settings=settings),
                settings=settings,
            )

    def test_no_samples_at_all(self, settings: AppSettings) -> None:
        _write_manifest(settings)
        with pytest.raises(NoEligibleSamplesError, match="sample directory"):
            embed_published_samples(
                "E1",
                embedder=DeterministicEmbedder(),
                storage=EmbeddingStorageService(settings=settings),
                settings=settings,
            )

    def test_unreadable_sample_is_fail_closed(
        self, settings: AppSettings
    ) -> None:
        _write_manifest(settings)
        _write_samples(settings, count=2)
        corrupt = settings.face_samples_dir / safe_dir_name("E1") / "sample_003.png"
        corrupt.write_bytes(b"definitely not a png")
        storage = EmbeddingStorageService(settings=settings)
        with pytest.raises(EnrollmentBridgeInputError, match="sample_003"):
            embed_published_samples(
                "E1",
                embedder=DeterministicEmbedder(),
                storage=storage,
                settings=settings,
            )
        assert storage.list_employee_ids() == ()

    def test_id_violating_employee_rules_rejected(
        self, settings: AppSettings
    ) -> None:
        with pytest.raises(EnrollmentBridgeInputError):
            embed_published_samples(
                "bad/slash",
                embedder=DeterministicEmbedder(),
                storage=EmbeddingStorageService(settings=settings),
                settings=settings,
            )

    def test_non_storage_backed_rejected(self, settings: AppSettings) -> None:
        _write_manifest(settings)
        with pytest.raises(EnrollmentBridgeInputError, match="storage must"):
            embed_published_samples(
                "E1",
                embedder=DeterministicEmbedder(),
                storage=object(),
                settings=settings,
            )


# ---------------------------------------------------------------------------
# 3. Embedding-generation & aggregation failures (nothing persisted)
# ---------------------------------------------------------------------------


class TestEmbeddingFailures:
    def test_embedder_failure_propagates_and_persists_nothing(
        self, settings: AppSettings
    ) -> None:
        from src.face_embedding import FaceEmbeddingModelError

        _write_manifest(settings)
        _write_samples(settings)
        storage = EmbeddingStorageService(settings=settings)
        with pytest.raises(FaceEmbeddingModelError):
            embed_published_samples(
                "E1",
                embedder=FailingEmbedder(FaceEmbeddingModelError("no model")),
                storage=storage,
                settings=settings,
            )
        assert storage.list_employee_ids() == ()

    def test_cross_sample_dimension_mismatch_rejected(
        self, settings: AppSettings
    ) -> None:
        _write_manifest(settings)
        _write_samples(settings, count=2)
        storage = EmbeddingStorageService(settings=settings)
        vectors = [np.ones((DIMS,), dtype=np.float32), np.ones((4,), dtype=np.float32)]
        with pytest.raises(EnrollmentBridgeInputError, match="dimensions"):
            embed_published_samples(
                "E1",
                embedder=SequenceEmbedder(vectors),
                storage=storage,
                settings=settings,
            )
        assert storage.list_employee_ids() == ()

    def test_zero_norm_embedding_rejected(
        self, settings: AppSettings
    ) -> None:
        _write_manifest(settings)
        _write_samples(settings, count=1)
        storage = EmbeddingStorageService(settings=settings)
        with pytest.raises(EnrollmentBridgeInputError, match="zero"):
            embed_published_samples(
                "E1",
                embedder=SequenceEmbedder([np.zeros((DIMS,), dtype=np.float32)]),
                storage=storage,
                settings=settings,
            )
        assert storage.list_employee_ids() == ()

    def test_parallel_cancelling_samples_rejected_before_save(
        self, settings: AppSettings
    ) -> None:
        _write_manifest(settings)
        _write_samples(settings, count=2)
        storage = EmbeddingStorageService(settings=settings)
        vectors = [
            np.array([1.0, 0.0, 0, 0, 0, 0, 0, 0], dtype=np.float32),
            np.array([-1.0, 0.0, 0, 0, 0, 0, 0, 0], dtype=np.float32),
        ]
        with pytest.raises(EnrollmentBridgeInputError, match="canceling"):
            embed_published_samples(
                "E1",
                embedder=SequenceEmbedder(vectors),
                storage=storage,
                settings=settings,
            )
        assert storage.list_employee_ids() == ()


# ---------------------------------------------------------------------------
# 4. Duplicate-protection semantics
# ---------------------------------------------------------------------------


class TestDuplicate:
    def test_existing_embedding_aborts_before_embedding(
        self, settings: AppSettings
    ) -> None:
        _write_manifest(settings)
        _write_samples(settings)
        storage = EmbeddingStorageService(settings=settings)
        original = storage.save("E1", np.array([1.0] * DIMS, dtype=np.float32))

        with pytest.raises(DuplicateEmbeddingRecordError, match="already"):
            embed_published_samples(
                "E1",
                embedder=DeterministicEmbedder(),
                storage=storage,
                settings=settings,
            )

        # The pre-existing record is untouched and still discoverable.
        loaded = storage.load("E1")
        assert np.array_equal(
            np.array(loaded.vector, copy=True),
            np.array(original.vector, copy=True),
        )

    def test_refuses_replace_true_via_saving_never_overwrites(
        self, settings: AppSettings
    ) -> None:
        # The bridge itself always calls save(..., replace=False); if a
        # first bridge run succeeded, a second must fail (which is the
        # end-state equivalent of a duplicate enrollment).
        _write_manifest(settings)
        _write_samples(settings)
        storage = EmbeddingStorageService(settings=settings)
        embed_published_samples(
            "E1",
            embedder=DeterministicEmbedder(),
            storage=storage,
            settings=settings,
        )
        with pytest.raises(DuplicateEmbeddingRecordError):
            embed_published_samples(
                "E1",
                embedder=DeterministicEmbedder(),
                storage=storage,
                settings=settings,
            )


# ---------------------------------------------------------------------------
# 5. Storage failure & consistent failure handling
# ---------------------------------------------------------------------------


class TestStorageFailure:
    def test_persistence_failure_propagates(
        self, settings: AppSettings
    ) -> None:
        _write_manifest(settings)
        _write_samples(settings, count=3)
        embedder = DeterministicEmbedder()
        storage = FlakyStorage(settings=settings)
        with pytest.raises(EmbeddingPersistenceError, match="simulated"):
            embed_published_samples(
                "E1",
                embedder=embedder,
                storage=storage,
                settings=settings,
            )
        assert storage.list_employee_ids() == ()
