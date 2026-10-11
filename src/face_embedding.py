"""Face embedding generation for the AI-Powered Visual Attendance
System (Phase 1 — Step 08).

Converts one accepted face sample (a BGR face crop, exactly the format
Step 07 persists as PNGs plus manifest) into a validated numerical
feature vector — nothing else. This module does **not** match faces,
identify employees (Step 10), persist embeddings (Step 09), decide
anything temporal or liveness-related, or touch the network.

Chosen model approach (evidence-backed, honest about limitations)
----------------------------------------------------------------

**SFace** (`cv2.FaceRecognizerSF`, OpenCV Zoo
``face_recognition_sface_2021dec.onnx``) is the intended backend,
because it is:

* bundled in the *already-pinned* ``opencv-python`` wheel (DNN loader)
  — **no new dependency**, no ``onnxruntime`` needed for inference;
* Apache-2.0 licensed (unlike the insightface buffalo packages,
  non-commercial research only — see ARCHITECTURE.md D4);
* designed for the pipeline's existing YuNet stack.

Critical honesty notes, carried into validation and docs:

* The literature/Demo SFace workflow aligns with **five landmarks**
  (``FaceRecognizerSF.alignCrop``). Step 07 persists **plain crops
  without landmarks**, so this module documents the "crop-only"
  mode: ``FaceRecognizerSF.feature()`` stretches the supplied crop to
  the model's 112×112 input with scale 1 / mean 0 / BGR→RGB swap —
  valid inference, but **not equivalent in accuracy to landmark
  alignment**. Crop-only embedding quality is explicitly pending
  Phase 2 evaluation on real footage.
* The ONNX artifact is **not present in the repository** and is
  **never downloaded** (operator-supplied at
  ``models/sface.onnx`` — see ``DEFAULT_MODEL_PATH``). Until a
  verified artifact is provided, every ``load()`` raises
  :class:`FaceEmbeddingModelError`; unit tests run exclusively on
  injected fake backends and contain **no real model inference**.

Module layout (mirrors Steps 05/06 conventions)
-----------------------------------------------

* ``FaceEmbedder`` — explicit ``load()``/(idempotent)/``is_loaded``/
  ``embed()`` lifecycle; construction performs no side effects.
* Inference backend injectable (``backend_factory``) — tests use
  deterministic fakes; the real default is ``cv2.FaceRecognizerSF``.
* Frozen typed results; explicit enums instead of bare booleans;
  ``ValueError``-derived input errors; ``RuntimeError``-derived
  model/inference errors. Outputs are copies (write-protected) so a
  caller mutating a returned vector can never corrupt model state.
* Vector validation: 1-D, finite, expected dimension (0 = accept any
  non-empty dimensionality from the backend), documented float32
  dtype; zero-norm is rejected rather than divided by zero. Output
  normalization is *backend-contract-driven* (``normalize_output``),
  not applied by convention.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Final

import cv2
import numpy as np

from src.config import PROJECT_ROOT
from src.logging_setup import get_logger

__all__ = [
    "FaceEmbeddingError",
    "FaceEmbeddingInputError",
    "FaceEmbeddingModelError",
    "FaceEmbeddingOutputError",
    "TARGET_EMBEDDING_BACKEND",
    "DEFAULT_MODEL_PATH",
    "FaceEmbedder",
]

_logger = get_logger(__name__)

#: Documented name of the embedding backend this module wraps.
TARGET_EMBEDDING_BACKEND: Final[str] = "cv2.FaceRecognizerSF (SFace ONNX)"

#: Expected model-input size for SFace (documented by OpenCV Zoo).
SFACE_INPUT_SIZE: Final[tuple[int, int]] = (112, 112)

#: On-disk location the operator must provide. Not created automatically;
#: never downloaded by this codebase.
DEFAULT_MODEL_PATH: Final[Path] = PROJECT_ROOT / "models" / "sface.onnx"


class FaceEmbeddingError(Exception):
    """Base class for face-embedding errors."""


class FaceEmbeddingInputError(FaceEmbeddingError, ValueError):
    """Raised when a face sample violates the documented input contract.

    Mirrors ``FaceDetectionInputError`` conventions: ``None``, empty,
    wrong dimensionality/channel count, non-uint8 dtype, or non-finite
    pixel content all raise this typed error instead of quietly
    producing a garbage embedding.
    """


class FaceEmbeddingModelError(FaceEmbeddingError, RuntimeError):
    """Raised when the embedding model cannot be loaded or is invalid."""


class FaceEmbeddingOutputError(FaceEmbeddingError, RuntimeError):
    """Raised when the backend's embedding output is unusable.

    Covers non-finite values, wrong dimensionality, zero norm (when
    normalization is required), and inference-time backend failures.
    """


class VectorDtype(Enum):
    """Documented dtype contract for returned embeddings."""

    FLOAT32 = "float32"


@dataclass(frozen=True)
class FaceEmbedding:
    """One validated face embedding plus its measured properties.

    Attributes:
        vector: Immutable-by-behavior copy of the embedding float32
            1-D vector with ``flags.writeable(False)`` — mutating it
            raises a ``ValueError`` and can never corrupt backend
            state or future results.
        dimension: ``vector.size`` (backend-documented; the module
            never invents a universal constant).
        norm: L2 norm of the vector *before* any requested
            normalization, rounded for stable reporting.
        dtype: explicit dtype marker (always ``FLOAT32``).
    """

    vector: np.ndarray
    dimension: int
    norm: float
    dtype: VectorDtype


class FaceEmbedder:
    """Load an embedding model and convert face crops to vectors.

    Lifecycle (mirrors :class:`src.face_detection.FaceDetector`):

    .. code-block:: python

        embedder = FaceEmbedder()          # no side effects
        embedder.load()                    # explicit init, idempotent
        emb = embedder.embed(crop_bgr)     # (112, 112, 3) uint8 in
        emb.vector                         # float32 1-D, read-only

    Without ``backend_factory`` the real ``cv2.FaceRecognizerSF`` is
    used, requiring an operator-supplied ONNX artifact at
    ``models/sface.onnx`` (never downloaded here). With an injected
    factory, whatever the factory returns is trusted as an already-
    initialized backend and ``load()`` just records it — tests use
    deterministic fakes and exercise real validation, not fake
    "recognition" semantics: fake outputs are validated with exactly
    the same rigor as real ones, and no test claims real-model
    accuracy.
    """

    def __init__(
        self,
        *,
        model_path: Path | str = DEFAULT_MODEL_PATH,
        expected_dimension: int = 0,
        normalize_output: bool = False,
        backend_factory: Callable[[], object] | None = None,
    ) -> None:
        """
        Args:
            model_path: ONNX artifact consumed by the real backend
                when no ``backend_factory`` is injected. Ignored (never
                opened, never checked for existence) when a factory is
                injected, so tests can run without any model file.
            expected_dimension: documented embedding dimension for the
                selected model contract (``0`` = accept any non-empty
                1-D output). Pass e.g. ``128`` for SFace to enforce it.
            normalize_output: ``True`` → L2-normalize validated outputs
                (for cosine-similarity consumers); zero-norm vectors
                are rejected, never divided by zero. ``False`` → return
                the raw validated vector. Governed by the model's
                contract (SFace similarity uses ``FaceRecognizerSF``
                cosine on raw features), not by convention.
            backend_factory: injectable inference backend. ``None``
                uses the real ``cv2.FaceRecognizerSF``.
        """
        if not isinstance(expected_dimension, int) or expected_dimension < 0:
            raise FaceEmbeddingInputError(
                "expected_dimension must be a non-negative int, got "
                f"{expected_dimension!r}"
            )
        if not isinstance(normalize_output, bool):
            raise FaceEmbeddingInputError(
                f"normalize_output must be a bool, got "
                f"{type(normalize_output).__name__}"
            )
        self._model_path = Path(model_path)
        self._expected_dimension = expected_dimension
        self._normalize_output = normalize_output
        self._backend_factory = backend_factory
        self._backend: object | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        """Whether the inference backend is initialized and ready."""
        return self._backend is not None

    def load(self) -> None:
        """Explicitly initialize the embedding backend (idempotent).

        Raises:
            FaceEmbeddingModelError: missing/invalid artifact or a
                backend-construction failure. Never downgraded to a
                warning, never retried by download, never raised at
                import or construction time.
        """
        if self.is_loaded:
            return
        if self._backend_factory is not None:
            try:
                backend = self._backend_factory()
            except Exception as exc:  # noqa: BLE001 - wrap factory failure
                raise FaceEmbeddingModelError(
                    f"embedding backend factory failed: {exc}"
                ) from exc
            self._backend = self._validate_backend(backend)
            _logger.info("injected embedding backend loaded")
            return

        if not self._model_path.is_file():
            _logger.error("embedding model missing: %s", self._model_path)
            raise FaceEmbeddingModelError(
                f"SFace ONNX model not found at '{self._model_path}'. "
                "Provide the operator-supplied artifact (OpenCV Zoo "
                "face_recognition_sface_2021dec.onnx) and pass its path "
                "to FaceEmbedder(). The model is never downloaded "
                "automatically."
            )
        try:
            backend = cv2.FaceRecognizerSF_create(str(self._model_path), "")
            if backend is None:
                raise FaceEmbeddingModelError(
                    f"FaceRecognizerSF_create returned no backend for "
                    f"'{self._model_path}'"
                )
        except FaceEmbeddingModelError:
            raise
        except Exception as exc:  # noqa: BLE001 - wrap backend init failure
            raise FaceEmbeddingModelError(
                f"failed to initialize FaceRecognizerSF from "
                f"'{self._model_path}': {exc}"
            ) from exc
        self._backend = backend
        _logger.info(
            "sface embedding model loaded: model=%s (crop-only mode: "
            "no landmark alignment is applied)",
            self._model_path,
        )

    @staticmethod
    def _validate_backend(backend: object) -> object:
        """Reject injected objects that cannot plausibly embed."""
        if not callable(getattr(backend, "feature", None)):
            raise FaceEmbeddingModelError(
                "injected backend lacks a callable feature(image) method"
            )
        return backend

    def _require_backend(self) -> object:
        if self._backend is None:
            raise FaceEmbeddingModelError(
                "embedding model is not loaded; call load() before embed()"
            )
        return self._backend

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def embed(self, face_crop: np.ndarray) -> FaceEmbedding:
        """Preprocess one face crop, run inference, validate output.

        Args:
            face_crop: BGR uint8 ``(H, W, 3)`` face crop with positive
                dimensions and finite pixels — exactly the shape
                Step 07 persists as PNG samples. Never mutated; not
                copied beyond the backend's internal resize.

        Returns:
            :class:`FaceEmbedding` with a write-protected float32 1-D
            vector, its dimension, and its pre-normalization L2 norm.

        Raises:
            FaceEmbeddingInputError: contract-violating crops.
            FaceEmbeddingModelError: backend not loaded (call
                :meth:`load` first).
            FaceEmbeddingOutputError: inference failure, or an output
                that is empty / non-1-D / non-finite / wrong dimension
                / zero-norm-when-normalizing.
        """
        validated = self._validate_crop(face_crop)
        backend = self._require_backend()
        try:
            raw = self._run_inference(backend, validated)
        except FaceEmbeddingOutputError:
            raise
        except Exception as exc:  # noqa: BLE001 - wrap inference failure
            _logger.error("embedding inference failed: %s", exc)
            raise FaceEmbeddingOutputError(
                f"embedding inference failed: {exc}"
            ) from exc
        return self._validated_result(raw)

    # ------------------------------------------------------------------
    # Preprocessing contract (documented, deterministic)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_crop(face_crop: np.ndarray) -> np.ndarray:
        """BGR uint8 ``(H, W, 3)``; ``None``/empty/wrong shapes/dtypes
        and non-finite pixels raise :class:`FaceEmbeddingInputError`."""
        if face_crop is None:
            raise FaceEmbeddingInputError("face_crop must not be None")
        if not isinstance(face_crop, np.ndarray):
            raise FaceEmbeddingInputError(
                f"face_crop must be a numpy array, got "
                f"{type(face_crop).__name__}"
            )
        if face_crop.size == 0:
            raise FaceEmbeddingInputError("face_crop must not be empty")
        if face_crop.ndim != 3:
            raise FaceEmbeddingInputError(
                f"face_crop must be 3-dimensional (H, W, 3) BGR, got "
                f"shape {face_crop.shape}"
            )
        if face_crop.shape[2] != 3:
            raise FaceEmbeddingInputError(
                f"face_crop must have 3 channels (BGR), got "
                f"{face_crop.shape[2]} channels"
            )
        if face_crop.shape[0] <= 0 or face_crop.shape[1] <= 0:
            raise FaceEmbeddingInputError(
                f"face_crop dimensions must be positive, got "
                f"{face_crop.shape[:2]}"
            )
        if face_crop.dtype != np.uint8:
            raise FaceEmbeddingInputError(
                f"face_crop dtype must be uint8, got {face_crop.dtype}"
            )
        return face_crop

    @staticmethod
    def _run_inference(backend: object, crop: np.ndarray) -> np.ndarray:
        """Dispatch to the inference surface and normalize the shape.

        Real ``cv2.FaceRecognizerSF.feature()`` and plausible fake
        backends both return either ``[1, D]`` / ``(D,)`` or ``[]`
        (no face); anything else is not an embedding this module can
        honestly validate.
        """
        raw = backend.feature(crop)
        if raw is None:
            raise FaceEmbeddingOutputError(
                "embedding backend returned no vector"
            )
        array = np.asarray(raw)
        if array.size == 0:
            raise FaceEmbeddingOutputError(
                "embedding backend returned an empty vector"
            )
        if array.ndim == 2 and array.shape[0] == 1:
            array = array.reshape(-1)
        return array

    def _validated_result(self, array: np.ndarray) -> FaceEmbedding:
        """Validate the tensor contract and build the frozen result.

        Order (documented, stable): dimensionality → dtype imposed ->
        dimension contract → finiteness → zero-norm guard →
        normalization (when enabled by the model contract) →
        write-protect.
        """
        if array.ndim != 1:
            raise FaceEmbeddingOutputError(
                f"embedding must be a 1-D vector, got shape {array.shape}"
            )
        if (
            self._expected_dimension
            and array.shape[0] != self._expected_dimension
        ):
            raise FaceEmbeddingOutputError(
                f"embedding dimension {array.shape[0]} does not match "
                f"the configured contract {self._expected_dimension}"
            )
        vector = array.astype(np.float32, copy=True)
        if not np.isfinite(vector).all():
            raise FaceEmbeddingOutputError(
                "embedding contains non-finite values (NaN or infinity)"
            )
        norm = float(np.linalg.norm(vector))
        if norm == 0.0 or math.isnan(norm):
            raise FaceEmbeddingOutputError(
                "embedding has zero norm; cannot normalize a zero vector"
                if self._normalize_output
                else "embedding has zero norm; rejecting a degenerate output"
            )
        if self._normalize_output and abs(norm - 1.0) > 1e-6:
            vector = vector / norm
        vector.setflags(write=False)
        return FaceEmbedding(
            vector=vector,
            dimension=int(vector.size),
            norm=round(norm, 6),
            dtype=VectorDtype.FLOAT32,
        )
