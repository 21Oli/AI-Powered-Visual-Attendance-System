"""Face detection for the AI-Powered Visual Attendance System
(Phase 1 — Step 05).

Locates faces in a caller-supplied frame — nothing else. This component
does **not** identify people (Step 10), assess quality (Step 06), decide
attendance (Step 13), or own a camera: frames come in from callers (e.g.
``CameraService.read_frame()`` in Step 04), and the module exposes one
explicit lifecycle:

.. code-block:: python

    detector = FaceDetector(Path("models/yunet.onnx"))  # no device work
    detector.load()                                     # explicit init
    dets = detector.detect(frame)                       # list[FaceDetection]

Conventions follow ``src/config.py`` and ``src/camera.py``:
frozen/small typed results, explicit exception types, no side effects on
import, no directory creation, no model download, no background threads.

Detection backend
-----------------

``cv2.FaceDetectorYN`` (YuNet ONNX) from the already-pinned
``opencv-python`` dependency. **No new dependency is added**. The ONNX
model file is *not* bundled with opencv-python 5.0.0 and must be supplied
by the operator at load time (see ``DEFAULT_MODEL_PATH``); the absence
of a model raises :class:`FaceDetectorLoadError` on :meth:`load()` —
never at import and never automatically downloaded.

Frame contract
--------------

``detect()`` expects a **BGR** frame as produced by OpenCV/cv2.VideoCapture
(a uint8 ``numpy`` array of shape ``(H, W, 3)``). Input validation rejects
``None``, empty arrays, wrong dimensionality, unsupported channel counts,
zero-size dimensions, and non-uint8 dtypes with :class:`FaceDetectionInputError`.

Per the task's documented limitation, the two-frame-difference fixture
used for integration verification produces **no detections by design**
(no face in the synthetic image), and detector-level internal failures
(surface occasionally in the C++ layer and are hard to force through
``FaceDetectorYN.detect`` itself) are handled by propagating them as
:class:`FaceDetectorError` rather than misreporting as "no faces". The
deterministic correctness coverage instead uses typed test doubles that
drive the decision paths precisely (defaults, boxes, confidence,
multi-face, zero-face, failure).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import cv2
import numpy as np

from src.config import FaceProcessingConfig, FACE_PROCESSING, PROJECT_ROOT
from src.logging_setup import get_logger

__all__ = [
    "FaceDetectionError",
    "FaceDetectionInputError",
    "FaceDetectorLoadError",
    "FaceDetectorError",
    "FaceDetection",
    "FaceDetector",
    "DEFAULT_MODEL_PATH",
    "TARGET_DETECTOR",
]

_logger = get_logger(__name__)

# Canonical representation of a detected face in pixel coordinates.
# (x, y, width, height), origin top-left. Documented and constant.
TARGET_DETECTOR: Final[str] = "cv2.FaceDetectorYN (YuNet ONNX)"

#: On-disk location the operator must provide. Not created automatically.
DEFAULT_MODEL_PATH: Final[Path] = PROJECT_ROOT / "models" / "yunet.onnx"


class FaceDetectionError(Exception):
    """Base class for face-detection errors."""


class FaceDetectionInputError(FaceDetectionError, ValueError):
    """Raised when a frame does not meet the documented input contract."""


class FaceDetectorLoadError(FaceDetectionError, RuntimeError):
    """Raised when the detector model cannot be loaded or is invalid."""


class FaceDetectorError(FaceDetectionError, RuntimeError):
    """Raised when the loaded detector fails during a detection pass."""


@dataclass(frozen=True, slots=True)
class FaceDetection:
    """One detected face: axis-aligned bounding box plus optional confidence.

    Attributes:
        x: Left edge in pixels (>= 0).
        y: Top edge in pixels (>= 0).
        width: Box width in pixels (> 0).
        height: Test-dependent height in pixels (> 0).
        confidence: Detector-supplied confidence, or ``None`` when the
            backend provides none. Not invented for backends lacking it.
        landmarks: Optional tuple of 5 detected landmarks, each an
            ``(x, y)`` pixel point, in the backend's canonical order:
            right eye, left eye, nose tip, right mouth corner, left
            mouth corner (YuNet's ordering — “right”/“left” is from
            the *subject's* perspective). Present only when the backend
            row carried the full 15-column format
            ``[x, y, w, h, landmarks(10), score]``; ``None`` otherwise.
            Never synthesized from a box. Landmarks may lie anywhere in
            the frame — they are NOT guaranteed to fall inside the
            bounding box.
    """

    x: int
    y: int
    width: int
    height: int
    confidence: float | None = None
    #: Optional five ``(x, y)`` landmark points; None when not supplied.
    landmarks: tuple[tuple[int, int], ...] | None = None

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise FaceDetectionInputError(
                f"face box must have positive width/height, got "
                f"{self.width}x{self.height}"
            )
        if self.x < 0 or self.y < 0:
            raise FaceDetectionInputError(
                f"face box origin must be non-negative, got ({self.x}, {self.y})"
            )
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise FaceDetectionInputError(
                f"confidence must be in [0, 1], got {self.confidence}"
            )


class FaceDetector:
    """Locate faces in caller-supplied BGR frames via the YuNet ONNX model.

    Construction touches no device and reads no model file: all resource
    work happens in :meth:`load`, which must be called before any
    :meth:`detect` call. ``load()`` is idempotent — a detector with a
    successfully initialized backend can be reused across many frames.
    """

    def __init__(
        self,
        model_path: Path | str = DEFAULT_MODEL_PATH,
        config: FaceProcessingConfig = FACE_PROCESSING,
    ) -> None:
        self._model_path = Path(model_path)
        self._config = config
        self._backend: cv2.FaceDetectorYN | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Explicitly initialize the detector (idempotent).

        Raises:
            FaceDetectorLoadError: if the model file is missing, unreadable,
                or rejected by the backend.
        """
        if self._backend is not None:
            return
        if not self._model_path.is_file():
            _logger.error(
                "face detector model missing: %s", self._model_path
            )
            raise FaceDetectorLoadError(
                f"YuNet ONNX model not found at '{self._model_path}'. "
                "Provide the model file (download from the official "
                "OpenCV zoo) and pass its path to FaceDetector()."
            )
        try:
            backend = cv2.FaceDetectorYN_create(
                str(self._model_path),
                "",
                (320, 320),  # provisional; setInputSize below adjusts it
                self._config.detection_confidence,
            )
            if backend is None:
                raise FaceDetectorLoadError(
                    f"cv2.FaceDetectorYN_create returned no detector for "
                    f"'{self._model_path}'"
                )
            self._backend = backend
            _logger.info(
                "face detector loaded: model=%s, score_threshold=%.2f",
                self._model_path,
                self._config.detection_confidence,
            )
        except FaceDetectorLoadError:
            raise
        except Exception as exc:  # noqa: BLE001 - wrap backend init failure
            raise FaceDetectorLoadError(
                f"failed to initialize FaceDetectorYN from "
                f"'{self._model_path}': {exc}"
            ) from exc

    @property
    def is_loaded(self) -> bool:
        """Whether the detector backend is initialized and ready."""
        return self._backend is not None

    def _require_backend(self) -> cv2.FaceDetectorYN:
        if self._backend is None:
            raise FaceDetectorError(
                "detector is not loaded; call load() before detect()"
            )
        return self._backend

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect(self, frame: np.ndarray) -> list[FaceDetection]:
        """Detect faces in ``frame`` (BGR uint8 ``(H, W, 3)``).

        Returns:
            A list of :class:`FaceDetection` — possibly empty for a valid
            frame with no detectable faces. Boxes are pixel coordinates
            ``(x, y, width, height)`` within the frame bounds.

        Raises:
            FaceDetectionInputError: if ``frame`` violates the input contract.
            FaceDetectorError: if the detector is not loaded or the backend
                itself fails (never silently as "no faces").
        """
        validated = self._validate_frame(frame)
        backend = self._require_backend()
        backend.setInputSize((validated.shape[1], validated.shape[0]))
        try:
            ok, raw = backend.detect(validated)
        except Exception as exc:  # noqa: BLE001 - wrap backend failure
            _logger.error("face detection pass failed: %s", exc)
            raise FaceDetectorError(f"face detection pass failed: {exc}") from exc
        return self._convert(raw, validated.shape)

    # ------------------------------------------------------------------
    # Frame validation (documented input contract)
    # ------------------------------------------------------------------

    def _validate_frame(self, frame: np.ndarray) -> np.ndarray:
        """Validate and return the frame untouched (no copies, no mutation).

        The contract is explicitly narrow: BGR uint8 ``(H, W, 3)`` with
        positive dimensions. Conversions/resizes are not performed —
        callers supplying other layouts get a clear error instead of a
        silently mis-detected image.
        """
        if frame is None:
            raise FaceDetectionInputError("frame must not be None")
        if not isinstance(frame, np.ndarray):
            raise FaceDetectionInputError(
                f"frame must be a numpy array, got {type(frame).__name__}"
            )
        if frame.size == 0:
            raise FaceDetectionInputError("frame must not be empty")
        if frame.ndim != 3:
            raise FaceDetectionInputError(
                f"frame must be 3-dimensional (H, W, 3) BGR, got "
                f"shape {frame.shape}"
            )
        if frame.shape[2] != 3:
            raise FaceDetectionInputError(
                f"frame must have 3 channels (BGR), got {frame.shape[2]} "
                "channels"
            )
        if frame.shape[0] <= 0 or frame.shape[1] <= 0:
            raise FaceDetectionInputError(
                f"frame dimensions must be positive, got {frame.shape[:2]}"
            )
        if frame.dtype != np.uint8:
            raise FaceDetectionInputError(
                f"frame dtype must be uint8, got {frame.dtype}"
            )
        return frame

    # ------------------------------------------------------------------
    # Result mapping
    # ------------------------------------------------------------------

    def _convert(
        self, raw: np.ndarray | None, frame_shape: tuple[int, ...]
    ) -> list[FaceDetection]:
        """Map the raw backend result to typed detections.

        Each row is ``[x, y, w, h, landmarks(10), score]``. Rows are
        checked against frame bounds; non-finite scores are filtered as
        backend anomalies (see module docstring).
        """
        frame_h, frame_w = frame_shape[0], frame_shape[1]
        detections: list[FaceDetection] = []
        if raw is None:
            return detections

        raw_array = np.asarray(raw)
        if raw_array.size == 0 or raw_array.ndim != 2:
            return detections

        for row in raw_array:
            if row.size < 4:
                _logger.warning(
                    "dropping malformed detection row with %d values",
                    row.size,
                )
                continue
            x, y, w, h = (int(round(float(v))) for v in row[:4])
            # YuNet emits 15 values per row: [x, y, w, h, landmarks(10),
            # score]. The score is the last column when present.
            score = float(row[-1]) if row.size > 4 else None
            if score is not None and not np.isfinite(score):
                _logger.warning("dropping detection with non-finite score")
                continue
            # Clamp to frame bounds; discard fully-outside boxes.
            if x >= frame_w or y >= frame_h:
                continue
            if x < 0 or y < 0:
                continue
            if w <= 0 or h <= 0:
                continue
            x2 = min(x + w, frame_w)
            y2 = min(y + h, frame_h)
            x = max(x, 0)
            y = max(y, 0)
            w = x2 - x
            h = y2 - y
            if w <= 0 or h <= 0:
                continue
            landmarks: tuple[tuple[int, int], ...] | None = None
            if row.size >= 15:
                raw_lm = row[4:14]
                if np.isfinite(raw_lm).all():
                    landmarks = tuple(
                        (int(px), int(py)) for px, py in raw_lm.reshape(-1, 2)
                    )
                else:
                    _logger.warning(
                        "dropping non-finite landmark values (box kept)"
                    )
            detections.append(
                FaceDetection(
                    x=x, y=y, width=w, height=h, confidence=score,
                    landmarks=landmarks,
                )
            )
        return detections
