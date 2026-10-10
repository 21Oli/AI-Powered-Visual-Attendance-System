"""Face quality assessment for the AI-Powered Visual Attendance System
(Phase 1 — Step 06).

Scores the *image quality* of a single detected face and applies the
frame's face-count policy — nothing else. This component does **not**
identify people (Step 10), decide attendance (Step 13), detect faces
(it consumes ``FaceDetection`` results produced by Step 05), own a
camera, write files, or touch the network. It is a pure, deterministic
image-metric layer between detection and enrollment.

Scope split (deliberate, per the architecture)
----------------------------------------------

* :meth:`FaceQualityAssessor.assess_face` — per-face **image quality**:
  size, sharpness, brightness, and an optional landmark-based
  orientation proxy. One ``FaceDetection`` + its source frame in, one
  :class:`FaceQuality` decision out.
* :meth:`FaceQualityAssessor.assess_frame_faces` — **face-count policy**
  only: whether the number of detections in a frame satisfies the
  configured single-face policy. Kept separate from image quality so
  future multi-face use cases (Step 09 embedding storage, Step 11
  temporal verification) can call per-face assessment freely.

Metric definitions (documented & deterministic)
-----------------------------------------------

* **Face size** — the axis-aligned box's usable (frame-clamped) width
  and height in pixels against ``min_face_size_px`` on *either* axis.
  A face partially outside the frame is measured on its clamped area;
  a face that is completely outside the frame is rejected outright —
  never silently accepted.
* **Sharpness / blur** — variance of the Laplacian:
  ``cv2.Laplacian(gray, cv2.CV_64F).var()`` on the grayscale face crop
  (the standard, deterministic proxy for blur). Higher = sharper.
  Absolute values are resolution/content dependent — treat the default
  threshold as a **starting point to calibrate on real footage**, not
  a universal biometric-quality constant.
* **Brightness** — mean of the grayscale face crop
  (``cv2.cvtColor(BGR→GRAY).mean()``, OpenCV's luminance weights).
  Outside ``[min_brightness, max_brightness]`` → rejected.
* **Pose / orientation** — *not* 3D pose estimation. With detector
  landmarks available, a lightweight geometric proxy is computed:
  the nose tip's horizontal offset from the eye midpoint (normalized
  by box width) and its vertical offset (normalized by box height).
  Without landmarks the check is **skipped and the metrics stay
  ``None``** — never invented values — under the default
  ``landmarks_optional`` policy; ``pose_required`` rejects instead.
  Documented limitation: this proxy cannot distinguish true head pose
  from facial-shape variation and is calibrated only in Phase 2.

Decisions & reasons: a face is accepted only when every executed check
passes; the single first-failed criterion is reported as a stable,
machine-readable :class:`FaceQualityReason`. When the caller violates
the input contract (bad frame, zero-area box, non-sequence detections)
the assessor raises :class:`FaceQualityInputError` — mirroring Step 05
conventions. Well-formed unusable inputs (out-of-frame face, tiny face,
flat image) are documented *returns*, not exceptions.

Configuration: all thresholds come from the frozen
:class:`src.config.QualityConfig` (Step 02 convention: centralized
constants, no scattered magic numbers, mutable nowhere). Defaults are
initial, testable values — **not** claims of production biometric
suitability without evaluation evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Sequence

import cv2
import numpy as np

from src.config import QUALITY, QualityConfig
from src.face_detection import FaceDetection
from src.logging_setup import get_logger

__all__ = [
    "FaceQualityAssessor",
    "FaceQuality",
    "FaceQualityDecision",
    "FaceQualityReason",
    "FaceQualityError",
    "FaceQualityInputError",
    "TARGET_QUALITY_METRICS",
]

_logger = get_logger(__name__)

#: Documented name of the metric set computed by this module.
TARGET_QUALITY_METRICS: Final[str] = (
    "box size + Laplacian-variance sharpness + grayscale mean brightness"
    " + landmark pose proxy (skipped & None without landmarks)"
)

#: Indices into ``FaceDetection.landmarks`` (canonical YuNet order,
#: from the subject's perspective: right eye, left eye, nose tip, ...).
_RIGHT_EYE: Final[int] = 0
_LEFT_EYE: Final[int] = 1
_NOSE: Final[int] = 2

#: Sentinel returned by the pose helper when landmarks are absent.
_NO_LANDMARKS: Final[str] = "no_landmarks"


class FaceQualityError(Exception):
    """Base class for face-quality errors."""


class FaceQualityInputError(FaceQualityError, ValueError):
    """Raised when inputs violate the documented assessor contract.

    Mirrors ``FaceDetectionInputError`` conventions: invalid frames,
    zero/negative-area boxes, or invalid detection collections raise
    this typed error instead of silently producing a decision.
    """


class FaceQualityReason(Enum):
    """Machine-readable, stable rejection/policy-violation reasons.

    The enum *values* are the public API — tests and callers match on
    them, so values are never renamed. New members may be appended.
    """

    # -- per-face image-quality reasons -----------------------------------
    #: Box (after clamping) has no area inside the frame.
    OUT_OF_FRAME = "out_of_frame"
    #: Usable box is below ``min_face_size_px`` on either axis.
    TOO_SMALL = "too_small"
    #: Crop contains no pixels (cannot happen after clamping; kept for
    #: exhaustive safety of the metric stage).
    EMPTY_CROP = "empty_crop"
    #: Strict-pose policy but the detection carries no landmarks.
    NO_LANDMARKS = "no_landmarks"
    #: Landmark orientation proxy outside the configured leeway.
    POSE = "pose"
    #: Laplacian-variance sharpness below ``min_sharpness``.
    BLUR = "blur"
    #: Mean crop brightness below ``min_brightness``.
    TOO_DARK = "too_dark"
    #: Mean crop brightness above ``max_brightness``.
    TOO_BRIGHT = "too_bright"
    # -- frame face-count policy reasons ----------------------------------
    #: Frame contains zero detections under the face-count policy.
    NO_FACES = "no_faces"
    #: Frame contains more faces than the policy allows.
    MULTIPLE_FACES = "multiple_faces"


class FaceQualityDecision(Enum):
    """Explicit decision surface — never a bare boolean."""

    ACCEPT = "accept"
    REJECT = "reject"


@dataclass(frozen=True)
class FaceQuality:
    """One face's measured quality metrics plus an explicit decision.

    Attributes:
        decision: ACCEPT or REJECT.
        reason: ``None`` when accepted; a :class:`FaceQualityReason`
            naming the single first-failed check otherwise.
        sharpness: Laplacian variance of the grayscale crop, or ``None``
            when the check never ran (undetermined — never faked).
        brightness: grayscale mean of the crop, or ``None`` likewise.
        pose_yaw_ratio: nose-tip horizontal offset from the eye
            midpoint, normalized by box width — or ``None`` without
            landmarks. NOT a 3D yaw angle.
        pose_pitch_ratio: nose-tip vertical offset from the eye
            midpoint, normalized by box height — or ``None`` likewise.
            NOT a 3D pitch angle.
        box: the usable (frame-clamped) ``(x, y, w, h)`` measured;
            ``(0, 0, 0, 0)`` only when nothing is usable (rejected
            before any crop existed).
    """

    decision: FaceQualityDecision
    reason: FaceQualityReason | None
    sharpness: float | None
    brightness: float | None
    pose_yaw_ratio: float | None
    pose_pitch_ratio: float | None
    box: tuple[int, int, int, int]


class FaceQualityAssessor:
    """Deterministic per-face image quality + frame face-count policy.

    Construction touches no camera, model, file, or network; instances
    are stateless and reusable. Thresholds come from a frozen
    :class:`QualityConfig` (default: the project-wide ``QUALITY``
    instance from ``src.config``).
    """

    def __init__(self, config: QualityConfig | None = None) -> None:
        self._config = config if config is not None else QUALITY

    # ------------------------------------------------------------------
    # Per-face image quality
    # ------------------------------------------------------------------

    def assess_face(
        self, frame: np.ndarray, detection: FaceDetection
    ) -> FaceQuality:
        """Measure image quality of one detected face (read-only).

        Args:
            frame: BGR uint8 ``(H, W, 3)`; not mutated, not copied.
            detection: a :class:`FaceDetection`. Its box may extend
                beyond the frame; the usable (clamped) area is measured.

        Returns:
            :class:`FaceQuality` with an explicit decision and, if
            rejected, the single first-failed stable reason. Rejection
            order (documented, stable): OUT_OF_FRAME → TOO_SMALL →
            (NO_LANDMARKS/POSE only when landmarks mandatory or
            measured-and-outside) → BLUR → TOO_DARK → TOO_BRIGHT.

        Raises:
            FaceQualityInputError: contract-violating inputs (``None``
                /empty/wrong-shape/dtype frame; ``None``/non-``FaceDetection``
                detection; zero/negative-area box). Such inputs are
                caller errors, never quality opinions.
        """
        self._validate_frame(frame)
        box = self._validate_box(detection, frame.shape)
        if box is None:
            return self._reject_without_metrics(FaceQualityReason.OUT_OF_FRAME)

        x, y, bw, bh = box
        crop = frame[y : y + bh, x : x + bw]
        if crop.size == 0:  # pragma: no cover - clamping precludes this
            return self._reject_without_metrics(FaceQualityReason.EMPTY_CROP)

        sharpness = self._sharpness(crop)
        brightness = self._brightness(crop)
        pose = self._pose_proxy(detection)
        size_ok = bw >= self._config.min_face_size_px and (
            bh >= self._config.min_face_size_px
        )

        def build(
            reason: FaceQualityReason | None,
        ) -> FaceQuality:
            return self._with_metrics(
                reason=reason,
                sharpness=sharpness,
                brightness=brightness,
                pose=pose,
                box=box,
            )

        # Stable check order; each branch documents its reason.
        if not size_ok:
            return build(FaceQualityReason.TOO_SMALL)
        if pose == _NO_LANDMARKS:
            if self._config.pose_policy == "required":
                return build(FaceQualityReason.NO_LANDMARKS)
        elif not self._pose_within_tolerance(pose):
            return build(FaceQualityReason.POSE)
        if sharpness < self._config.min_sharpness:
            return build(FaceQualityReason.BLUR)
        if brightness < self._config.min_brightness:
            return build(FaceQualityReason.TOO_DARK)
        if brightness > self._config.max_brightness:
            return build(FaceQualityReason.TOO_BRIGHT)
        return build(None)  # ACCEPT

    # ------------------------------------------------------------------
    # Frame face-count policy (kept separate from image quality)
    # ------------------------------------------------------------------

    def assess_frame_faces(
        self, detections: Sequence[FaceDetection]
    ) -> FaceQualityReason | None:
        """Apply the frame face-count policy to a detection collection.

        Args:
            detections: the detections returned by the detector for one
                frame (possibly empty).

        Returns:
            ``None`` when the frame satisfies the policy (exactly one
            face); a stable :class:`FaceQualityReason` naming the
            violation otherwise — :attr:`FaceQualityReason.NO_FACES`
            for an empty frame, :attr:`FaceQualityReason.MULTIPLE_FACES`
            for 2+ faces while ``require_single_face`` holds. With
            ``require_single_face`` disabled, 2+ faces are accepted at
            the policy level (per-face quality remains the caller's
            separate concern).

        Raises:
            FaceQualityInputError: if ``detections`` is ``None`` or not
                an iterable sequence — a caller contract breach.
        """
        if detections is None:
            raise FaceQualityInputError(
                "detections must be a sequence of FaceDetection, got None"
            )
        if isinstance(detections, (str, bytes, dict)) or not hasattr(
            detections, "__iter__"
        ):
            raise FaceQualityInputError(
                "detections must be a sequence of FaceDetection, got "
                f"{type(detections).__name__}"
            )
        count = len(detections) if hasattr(detections, "__len__") else sum(
            1 for _ in detections
        )
        if count == 0:
            return FaceQualityReason.NO_FACES
        if count > 1 and self._config.require_single_face:
            return FaceQualityReason.MULTIPLE_FACES
        return None

    # ------------------------------------------------------------------
    # Internals (documented metric implementations)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_frame(frame: np.ndarray) -> None:
        """Same frame contract as detection: BGR uint8 (H, W, 3)."""
        if frame is None:
            raise FaceQualityInputError("frame must not be None")
        if not isinstance(frame, np.ndarray):
            raise FaceQualityInputError(
                f"frame must be a numpy array, got {type(frame).__name__}"
            )
        if frame.size == 0:
            raise FaceQualityInputError("frame must not be empty")
        if frame.ndim != 3:
            raise FaceQualityInputError(
                f"frame must be 3-dimensional (H, W, 3) BGR, got shape "
                f"{frame.shape}"
            )
        if frame.shape[2] != 3:
            raise FaceQualityInputError(
                f"frame must have 3 channels (BGR), got {frame.shape[2]} "
                "channels"
            )
        if frame.shape[0] <= 0 or frame.shape[1] <= 0:
            raise FaceQualityInputError(
                f"frame dimensions must be positive, got {frame.shape[:2]}"
            )
        if frame.dtype != np.uint8:
            raise FaceQualityInputError(
                f"frame dtype must be uint8, got {frame.dtype}"
            )

    @staticmethod
    def _validate_box(
        detection: FaceDetection, frame_shape: tuple[int, ...]
    ) -> tuple[int, int, int, int] | None:
        """Return the usable (frame-clamped) box, or ``None`` if fully
        outside the frame.

        Raises:
            FaceQualityInputError: ``None``/wrong-type detection or a
                zero/negative-area box (malformed input, not opinion).
        """
        if detection is None:
            raise FaceQualityInputError("detection must not be None")
        if not isinstance(detection, FaceDetection):
            raise FaceQualityInputError(
                "detection must be a FaceDetection, got "
                f"{type(detection).__name__}"
            )
        if detection.width <= 0 or detection.height <= 0:
            raise FaceQualityInputError(
                f"box dimensions must be positive, got "
                f"{detection.width}x{detection.height}"
            )
        frame_h, frame_w = frame_shape[:2]
        x = max(detection.x, 0)
        y = max(detection.y, 0)
        x2 = min(detection.x + detection.width, frame_w)
        y2 = min(detection.y + detection.height, frame_h)
        if x2 <= x or y2 <= y:
            return None
        return (x, y, x2 - x, y2 - y)

    def _sharpness(self, crop: np.ndarray) -> float:
        """Variance of the Laplacian — the canonical blur proxy."""
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    def _brightness(self, crop: np.ndarray) -> float:
        """Mean grayscale level (OpenCV BGR→GRAY luminance weights)."""
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        return float(gray.mean())

    @staticmethod
    def _pose_proxy(
        detection: FaceDetection,
    ) -> tuple[float, float] | str:
        """Landmark orientation proxy (documented, not 3D pose).

        Returns:
            ``(yaw_ratio, pitch_ratio)`` when landmarks exist — the
            nose tip's horizontal offset from the eye midpoint
            normalized by box width and its vertical offset normalized
            by box height; the :data:`_NO_LANDMARKS` sentinel otherwise.
        """
        lm = detection.landmarks
        if not lm or len(lm) <= _NOSE:
            return _NO_LANDMARKS
        rx, ry = lm[_RIGHT_EYE]
        lx, ly = lm[_LEFT_EYE]
        nx, ny = lm[_NOSE]
        eye_mid_x = (rx + lx) / 2.0
        eye_mid_y = (ry + ly) / 2.0
        box_w = max(detection.width, 1)
        box_h = max(detection.height, 1)
        return ((nx - eye_mid_x) / box_w, (ny - eye_mid_y) / box_h)

    def _pose_within_tolerance(self, pose: tuple[float, float]) -> bool:
        """Whether the orientation proxy is inside the allowed leeway."""
        yaw, pitch = pose
        return (
            abs(yaw) <= self._config.pose_yaw_tolerance
            and abs(pitch) <= self._config.pose_pitch_tolerance
        )

    def _with_metrics(
        self,
        *,
        reason: FaceQualityReason | None,
        sharpness: float,
        brightness: float,
        pose: tuple[float, float] | str,
        box: tuple[int, int, int, int],
    ) -> FaceQuality:
        if isinstance(pose, tuple):
            yaw: float | None = pose[0]
            pitch: float | None = pose[1]
        else:
            yaw = pitch = None
        decision = (
            FaceQualityDecision.REJECT if reason is not None
            else FaceQualityDecision.ACCEPT
        )
        return FaceQuality(
            decision=decision,
            reason=reason,
            sharpness=sharpness,
            brightness=brightness,
            pose_yaw_ratio=yaw,
            pose_pitch_ratio=pitch,
            box=box,
        )

    def _reject_without_metrics(
        self, reason: FaceQualityReason
    ) -> FaceQuality:
        """Early reject before any metric could be computed (all None)."""
        return FaceQuality(
            decision=FaceQualityDecision.REJECT,
            reason=reason,
            sharpness=None,
            brightness=None,
            pose_yaw_ratio=None,
            pose_pitch_ratio=None,
            box=(0, 0, 0, 0),
        )
