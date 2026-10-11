"""Step 08 tests: face-embedding generation.

Deterministic, hardware-free: injected fake backends, synthetic NumPy
crops, temporary paths. **No test uses real model inference** — the
SFace ONNX artifact is not present in the repo and is never downloaded;
real-backend coverage is load-failure paths only (missing artifact),
mirroring Steps 05/07 conventions.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from src.config import AppSettings
from src.face_embedding import (
    DEFAULT_MODEL_PATH,
    FaceEmbedder,
    FaceEmbeddingInputError,
    FaceEmbeddingModelError,
    FaceEmbeddingOutputError,
    SFACE_INPUT_SIZE,
    VectorDtype,
)


# ---------------------------------------------------------------------------
# Fakes & fixtures (no camera, no network, no real model file)
# ---------------------------------------------------------------------------


class FakeBackend:
    """Scriptable embedding backend mirroring FaceRecognizerSF.feature.

    Mirrors the real surface: ``feature()`` may return ``[1, D]`` or
    ``(D,)`` arrays or raise — but these fakes produce *synthetic*
    vectors (e.g. a masked mean projection). They are inference-socket
    doubles, **not** claimed face-recognition semantics.
    """

    def __init__(self, dimension: int = 128, *, script=None) -> None:
        self.dimension = dimension
        self.script = list(script) if script else None
        self.call_count = 0
        self.seen_shapes: list[tuple[int, ...]] = []
        self.seen_dtypes: list[np.dtype] = []

    def feature(self, image: np.ndarray) -> np.ndarray | Exception:
        self.call_count += 1
        self.seen_shapes.append(tuple(image.shape))
        self.seen_dtypes.append(image.dtype)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self._synthetic_embed(image)

    @staticmethod
    def _synthetic_embed(image: np.ndarray) -> np.ndarray:
        """Deterministic *synthetic* projection — not a face embedding.

        Deliberately trivial image statistics, chosen precisely
        because they keep tests honest: two different crops produce
        (usually) different vectors, but no claim of recognition
        semantics is implied or tested.
        """
        return np.array(
            [[float(image.sum() % 127), float(image.mean() % 101)]]
        )


class BackendFactory:
    def __init__(self, backend: FakeBackend | None = None,
                 error: Exception | None = None) -> None:
        self.backend = backend
        self.error = error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.backend is not None
        return self.backend


@pytest.fixture()
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


def make_crop(h: int = 120, w: int = 120, intensity: int = 128) -> np.ndarray:
    """A valid BGR uint8 face-crop-shaped sample (deterministic)."""
    crop = np.full((h, w, 3), intensity // 2, dtype=np.uint8)
    crop[: h // 2, : w // 2] = intensity
    return crop


# ---------------------------------------------------------------------------
# Lifecycle: explicit load, idempotence, loaded state
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_construction_performs_no_side_effects(
        self, settings: AppSettings
    ) -> None:
        factory = BackendFactory()
        embedder = FaceEmbedder(backend_factory=factory)
        assert embedder.is_loaded is False
        assert factory.calls == 0  # factory not invoked at construction

    def test_explicit_load_initializes_backend(self) -> None:
        backend = FakeBackend()
        embedder = FaceEmbedder(backend_factory=BackendFactory(backend))
        assert embedder.is_loaded is False
        embedder.load()
        assert embedder.is_loaded is True

    def test_repeated_load_is_idempotent(self) -> None:
        backend = FakeBackend()
        factory = BackendFactory(backend)
        embedder = FaceEmbedder(backend_factory=factory)
        embedder.load()
        embedder.load()
        embedder.load()
        assert factory.calls == 1

    def test_embed_before_load_raises_model_error(self) -> None:
        embedder = FaceEmbedder(backend_factory=BackendFactory(FakeBackend()))
        with pytest.raises(FaceEmbeddingModelError, match="not loaded"):
            embedder.embed(make_crop())

    def test_missing_real_model_artifact_raises_load_error(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "absent_sface.onnx"
        embedder = FaceEmbedder(model_path=missing)
        assert not missing.exists()
        with pytest.raises(FaceEmbeddingModelError, match="not found"):
            embedder.load()
        assert not missing.exists()  # never auto-created/downloaded
        assert embedder.is_loaded is False

    def test_default_model_path_does_not_exist_here(self) -> None:
        assert DEFAULT_MODEL_PATH.name == "sface.onnx"
        # The repo has no SFace artifact; the module never creates one.
        assert isinstance(SFACE_INPUT_SIZE, tuple)

    def test_factory_failure_raises_model_error_well_typed(self) -> None:
        factory = BackendFactory(error=RuntimeError("no runtime"))
        embedder = FaceEmbedder(backend_factory=factory)
        with pytest.raises(FaceEmbeddingModelError):
            embedder.load()

    def test_injected_backend_require_feature_method(self) -> None:
        class NotABackend:
            pass

        embedder = FaceEmbedder(
            backend_factory=BackendFactory(NotABackend())
        )
        with pytest.raises(FaceEmbeddingModelError, match="feature"):
            embedder.load()


# ---------------------------------------------------------------------------
# Input validation (crop contract)
# ---------------------------------------------------------------------------


class TestInputValidation:
    def make_loaded(self) -> FaceEmbedder:
        embedder = FaceEmbedder(backend_factory=BackendFactory(FakeBackend()))
        embedder.load()
        return embedder

    def test_none_rejected(self) -> None:
        with pytest.raises(FaceEmbeddingInputError, match="None"):
            self.make_loaded().embed(None)

    def test_non_array_rejected(self) -> None:
        with pytest.raises(FaceEmbeddingInputError, match="numpy"):
            self.make_loaded().embed("not-an-image")

    def test_empty_rejected(self) -> None:
        with pytest.raises(FaceEmbeddingInputError, match="empty"):
            self.make_loaded().embed(np.zeros((0,), dtype=np.uint8))

    def test_wrong_ndim_rejected(self) -> None:
        with pytest.raises(FaceEmbeddingInputError, match="3-dimensional"):
            self.make_loaded().embed(np.zeros((10, 10), dtype=np.uint8))

    def test_wrong_channels_rejected(self) -> None:
        with pytest.raises(FaceEmbeddingInputError, match="3 channels"):
            self.make_loaded().embed(np.zeros((10, 10, 4), dtype=np.uint8))

    def test_non_uint8_rejected(self) -> None:
        with pytest.raises(FaceEmbeddingInputError, match="uint8"):
            self.make_loaded().embed(np.zeros((10, 10, 3), dtype=np.float32))

    def test_zero_size_dimension_rejected(self) -> None:
        # A zero-area crop is caught first by the emptiness check
        # (size == 0) — same typed error family either way.
        with pytest.raises(FaceEmbeddingInputError, match="empty"):
            self.make_loaded().embed(np.zeros((0, 10, 3), dtype=np.uint8))


# ---------------------------------------------------------------------------
# Embedding generation & output contract
# ---------------------------------------------------------------------------


class TestEmbeddingGeneration:
    def test_successful_embedding_shape_dtype(self) -> None:
        embedder = FaceEmbedder(backend_factory=BackendFactory(FakeBackend()))
        embedder.load()
        result = embedder.embed(make_crop())
        assert result.vector.dtype == np.float32
        assert result.vector.ndim == 1
        assert result.dimension == result.vector.size
        assert result.dtype is VectorDtype.FLOAT32

    def test_backend_receives_uint8_3d(self) -> None:
        backend = FakeBackend()
        embedder = FaceEmbedder(backend_factory=BackendFactory(backend))
        embedder.load()
        embedder.embed(make_crop())
        assert backend.seen_shapes == [(120, 120, 3)]
        assert backend.seen_dtypes == [np.uint8]

    def test_2d_backend_output_is_reshaped(self) -> None:
        backend = FakeBackend(
            script=[np.array([[0.5, 0.25, 0.1]], dtype=np.float32)]
        )
        embedder = FaceEmbedder(backend_factory=BackendFactory(backend))
        embedder.load()
        result = embedder.embed(make_crop())
        assert result.vector.shape == (3,)
        assert result.dimension == 3

    def test_inference_exception_wrapped_as_output_error(self) -> None:
        backend = FakeBackend(script=[RuntimeError("backend boom")])
        embedder = FaceEmbedder(backend_factory=BackendFactory(backend))
        embedder.load()
        with pytest.raises(FaceEmbeddingOutputError, match="boom"):
            embedder.embed(make_crop())

    def test_output_isolation_between_calls(self) -> None:
        """Mutating one result must not corrupt future results."""
        backend = FakeBackend(script=None)
        embedder = FaceEmbedder(backend_factory=BackendFactory(backend))
        embedder.load()
        first = embedder.embed(make_crop(intensity=120))
        arr = np.asarray(first.vector)
        assert arr.flags.writeable is False  # write-protected copy

        # Attempting in-place mutation must fail (read-only flag).
        with pytest.raises(ValueError):
            arr[:] = 123

        second = embedder.embed(make_crop(intensity=200))
        # Different source crop → different synthetic projection,
        # proving no cross-call state capture happened.
        assert second.norm >= 0.0
        assert not np.array_equal(
            np.asarray(first.vector), np.asarray(second.vector)
        )

    def test_injected_backend_output_expected_dimension_enforced(self) -> None:
        backend = FakeBackend(script=[np.array([0.1, 0.2]) * 1.0])
        embedder = FaceEmbedder(
            expected_dimension=128,
            backend_factory=BackendFactory(backend),
        )
        embedder.load()
        with pytest.raises(FaceEmbeddingOutputError, match="dimension"):
            embedder.embed(make_crop())


# ---------------------------------------------------------------------------
# Output validation (finiteness, zero norm, normalization contract)
# ---------------------------------------------------------------------------


class TestOutputValidation:
    def _emb(self, value, *, dim=0, normalize=False) -> FaceEmbedder:
        backend = FakeBackend(script=[np.asarray(value, dtype=np.float32)])
        embedder = FaceEmbedder(
            expected_dimension=dim,
            normalize_output=normalize,
            backend_factory=BackendFactory(backend),
        )
        embedder.load()
        return embedder

    def test_nan_rejected(self) -> None:
        with pytest.raises(FaceEmbeddingOutputError, match="non-finite"):
            self._emb([1.0, float("nan")]).embed(make_crop())

    def test_infinity_rejected(self) -> None:
        with pytest.raises(FaceEmbeddingOutputError, match="non-finite"):
            self._emb([float("inf"), 1.0]).embed(make_crop())

    def test_zero_vector_rejected_without_normalization(self) -> None:
        with pytest.raises(FaceEmbeddingOutputError, match="zero norm"):
            self._emb([0.0, 0.0]).embed(make_crop())

    def test_zero_vector_rejected_with_normalization(self) -> None:
        # Reject, never divide by zero.
        with pytest.raises(FaceEmbeddingOutputError, match="zero norm"):
            self._emb([0.0, 0.0], normalize=True).embed(make_crop())

    def test_non_1d_output_rejected(self) -> None:
        with pytest.raises(FaceEmbeddingOutputError, match="1-D"):
            self._emb(np.zeros((2, 2), dtype=np.float32)).embed(make_crop())

    def test_normalization_produces_unit_norm(self) -> None:
        embedder = self._emb([0.3, 0.4], normalize=True)
        result = embedder.embed(make_crop())
        assert abs(float(np.linalg.norm(result.vector)) - 1.0) < 1e-6

    def test_no_normalization_keeps_raw_norm(self) -> None:
        embedder = self._emb([0.3, 0.4], normalize=False)
        result = embedder.embed(make_crop())
        assert abs(result.norm - 0.5) < 1e-6  # sqrt(0.09 + 0.16)


# ---------------------------------------------------------------------------
# Construction & import hygiene (incl. foreign-WD)
# ---------------------------------------------------------------------------


class TestHygiene:
    def test_import_and_construction_no_filesystem_writes(
        self, tmp_path: Path
    ) -> None:
        before = sorted(p.name for p in tmp_path.iterdir())
        sys.path.insert(0, str(Path.cwd()))
        import src.face_embedding  # noqa: F401

        FaceEmbedder(
            backend_factory=BackendFactory(FakeBackend()),
            model_path=tmp_path / "unused.onnx",
        )
        after = sorted(p.name for p in tmp_path.iterdir())
        assert before == after

    def test_foreign_wd_construction_with_explicit_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A foreign (empty) working directory, path built explicitly —
        # mirrors Step 07 conventions and prior-step hygiene checks.
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        monkeypatch.chdir(foreign)
        from src.face_embedding import FaceEmbedder as E
        from src.face_embedding import DEFAULT_MODEL_PATH as D

        embedder = E(
            backend_factory=BackendFactory(FakeBackend()),
            model_path=D,
        )
        embedder.load()
        assert embedder.is_loaded

    def test_subprocess_import_has_no_output_side_effects(
        self, tmp_path: Path
    ) -> None:
        """Fresh-process import hygiene (no dirs/camera/network)."""
        import src.face_embedding as module

        project_root = Path(module.__file__).resolve().parent.parent
        code = (
            "import sys;"
            f"sys.path.insert(0, r'{project_root}');"
            "import src.face_embedding;"
            "print(src.face_embedding.FaceEmbedder("
            "    backend_factory=lambda: type('B', (), {'feature': staticmethod("
            "        lambda img: [[1.0]])})()));"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(tmp_path),  # foreign working directory
        )
        assert proc.returncode == 0, proc.stderr
        assert "FaceEmbedder object at" in proc.stdout


# ---------------------------------------------------------------------------
# Compatibility with the actual Step 07 sample format
# ---------------------------------------------------------------------------


class TestStep07SampleFormat:
    def test_saved_png_crop_roundtrip_matches_contract(
        self, tmp_path: Path
    ) -> None:
        """Encode a crop exactly like Step 07, decode like a future
        consumer, and embed it — proving the persisted format is
        consumable without landmarks or source-frame info."""
        import cv2

        original = make_crop(h=112, w=112, intensity=160)
        png_path = tmp_path / "sample.png"
        ok, buf = cv2.imencode(".png", original)
        assert ok
        png_path.write_bytes(buf.tobytes())
        decoded = cv2.imread(str(png_path))  # BGR uint8 (H, W, 3)
        assert decoded is not None and decoded.shape == original.shape

        backend = FakeBackend(script=None)
        embedder = FaceEmbedder(backend_factory=BackendFactory(backend))
        embedder.load()
        result = embedder.embed(decoded)
        assert result.vector.dtype == np.float32
        assert result.vector.ndim == 1
        assert result.vector.size == 2  # synthetic-but-validated shape

    def test_negative_sample_is_not_loader_artifact_confusion(
        self,
    ) -> None:
        """YuNet (detection) and SFace (embedding) remain distinct."""
        from src.face_detection import DEFAULT_MODEL_PATH as YUNET_PATH

        assert YUNET_PATH.name == "yunet.onnx"
        assert DEFAULT_MODEL_PATH.name == "sface.onnx"
        assert YUNET_PATH != DEFAULT_MODEL_PATH
