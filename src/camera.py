"""Live camera service for the AI-Powered Visual Attendance System
(Phase 1 — Step 04).

Safely opens, reads from, and releases a physical camera through OpenCV's
``cv2.VideoCapture`` (already a pinned dependency). This is the *frame
source* for the future face-detection component — it contains no
detection, quality, recognition, enrollment, or attendance logic.

Configuration comes exclusively from ``src.config.CAMERA`` (Step 02
constants: ``index``, ``frame_width``, ``frame_height``). No camera
parameters are duplicated here. Logging reuses ``src.logging_setup``.

Interface
---------

``CameraService`` is the one public class:

.. code-block:: python

    service = CameraService()        # no device is touched yet
    service.open()                   # explicit acquisition; raises
                                     # CameraOpenError on failure
    if service.is_open:
        ok, frame = service.read_frame()   # (False, None) on failure
    service.release()                # idempotent

    # or, equivalently:
    with CameraService() as camera:
        ok, frame = camera.read_frame()

``__enter__`` performs the open (so acquisition failure propagates as
``CameraOpenError``); ``__exit__`` always releases the capture, even on
exception, and never masks the in-flight exception.

Error semantics (fail fast, never fabricate frames):

* ``open()`` raises :class:`CameraOpenError` when the device cannot be
  opened or a requested property could not be applied. Once ``open()``
  has failed, ``is_open`` is ``False`` — a failed open is never reported
  as a usable camera.
* ``read_frame()`` returns ``(False, None)`` when the capture fails; it
  returns ``(True, frame)`` only for a verified, non-empty frame read
  from the device. Fabricated or stale frames are never returned.
* Operations used in an invalid lifecycle state (reading/closing before
  opening, re-opening an open service) raise :class:`CameraStateError`.
* ``release()`` is safe to call any number of times and from any state.

Import hygiene: importing this module does not open a camera, create a
``VideoCapture``, start threads, create directories, or touch
network/database/ML resources. Device acquisition happens only when the
caller explicitly opens the service.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

import cv2
import numpy as np

from src.config import CameraConfig, CAMERA
from src.logging_setup import get_logger

__all__ = [
    "CameraError",
    "CameraOpenError",
    "CameraStateError",
    "CameraService",
]

_logger = get_logger(__name__)


class CameraError(Exception):
    """Base class for camera-service errors."""


class CameraOpenError(CameraError):
    """Raised when the camera cannot be opened or configured."""


class CameraStateError(CameraError):
    """Raised when an operation is attempted in an invalid lifecycle state."""


class CameraService:
    """Open, read from, and release a physical camera via OpenCV.

    Constructing the service touches no device. All device acquisition
    happens in :meth:`open` (or :meth:`__enter__`), and release happens in
    :meth:`release` (or :meth:`__exit__`).
    """

    def __init__(self, config: CameraConfig = CAMERA) -> None:
        self._config = config
        self._capture: cv2.VideoCapture | None = None

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """Whether a camera is currently open and usable.

        Mirrors ``cv2.VideoCapture.isOpened()``; a failed open is never
        reported as open.
        """
        return self._capture is not None and self._capture.isOpened()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the configured camera and apply configured frame settings.

        Raises:
            CameraStateError: if the service is already open.
            CameraOpenError: if the device cannot be opened or a
                requested property could not be applied.
        """
        if self.is_open:
            raise CameraStateError(
                "camera is already open; call release() before re-opening"
            )
        # Release any capture object left over from a *failed* previous
        # open so a retry never accumulates half-initialized handles.
        self._discard_unusable_capture()

        capture = cv2.VideoCapture(self._config.index)
        if not capture.isOpened():
            self._safe_release(capture)
            self._capture = None
            _logger.error(
                "camera open failed: unable to open device index %d",
                self._config.index,
            )
            raise CameraOpenError(
                f"unable to open camera device index {self._config.index}"
            )

        property_failures = self._apply_framing(capture)
        if property_failures:
            self._safe_release(capture)
            self._capture = None
            _logger.error(
                "camera open failed: could not apply requested camera "
                "properties on device index %d: %s",
                self._config.index,
                property_failures,
            )
            raise CameraOpenError(
                f"could not apply requested camera properties on device "
                f"index {self._config.index}: {property_failures}"
            )

        self._capture = capture
        _logger.info(
            "camera opened: index=%d, %dx%d",
            self._config.index,
            self._config.frame_width,
            self._config.frame_height,
        )

    def _apply_framing(self, capture: cv2.VideoCapture) -> list[str]:
        """Apply configured width/height; return descriptions of failures."""
        failures: list[str] = []
        for property_id, requested, label in (
            (cv2.CAP_PROP_FRAME_WIDTH, self._config.frame_width, "frame_width"),
            (cv2.CAP_PROP_FRAME_HEIGHT, self._config.frame_height, "frame_height"),
        ):
            capture.set(property_id, float(requested))
            applied = capture.get(property_id)
            if abs(applied - requested) > 0.5:
                failures.append(
                    f"requested {label}={requested}, device reports {applied:g}"
                )
        return failures

    def release(self) -> None:
        """Release the camera. Safe to call repeatedly, from any state."""
        if self._capture is None:
            return
        self._safe_release(self._capture)
        self._capture = None
        _logger.info("camera released: index=%d", self._config.index)

    def _discard_unusable_capture(self) -> None:
        """Release a capture object that is not reported as open."""
        if self._capture is not None and not self._capture.isOpened():
            self._safe_release(self._capture)
            self._capture = None

    @staticmethod
    def _safe_release(capture: cv2.VideoCapture) -> None:
        """Release ``capture``; failures are logged, never raised.

        Cleanup failures must not hide an exception already propagating
        from a caller's context block, so they are only logged here.
        """
        try:
            capture.release()
        except Exception:  # noqa: BLE001 - release failure is logged only
            _logger.exception("cv2.VideoCapture.release() failed")

    # ------------------------------------------------------------------
    # Frame access
    # ------------------------------------------------------------------

    def read_frame(self) -> tuple[bool, np.ndarray | None]:
        """Read one frame from the open camera.

        Returns:
            ``(False, None)`` when the read fails or not open; otherwise
            ``(True, frame)`` for a verified non-empty device frame.
            No fabricated or stale image is ever returned.

        Raises:
            CameraStateError: if the capture handle is missing but reads
                are attempted (invalid lifecycle state).
        """
        if self._capture is None:
            raise CameraStateError("camera is not open; call open() before read_frame()")
        if not self.is_open:
            _logger.error("frame read failed: capture is no longer open")
            return (False, None)

        ok, frame = self._capture.read()
        if not ok or frame is None or frame.size == 0:
            _logger.error("frame read failed: capture.read() returned no frame")
            return (False, None)

        return (True, frame)

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
        # Returning None lets any in-flight exception propagate unchanged.
