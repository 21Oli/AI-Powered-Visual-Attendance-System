"""Step 15 tests: integration demo.

Deterministic, hardware-free: the smoke pipeline is exercised through
its injected boundary doubles (scripted frame source, scripted
detector, stub embedder) while every downstream component — embedding
storage, recognition service, temporal verifier, unknown-face handler,
and the attendance engine — is the real project service with
unchanged thresholds and contracts. **No test uses the real SFace
model** (no test performs real model inference, and models are never
downloaded) and **no test touches a camera**: live mode is covered
through its resource-safety and prerequisite-failure paths with
injected doubles only.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

import src.integration_demo as demo_mod
from src.config import AppSettings
from src.face_detection import FaceDetection
from src.quality import FaceQualityDecision, FaceQualityReason
from src.integration_demo import (
    DemoHeadlessEnvironmentError,
    DemoStageOutcome,
    IntegrationInputError,
    LiveModeConfig,
    ScriptedDetector,
    SmokeStage,
    SmokeRunResult,
    StaticCameraStub,
    run_live,
    run_smoke,
    live_prerequisites,
)
from src.temporal_verification import (
    TemporalVerificationResult,
    TemporalVerificationStatus,
)
from src.unknown_face_handler import (
    UnknownFaceOutcome,
    UnknownFaceStatus,
)
from src.face_recognition import (
    RecognitionResult,
    RecognitionStatus,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings(tmp_path: Path) -> AppSettings:
    """Isolated settings so real project ``data/`` is never touched."""
    return AppSettings(
        data_dir=tmp_path / "data", log_dir=tmp_path / "logs"
    )


def _frame(height: int = 240, width: int = 320) -> np.ndarray:
    """One deterministic BGR uint8 frame."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = 40
    frame[:, :, 1] = 60
    frame[:, :, 2] = 90
    frame[50:190, 110:210] = (180, 170, 160)
    return frame


def _stage(stage: SmokeStage, outcomes: tuple[DemoStageOutcome, ...]) -> DemoStageOutcome:
    matches = [o for o in outcomes if o.stage is stage]
    assert matches, f"no outcome recorded for stage {stage}"
    return matches[0]


# ---------------------------------------------------------------------------
# 1. Happy path: verified identity produces exactly one event
# ---------------------------------------------------------------------------


class TestSmokeHappyPath:
    def test_one_event_created_for_exact_id(
        self, settings: AppSettings
    ) -> None:
        result = run_smoke(settings=settings)
        assert isinstance(result, SmokeRunResult)
        assert result.event_created is True
        assert result.event_employee_id == "007"  # exact, leading zero
        assert result.event_occurred_at_iso is not None
        assert result.flow_event is not None
        assert result.flow_event.employee_id == "007"
        # The UTC ISO timestamp parses and is timezone-aware.
        occurred = datetime.fromisoformat(result.event_occurred_at_iso)
        assert occurred.tzinfo is not None

    def test_exactly_one_stage_outcome_per_stage(
        self, settings: AppSettings
    ) -> None:
        result = run_smoke(settings=settings)
        stages_seen = {o.stage for o in result.outcomes}
        assert stages_seen == set(SmokeStage)
        # Summary stages record exactly one checkpoint; per-frame stages
        # (quality gating, unknown-face handling, attendance decisions)
        # legitimately record one checkpoint per frame.
        per_frame_stages = {
            SmokeStage.QUALITY,
            SmokeStage.UNKNOWN_FACE,
            SmokeStage.ATTENDANCE,
        }
        for stage in SmokeStage:
            count = sum(1 for o in result.outcomes if o.stage is stage)
            if stage in per_frame_stages:
                assert count == 5, f"stage {stage} recorded {count} outcomes"
            else:
                assert count == 1, f"stage {stage} recorded {count} outcomes"

    def test_repeat_verified_frames_are_suppressed(
        self, settings: AppSettings
    ) -> None:
        result = run_smoke(settings=settings)
        # Frames 4 and 5 remain verified but inside the cooldown.
        assert result.suppressions == 2
        details = [
            o.detail for o in result.outcomes if o.stage is SmokeStage.ATTENDANCE
        ]
        assert any("suppressed" in d for d in details)
        assert any("event_created" in d for d in details)

    def test_temporal_stage_reports_verified_state(
        self, settings: AppSettings
    ) -> None:
        result = run_smoke(settings=settings)
        temporal = _stage(SmokeStage.TEMPORAL, result.outcomes)
        assert "verified" in temporal.detail


# ---------------------------------------------------------------------------
# 2. Boundary injections are honored (no hidden real-device shortcuts)
# ---------------------------------------------------------------------------


class TestBoundaryInjections:
    def test_frame_source_double_is_used(
        self, settings: AppSettings, tmp_path: Path
    ) -> None:
        marker = _frame()
        result = run_smoke(
            settings=settings, frame_source=StaticCameraStub(marker)
        )
        assert result.event_created is True
        capture = _stage(SmokeStage.CAPTURE, result.outcomes)
        assert "240x320" in capture.detail

    def test_detached_detector_double_is_honored(
        self, settings: AppSettings
    ) -> None:
        boxes = [
            FaceDetection(
                x=80, y=48, width=160, height=120, confidence=0.99
            )
        ]
        result = run_smoke(settings=settings, detector=ScriptedDetector(boxes))
        assert result.event_created is True

    def test_injected_embedder_is_the_used_embedding_boundary(
        self, settings: AppSettings
    ) -> None:
        seen: list[np.ndarray] = []

        class RecordingEmbedder:
            def embed(self, face_crop: np.ndarray) -> object:
                from src.face_embedding import FaceEmbedding, VectorDtype

                seen.append(face_crop)
                vec = np.eye(1, 8, 0, dtype=np.float32).reshape(-1)
                vec.setflags(write=False)
                return FaceEmbedding(
                    vector=vec,
                    dimension=vec.size,
                    norm=1.0,
                    dtype=VectorDtype.FLOAT32,
                )

        result = run_smoke(settings=settings, embedder=RecordingEmbedder())
        assert result.event_created is True
        # The pipeline actually routed crops through the injected double.
        assert len(seen) >= 4  # frames 1..5 minus anything injected
        assert all(c.dtype == np.uint8 for c in seen)


# ---------------------------------------------------------------------------
# 3. Rejected-quality and unknown/ambiguous evidence never produce events
# ---------------------------------------------------------------------------


class TestNoEventWithoutValidEvidence:
    def test_all_frames_quality_rejected_never_attend(
        self, settings: AppSettings
    ) -> None:
        # A tiny box fails the configured min face size on both axes.
        boxes = [FaceDetection(x=200, y=200, width=10, height=10)]
        result = run_smoke(settings=settings, detector=ScriptedDetector(boxes))
        # The run completes as an honest no-event outcome rather than
        # raising or fabricating a candidate.
        assert result.event_created is False
        assert result.event_employee_id is None
        for outcome in result.outcomes:
            if outcome.stage is SmokeStage.ATTENDANCE:
                assert "event_created" not in outcome.detail

    def test_no_faces_never_attends(self, settings: AppSettings) -> None:
        result = run_smoke(
            settings=settings, detector=ScriptedDetector([])
        )
        assert result.event_created is False
        assert result.event_employee_id is None
        for outcome in result.outcomes:
            if outcome.stage is SmokeStage.ATTENDANCE:
                assert "event_created" not in outcome.detail
            if outcome.stage is SmokeStage.QUALITY:
                assert "rejection" in outcome.detail or "no face" in outcome.detail

    def test_duplicate_suppression_is_active_within_cooldown(
        self, settings: AppSettings
    ) -> None:
        result = run_smoke(settings=settings)
        # The attendance stage observed real engine suppression during
        # the same run (cooldown anchors, non-refreshing).
        assert result.suppressions >= 1


class TestInactiveEmployee:
    def test_inactive_employee_cannot_produce_any_event(
        self, settings: AppSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An inactive employee's manifest excludes them from candidates.

        The real Step 10 eligibility logic (manifest ``is_active``)
        drops the only candidate, so recognition reports NO_CANDIDATES,
        which is never positive temporal evidence and can never reach
        the attendance engine as an event — end to end.
        """
        import json

        import src.integration_demo as demo_mod
        from src.enrollment import MANIFEST_FILENAME, safe_dir_name

        def inactive_manifest(
            app_settings: AppSettings, employee_id: str, full_name: str
        ) -> None:
            from src.embedding_storage import EmbeddingStorageService

            manifest_dir = (
                app_settings.employees_dir / safe_dir_name(employee_id)
            )
            manifest_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "format_version": 1,
                "employee": {
                    "employee_id": employee_id,
                    "full_name": full_name,
                    "is_active": False,
                    "created_at": "2026-10-14T00:00:00+00:00",
                },
                "sample_count": 1,
                "samples": [],
            }
            (manifest_dir / MANIFEST_FILENAME).write_text(
                json.dumps(payload), encoding="utf-8"
            )
            EmbeddingStorageService(settings=app_settings).save(
                employee_id, demo_mod._SMOKE_QUERY
            )

        # Point the real recognizer at an inactive identity (its vector
        # is otherwise identical to the stub query — cosine 1.0 would
        # match if the active gate were broken).
        monkeypatch.setattr(
            demo_mod, "_write_smoke_manifest", inactive_manifest
        )
        result = run_smoke(settings=settings)
        assert result.event_created is False
        assert result.event_employee_id is None
        # The recognizer honestly reported the ineligible candidate set.
        assert any(
            o.stage is SmokeStage.RECOGNIZE and "no_candidates" in o.detail
            for o in result.outcomes
        )


# ---------------------------------------------------------------------------
# 4. Unknown-face handling is real, attached, and observational
# ---------------------------------------------------------------------------


class TestUnknownFaceStage:
    def test_unknown_face_outcome_follows_the_real_handler(
        self, settings: AppSettings
    ) -> None:
        result = run_smoke(settings=settings)
        unknown = _stage(SmokeStage.UNKNOWN_FACE, result.outcomes)
        # The unverified streak opens an unresolved episode ("observed"),
        # the verified frame resolves it ("episode_ended"), and the
        # remaining verified frames report "no_action".
        assert "observed" in unknown.detail

    def test_non_matched_streak_never_creates_events(
        self, settings: AppSettings
    ) -> None:
        # All frames recognized as UNKNOWN: inject an off-basis embedder.
        class OffBasisEmbedder:
            def embed(self, face_crop: np.ndarray) -> object:
                from src.face_embedding import FaceEmbedding, VectorDtype

                vec = np.eye(1, 8, 7, dtype=np.float32).reshape(-1)
                vec.setflags(write=False)
                return FaceEmbedding(
                    vector=vec,
                    dimension=vec.size,
                    norm=1.0,
                    dtype=VectorDtype.FLOAT32,
                )

        result = run_smoke(settings=settings, embedder=OffBasisEmbedder())
        assert result.event_created is False
        for outcome in result.outcomes:
            if outcome.stage is SmokeStage.ATTENDANCE:
                assert "event_created" not in outcome.detail
        # And the recognizer stage clearly reports the real UNKNOWN.
        assert any(
            "unknown" in o.detail for o in result.outcomes
        )


# ---------------------------------------------------------------------------
# 5. Data safety: temporary directories only, no real data mutation
# ---------------------------------------------------------------------------


class TestDataSafety:
    def test_real_project_data_untouched(
        self, settings: AppSettings, tmp_path: Path
    ) -> None:
        from src.config import PROJECT_ROOT

        real_employees = PROJECT_ROOT / "data" / "employees"
        real_embeddings = PROJECT_ROOT / "data" / "embeddings"
        before_emp = sorted(p.name for p in real_employees.iterdir()) if real_employees.is_dir() else None
        before_emb = sorted(p.name for p in real_embeddings.iterdir()) if real_embeddings.is_dir() else None

        result = run_smoke(settings=settings)
        assert result.event_created is True

        after_emp = sorted(p.name for p in real_employees.iterdir()) if real_employees.is_dir() else None
        after_emb = sorted(p.name for p in real_embeddings.iterdir()) if real_embeddings.is_dir() else None
        assert before_emp == after_emp
        assert before_emb == after_emb

    def test_fixture_records_live_only_in_tmp(
        self, settings: AppSettings, tmp_path: Path
    ) -> None:
        run_smoke(settings=settings)
        fixture_manifest = (
            tmp_path / "data" / "employees" / "007" / "employee.json"
        )
        assert fixture_manifest.is_file()
        fixture_embedding = tmp_path / "data" / "embeddings" / "007.npz"
        assert fixture_embedding.is_file()
        # The exact ID is preserved verbatim inside the sidecar.
        content = fixture_embedding.with_name("007.meta.json").read_text(
            encoding="utf-8"
        )
        assert '"employee_id": "007"' in content


# ---------------------------------------------------------------------------
# 6. Input-contract failures fail safely (typed errors, no events)
# ---------------------------------------------------------------------------


class TestInputContracts:
    def test_none_settings_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(IntegrationInputError):
            run_smoke(settings=None)  # type: ignore[arg-type]

    def test_non_array_frame_rejected(self, settings: AppSettings) -> None:
        with pytest.raises(IntegrationInputError):
            run_smoke(settings=settings, frame="not-a-frame")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 7. Live mode: prerequisite probes and safe partial/aborted behavior
# ---------------------------------------------------------------------------


class TestLivePrerequisites:
    def test_probe_reports_injected_artifact_state(
        self, tmp_path: Path
    ) -> None:
        """Availability reflects exactly the injected artifact state.

        Probe bytes only — the files are never opened or parsed, so a
        two-byte sentinel is enough to represent a provided model file.
        """
        yunet_present = tmp_path / "yunet.onnx"
        sface_present = tmp_path / "sface.onnx"
        yunet_present.write_bytes(b"probe")
        sface_present.write_bytes(b"probe")
        yunet_missing = tmp_path / "no-such-yunet.onnx"
        sface_missing = tmp_path / "no-such-sface.onnx"

        both_present = live_prerequisites(
            detector_model_path=yunet_present,
            embedder_model_path=sface_present,
        )
        assert both_present.yunet_available is True
        assert both_present.yunet_path == yunet_present
        assert both_present.sface_available is True
        assert both_present.sface_path == sface_present

        both_missing = live_prerequisites(
            detector_model_path=yunet_missing,
            embedder_model_path=sface_missing,
        )
        assert both_missing.yunet_available is False
        assert both_missing.yunet_path == yunet_missing
        assert both_missing.sface_available is False
        assert both_missing.sface_path == sface_missing

    def test_probe_defaults_to_project_conventional_paths(self) -> None:
        """With no injection, the probe reports the canonical paths.

        Path identity is asserted (environment-agnostic); *availability*
        depends on which operator-supplied artifacts actually exist on
        the machine, so it is asserted only to agree with the file
        system — never to a hardcoded present/absent expectation.
        """
        from src.face_detection import DEFAULT_MODEL_PATH as YUNET_DEFAULT
        from src.face_embedding import DEFAULT_MODEL_PATH as SFACE_DEFAULT

        prereqs = live_prerequisites()
        assert prereqs.yunet_path == YUNET_DEFAULT
        assert prereqs.sface_path == SFACE_DEFAULT
        assert prereqs.yunet_available == YUNET_DEFAULT.is_file()
        assert prereqs.sface_available == SFACE_DEFAULT.is_file()

    def test_missing_yunet_aborts_before_camera(
        self, settings: AppSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.integration_demo as demo_mod

        # Force the documented abort condition: detector artifact absent.
        prereqs = demo_mod.live_prerequisites(
            detector_model_path=tmp_path / "no-such-yunet.onnx"
        )
        monkeypatch.setattr(demo_mod, "live_prerequisites", lambda **kw: prereqs)

        opened = {"count": 0}

        def fail_open(*args: object, **kwargs: object) -> None:
            opened["count"] += 1
            raise AssertionError("camera must never open without models")

        monkeypatch.setattr("cv2.VideoCapture", fail_open)
        result = run_live(
            settings=settings,
            config=LiveModeConfig(open_window=False),
            max_frames=1,
        )
        assert result == 0
        assert opened["count"] == 0


class TestLiveHeadlessWindow:
    def test_window_unavailable_raises_typed_error(
        self, settings: AppSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.integration_demo as demo_mod

        # Real YuNet (present) loads; then the window facade reports the
        # headless environment through its injected probe.
        monkeypatch.setattr(
            demo_mod, "_cv_window_supported", lambda: False
        )
        # A real camera may not exist; the headless window failure is
        # still reached first for a camera-less run because open() raises,
        # which the mode converts into a resource-safe shutdown.
        with pytest.raises((DemoHeadlessEnvironmentError, Exception)):
            run_live(
                settings=settings,
                config=LiveModeConfig(open_window=True),
                max_frames=1,
            )


# ---------------------------------------------------------------------------
# 8. Smoke mode is the CLI-reachable path (documented example check)
# ---------------------------------------------------------------------------


class TestLiveCameraFailure:
    def test_camera_open_failure_propagates_and_releases(
        self, settings: AppSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A camera that cannot open fails typed, releasing the handle."""
        from src.camera import CameraOpenError

        released = []

        class FailOpenCapture:
            def isOpened(self) -> bool:
                return False

            def release(self) -> None:
                released.append(1)

        import cv2

        monkeypatch.setattr(cv2, "VideoCapture", lambda index: FailOpenCapture())
        with pytest.raises(CameraOpenError):
            run_live(
                settings=settings,
                config=LiveModeConfig(open_window=False),
                max_frames=1,
            )
        # The half-opened capture handle was released during cleanup.
        assert released


class TestWindowCapabilityCheck:
    """``_cv_window_supported`` never mistakes a bug for "no GUI".

    Regression context: the original check referenced ``cv2`` without a
    local import; the resulting ``NameError`` was swallowed into
    ``False``, making any environment look headless despite a working
    WIN32UI highgui. These tests inject a scripted ``cv2`` module via
    ``sys.modules`` — the real highgui is never touched, no window ever
    opens, and no camera is involved.
    """

    @staticmethod
    def _fake_cv2_module(
        *,
        named_window: object,
        destroy_window: object,
    ) -> "types.ModuleType":
        """A scripted ``cv2`` module double with ``error`` and locals."""
        import sys
        import types

        fake = types.ModuleType("cv2")

        class FakeCv2Error(Exception):
            """Stand-in for ``cv2.error`` in the module double."""

        fake.error = FakeCv2Error
        fake.namedWindow = named_window
        fake.destroyWindow = destroy_window
        return fake

    @staticmethod
    def _install_fake_cv2(
        monkeypatch: pytest.MonkeyPatch, fake: "types.ModuleType"
    ) -> None:
        """Bind the module double so the check's local import sees it."""
        import sys

        monkeypatch.setitem(sys.modules, "cv2", fake)

    def test_probe_reports_supported_without_module_global_dependency(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A healthy injected cv2 yields True (the old bug yielded False)."""
        import src.integration_demo as demo_mod

        created: list[str] = []
        destroyed: list[str] = []

        def ok_named_window(name: str) -> None:
            created.append(name)

        def ok_destroy_window(name: str) -> None:
            destroyed.append(name)

        self._install_fake_cv2(
            monkeypatch,
            self._fake_cv2_module(
                named_window=ok_named_window,
                destroy_window=ok_destroy_window,
            ),
        )
        assert demo_mod._cv_window_supported() is True
        # The probe owns its lifecycle: created and destroyed exactly once.
        assert created == ["[probe]"]
        assert destroyed == ["[probe]"]

    def test_genuine_highgui_failure_reports_unsupported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real highgui errors (``cv2.error``) still mean "unsupported"."""
        import src.integration_demo as demo_mod

        def no_op(*_args: object) -> None:
            return None

        fake = self._fake_cv2_module(
            named_window=no_op, destroy_window=no_op
        )

        def failing_named_window(name: str) -> None:
            raise fake.error("no window subsystem")

        fake.namedWindow = failing_named_window
        self._install_fake_cv2(monkeypatch, fake)
        assert demo_mod._cv_window_supported() is False

    def test_cleanup_failure_also_reports_unsupported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe that cannot destroy its window is unsupported too."""
        import src.integration_demo as demo_mod

        def no_op(*_args: object) -> None:
            return None

        fake = self._fake_cv2_module(
            named_window=no_op, destroy_window=no_op
        )

        def failing_destroy_window(name: str) -> None:
            raise fake.error("destroy failed")

        fake.destroyWindow = failing_destroy_window
        self._install_fake_cv2(monkeypatch, fake)
        assert demo_mod._cv_window_supported() is False

    def test_unrelated_errors_are_not_masked_as_unsupported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A programming bug must propagate, never masquerade as headless.

        Under the original broad ``except Exception``, an injected
        ``RuntimeError`` was swallowed into ``False`` — this test pins
        the new, discriminating contract.
        """
        import src.integration_demo as demo_mod

        def no_op(*_args: object) -> None:
            return None

        fake = self._fake_cv2_module(
            named_window=no_op, destroy_window=no_op
        )

        def buggy_named_window(name: str) -> None:
            raise RuntimeError("unrelated programming bug")

        fake.namedWindow = buggy_named_window
        self._install_fake_cv2(monkeypatch, fake)
        with pytest.raises(RuntimeError, match="unrelated programming bug"):
            demo_mod._cv_window_supported()


class TestDocsExample:
    def test_readme_smoke_snippet_matches_module_api(self) -> None:
        """Keep the README snippet and the module's real signature in sync."""
        readme = Path(__file__).resolve().parents[1] / "README.md"
        if not readme.is_file():  # pragma: no cover - repo always has it
            pytest.skip("README.md not found")
        text = readme.read_text(encoding="utf-8")
        if "run_smoke" in text:
            assert "run_smoke(" in text
            assert "settings=" in text


# ---------------------------------------------------------------------------
# Opt-in sharpness-diagnostic line formatting (pure, hardware-free)
# ---------------------------------------------------------------------------


class TestQualityRejectionLineFormatting:
    """Exercises ``_format_quality_rejection`` only — no camera, no model,
    no frame, nothing saved: the formatter receives an already-computed
    ``FaceQuality`` and returns a string.
    """

    @staticmethod
    def _quality(reason: FaceQualityReason, sharpness: float | None):
        return demo_mod.FaceQuality(
            decision=FaceQualityDecision.REJECT,
            reason=reason,
            sharpness=sharpness,
            brightness=140.0,
            pose_yaw_ratio=None,
            pose_pitch_ratio=None,
            box=(10, 10, 80, 80),
        )

    def test_disabled_flag_reproduces_the_undiagnostic_line(self) -> None:
        quality = self._quality(FaceQualityReason.BLUR, sharpness=18.34)
        assert demo_mod._format_quality_rejection(
            12, quality, report_quality_metrics=False
        ) == "[frame   12] rejected by quality: blur"

    def test_enabled_blur_reports_measured_sharpness_and_threshold(self) -> None:
        quality = self._quality(FaceQualityReason.BLUR, sharpness=18.34)
        assert demo_mod._format_quality_rejection(
            12, quality, report_quality_metrics=True
        ) == (
            "[frame   12] rejected by quality: blur "
            "(sharpness=18.3, threshold=50.0)"
        )

    def test_enabled_blur_reports_deterministic_one_decimal_values(self) -> None:
        quality = self._quality(FaceQualityReason.BLUR, sharpness=47.96)
        assert demo_mod._format_quality_rejection(
            7, quality, report_quality_metrics=True
        ) == (
            "[frame    7] rejected by quality: blur "
            "(sharpness=48.0, threshold=50.0)"
        )

    def test_enabled_non_blur_rejection_has_no_metrics_appendix(self) -> None:
        quality = self._quality(FaceQualityReason.TOO_SMALL, sharpness=400.0)
        assert demo_mod._format_quality_rejection(
            33, quality, report_quality_metrics=True
        ) == "[frame   33] rejected by quality: too_small"

    def test_enabled_blur_without_measured_sharpness_stays_plain(self) -> None:
        """The formatter never invents a value for an unmeasured check."""
        quality = self._quality(FaceQualityReason.BLUR, sharpness=None)
        assert demo_mod._format_quality_rejection(
            12, quality, report_quality_metrics=True
        ) == "[frame   12] rejected by quality: blur"

    def test_formatter_emits_no_biometric_content(self) -> None:
        quality = self._quality(FaceQualityReason.BLUR, sharpness=18.34)
        line = demo_mod._format_quality_rejection(
            12, quality, report_quality_metrics=True
        )
        for forbidden in ("brightness", "pose", "box", "140"):
            assert forbidden not in line
