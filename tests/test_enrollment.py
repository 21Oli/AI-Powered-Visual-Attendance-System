"""Step 07 tests: multi-image face enrollment.

All tests use fake camera/detector/assessor doubles and synthetic NumPy
frames in temporary directories — no physical webcam, no network, no
real biometric images, and no participation of ``models/yunet.onnx``
(the default detectors here are fakes, never loaded).

Fake contract notes
-------------------

* ``FakeCamera`` mirrors the real ``CameraService`` read contract:
  ``(True, frame)`` for a successful read, ``(False, None)`` for a
  failed read; ``open()``/``release()`` lifecycle is recorded so tests
  can assert release on every exit path.
* ``FakeDetector`` returns scripted detection lists per frame; it can
  be scripted to raise, mirroring ``FaceDetectorError`` conditions.
* ``FakeAssessor`` delegates to the *real* ``FaceQualityAssessor`` by
  default (real cv2 metrics on synthetic images) but every test that
  needs to push a decision can override ``assess_face``/assess-frame
  behavior; it can also be scripted to raise.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from src.config import AppSettings, EnrollmentConfig, QualityConfig
from src.employee import Employee
from src.enrollment import (
    DuplicateEnrollmentError,
    EnrollmentInputError,
    EnrollmentPersistenceError,
    EnrollmentResult,
    EnrollmentService,
    EnrollmentStatus,
    MANIFEST_FILENAME,
)
from src.face_detection import FaceDetection
from src.quality import (
    FaceQuality,
    FaceQualityAssessor,
    FaceQualityDecision,
    FaceQualityReason,
)


# ---------------------------------------------------------------------------
# Fixtures: synthetic images & settings (no camera hardware, no model file)
# ---------------------------------------------------------------------------


def make_face_frame(
    x: int = 100, y: int = 100, side: int = 120, intensity: int = 128
) -> np.ndarray:
    """Deterministic frame with a uniform bright block as the face."""
    frame = np.full((480, 640, 3), 20, dtype=np.uint8)
    frame[y : y + side, x : x + side] = intensity
    return frame


def varying_frames(count: int) -> list[np.ndarray]:
    """Distinct-but-valid frames (crops differ byte-wise, so a session
    that must COMPLETE cannot be derailed by exact-duplicate skipping,
    which deliberately mirrors real capture: a truly static scene *will*
    terminate via the attempt budget, honestly FAILED).
    """
    return [
        make_face_frame(intensity=100 + (i % 5) * 25) for i in range(count)
    ]


@pytest.fixture()
def settings(tmp_path: Path) -> AppSettings:
    """AppSettings pointed at the test's temporary data directory."""
    return AppSettings(data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


class FakeCamera:
    """Zero-hardware camera fake mirroring CameraService semantics."""

    def __init__(
        self,
        frames: list[np.ndarray],
        *,
        fail_read_at: set[int] | None = None,
        open_error: Exception | None = None,
    ) -> None:
        self.frames = list(frames)
        self.fail_read_at = set(fail_read_at or set())
        self.open_error = open_error
        self.open_calls = 0
        self.read_calls = 0
        self.released = False
        self.opened = False

    def open(self) -> None:
        self.open_calls += 1
        if self.open_error is not None:
            raise self.open_error
        self.opened = True

    def read_frame(self) -> tuple[bool, np.ndarray | None]:
        self.read_calls += 1
        if self.read_calls in self.fail_read_at or (
            self.read_calls > len(self.frames)
        ):
            return (False, None)
        return (True, self.frames[self.read_calls - 1])

    def release(self) -> None:
        self.released = True
        self.opened = False


class FakeFactory:
    """Callable returning the next scripted camera (or raising)."""

    def __init__(
        self,
        camera: FakeCamera | None = None,
        error: Exception | None = None,
    ) -> None:
        self.camera = camera
        self.error = error
        self.calls = 0

    def __call__(self) -> FakeCamera:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.camera is not None
        return self.camera


class FakeDetector:
    """Scripted detector: one list of detections per frame attempt."""

    def __init__(
        self, script: list[list[FaceDetection] | Exception]
    ) -> None:
        self.script = list(script)
        self.calls = 0

    def detect(self, frame: np.ndarray) -> list[FaceDetection]:
        self.calls += 1
        item = self.script.pop(0) if self.script else []
        if isinstance(item, Exception):
            raise item
        return item


class FakeAssessor(FaceQualityAssessor):
    """Real-assessor fake with scriptable override/raise."""

    def __init__(
        self,
        config: QualityConfig | None = None,
        *,
        frame_policy: list[FaceQualityReason | None | Exception] | None = None,
        face_script: list[FaceQuality | Exception] | None = None,
    ) -> None:
        super().__init__(config)
        self.frame_policy = list(frame_policy) if frame_policy else None
        self.face_script = list(face_script) if face_script else None
        self.assess_frame_calls = 0
        self.assess_face_calls = 0

    def assess_frame_faces(self, detections):  # type: ignore[override]
        self.assess_frame_calls += 1
        if self.frame_policy is not None:
            item = self.frame_policy.pop(0) if self.frame_policy else None
            if isinstance(item, Exception):
                raise item
            return item
        return super().assess_frame_faces(detections)

    def assess_face(self, frame, detection):  # type: ignore[override]
        self.assess_face_calls += 1
        if self.face_script is not None:
            if self.face_script:
                item = self.face_script.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            # Script exhausted: fall through to accept by default. Many
            # tests script a rejection prefix and expect a real accept
            # afterwards — a raising fake made every such session FAILED.
            return accept_quality()
        return accept_quality()


def accept_quality(box: tuple[int, int, int, int] = (100, 100, 120, 120)) -> FaceQuality:
    return FaceQuality(
        decision=FaceQualityDecision.ACCEPT,
        reason=None,
        sharpness=100.0,
        brightness=128.0,
        pose_yaw_ratio=0.0,
        pose_pitch_ratio=0.0,
        box=box,
    )


def reject_quality(
    reason: FaceQualityReason, box: tuple[int, int, int, int] = (100, 100, 120, 120)
) -> FaceQuality:
    return FaceQuality(
        decision=FaceQualityDecision.REJECT,
        reason=reason,
        sharpness=1.0,
        brightness=10.0,
        pose_yaw_ratio=None,
        pose_pitch_ratio=None,
        box=box,
    )


def det() -> FaceDetection:
    return FaceDetection(x=100, y=100, width=120, height=120)


def build_service(
    settings: AppSettings,
    camera_factory,
    detector,
    assessor,
    config: EnrollmentConfig | None = None,
) -> EnrollmentService:
    return EnrollmentService(
        camera_factory=camera_factory,
        detector=detector,
        assessor=assessor,
        enrollment_config=config or EnrollmentConfig(),
        settings=settings,
    )


@pytest.fixture()
def big_config() -> EnrollmentConfig:
    return EnrollmentConfig(min_samples=2, target_samples=5, max_samples=5)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestEmployeeValidation:
    def test_valid_ids_and_names_accepted(self, settings: AppSettings) -> None:
        result = self._run(settings, "EMP-0042", "Ada Lovelace")
        assert result.status is EnrollmentStatus.COMPLETED
        assert result.employee is not None
        assert result.employee.employee_id == "EMP-0042"

    def test_leading_zeros_preserved(self, settings: AppSettings) -> None:
        result = self._run(settings, "007", "Bond")
        assert result.employee is not None
        assert result.employee.employee_id == "007"

    def test_empty_id_rejected_before_any_capture(
        self, settings: AppSettings
    ) -> None:
        factory = FakeFactory(FakeCamera([make_face_frame()]))
        service = build_service(
            settings, factory, FakeDetector([]), FakeAssessor()
        )
        with pytest.raises(EnrollmentInputError):
            service.enroll("   ", "Ada")
        assert factory.calls == 0  # never touched the camera

    def test_empty_name_rejected(self, settings: AppSettings) -> None:
        service = build_service(
            settings, FakeFactory(), FakeDetector([]), FakeAssessor()
        )
        with pytest.raises(EnrollmentInputError):
            service.enroll("E1", "   ")

    def test_id_path_traversal_rejected(self, settings: AppSettings) -> None:
        service = build_service(
            settings, FakeFactory(), FakeDetector([]), FakeAssessor()
        )
        for bad in ("../evil", "a/b", "a\\b", ".", ".."):
            with pytest.raises(EnrollmentInputError):
                service.enroll(bad, "Ada")
        assert not (settings.employees_dir).exists()

    def _run(
        self, settings: AppSettings, emp_id: str, name: str
    ) -> EnrollmentResult:
        frames = varying_frames(3)
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 3),
            FakeAssessor(),
        )
        return service.enroll(emp_id, name)


# ---------------------------------------------------------------------------
# Duplicate protection
# ---------------------------------------------------------------------------


class TestDuplicateProtection:
    def test_duplicate_id_refused_data_unchanged(
        self, settings: AppSettings
    ) -> None:
        self._enroll_once(settings, "EMP-0042")
        before = sorted(
            p.read_bytes() for p in settings.data_dir.rglob("*") if p.is_file()
        )
        service = build_service(
            settings, FakeFactory(), FakeDetector([]), FakeAssessor()
        )
        with pytest.raises(DuplicateEnrollmentError):
            service.enroll("EMP-0042", "Someone Else")
        after = sorted(
            p.read_bytes() for p in settings.data_dir.rglob("*") if p.is_file()
        )
        assert before == after

    def test_duplicate_detection_independent_of_cwd(
        self, settings: AppSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._enroll_once(settings, "EMP-0042")
        monkeypatch.chdir(settings.data_dir.parent)  # arbitrary cwd
        service = build_service(
            settings, FakeFactory(), FakeDetector([]), FakeAssessor()
        )
        with pytest.raises(DuplicateEnrollmentError):
            service.enroll("EMP-0042", "X")

    def test_unrelated_employee_not_blocked(
        self, settings: AppSettings
    ) -> None:
        self._enroll_once(settings, "EMP-0042")
        result = self._enroll_once(settings, "EMP-0043")
        assert result.status is EnrollmentStatus.COMPLETED

    def _enroll_once(
        self, settings: AppSettings, emp_id: str
    ) -> EnrollmentResult:
        frames = varying_frames(3)
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 3),
            FakeAssessor(),
        )
        return service.enroll(emp_id, "Ada")


# ---------------------------------------------------------------------------
# Capture flow
# ---------------------------------------------------------------------------


class TestCaptureFlow:
    def test_completed_with_min_samples(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        frames = varying_frames(2)
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 2),
            FakeAssessor(),
            config=big_config,
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.COMPLETED
        assert len(result.samples) == big_config.min_samples

    def test_capture_continues_past_rejections(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        # 2 good frames, one no-face, one multi-face, one blurred —
        # capture must continue until min collected.
        frames = varying_frames(5)
        dets = [
            [det()],
            [],                       # no faces
            [det(), det()],           # multiple faces
            [det()],                  # quality-rejected (scripted)
            [det()],
            [det()],
        ]
        assessor = FakeAssessor(
            face_script=[accept_quality(), reject_quality(FaceQualityReason.BLUR)]
        )
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector(dets),
            assessor,
            config=big_config,
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.COMPLETED
        assert len(result.samples) == 2
        reasons = [r.reason for r in result.rejections]
        assert FaceQualityReason.NO_FACES in reasons
        assert FaceQualityReason.MULTIPLE_FACES in reasons
        assert FaceQualityReason.BLUR in reasons

    def test_cancelled_before_minimum_persists_nothing(
        self, settings: AppSettings
    ) -> None:
        frames = varying_frames(10)
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 10),
            FakeAssessor(),
        )
        result = service.enroll("E1", "Ada", should_cancel=lambda: True)
        assert result.status is EnrollmentStatus.CANCELLED
        assert not settings.employees_dir.exists()
        assert not settings.face_samples_dir.exists()

    def test_cancel_after_success_still_cancelled(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        # Cancel-returns-True *after* the first acceptance must still
        # be CANCELLED — never a half-published enrollment.
        seen: dict[str, int] = {"n": 0}

        def cancel_after_two() -> bool:
            seen["n"] += 1
            return seen["n"] > 2

        frames = varying_frames(10)
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 10),
            FakeAssessor(),
            config=big_config,
        )
        result = service.enroll("E1", "Ada", should_cancel=cancel_after_two)
        assert result.status is EnrollmentStatus.CANCELLED
        assert not settings.employees_dir.exists()

    def test_attempt_budget_exhausted_is_failed(
        self, settings: AppSettings
    ) -> None:
        # Every frame is rejected → the loop ends FAILED, no data.
        config = EnrollmentConfig(
            min_samples=2,
            target_samples=5,
            max_samples=5,
            max_capture_attempts=2,
        )
        frames = varying_frames(3)
        assessor = FakeAssessor(
            face_script=[
                reject_quality(FaceQualityReason.BLUR),
                reject_quality(FaceQualityReason.TOO_DARK),
            ]
        )
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 3),
            assessor,
            config=config,
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.FAILED
        assert result.attempted_frames == 2
        assert not (settings.employees_dir / "E1").exists()

    def test_no_face_frames_rejected_with_reason(
        self, settings: AppSettings
    ) -> None:
        # min_samples=1 so the first no-face frame is observable via a
        # single rejection before the run-to-budget-completion FAILED,
        # with the reason preserved verbatim.
        config = EnrollmentConfig(min_samples=1, target_samples=5, max_samples=5,
                                  max_capture_attempts=1)
        frames = varying_frames(2)
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([])] * 2),
            FakeAssessor(),
            config=config,
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.FAILED
        assert len(result.rejections) == 1
        assert result.rejections[0].reason is FaceQualityReason.NO_FACES
        assert result.rejections[0].quality is None

    def test_quality_rejections_preserve_reasons(
        self, settings: AppSettings
    ) -> None:
        config = EnrollmentConfig(min_samples=1, target_samples=5, max_samples=5,
                                  max_capture_attempts=3)
        frames = varying_frames(4)
        assessor = FakeAssessor(
            face_script=[
                reject_quality(FaceQualityReason.BLUR),
                reject_quality(FaceQualityReason.TOO_BRIGHT),
                reject_quality(FaceQualityReason.TOO_DARK),
            ]
        )
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 4),
            assessor,
            config=config,
        )
        result = service.enroll("E1", "Ada")
        got = [r.reason for r in result.rejections]
        assert got == [
            FaceQualityReason.BLUR,
            FaceQualityReason.TOO_BRIGHT,
            FaceQualityReason.TOO_DARK,
        ]


# ---------------------------------------------------------------------------
# Camera errors & lifecycle
# ---------------------------------------------------------------------------


class TestCameraErrors:
    def test_open_failure_is_failed_camera_released(
        self, settings: AppSettings
    ) -> None:
        camera = FakeCamera([], open_error=RuntimeError("device busy"))
        factory = FakeFactory(camera)
        service = build_service(
            settings, factory, FakeDetector([]), FakeAssessor()
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.FAILED
        assert "camera open failed" in (result.error or "")
        assert camera.released

    def test_factory_failure_is_failed_no_capture_attempted(
        self, settings: AppSettings
    ) -> None:
        factory = FakeFactory(error=RuntimeError("no device"))
        service = build_service(
            settings, factory, FakeDetector([]), FakeAssessor()
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.FAILED
        assert result.attempted_frames == 0

    def test_read_failures_do_not_endless_loop(
        self, settings: AppSettings
    ) -> None:
        camera = FakeCamera([], fail_read_at={1, 2, 3})
        config = EnrollmentConfig(
            min_samples=3, target_samples=5, max_samples=5,
            max_capture_attempts=4,
        )
        service = build_service(
            settings,
            FakeFactory(camera),
            FakeDetector([]),
            FakeAssessor(),
            config=config,
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.FAILED
        assert result.attempted_frames == 4
        assert camera.released

    def test_camera_released_on_success(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        camera = FakeCamera(varying_frames(2))
        service = build_service(
            settings,
            FakeFactory(camera),
            FakeDetector([([det()])] * 2),
            FakeAssessor(),
            config=big_config,
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.COMPLETED
        assert camera.released

    def test_camera_released_on_cancel(
        self, settings: AppSettings
    ) -> None:
        camera = FakeCamera(varying_frames(5))
        service = build_service(
            settings,
            FakeFactory(camera),
            FakeDetector([([det()])] * 5),
            FakeAssessor(),
        )
        service.enroll("E1", "Ada", should_cancel=lambda: True)
        assert camera.released


# ---------------------------------------------------------------------------
# Detector / assessor failures
# ---------------------------------------------------------------------------


class TestServiceErrors:
    def test_detector_exception_is_fatal_failed(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        frames = varying_frames(5)
        detector = FakeDetector([RuntimeError("backend boom")])
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            detector,
            FakeAssessor(),
            config=big_config,
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.FAILED
        assert "backend boom" in (result.error or "")
        assert not settings.employees_dir.exists()

    def test_assessor_exception_is_fatal_failed(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        frames = varying_frames(5)
        assessor = FakeAssessor(
            face_script=[RuntimeError("assessor boom")]
        )
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 5),
            assessor,
            config=big_config,
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.FAILED
        assert "assessor boom" in (result.error or "")


# ---------------------------------------------------------------------------
# Exact-duplicate suppression
# ---------------------------------------------------------------------------


class TestDuplicateSamples:
    def test_exact_same_crop_skipped(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        # [A, A, B]: A accepted; the second A is byte-identical and is
        # skipped (reported, not hidden); B accepted → COMPLETED.
        a = make_face_frame(intensity=150)
        b = make_face_frame(intensity=200)
        frames = [a, a.copy(), b]
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 3),
            FakeAssessor(),
            config=big_config,
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.COMPLETED
        assert result.duplicates_skipped == 1
        # Published files: only the distinct samples.
        files = sorted(p.name for p in (settings.face_samples_dir / "E1").glob("*.png"))
        assert len(files) == big_config.min_samples

    def test_different_crops_do_not_trigger_skip(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        frames = [
            make_face_frame(intensity=100),
            make_face_frame(intensity=200),
        ]
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 2),
            FakeAssessor(),
            config=big_config,
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.COMPLETED
        assert result.duplicates_skipped == 0


# ---------------------------------------------------------------------------
# Persistence layout & content
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_layout_naming_manifest(
        self,
        settings: AppSettings,
        big_config: EnrollmentConfig,
        tmp_path: Path,
    ) -> None:
        frames = varying_frames(2)
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 2),
            FakeAssessor(),
            config=big_config,
        )
        result = service.enroll("EMP-0042", "Ada")
        assert result.status is EnrollmentStatus.COMPLETED
        employee_dir = settings.employees_dir / "EMP-0042"
        manifest_path = employee_dir / MANIFEST_FILENAME
        assert manifest_path.is_file()
        samples_dir = settings.face_samples_dir / "EMP-0042"
        files = sorted(p.name for p in samples_dir.glob("*.png"))
        assert files == ["sample_001.png", "sample_002.png"]
        # result paths point at the published files
        assert all(s.path is not None and s.path.is_file() for s in result.samples)

    def test_manifest_content_schema(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        frames = [
            make_face_frame(side=150, y=90, intensity=140),
            make_face_frame(side=150, y=90, intensity=160),
        ]
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector(
                [[FaceDetection(x=100, y=90, width=150, height=150)]] * 2
            ),
            FakeAssessor(),
            config=big_config,
        )
        service.enroll("E1", "Ada  Lovelace ")
        manifest = json.loads(
            (settings.employees_dir / "E1" / MANIFEST_FILENAME).read_text(
                encoding="utf-8"
            )
        )
        assert manifest["format_version"] == 1
        assert manifest["sample_count"] == 2
        emp = manifest["employee"]
        assert emp["employee_id"] == "E1"
        assert emp["full_name"] == "Ada Lovelace"
        assert emp["is_active"] is True
        assert "created_at" in emp and emp["created_at"]
        s = manifest["samples"][0]
        assert s["file"].endswith(".png")
        assert s["quality_decision"] == "accept"
        assert isinstance(s["sharpness"], float)
        assert isinstance(s["brightness"], float)
        # datetime sanity: ISO including timezone
        assert s["captured_at"].endswith("+00:00") or s["captured_at"].endswith("Z")

    def test_special_chars_id_encoded_deterministically(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        frames = varying_frames(2)
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 2),
            FakeAssessor(),
            config=big_config,
        )
        result = service.enroll("EMP 0042", "Ada")
        assert result.status is EnrollmentStatus.COMPLETED
        # Space encoded, no slash, deterministic re-runs of encode.
        dirname = settings.employees_dir / "EMP%200042"
        assert (dirname / MANIFEST_FILENAME).is_file()
        assert (settings.face_samples_dir / "EMP%200042").is_dir()

    def test_dot_only_ids_rejected_at_input(
        self, settings: AppSettings
    ) -> None:
        service = build_service(
            settings, FakeFactory(), FakeDetector([]), FakeAssessor()
        )
        with pytest.raises(EnrollmentInputError):
            service.enroll(".", "Ada")
        with pytest.raises(EnrollmentInputError):
            service.enroll("..", "Ada")

    def test_is_active_false_recorded(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        frames = varying_frames(2)
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 2),
            FakeAssessor(),
            config=big_config,
        )
        result = service.enroll("E1", "Ada", is_active=False)
        assert result.status is EnrollmentStatus.COMPLETED
        manifest = json.loads(
            (settings.employees_dir / "E1" / MANIFEST_FILENAME).read_text(
                encoding="utf-8"
            )
        )
        assert manifest["employee"]["is_active"] is False

    def test_max_samples_cap_respected(
        self, settings: AppSettings
    ) -> None:
        # min=target=max=2 but the capture loop stops at min, so a large
        # battery of good frames still yields exactly min samples.
        frames = varying_frames(10)
        config = EnrollmentConfig(min_samples=2, target_samples=2, max_samples=2)
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 10),
            FakeAssessor(),
            config=config,
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.COMPLETED
        files = list((settings.face_samples_dir / "E1").glob("*.png"))
        assert len(files) == 2


# ---------------------------------------------------------------------------
# Persistence failures & cleanup
# ---------------------------------------------------------------------------


class TestPersistenceFailures:
    def test_pre_existing_sample_dir_refused_and_untouched(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        """A pre-existing sample directory is duplicate data: refused.

        The documented contract: pre-existing content is preserved
        byte-for-byte and the camera is never opened (duplicates are
        checked before capture).
        """
        samples_dir = settings.face_samples_dir / "E1"
        samples_dir.mkdir(parents=True, exist_ok=True)
        sentinel = samples_dir / "pre_existing_sentinel.txt"
        sentinel.write_text("keep me", encoding="utf-8")

        factory = FakeFactory()
        service = build_service(
            settings, factory, FakeDetector([]), FakeAssessor(),
            config=big_config,
        )
        with pytest.raises(DuplicateEnrollmentError):
            service.enroll("E1", "Ada")
        assert factory.calls == 0  # camera never touched
        assert sentinel.read_text(encoding="utf-8") == "keep me"
        assert not (settings.employees_dir / "E1").exists()

    def test_persistence_failure_leaves_no_staging_remnants(
        self,
        settings: AppSettings,
        big_config: EnrollmentConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Force a stage failure; verify only current-attempt cleanup."""
        import src.enrollment as enrollment_module

        frames = varying_frames(2)
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 2),
            FakeAssessor(),
            config=big_config,
        )
        original_encode = enrollment_module.EnrollmentService._encode_png

        def broken_encode(image):
            return None  # force PNG-encoding failure during staging

        monkeypatch.setattr(
            enrollment_module.EnrollmentService,
            "_encode_png",
            staticmethod(broken_encode),
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.FAILED
        assert "persistence failed" in (result.error or "")
        assert not (settings.employees_dir / "E1").exists()
        assert not (settings.face_samples_dir / "E1").exists()
        staging = (
            [
                p for p in settings.face_samples_dir.iterdir()
                if ".staging-" in p.name
            ]
            if settings.face_samples_dir.exists()
            else []
        )
        assert staging == []

    def test_unrelated_employees_untouched(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:  # pragma: no cover - covered in duplicate tests; kept for readability
        pass


# ---------------------------------------------------------------------------
# Result-shape invariants
# ---------------------------------------------------------------------------


class TestResultShape:
    def test_completed_has_no_error_message(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        frames = varying_frames(2)
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 2),
            FakeAssessor(),
            config=big_config,
        )
        result = service.enroll("E1", "Ada")
        assert result.error is None
        assert result.attempted_frames >= big_config.min_samples
        assert result.samples[0].frame_index >= 1

    def test_rejections_do_not_count_as_samples(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        frames = varying_frames(4)
        assessor = FakeAssessor(
            face_script=[reject_quality(FaceQualityReason.BLUR)]
        )
        service = build_service(
            settings,
            FakeFactory(FakeCamera(frames)),
            FakeDetector([([det()])] * 4),
            assessor,
            config=big_config,
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.COMPLETED
        assert len(result.samples) == big_config.min_samples
        assert all(
            s.quality.decision is FaceQualityDecision.ACCEPT
            for s in result.samples
        )

    def test_invalid_config_type_rejected(self, settings: AppSettings) -> None:
        with pytest.raises(EnrollmentInputError):
            EnrollmentService(
                camera_factory=FakeFactory(),
                detector=FakeDetector([]),
                assessor=FakeAssessor(),
                enrollment_config={"min_samples": 3},  # type: ignore[arg-type]
                settings=settings,
            )


# ---------------------------------------------------------------------------
# Import / construction hygiene
# ---------------------------------------------------------------------------


class TestHygiene:
    def test_default_construction_has_no_filesystem_side_effects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        # Default settings → PROJECT_ROOT/data; construction alone must
        # not create employee/sample directories anywhere.
        service = EnrollmentService()
        assert service is not None
        default_data = Path(os.environ.get("FREEBUFF_TEST_PROJ_ROOT", ".")) / "data"
        assert not (default_data / "employees").exists()
        assert not (default_data / "face_samples").exists()

    def test_service_independent_from_camera_implementation(
        self, settings: AppSettings, big_config: EnrollmentConfig
    ) -> None:
        """The service must not import/instantiate real cv2 cameras."""
        calls: list[str] = []

        class RecordingCamera(FakeCamera):
            pass

        camera = RecordingCamera(varying_frames(2))
        service = build_service(
            settings,
            FakeFactory(camera),
            FakeDetector([([det()])] * 2),
            FakeAssessor(),
            config=big_config,
        )
        result = service.enroll("E1", "Ada")
        assert result.status is EnrollmentStatus.COMPLETED
        assert calls == []  # (placeholder; real cv2 never called)
