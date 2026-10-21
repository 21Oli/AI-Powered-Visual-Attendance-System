"""Focused CLI tests for ``tools/run_integration_demo.py`` (Step 15).

Covers only the CLI boundary — argument parsing, frame-size parsing and
validation, configuration propagation into :class:`LiveModeConfig`, and
preservation of the documented default behavior — plus the live mode's
frame-size propagation with fully injected (hardware-free, model-free)
doubles. No camera is opened and no real model is loaded by any test
here; the automated suite must never touch physical devices.

The tool module is loaded from its file path (same ``importlib`` pattern
as ``tests/test_yunet_model_download.py``) because ``tools/`` scripts are
not importable packages.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

import pytest

from src.config import AppSettings

_TOOL_PATH = (
    Path(__file__).resolve().parent.parent / "tools" / "run_integration_demo.py"
)


def _load_cli_module():
    spec = importlib.util.spec_from_file_location(
        "run_integration_demo", _TOOL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli_module()


# ---------------------------------------------------------------------------
# Shared fixture: patch the CLI's own prerequisite probe (never any hardware)
# ---------------------------------------------------------------------------


def _force_full_prereqs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the CLI's guard believe both models exist (no files touched).

    ``run_live`` itself is replaced by callers of this helper, so no
    camera open, model load, or frame handling ever runs.
    """
    import src.integration_demo as demo_mod

    prereqs_full = demo_mod._LivePrereq(
        yunet_available=True,
        yunet_path=Path("fake-yunet.onnx"),
        sface_available=True,
        sface_path=Path("fake-sface.onnx"),
    )
    monkeypatch.setattr(
        cli, "live_prerequisites", lambda **_kw: prereqs_full
    )


# ---------------------------------------------------------------------------
# 1. parse_frame_size — accepted forms
# ---------------------------------------------------------------------------


class TestParseFrameSizeAccepted:
    def test_lowercase_separator(self) -> None:
        assert cli.parse_frame_size("640x480") == (640, 480)

    def test_uppercase_separator(self) -> None:
        assert cli.parse_frame_size("640X480") == (640, 480)

    def test_project_default_dimensions(self) -> None:
        assert cli.parse_frame_size("1280x720") == (1280, 720)

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        assert cli.parse_frame_size("  640x480 ") == (640, 480)

    def test_inner_whitespace_around_dimensions_is_tolerated(self) -> None:
        assert cli.parse_frame_size("640 x 480") == (640, 480)


# ---------------------------------------------------------------------------
# 2. parse_frame_size — rejected forms (typed, clear errors)
# ---------------------------------------------------------------------------


class TestParseFrameSizeRejected:
    @pytest.mark.parametrize(
        ("raw", "why"),
        [
            ("", "empty value"),
            ("abc", "no separator"),
            ("640", "only one dimension"),
            ("640x", "a zero-width dimension"),
            ("x480", "a zero-height dimension"),
            ("640x480x", "two separators"),
            ("0x480", "a zero width"),
            ("-640x480", "a negative width"),
            ("640x0", "a zero height"),
            ("640x-480", "a negative height"),
            ("640.0x480", "a non-integer width"),
            ("640,480", "a wrong separator character"),
            ("64 0x480", "an embedded space inside a dimension"),
        ],
    )
    def test_malformed_zero_and_negative_values_fail_typed(
        self, raw: str, why: str
    ) -> None:
        with pytest.raises(argparse.ArgumentTypeError, match="WIDTHxHEIGHT"):
            cli.parse_frame_size(raw)

    def test_error_message_names_the_received_value(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError) as excinfo:
            cli.parse_frame_size("-40x480")
        assert "-40x480" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. Parser surface: default flags unchanged, type callable applied
# ---------------------------------------------------------------------------


class TestParserSurface:
    def test_frame_size_defaults_to_none(self) -> None:
        args = cli._build_parser().parse_args([])
        assert args.frame_size is None
        assert args.live is False  # smoke remains the default mode
        assert args.camera_index == 0
        assert args.no_window is False

    def test_frame_size_is_parsed_through_the_type_callable(self) -> None:
        args = cli._build_parser().parse_args(
            ["--live", "--frame-size", "640x480"]
        )
        assert args.frame_size == (640, 480)

    def test_frame_size_is_alone_parseable_next_to_smoke(self) -> None:
        # --frame-size only takes effect in live mode, so it must parse
        # together with --smoke (it is simply ignored there).
        args = cli._build_parser().parse_args(
            ["--smoke", "--frame-size", "640x480"]
        )
        assert args.frame_size == (640, 480)

    def test_report_quality_metrics_defaults_to_disabled(self) -> None:
        args = cli._build_parser().parse_args([])
        assert args.report_quality_metrics is False

    def test_report_quality_metrics_is_an_explicit_opt_in(self) -> None:
        args = cli._build_parser().parse_args(
            ["--live", "--report-quality-metrics"]
        )
        assert args.report_quality_metrics is True


# ---------------------------------------------------------------------------
# 4. Default behavior is preserved exactly when --frame-size is omitted
# ---------------------------------------------------------------------------


class TestDefaultBehaviorPreserved:
    def test_missing_yunet_exits_before_config_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI's own guard exits 1 before any config is built."""
        import src.integration_demo as demo_mod

        prereqs_missing = demo_mod.live_prerequisites(
            detector_model_path=Path("no-such-yunet-probe.onnx"),
            embedder_model_path=Path("no-such-sface-probe.onnx"),
        )
        monkeypatch.setattr(
            cli, "live_prerequisites", lambda **_kw: prereqs_missing
        )

        def never_called(**_kw: Any) -> Any:  # pragma: no cover
            raise AssertionError("run_live must not be reached without models")

        monkeypatch.setattr(cli, "run_live", never_called)

        exit_code = cli._run_live_mode(cli._build_parser().parse_args([]))
        assert exit_code == 1

    def test_omitted_frame_size_builds_the_exact_pre_change_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No --frame-size → ``run_live`` receives the unchanged config.

        The CLI boundary is tested end to end here: the recorded config
        is the one actually handed to ``run_live``.
        """
        migrated = cli.LiveModeConfig

        class RecordingLiveModeConfig(migrated):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)

        constructed: dict[str, Any] = {}
        handed_over: list[Any] = []

        real_init = migrated.__init__

        def recording_init(self: Any, **kwargs: Any) -> None:
            constructed.update(kwargs)
            real_init(self, **kwargs)

        monkeypatch.setattr(migrated, "__init__", recording_init)
        _force_full_prereqs(monkeypatch)

        def recording_run_live(
            *, settings: Any, config: Any, max_frames: Any
        ) -> int:
            handed_over.append(config)
            return 0

        monkeypatch.setattr(cli, "run_live", recording_run_live)

        exit_code = cli._run_live_mode(cli._build_parser().parse_args([]))
        assert exit_code == 0
        # Exact pre-change configuration surface: nothing new injected.
        assert constructed == {"camera_index": 0, "open_window": True}
        assert len(handed_over) == 1
        assert isinstance(handed_over[0], migrated)


# ---------------------------------------------------------------------------
# 5. Override propagation: CLI value → LiveModeConfig → camera config
# ---------------------------------------------------------------------------


class TestFrameSizePropagation:
    def test_cli_override_reaches_live_mode_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--frame-size 640x480`` sets exactly the two frame dims.

        ``run_live`` is replaced by a recorder at the CLI boundary, so
        the test asserts both the constructed kwargs and that the built
        config object is what the CLI actually hands over.
        """
        migrated = cli.LiveModeConfig

        constructed: dict[str, Any] = {}
        real_init = migrated.__init__

        def recording_init(self: Any, **kwargs: Any) -> None:
            constructed.update(kwargs)
            real_init(self, **kwargs)

        monkeypatch.setattr(migrated, "__init__", recording_init)
        _force_full_prereqs(monkeypatch)

        handed_over: list[Any] = []

        def recording_run_live(
            *, settings: Any, config: Any, max_frames: Any
        ) -> int:
            handed_over.append(config)
            return 0

        monkeypatch.setattr(cli, "run_live", recording_run_live)

        exit_code = cli._run_live_mode(
            cli._build_parser().parse_args(
                ["--live", "--frame-size", "640x480", "--camera-index", "2"]
            )
        )
        assert exit_code == 0
        assert constructed == {
            "camera_index": 2,
            "frame_width": 640,
            "frame_height": 480,
            "open_window": True,
        }
        assert len(handed_over) == 1
        assert handed_over[0].frame_width == 640
        assert handed_over[0].frame_height == 480
        assert handed_over[0].camera_index == 2

    def test_enabled_flag_reaches_live_mode_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--report-quality-metrics`` sets exactly one new config field."""
        migrated = cli.LiveModeConfig

        constructed: dict[str, Any] = {}
        real_init = migrated.__init__

        def recording_init(self: Any, **kwargs: Any) -> None:
            constructed.update(kwargs)
            real_init(self, **kwargs)

        monkeypatch.setattr(migrated, "__init__", recording_init)
        _force_full_prereqs(monkeypatch)

        handed_over: list[Any] = []

        def recording_run_live(
            *, settings: Any, config: Any, max_frames: Any
        ) -> int:
            handed_over.append(config)
            return 0

        monkeypatch.setattr(cli, "run_live", recording_run_live)

        exit_code = cli._run_live_mode(
            cli._build_parser().parse_args(
                ["--live", "--frame-size", "640x480", "--report-quality-metrics"]
            )
        )
        assert exit_code == 0
        assert constructed == {
            "camera_index": 0,
            "frame_width": 640,
            "frame_height": 480,
            "open_window": True,
            "report_quality_metrics": True,
        }
        assert handed_over[0].report_quality_metrics is True

    def test_absent_flag_builds_the_exact_undiagnostic_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the flag, the config carries no diagnostic keyword."""
        migrated = cli.LiveModeConfig

        constructed: dict[str, Any] = {}
        real_init = migrated.__init__

        def recording_init(self: Any, **kwargs: Any) -> None:
            constructed.update(kwargs)
            real_init(self, **kwargs)

        monkeypatch.setattr(migrated, "__init__", recording_init)
        _force_full_prereqs(monkeypatch)

        handed_over: list[Any] = []

        def recording_run_live(
            *, settings: Any, config: Any, max_frames: Any
        ) -> int:
            handed_over.append(config)
            return 0

        monkeypatch.setattr(cli, "run_live", recording_run_live)

        exit_code = cli._run_live_mode(
            cli._build_parser().parse_args(["--live"])
        )
        assert exit_code == 0
        assert constructed == {"camera_index": 0, "open_window": True}
        assert handed_over[0].report_quality_metrics is False  # class default

    def test_override_flows_into_the_real_camera_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_live builds a CameraConfig honoring the override.

        Fully injected: the prerequisite probe, the detector, and the
        camera service are doubles, so neither a model file nor a
        physical device is required. The stopping first read makes the
        loop terminate frame-free; resource cleanup must still run.
        """
        import src.camera as camera_mod
        from src.config import CameraConfig
        from src.integration_demo import LiveModeConfig, run_live
        import src.integration_demo as demo_mod

        events: list[Any] = []

        class FakeDetector:
            def load(self) -> None:
                pass

            def detect(self, frame: Any) -> list[object]:
                return []  # "no faces" — the real frame-policy path

        from src.config import CameraConfig as _CC

        class FakeCamera:
            def __init__(self, config: _CC) -> None:
                events.append(config)

            @property
            def is_open(self) -> bool:
                return True

            def open(self) -> None:
                pass

            def read_frame(self) -> tuple[bool, None]:
                return (False, None)  # stops the loop cleanly, frame-free

            def release(self) -> None:
                events.append("released")

        prereqs_full = demo_mod._LivePrereq(
            yunet_available=True,
            yunet_path=Path("fake-yunet.onnx"),
            sface_available=False,  # partial demo: no embedder is built
            sface_path=Path("fake-sface.onnx"),
        )
        monkeypatch.setattr(
            demo_mod, "live_prerequisites", lambda **_kw: prereqs_full
        )
        monkeypatch.setattr(demo_mod, "FaceDetector", FakeDetector)
        monkeypatch.setattr(camera_mod, "CameraService", FakeCamera)

        frames_processed = run_live(
            settings=AppSettings(),
            config=LiveModeConfig(
                camera_index=0,
                frame_width=640,
                frame_height=480,
                open_window=False,
            ),
            max_frames=1,
        )
        assert frames_processed == 0  # the fake camera reports a first-pass stop

        configs = [e for e in events if isinstance(e, CameraConfig)]
        assert len(configs) == 1
        (camera_config,) = configs
        assert camera_config.index == 0
        assert camera_config.frame_width == 640
        assert camera_config.frame_height == 480
        assert "released" in events  # resource cleanup still ran


# ---------------------------------------------------------------------------
# 6. CLI-level rejection surfaces as a formal argparse error (exit 2)
# ---------------------------------------------------------------------------


class TestCliErrorSurface:
    @pytest.mark.parametrize(
        "raw", ["abc", "640", "640x", "640x480x", "0x480", "640x-480"]
    )
    def test_invalid_value_is_a_formal_parser_error(self, raw: str) -> None:
        """argparse turns the typed error into `error:` text + exit 2."""
        parser = cli._build_parser()
        with pytest.raises(SystemExit) as excinfo, redirect_stderr(
            io.StringIO()
        ):
            parser.parse_args(["--live", "--frame-size", raw])
        assert excinfo.value.code == 2
