"""Focused CLI tests for ``tools/enroll_employee.py`` (Step 17).

Covers the eight operator-flow categories with fully injected doubles:
no camera is opened, no model is loaded, and no biometric data is ever
created or persisted. The real enrollment *validation* path is still
exercised (via the real ``EnrollmentService`` with a recording camera
factory) because invalid identity must be rejected before any capture.

The tool module is loaded from its file path (same ``importlib``
pattern as ``tests/test_run_integration_demo_cli.py``) because
``tools/`` scripts are not importable packages.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable

import pytest

from src.employee import Employee
from src.embedding_storage import (
    DuplicateEmbeddingRecordError,
    EmbeddingStorageError,
)
from src.enrollment import (
    DuplicateEnrollmentError,
    EnrollmentResult,
    EnrollmentService,
    EnrollmentStatus,
    SampleRejection,
)
from src.quality import FaceQualityReason
from src.enrollment_bridge import EnrollmentBridgeResult, embed_published_samples

_TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "enroll_employee.py"

spec = importlib.util.spec_from_file_location("enroll_employee", _TOOL_PATH)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

ID = "007"
NAME = "CLI Test Employee"


class _FakePrereqs:
    """Probe-shaped double naming fake artifact paths (no files touched)."""

    yunet_available = True
    sface_available = True
    yunet_path = Path("fake-yunet.onnx")
    sface_path = Path("fake-sface.onnx")


_PROBE_OK = lambda **_kw: _FakePrereqs()  # noqa: E731 - injected prerequisite probe


def _completed_result() -> EnrollmentResult:
    return EnrollmentResult(
        status=EnrollmentStatus.COMPLETED,
        employee=Employee(employee_id=ID, full_name=NAME),
        samples=(),
        rejections=(),
        attempted_frames=5,
        duplicates_skipped=1,
        error=None,
    )


def _failed_result(error: str) -> EnrollmentResult:
    return EnrollmentResult(
        status=EnrollmentStatus.FAILED,
        employee=Employee(employee_id=ID, full_name=NAME),
        samples=(),
        rejections=(),
        attempted_frames=20,
        duplicates_skipped=0,
        error=error,
    )


def _bridge_result() -> EnrollmentBridgeResult:
    return EnrollmentBridgeResult(employee_id=ID, dimension=8, samples_used=3)


class _FakeStorage:
    """Duck-typed storage double reporting a fixed discoverable ID list."""

    def __init__(self, ids: tuple[str, ...]) -> None:
        self._ids = ids

    def list_employee_ids(self) -> tuple[str, ...]:
        return self._ids


class _Doubles:
    """Recording factories: every stage appends to ``calls`` in order."""

    def __init__(
        self,
        *,
        enroll_result: EnrollmentResult | None = None,
        enroll_error: Exception | None = None,
        bridge_error: Exception | None = None,
        bridge_result: EnrollmentBridgeResult | None = None,
        discoverable_ids: tuple[str, ...] = (ID,),
    ) -> None:
        self.calls: list[str] = []
        calls = self.calls  # closure-visible recorder (inner 'self' is the fake)

        def enrollment_factory() -> Any:
            calls.append("enrollment_factory")

            class _FakeEnrollmentService:
                def enroll(
                    self, _employee_id: str, _full_name: str, **_kw: Any
                ) -> EnrollmentResult:
                    calls.append("enroll")
                    if enroll_error is not None:
                        raise enroll_error
                    return enroll_result if enroll_result else _completed_result()

            return _FakeEnrollmentService()

        def embedder_factory() -> Any:
            calls.append("embedder_factory")
            return object()  # injected fakes never run real embedding

        def storage_factory() -> Any:
            calls.append("storage_factory")
            return _FakeStorage(discoverable_ids)

        def bridge(employee_id: str, **_kw: Any) -> EnrollmentBridgeResult:
            calls.append(f"bridge:{employee_id}")
            if bridge_error is not None:
                raise bridge_error
            return bridge_result if bridge_result else _bridge_result()

        self.enrollment_factory: Callable[[], Any] = enrollment_factory
        self.embedder_factory: Callable[[], Any] = embedder_factory
        self.storage_factory: Callable[[], Any] = storage_factory
        self.bridge: Callable[..., EnrollmentBridgeResult] = bridge


def _run(doubles: _Doubles, *, consent: bool = True) -> list[str]:
    """Run the flow with full instrumentation; return captured output."""
    out: list[str] = []
    cli.run_employee_enrollment(
        ID,
        NAME,
        confirm_consent=consent,
        enrollment_factory=doubles.enrollment_factory,
        embedder_factory=doubles.embedder_factory,
        storage_factory=doubles.storage_factory,
        bridge=doubles.bridge,
        out=out.append,
    )
    return out


# ---------------------------------------------------------------------------
# 1. Consent declined — nothing at all runs (exit 2)
# ---------------------------------------------------------------------------


class TestConsentDeclined:
    def test_no_factory_no_bridge_no_prereq_probe(self, monkeypatch) -> None:
        probed: list[str] = []
        monkeypatch.setattr(
            cli, "live_prerequisites", lambda **_kw: probed.append("probe")
        )
        doubles = _Doubles()
        out = _run(doubles, consent=False)
        assert doubles.calls == []  # no enrollment/embedder/storage/bridge
        assert probed == []  # not even a prerequisite probe ran
        assert any("Nothing was run" in line for line in out)


# ---------------------------------------------------------------------------
# 2. Consent accepted — enrollment then bridge, in order (exit 0)
# ---------------------------------------------------------------------------


class TestConsentAcceptedOrder:
    def test_enroll_then_bridge_in_order_success(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        doubles = _Doubles()
        out = _run(doubles)
        assert doubles.calls == [
            "enrollment_factory",
            "enroll",
            "embedder_factory",
            "storage_factory",
            f"bridge:{ID}",
        ]
        assert any("recognition candidate" in line for line in out)


# ---------------------------------------------------------------------------
# 3. Invalid employee ID / name — real validation, camera never touched
# ---------------------------------------------------------------------------


class TestInvalidIdentityRejectedBeforeCapture:
    @pytest.mark.parametrize(
        ("bad_id", "bad_name"),
        [
            ("", NAME),
            ("   ", NAME),
            ("../escape", NAME),
            (ID, ""),
            (ID, "   "),
        ],
    )
    def test_real_validation_rejects_before_any_camera_work(
        self, monkeypatch, bad_id: str, bad_name: str
    ) -> None:
        camera_touched: list[str] = []

        def recording_camera_factory() -> Any:
            camera_touched.append("acquired")
            raise AssertionError("camera must never be acquired for bad input")

        real_service = EnrollmentService(camera_factory=recording_camera_factory)

        def enrollment_factory() -> Any:
            return real_service

        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        out: list[str] = []
        exit_code = cli.run_employee_enrollment(
            bad_id,
            bad_name,
            confirm_consent=True,
            enrollment_factory=enrollment_factory,
            embedder_factory=lambda: object(),  # pragma: no cover - must not run
            storage_factory=lambda: _FakeStorage((ID,)),
            bridge=lambda *a, **k: _tripwire(),
            out=out.append,
        )
        assert exit_code == 1
        assert camera_touched == []  # the camera factory was never called


def _tripwire() -> EnrollmentBridgeResult:
    raise AssertionError("bridge must never run for this scenario")


# ---------------------------------------------------------------------------
# 4. Duplicates refused — nothing overwritten
# ---------------------------------------------------------------------------


class TestDuplicatesRefused:
    def test_duplicate_enrollment_refused_bridge_never_called(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        doubles = _Doubles(
            enroll_error=DuplicateEnrollmentError(
                "employee already has enrollment data"
            )
        )
        doubles.bridge = lambda *_a, **_k: _tripwire()  # roadblock, never expected
        out = _run(doubles)
        assert doubles.calls == ["enrollment_factory", "enroll"]
        assert "refused" in "".join(out)

    def test_existing_embedding_refused_after_completed_enrollment(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        doubles = _Doubles(
            bridge_error=DuplicateEmbeddingRecordError("record already exists")
        )
        out = _run(doubles)
        assert "bridge:007" in doubles.calls
        assert doubles.calls[-1] == f"bridge:{ID}"
        assert "already exists" in "".join(out)


# ---------------------------------------------------------------------------
# 5. Capture/enrollment failure — no embedding work, no success
# ---------------------------------------------------------------------------


def _failed_result_with_rejections() -> EnrollmentResult:
    """Failed enrollment carrying several structured rejection reasons."""
    return EnrollmentResult(
        status=EnrollmentStatus.FAILED,
        employee=Employee(employee_id=ID, full_name=NAME),
        samples=(),
        rejections=(
            SampleRejection(
                frame_index=1, reason=FaceQualityReason.NO_FACES, quality=None
            ),
            SampleRejection(
                frame_index=2, reason=FaceQualityReason.NO_FACES, quality=None
            ),
            SampleRejection(
                frame_index=3, reason=FaceQualityReason.BLUR, quality=None
            ),
            SampleRejection(
                frame_index=4, reason=FaceQualityReason.TOO_SMALL, quality=None
            ),
        ),
        attempted_frames=20,
        duplicates_skipped=0,
        error="capture attempt budget exhausted: 20",
    )


class TestEnrollmentFailure:
    def test_failed_capture_reports_and_never_bridges(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        doubles = _Doubles(
            enroll_result=_failed_result("capture attempt budget exhausted: 20")
        )
        out = _run(doubles)
        assert "enroll" in doubles.calls
        assert all(not c.startswith("bridge:") for c in doubles.calls)
        assert any("did not complete" in line for line in out)


# ---------------------------------------------------------------------------
# 6. Embedding generation / storage failure — exit 1, recovery guidance
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5b. Failure output reports rejection counts by reason (diagnostic tally)
# ---------------------------------------------------------------------------


class TestFailureRejectionTally:
    def test_multiple_reasons_are_counted_and_sorted(self, monkeypatch) -> None:
        """Counts deterministic and reason names lexicographically sorted."""
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        doubles = _Doubles(enroll_result=_failed_result_with_rejections())
        out = _run(doubles)
        joined = "".join(out)
        assert (
            "rejections by reason: blur=1, no_faces=2, too_small=1" in joined
        )
        assert "did not complete" in joined  # the existing line is unchanged
        assert "Nothing was published." in joined

    def test_tally_is_deterministic_across_runs(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        first = _run(_Doubles(enroll_result=_failed_result_with_rejections()))
        second = _run(_Doubles(enroll_result=_failed_result_with_rejections()))
        assert first == second

    def test_empty_rejection_list_adds_no_tally(self, monkeypatch) -> None:
        """No rejections → output identical to the pre-tally failure line."""
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        doubles = _Doubles(
            enroll_result=_failed_result("capture attempt budget exhausted: 20")
        )
        out = _run(doubles)
        joined = "".join(out)
        assert "rejections by reason" not in joined
        assert "did not complete" in joined

    def test_tally_prints_names_and_counts_only(self, monkeypatch) -> None:
        """Biometric discipline: no metrics, boxes, crops, or frame indices."""
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        doubles = _Doubles(enroll_result=_failed_result_with_rejections())
        out = _run(doubles)
        joined = "".join(out)
        for forbidden in ("sharpness", "brightness", "quality=", "box="):
            assert forbidden not in joined
        # Frame indices of the individual rejections are never printed.
        assert "frame_index" not in joined


class TestEmbeddingFailure:
    def test_storage_failure_reports_recovery_note(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        doubles = _Doubles(bridge_error=EmbeddingStorageError("persistence failed"))
        out = _run(doubles)
        assert f"bridge:{ID}" in doubles.calls
        assert "published enrollment artifacts" in "".join(out)

    def test_unexpected_bridge_error_fails_closed(self, monkeypatch) -> None:
        """A non-typed bridge surprise is a reported failure, never success."""
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        doubles = _Doubles(bridge_error=RuntimeError("embedding model exploded"))
        out = _run(doubles)
        assert f"bridge:{ID}" in doubles.calls  # bridge was reached and failed
        assert "published enrollment artifacts" in "".join(out)  # recovery note
        assert "success" not in "".join(out)


# ---------------------------------------------------------------------------
# 7. Final candidate verification failure — no success message
# ---------------------------------------------------------------------------


class TestFinalVerificationFailure:
    def test_missing_id_in_discoverable_list_fails_loudly(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        doubles = _Doubles(discoverable_ids=())
        out = _run(doubles)
        assert f"bridge:{ID}" in doubles.calls  # bridge ran...
        assert "final candidate verification failed" in "".join(out)  # ...loudly
        assert "success" not in "".join(out)


# ---------------------------------------------------------------------------
# 8. Success requires discoverable ID in list_employee_ids()
# ---------------------------------------------------------------------------


class TestSuccess:
    def test_success_only_when_id_discoverable(self, monkeypatch) -> None:
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        doubles = _Doubles(discoverable_ids=(ID,))
        out = _run(doubles)
        assert f"bridge:{ID}" in doubles.calls  # the bridge ran
        assert "recognition candidate" in "".join(out)  # verified + printed

    def test_runner_wraps_unexpected_exceptions_as_failure(self, monkeypatch) -> None:
        """cli.main's top-level guard turns surprises into a nonzero exit."""

        def exploding_flow(**_kw: Any) -> int:
            raise RuntimeError("boom")

        monkeypatch.setattr(cli, "run_employee_enrollment", exploding_flow)
        exit_code = cli.main(
            ["--employee-id", ID, "--full-name", NAME, "--confirm-consent"]
        )
        assert exit_code == 1


# ---------------------------------------------------------------------------
# 9. Parser surface — consent flag exists, missing args exit 2
# ---------------------------------------------------------------------------


class TestParserSurface:
    def test_consent_defaults_to_false(self) -> None:
        args = cli._build_parser().parse_args(
            ["--employee-id", ID, "--full-name", NAME]
        )
        assert args.confirm_consent is False

    def test_both_flags_required(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli._build_parser().parse_args(["--employee-id", ID])
        assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# 10. Frame-size option — explicit camera resolution override (no weakening)
# ---------------------------------------------------------------------------


class TestFrameSizeParserSurface:
    def test_frame_size_defaults_to_none(self) -> None:
        args = cli._build_parser().parse_args(
            ["--employee-id", ID, "--full-name", NAME, "--confirm-consent"]
        )
        assert args.frame_size is None

    def test_reuses_the_demo_framesize_parser(self) -> None:
        from tools.run_integration_demo import parse_frame_size as demo_parser

        assert cli._build_parser()._option_string_actions[
            "--frame-size"
        ].type is demo_parser

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("640x480", (640, 480)),
            ("640X480", (640, 480)),
            (" 640x480 ", (640, 480)),
            ("640 x 480", (640, 480)),
            ("1280x720", (1280, 720)),
        ],
    )
    def test_accepted_forms(self, raw: str, expected: tuple[int, int]) -> None:
        args = cli._build_parser().parse_args(
            ["--employee-id", ID, "--full-name", NAME, "--frame-size", raw]
        )
        assert args.frame_size == expected

    @pytest.mark.parametrize(
        "raw",
        ["0x480", "640x0", "-640x480", "640", "640x480x1", "abc", "640,80x480"],
    )
    def test_malformed_or_nonpositive_values_exit_2(self, raw: str) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli._build_parser().parse_args(
                ["--employee-id", ID, "--full-name", NAME, "--frame-size", raw]
            )
        assert excinfo.value.code == 2


class TestFrameSizeCameraConfig:
    def test_overrides_only_size_index_stays_configured(self) -> None:
        from src.config import CAMERA

        camera_config = cli._frame_size_camera_config((640, 480))
        assert (camera_config.frame_width, camera_config.frame_height) == (640, 480)
        assert camera_config.index == CAMERA.index

    def test_project_default_constant_is_never_mutated_or_changed(self) -> None:
        from src.config import CAMERA

        cli._frame_size_camera_config((640, 480))
        assert (CAMERA.frame_width, CAMERA.frame_height) == (1280, 720)

    def test_zero_dimensions_are_rejected_by_cameraconfig(self) -> None:
        with pytest.raises(ValueError):
            cli._frame_size_camera_config((0, 480))


class TestFrameSizeFlowWiring:
    def test_main_forwards_frame_size_to_the_flow(self, monkeypatch) -> None:
        captured: dict[str, Any] = {}

        def recording_flow(*args: Any, **kwargs: Any) -> int:
            captured["frame_size"] = kwargs.get("frame_size")
            captured["args"] = args
            return 0

        monkeypatch.setattr(cli, "run_employee_enrollment", recording_flow)
        exit_code = cli.main(
            [
                "--employee-id", ID,
                "--full-name", NAME,
                "--confirm-consent",
                "--frame-size", "640x480",
            ]
        )
        assert exit_code == 0
        assert captured["frame_size"] == (640, 480)
        assert captured["args"] == (ID, NAME)

    def test_flow_without_override_builds_default_service_with_none(
        self, monkeypatch
    ) -> None:
        """No --frame-size → the unchanged pre-existing default path."""
        captured: dict[str, Any] = {}
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)

        def recording_builder(*, frame_size: Any = "missing") -> Any:
            captured["frame_size"] = frame_size
            return _Doubles().enrollment_factory()

        monkeypatch.setattr(cli, "_make_real_enrollment_service", recording_builder)
        doubles = _Doubles()
        out: list[str] = []
        exit_code = cli.run_employee_enrollment(
            ID,
            NAME,
            confirm_consent=True,
            embedder_factory=doubles.embedder_factory,
            storage_factory=doubles.storage_factory,
            bridge=doubles.bridge,
            out=out.append,
        )
        assert exit_code == 0
        assert captured["frame_size"] is None

    def test_flow_forwards_override_to_the_real_default_builder(
        self, monkeypatch
    ) -> None:
        captured: dict[str, Any] = {}
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)

        def recording_builder(*, frame_size: tuple[int, int] | None) -> Any:
            captured["frame_size"] = frame_size
            return _Doubles().enrollment_factory()

        monkeypatch.setattr(cli, "_make_real_enrollment_service", recording_builder)
        doubles = _Doubles()
        out: list[str] = []
        exit_code = cli.run_employee_enrollment(
            ID,
            NAME,
            confirm_consent=True,
            frame_size=(640, 480),
            embedder_factory=doubles.embedder_factory,
            storage_factory=doubles.storage_factory,
            bridge=doubles.bridge,
            out=out.append,
        )
        assert exit_code == 0
        assert captured["frame_size"] == (640, 480)
        assert any("recognition candidate" in line for line in out)

    def test_overridden_run_still_refuses_duplicates(self, monkeypatch) -> None:
        """The override changes nothing about duplicate/data discipline."""
        monkeypatch.setattr(cli, "live_prerequisites", _PROBE_OK)
        monkeypatch.setattr(
            cli,
            "_make_real_enrollment_service",
            lambda *, frame_size=None: doubles.enrollment_factory(),
        )
        doubles = _Doubles(
            enroll_error=DuplicateEnrollmentError("employee already has data")
        )
        doubles.bridge = lambda *_a, **_k: _tripwire()
        out: list[str] = []
        exit_code = cli.run_employee_enrollment(
            ID,
            NAME,
            confirm_consent=True,
            frame_size=(640, 480),
            embedder_factory=doubles.embedder_factory,
            storage_factory=doubles.storage_factory,
            bridge=doubles.bridge,
            out=out.append,
        )
        assert exit_code == 1
        assert "refused" in "".join(out)

    def test_demo_framesize_parser_is_the_shared_object(self) -> None:
        """The enrollment CLI parses frame sizes identically to the demo."""
        from tools.run_integration_demo import parse_frame_size as demo_parser

        assert cli.parse_frame_size is demo_parser
