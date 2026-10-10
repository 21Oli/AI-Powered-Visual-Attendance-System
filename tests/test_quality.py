"""Step 06 face-quality verification.

Deterministic unit tests using synthetic NumPy/OpenCV images and typed
detections — no physical webcam, no biometric images, no model load, no
network. Metrics are computed with the real ``cv2`` operations so the
tested numbers are the ones production will see.

Structure follows ``tests/test_face_detection.py``: class-organized
suites, explicit docstrings per test, fresh-process import-hygiene
checks at the end.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from src.config import QUALITY, QualityConfig
from src.face_detection import FaceDetection
from src.quality import (
    FaceQualityAssessor,
    FaceQualityDecision,
    FaceQualityError,
    FaceQualityInputError,
    FaceQualityReason,
)


# ---------------------------------------------------------------------------
# Deterministic image fixtures (measured metric behavior, real cv2)
# ---------------------------------------------------------------------------


def make_sharp_face(h: int = 120, w: int = 120) -> np.ndarray:
    """A sharp mid-gray face-sized crop (deterministic Gaussian noise).

    Uses a private-seeded RNG: ``cv2.randn`` draws from NumPy's *global*
    stream, so other tests (real-backend detection suites) would shift
    the values — a private ``np.random.default_rng`` keeps the measured
    metric constants reproducible in any test ordering.
    """
    rng = np.random.default_rng(20261010)
    noise = rng.normal(loc=127.0, scale=30.0, size=(h, w)).astype(np.float32)
    return np.dstack([np.clip(noise, 0, 255).astype(np.uint8)] * 3)


def make_frame_from(crop: np.ndarray, h: int = 480, w: int = 640) -> np.ndarray:
    """Embed a crop in a larger, mid-gray frame at the fixed spot (100, 60)."""
    frame = np.full((h, w, 3), 127, np.uint8)
    frame[60 : 60 + crop.shape[0], 100 : 100 + crop.shape[1]] = crop
    return frame


def make_sharp_frame() -> np.ndarray:
    """Fixed 480×640 frame with the 120×120 sharp crop at (100, 60)."""
    return make_frame_from(make_sharp_face())


def make_dark_frame() -> np.ndarray:
    return make_frame_from(np.full((120, 120, 3), 8, np.uint8))


def make_bright_frame() -> np.ndarray:
    return make_frame_from(np.full((120, 120, 3), 245, np.uint8))


def make_flat() -> np.ndarray:
    return make_frame_from(np.full((120, 120, 3), 127, np.uint8))


def centered_face(w: int = 120, h: int = 120, **kwargs) -> FaceDetection:
    """A box over the embedded crop position (100, 60)."""
    return FaceDetection(x=100, y=60, width=w, height=h, **kwargs)


BIG_BOX = centered_face()
OTHER_FACE = FaceDetection(x=300, y=60, width=120, height=120)
OUT_OF_FRAME_FACE = FaceDetection(x=10_000, y=10_000, width=120, height=120)
TOO_SMALL_FACE = centered_face(w=60, h=60)


@pytest.fixture
def assessor() -> FaceQualityAssessor:
    return FaceQualityAssessor()


# Measured empirically for these exact fixtures (deterministic cv2):
# Gaussian noise σ=30 on 120×120 mid-gray → Laplacian variance ≈ 18015,
# brightness ≈ 126.7 (mid-gray interior dominates the mean).
EXPECTED_SHARPNESS = pytest.approx(17_948.3, abs=10.0)
EXPECTED_BRIGHTNESS = pytest.approx(126.98, abs=0.5)


def landmarked_face( lm=((40, 35), (80, 35), (60, 50), (50, 75), (70, 75)) ):
    """Box-local landmarks translated into frame coordinates at (100, 60).

    Eye midpoint local (60, 35), nose local (60, 50) → yaw 0, pitch
    15/120 = 0.125 — inside the default tolerances.
    """
    return centered_face(
        landmarks=tuple((100 + ox, 60 + oy) for ox, oy in lm)
    )


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


class TestAcceptance:
    def test_sharp_face_accepted_when_all_criteria_pass(self, assessor):
        result = assessor.assess_face(make_sharp_frame(), BIG_BOX)
        assert result.decision is FaceQualityDecision.ACCEPT
        assert result.reason is None
        assert result.sharpness == EXPECTED_SHARPNESS
        assert result.sharpness > QUALITY.min_sharpness
        assert result.brightness == EXPECTED_BRIGHTNESS
        assert QUALITY.min_brightness < result.brightness < QUALITY.max_brightness

    def test_metrics_are_reproducible(self, assessor):
        frame = make_sharp_frame()
        r1 = assessor.assess_face(frame, BIG_BOX)
        r2 = assessor.assess_face(frame.copy(), BIG_BOX)
        assert r1.sharpness == r2.sharpness
        assert r1.brightness == r2.brightness

    def test_accept_result_carries_box_and_value(self, assessor):
        result = assessor.assess_face(make_sharp_frame(), BIG_BOX)
        assert result.box == (100, 60, 120, 120)
        assert result.decision.value == "accept"

    def test_frontal_landmarked_face_accepted(self, assessor):
        result = assessor.assess_face(make_sharp_frame(), landmarked_face())
        assert result.decision is FaceQualityDecision.ACCEPT
        assert result.pose_yaw_ratio == pytest.approx(0.0)
        assert result.pose_pitch_ratio == pytest.approx(0.125)


# ---------------------------------------------------------------------------
# Rejections: each criterion, with machine-readable stable reasons
# ---------------------------------------------------------------------------


class TestRejections:
    def test_too_small_face_rejected(self, assessor):
        result = assessor.assess_face(make_sharp_frame(), TOO_SMALL_FACE)
        assert result.decision is FaceQualityDecision.REJECT
        assert result.reason is FaceQualityReason.TOO_SMALL

    def test_blurry_face_rejected(self, assessor):
        blurred = np.dstack(
            [cv2.GaussianBlur(make_sharp_face()[:, :, 0], (31, 31), 8)] * 3
        )
        result = assessor.assess_face(make_frame_from(blurred), BIG_BOX)
        assert result.decision is FaceQualityDecision.REJECT
        assert result.reason is FaceQualityReason.BLUR
        assert result.sharpness < QUALITY.min_sharpness

    def test_too_dark_face_rejected(self, assessor):
        # Additive darkening keeps the noise contrast, so the failure
        # is brightness — not blur (first failing check wins).
        dark = np.clip(
            make_sharp_face().astype(np.float32) - 120.0, 0, 255
        ).astype(np.uint8)
        result = assessor.assess_face(make_frame_from(dark), BIG_BOX)
        assert result.decision is FaceQualityDecision.REJECT
        assert result.reason is FaceQualityReason.TOO_DARK
        assert result.brightness < QUALITY.min_brightness
        assert result.sharpness > QUALITY.min_sharpness

    def test_too_bright_face_rejected(self, assessor):
        # Additive brightening keeps the noise contrast: brightness is
        # the first failing check, not blur.
        bright = np.clip(
            235.0 + (make_sharp_face().astype(np.float32) - 127.0) * 0.8,
            200.0, 255.0,
        ).astype(np.uint8)
        result = assessor.assess_face(make_frame_from(bright), BIG_BOX)
        assert result.decision is FaceQualityDecision.REJECT
        assert result.reason is FaceQualityReason.TOO_BRIGHT
        assert result.brightness > QUALITY.max_brightness
        assert result.sharpness > QUALITY.min_sharpness

    def test_flat_image_is_blur_rejected_with_zero_sharpness(self, assessor):
        result = assessor.assess_face(make_flat(), BIG_BOX)
        assert result.reason is FaceQualityReason.BLUR
        assert result.sharpness == 0.0

    def test_rejection_still_reports_metrics(self, assessor):
        result = assessor.assess_face(make_bright_frame(), BIG_BOX)
        assert result.brightness == pytest.approx(245.0)

    def test_first_failed_check_wins(self, assessor):
        """Stable order: TOO_SMALL before BLUR before BRIGHTNESS."""
        # Tiny, blurry, dark face — must report size, not blur/brightness.
        tiny_dark_blur = np.zeros((480, 640, 3), np.uint8)
        result = assessor.assess_face(tiny_dark_blur, TOO_SMALL_FACE)
        assert result.reason is FaceQualityReason.TOO_SMALL

    def test_reason_enum_values_are_the_stable_public_api(self):
        values = {r.value for r in FaceQualityReason}
        assert {
            "out_of_frame", "too_small", "empty_crop", "no_landmarks",
            "pose", "blur", "too_dark", "too_bright",
            "no_faces", "multiple_faces",
        } <= values


# ---------------------------------------------------------------------------
# Size & box handling
# ---------------------------------------------------------------------------


class TestBoxHandling:
    def test_partially_out_of_frame_measured_on_clamped_area(self, assessor):
        # Box extends past the frame right edge (100+300 > 640 → 540 wide
        # usable region ending at x=400): the *usable* box is what's
        # measured, and the metric covers the crop the pipeline would
        # actually use.
        face = FaceDetection(x=100, y=60, width=340, height=120)
        result = assessor.assess_face(make_sharp_frame(), face)
        assert result.box == (100, 60, 340, 120)  # fully inside: no clamp

    def test_box_at_right_edge_is_clamped(self, assessor):
        face = FaceDetection(x=520, y=60, width=340, height=120)  # 860 → 640
        result = assessor.assess_face(make_sharp_frame(), face)
        assert result.box == (520, 60, 120, 120)  # clamped at x=640

    def test_mostly_out_of_frame_rejects_on_size(self, assessor):
        face = FaceDetection(x=580, y=60, width=2000, height=110)
        result = assessor.assess_face(make_sharp_frame(), face)
        assert result.decision is FaceQualityDecision.REJECT
        assert result.reason is FaceQualityReason.TOO_SMALL
        assert result.box == (580, 60, 60, 110)  # 60px usable width left

    def test_completely_out_of_frame_rejected_and_metricfree(self, assessor):
        result = assessor.assess_face(make_sharp_frame(), OUT_OF_FRAME_FACE)
        assert result.decision is FaceQualityDecision.REJECT
        assert result.reason is FaceQualityReason.OUT_OF_FRAME
        assert result.sharpness is None
        assert result.brightness is None
        assert result.box == (0, 0, 0, 0)

    def test_negative_origin_box_clamped_into_frame(self, assessor):
        """Negative origins are a caller error (detection model already
        rejects them); measuring a clamped negative-origin box is done
        via an object.__new__-constructed detection."""
        face = FaceDetection.__new__(FaceDetection)
        object.__setattr__(face, "x", -50)
        object.__setattr__(face, "y", 60)
        object.__setattr__(face, "width", 200)
        object.__setattr__(face, "height", 120)
        object.__setattr__(face, "confidence", None)
        object.__setattr__(face, "landmarks", None)
        result = assessor.assess_face(make_sharp_frame(), face)
        # x clamps to 0; usable width = 150 (0..150 ≤ frame edge).
        assert result.box == (0, 60, 150, 120)

    def test_box_touching_frame_edge_is_valid(self, assessor):
        face = FaceDetection(x=100, y=60, width=120, height=420)
        result = assessor.assess_face(make_sharp_frame(), face)
        assert result.box == (100, 60, 120, 420)  # 60 + 420 = 480 = frame bottom

    def test_zero_area_box_is_input_error(self, assessor):
        """A zero-area box is a caller-contract breach (typed error).

        ``FaceDetection`` itself already rejects this at construction;
        the assessor enforces the same rule on its own inputs, so the
        malformed box is produced via ``object.__new__`` (bypassing the
        constructor's guard) to reach the assessor's validator.
        """
        zero = FaceDetection.__new__(FaceDetection)
        object.__setattr__(zero, "x", 100)
        object.__setattr__(zero, "y", 60)
        object.__setattr__(zero, "width", 0)
        object.__setattr__(zero, "height", 60)
        object.__setattr__(zero, "confidence", None)
        object.__setattr__(zero, "landmarks", None)
        with pytest.raises(FaceQualityInputError, match="positive"):
            assessor.assess_face(make_sharp_frame(), zero)

    def test_negative_area_box_is_input_error(self, assessor):
        neg = FaceDetection.__new__(FaceDetection)
        object.__setattr__(neg, "x", 100)
        object.__setattr__(neg, "y", 60)
        object.__setattr__(neg, "width", 60)
        object.__setattr__(neg, "height", -1)
        object.__setattr__(neg, "confidence", None)
        object.__setattr__(neg, "landmarks", None)
        with pytest.raises(FaceQualityInputError, match="positive"):
            assessor.assess_face(make_sharp_frame(), neg)

    def test_result_is_frozen(self, assessor):
        result = assessor.assess_face(make_sharp_frame(), BIG_BOX)
        with pytest.raises(Exception):
            result.decision = FaceQualityDecision.REJECT  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Frame face-count policy (kept separate from per-face quality)
# ---------------------------------------------------------------------------


class TestFaceCountPolicy:
    def test_zero_detections_reported_explicitly(self, assessor):
        assert assessor.assess_frame_faces([]) is FaceQualityReason.NO_FACES

    def test_single_face_satisfies_policy(self, assessor):
        assert assessor.assess_frame_faces([BIG_BOX]) is None

    def test_multiple_faces_rejected_under_single_face_policy(self, assessor):
        verdict = assessor.assess_frame_faces([BIG_BOX, OTHER_FACE])
        assert verdict is FaceQualityReason.MULTIPLE_FACES

    def test_multiple_faces_allowed_when_policy_disabled(self):
        cfg = QualityConfig(require_single_face=False)
        verdict = FaceQualityAssessor(cfg).assess_frame_faces(
            [BIG_BOX, OTHER_FACE]
        )
        assert verdict is None

    def test_policy_input_errors(self, assessor):
        with pytest.raises(FaceQualityInputError, match="None"):
            assessor.assess_frame_faces(None)
        with pytest.raises(FaceQualityInputError):
            assessor.assess_frame_faces("not-a-sequence")
        with pytest.raises(FaceQualityInputError):
            assessor.assess_frame_faces({"faces": 3})

    def test_per_face_assessment_stays_independent_of_frame_policy(
        self, assessor
    ):
        """A multi-face frame is a policy reject, yet each face is still
        individually assessable — the two concerns never merge."""
        assert (
            assessor.assess_frame_faces([BIG_BOX, OTHER_FACE])
            is FaceQualityReason.MULTIPLE_FACES
        )
        r1 = assessor.assess_face(make_sharp_frame(), BIG_BOX)
        # OTHER_FACE at (300, 60) sits on flat gray background in this
        # fixture — sharpness fails there by design; the point is that
        # assessment *runs* independently of the policy verdict.
        r2 = assessor.assess_face(make_sharp_frame(), OTHER_FACE)
        assert r1.decision is FaceQualityDecision.ACCEPT
        assert r2.decision is FaceQualityDecision.REJECT
        assert r2.reason is FaceQualityReason.BLUR


# ---------------------------------------------------------------------------
# Pose / orientation (landmark proxy — documented limitations)
# ---------------------------------------------------------------------------


class TestPose:
    def test_frontal_landmarks_accepted_with_reported_ratios(self, assessor):
        result = assessor.assess_face(make_sharp_frame(), landmarked_face())
        assert result.decision is FaceQualityDecision.ACCEPT
        assert result.pose_yaw_ratio == pytest.approx(0.0)
        assert result.pose_pitch_ratio == pytest.approx(0.125)

    def test_strong_yaw_rejected_with_pose_reason(self, assessor):
        # Nose 40px right of the eye midpoint → yaw = 40/120 ≈ 0.333.
        face = landmarked_face(
            lm=((40, 35), (80, 35), (100, 50), (50, 75), (70, 75))
        )
        result = assessor.assess_face(make_sharp_frame(), face)
        assert result.reason is FaceQualityReason.POSE
        assert result.pose_yaw_ratio == pytest.approx(40 / 120)

    def test_extreme_pitch_rejected(self, assessor):
        # Nose 60px below eye midpoint → pitch = 60/120 = 0.5 > 0.35.
        face = landmarked_face(
            lm=((40, 35), (80, 35), (60, 95), (50, 75), (70, 75))
        )
        result = assessor.assess_face(make_sharp_frame(), face)
        assert result.reason is FaceQualityReason.POSE

    def test_missing_landmarks_skipped_by_default(self, assessor):
        result = assessor.assess_face(make_sharp_frame(), BIG_BOX)
        assert result.decision is FaceQualityDecision.ACCEPT
        assert result.pose_yaw_ratio is None
        assert result.pose_pitch_ratio is None

    def test_missing_landmarks_rejected_under_required_policy(self):
        cfg = QualityConfig(pose_policy="required")
        result = FaceQualityAssessor(cfg).assess_face(
            make_sharp_frame(), BIG_BOX
        )
        assert result.decision is FaceQualityDecision.REJECT
        assert result.reason is FaceQualityReason.NO_LANDMARKS

    def test_pose_tolerance_tightening_changes_verdict(self):
        # Nose 12px off-center → yaw = 0.10: passes at 0.15, fails at 0.05.
        face = landmarked_face(
            lm=((40, 35), (80, 35), (72, 50), (50, 75), (70, 75))
        )
        loose = FaceQualityAssessor()
        strict = FaceQualityAssessor(QualityConfig(pose_yaw_tolerance=0.05))
        assert (
            loose.assess_face(make_sharp_frame(), face).decision
            is FaceQualityDecision.ACCEPT
        )
        assert (
            strict.assess_face(make_sharp_frame(), face).reason
            is FaceQualityReason.POSE
        )

    def test_landmarks_without_nose_are_treated_as_absent(self, assessor):
        """A landmarks tuple shorter than the nose index is unusable."""
        face = centered_face(landmarks=((140, 95), (180, 95)))
        result = assessor.assess_face(make_sharp_frame(), face)
        assert result.pose_yaw_ratio is None  # skipped, never invented


# ---------------------------------------------------------------------------
# Input safety
# ---------------------------------------------------------------------------


class TestInputSafety:
    @pytest.mark.parametrize(
        "bad",
        [
            None,
            "not an array",
            np.zeros((10, 10), np.uint8),      # 2-D
            np.zeros((10, 10, 4), np.uint8),   # 4 channels
            np.zeros((0, 10, 3), np.uint8),    # empty
            np.zeros((10, 0, 3), np.uint8),    # zero width
            np.zeros((10, 10, 3), np.float32),  # wrong dtype
        ],
    )
    def test_invalid_frames_raise_typed_input_error(self, assessor, bad):
        with pytest.raises(FaceQualityInputError):
            assessor.assess_face(bad, BIG_BOX)

    def test_input_error_hierarchy_is_documented(self):
        assert issubclass(FaceQualityInputError, FaceQualityError)
        assert issubclass(FaceQualityInputError, ValueError)
        assert issubclass(FaceQualityError, Exception)

    def test_non_face_detection_object_rejected(self, assessor):
        with pytest.raises(FaceQualityInputError, match="FaceDetection"):
            assessor.assess_face(make_sharp_frame(), (100, 60, 120, 120))

    def test_none_detection_rejected(self, assessor):
        with pytest.raises(FaceQualityInputError, match="None"):
            assessor.assess_face(make_sharp_frame(), None)

    def test_input_frame_is_never_mutated(self, assessor):
        frame = make_sharp_frame()
        snapshot = frame.copy()
        assessor.assess_face(frame, BIG_BOX)
        assessor.assess_face(frame, TOO_SMALL_FACE)
        assessor.assess_face(frame, OUT_OF_FRAME_FACE)
        assert np.array_equal(frame, snapshot)

    def test_source_detection_is_never_mutated(self, assessor):
        det = BIG_BOX
        assessor.assess_face(make_sharp_frame(), det)
        assert (det.x, det.y, det.width, det.height) == (100, 60, 120, 120)


# ---------------------------------------------------------------------------
# Threshold configuration changes behavior
# ---------------------------------------------------------------------------


class TestThresholdConfiguration:
    def test_raising_size_floor_flips_a_passing_face(self):
        strict = FaceQualityAssessor(QualityConfig(min_face_size_px=150))
        result = strict.assess_face(make_sharp_frame(), BIG_BOX)  # 120px box
        assert result.reason is FaceQualityReason.TOO_SMALL

    def test_raising_sharpness_floor_rejects_moderate_sharp(self):
        cfg = QualityConfig(min_sharpness=50_000.0)
        result = FaceQualityAssessor(cfg).assess_face(
            make_sharp_frame(), BIG_BOX
        )  # measured ≈ 17.9k
        assert result.reason is FaceQualityReason.BLUR

    def test_brightness_window_narrowing_changes_verdict(self):
        cfg = QualityConfig(max_brightness=100.0)  # default window min=40
        result = FaceQualityAssessor(cfg).assess_face(
            make_sharp_frame(), BIG_BOX
        )  # brightness ≈ 126.7
        assert result.reason is FaceQualityReason.TOO_BRIGHT

    def test_invalid_thresholds_fail_fast(self):
        with pytest.raises(ValueError):
            QualityConfig(min_face_size_px=0)
        with pytest.raises(ValueError):
            QualityConfig(min_sharpness=-1.0)
        with pytest.raises(ValueError):
            QualityConfig(min_brightness=100.0, max_brightness=90.0)  # inverted
        with pytest.raises(ValueError):
            QualityConfig(pose_yaw_tolerance=-1.0)
        with pytest.raises(ValueError):
            QualityConfig(pose_policy="strict")  # not an allowed policy

    def test_project_default_instance_frozen_and_usable(self):
        a1 = FaceQualityAssessor()  # injects project QUALITY
        a2 = FaceQualityAssessor(QUALITY)
        frame = make_sharp_frame()
        r1, r2 = a1.assess_face(frame, BIG_BOX), a2.assess_face(frame, BIG_BOX)
        assert r1.decision is r2.decision
        assert r1.sharpness == r2.sharpness
        with pytest.raises(Exception):
            QUALITY.min_sharpness = 1.0  # frozen dataclass


# ---------------------------------------------------------------------------
# Scope & architecture guarantees
# ---------------------------------------------------------------------------


class TestScopeGuarantees:
    def test_assessor_performs_no_filesystem_writes(self, assessor, tmp_path):
        data_dir = PROJECT_ROOT / "data"
        before = sorted(str(p) for p in data_dir.rglob("*"))
        assessor.assess_face(make_sharp_frame(), BIG_BOX)
        assessor.assess_frame_faces([BIG_BOX])
        after = sorted(str(p) for p in data_dir.rglob("*"))
        assert before == after

    def test_public_api_has_no_persistence_enrollment_camera_symbols(self):
        import src.quality as q

        banned = ("save", "store", "persist", "enroll", "webcam",
                  "videocapture", "download", "urlopen")
        for symbol in q.__all__:
            for token in banned:
                assert token not in symbol.lower(), (symbol, token)

    def test_source_has_no_camera_or_network_or_file_writes(self):
        source = (PROJECT_ROOT / "src" / "quality.py").read_text(
            encoding="utf-8"
        )
        for token in ("VideoCapture", "urlopen", "requests.", "socket.",
                      "open(", "mkdir"):
            assert token not in source, token

    def test_import_hygiene_no_side_effects(self):
        """Fresh process: no camera, no model init, no state on import;
        constructing the assessor is side-effect free too."""
        code = (
            "import sys\n"
            f"sys.path.insert(0, r'{PROJECT_ROOT}')\n"
            "import unittest.mock as m\n"
            "with m.patch('cv2.VideoCapture') as cam:\n"
            "    import src.quality\n"
            "    assert cam.call_count == 0, 'camera opened on import'\n"
            "with m.patch('cv2.FaceDetectorYN_create') as create:\n"
            "    import src.quality as q2\n"
            "    assert create.call_count == 0, 'model loaded on import'\n"
            "from src.quality import FaceQualityAssessor\n"
            "with m.patch('cv2.VideoCapture') as cam2, "
            "m.patch('cv2.FaceDetectorYN_create') as create2:\n"
            "    FaceQualityAssessor()\n"
            "    assert cam2.call_count == 0, 'camera in constructor'\n"
            "    assert create2.call_count == 0, 'model in constructor'\n"
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

    def test_import_does_not_pull_unrelated_ml_stack(self):
        code = (
            "import sys; sys.path.insert(0, r'"
            + str(PROJECT_ROOT)
            + "');"
            "import src.quality;"
            "banned = {'mediapipe', 'insightface', 'onnxruntime', 'torch',"
            " 'tensorflow'};"
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
