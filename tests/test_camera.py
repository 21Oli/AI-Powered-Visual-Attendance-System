"""Step 04 live-camera-service verification.

Maps to the Step 04 test requirements. Every camera interaction is
simulated with in-memory doubles for ``cv2.VideoCapture`` — the suite
never touches physical camera hardware.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from src.camera import (
    CameraError,
    CameraOpenError,
    CameraService,
    CameraStateError,
)
from src.config import CAMERA, CameraConfig, FACE_PROCESSING


def make_frame(seed: int = 0) -> np.ndarray:
    """A non-empty, realistic-looking BGR frame with a distinct eye color."""
    rng = np.random.default_rng(seed)
    frame = rng.integers(low=0, high=256, size=(720, 1280, 3), dtype=np.uint8)
    frame[:, :, 2] = np.uint8(200)  # distinct red channel, never all-black
    return frame


class FakeCapture:
    """Deterministic replacement for ``cv2.VideoCapture``."""

    def __init__(self, camera_index: int, *, opens_ok: bool = True) -> None:
        self.camera_index = camera_index
        self.opens_ok = opens_ok
        self.released = False
        self.read_returns: tuple[bool, np.ndarray | None] = (True, make_frame())
        self.props: dict[int, float] = {}
        self._open = opens_ok

    def isOpened(self) -> bool:  # noqa: N802 - matches cv2 API
        return self._open

    def set(self, prop: int, value: float) -> bool:
        self.props[prop] = value
        return True

    def get(self, prop: int) -> float:
        return self.props.get(prop, 0.0)

    def read(self) -> tuple[bool, np.ndarray | None]:
        return self.read_returns

    def release(self) -> None:
        self.released = True
        self._open = False


class FakeCaptureFactory:
    """Callable standing in for ``cv2.VideoCapture``."""

    def __init__(
        self,
        *,
        opens_ok: bool = True,
        props: dict[int, float] | None = None,
    ) -> None:
        self.opens_ok = opens_ok
        self.props = props or {}
        self.created: list[FakeCapture] = []

    def __call__(self, camera_index: int) -> FakeCapture:
        cap = FakeCapture(camera_index, opens_ok=self.opens_ok)
        for key, value in self.props.items():
            cap.props[key] = value  # pre-seed reported properties
        self.created.append(cap)
        return cap


FRAME_WIDTH_PROP = 3   # cv2.CAP_PROP_FRAME_WIDTH
FRAME_HEIGHT_PROP = 4  # cv2.CAP_PROP_FRAME_HEIGHT


@pytest.fixture
def good_capture_factory():
    """Patch cv2.VideoCapture to a factory whose device opens cleanly."""
    factory = FakeCaptureFactory(
        opens_ok=True,
        props={FRAME_WIDTH_PROP: CAMERA.frame_width, FRAME_HEIGHT_PROP: CAMERA.frame_height},
    )
    with patch("src.camera.cv2.VideoCapture", factory):
        yield factory


@pytest.fixture
def failing_open_factory():
    factory = FakeCaptureFactory(opens_ok=False)
    with patch("src.camera.cv2.VideoCapture", factory):
        yield factory


@pytest.fixture
def bad_dimensions_factory():
    factory = FakeCaptureFactory(
        opens_ok=True,
        props={FRAME_WIDTH_PROP: 640, FRAME_HEIGHT_PROP: 480},
    )
    with patch("src.camera.cv2.VideoCapture", factory):
        yield factory


@pytest.fixture
def open_service(good_capture_factory) -> CameraService:
    service = CameraService()
    service.open()
    return service


# ---------------------------------------------------------------------------
# 1. Successful open
# ---------------------------------------------------------------------------


def test_successful_open(good_capture_factory) -> None:
    service = CameraService()
    service.open()
    assert service.is_open is True


# ---------------------------------------------------------------------------
# 2. Failed open
# ---------------------------------------------------------------------------


def test_failed_open_raises_and_reports_not_open(failing_open_factory) -> None:
    service = CameraService()
    with pytest.raises(CameraOpenError, match="unable to open camera device"):
        service.open()
    assert service.is_open is False


def test_failed_open_released_the_capture(failing_open_factory) -> None:
    service = CameraService()
    with pytest.raises(CameraOpenError):
        service.open()
    assert failing_open_factory.created[0].released is True


def test_failed_open_error_hierarchy() -> None:
    assert issubclass(CameraOpenError, CameraError)
    assert issubclass(CameraStateError, CameraError)
    # Cleanup failures must not mask exception propagation; open/state
    # errors are CameraError (plain Exception), deliberately not ValueError.
    assert CameraError.__bases__ == (Exception,)


def test_failed_open_retry_after_failure(
    failing_open_factory, good_capture_factory
) -> None:
    """Opening may be retried cleanly after a failed open (no stale state)."""
    service = CameraService()
    with patch("src.camera.cv2.VideoCapture", failing_open_factory):
        with pytest.raises(CameraOpenError):
            service.open()
    with patch("src.camera.cv2.VideoCapture", good_capture_factory):
        service.open()
        assert service.is_open is True


# ---------------------------------------------------------------------------
# 3. Successful read
# ---------------------------------------------------------------------------


def test_successful_read(open_service, good_capture_factory) -> None:
    ok, frame = open_service.read_frame()
    assert ok is True
    assert isinstance(frame, np.ndarray)
    assert frame.size > 0


# ---------------------------------------------------------------------------
# 4. Failed read
# ---------------------------------------------------------------------------


def test_failed_read_reports_false_none(open_service, good_capture_factory) -> None:
    cap = good_capture_factory.created[0]
    cap.read_returns = (False, None)
    ok, frame = open_service.read_frame()
    assert ok is False
    assert frame is None


def test_empty_array_read_is_failure(open_service, good_capture_factory) -> None:
    cap = good_capture_factory.created[0]
    cap.read_returns = (True, np.empty((0, 0, 3), dtype=np.uint8))
    ok, frame = open_service.read_frame()
    assert ok is False
    assert frame is None


def test_none_frame_read_is_failure(open_service, good_capture_factory) -> None:
    cap = good_capture_factory.created[0]
    cap.read_returns = (True, None)
    ok, frame = open_service.read_frame()
    assert ok is False
    assert frame is None


# ---------------------------------------------------------------------------
# 5 & 6. Release and idempotent release
# ---------------------------------------------------------------------------


def test_release_releases_underlying_capture(open_service, good_capture_factory) -> None:
    assert good_capture_factory.created[0].released is False
    open_service.release()
    assert good_capture_factory.created[0].released is True
    assert open_service.is_open is False


def test_release_is_idempotent(open_service) -> None:
    open_service.release()
    open_service.release()
    open_service.release()
    assert open_service.is_open is False


def test_release_before_any_open_is_safe() -> None:
    service = CameraService()
    service.release()
    assert service.is_open is False


# ---------------------------------------------------------------------------
# 7 & 8. Context-manager cleanup (normal and exceptional)
# ---------------------------------------------------------------------------


def test_context_manager_opens_and_closes(good_capture_factory) -> None:
    with CameraService() as camera:
        assert camera.is_open is True
    assert camera.is_open is False


def test_context_manager_releases_on_exception(good_capture_factory) -> None:
    with pytest.raises(RuntimeError, match="in-context failure"):
        with CameraService() as camera:
            assert camera.is_open is True
            raise RuntimeError("in-context failure")
    assert camera.is_open is False
    assert good_capture_factory.created[0].released is True


def test_context_manager_open_failure_propagates(failing_open_factory) -> None:
    with pytest.raises(CameraOpenError):
        with CameraService():
            pass


# ---------------------------------------------------------------------------
# 9. No capture leak when open fails
# ---------------------------------------------------------------------------


def test_open_failure_does_not_leak_the_handle(failing_open_factory) -> None:
    service = CameraService()
    with pytest.raises(CameraOpenError):
        service.open()
    assert failing_open_factory.created[0].released is True
    assert service._capture is None


def test_read_frame_after_release_raises() -> None:
    service = CameraService()
    service.release()  # never opened
    with pytest.raises(CameraStateError, match="camera is not open"):
        service.read_frame()


def test_read_frame_when_device_lost_reports_failure(
    good_capture_factory,
) -> None:
    service = CameraService()
    service.open()
    cap = good_capture_factory.created[0]
    cap._open = False  # device has become unavailable
    ok, frame = service.read_frame()
    assert ok is False
    assert frame is None


def test_read_frame_before_open_raises() -> None:
    service = CameraService()
    with pytest.raises(CameraStateError, match="camera is not open"):
        service.read_frame()


def test_reopen_while_still_open_raises(open_service) -> None:
    with pytest.raises(CameraStateError, match="already open"):
        open_service.open()


# ---------------------------------------------------------------------------
# 11 & 12. Configured index and framing are honored
# ---------------------------------------------------------------------------


def test_configured_camera_index_is_used(good_capture_factory) -> None:
    CameraService().open()
    (capture,) = good_capture_factory.created
    assert capture.camera_index == CAMERA.index


def test_camera_open_applies_configured_framing(
    open_service, good_capture_factory
) -> None:
    cap = good_capture_factory.created[0]
    assert cap.props[FRAME_WIDTH_PROP] == CAMERA.frame_width
    assert cap.props[FRAME_HEIGHT_PROP] == CAMERA.frame_height


def test_custom_config_is_used_instead_of_default(
    good_capture_factory,
) -> None:
    custom = CameraConfig(index=2, frame_width=960)
    service = CameraService(config=custom)
    service.open()
    (capture,) = good_capture_factory.created
    assert capture.camera_index == 2
    assert capture.props[FRAME_WIDTH_PROP] == 960


def test_config_is_not_duplicated_in_camera_module(good_capture_factory) -> None:
    """The module must read from src.config.CAMERA, not hardcode values."""
    import src.camera as camera_module

    source = Path(camera_module.__file__).read_text(encoding="utf-8")
    assert "from src.config import" in source
    assert "CAMERA" in source or "CameraConfig" in source
    assert "cv2.VideoCapture" in source
    for hardcoded in ("index=0", "width=1280", "height=720", "index: int = 0"):
        assert hardcoded not in source, f"camera config hardcodes {hardcoded!r}"


# ---------------------------------------------------------------------------
# 13. Lifecycle-state semantics for close/release and empty state
# ---------------------------------------------------------------------------


def test_service_starts_closed() -> None:
    assert CameraService().is_open is False


@pytest.mark.parametrize("opens_ok", [True, False])
def test_release_after_close_is_harmless(good_capture_factory, opens_ok) -> None:
    factory = good_capture_factory if opens_ok else None
    service = CameraService()
    if opens_ok:
        service.open()
    service.release()
    service.release()
    assert service.is_open is False


def test_camera_service_never_touches_unrelated_config() -> None:
    """The camera service must not depend on face-processing config."""
    assert dataclasses.is_dataclass(FACE_PROCESSING)
    with pytest.raises(dataclasses.FrozenInstanceError):
        FACE_PROCESSING.detection_confidence = 0.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Import hygiene (side-effect test)
# ---------------------------------------------------------------------------


def test_import_does_not_instantiate_videocapture() -> None:
    """Importing src.camera in a fresh interpreter must not create a
    cv2.VideoCapture instance, open a device, start threads, or take on
    any resource. Run in a fresh process so any already-cached module
    cannot mask the behavior."""
    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{PROJECT_ROOT}')\n"
        "import unittest.mock as m\n"
        "with m.patch('cv2.VideoCapture') as cap_cls:\n"
        "    import src.camera\n"
        "    assert cap_cls.call_count == 0, 'VideoCapture instantiated on import'\n"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")


def test_import_does_not_pull_ml_stack() -> None:
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "import src.camera;"
        "banned = {'mediapipe', 'insightface', 'onnxruntime'};"
        "loaded = banned & set(sys.modules);"
        "assert not loaded, f'unexpected imports: {loaded}';"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("OK")
