"""Step 15 CLI: run the Phase 1 integration demo.

Two explicitly separated modes:

* ``--smoke`` (default) — deterministic, hardware-free, side-effect
  free. Uses temporary directories; the only real components are the
  actual storage/recognition/temporal/unknown-face/attendance services
  with unchanged contracts. Never claims real-model accuracy.
* ``--live`` — opt-in real-camera run. Requires the operator-supplied
  ``models/yunet.onnx``; with ``models/sface.onnx`` absent it runs as a clearly-labeled *partial*
  demo (capture → detection → quality only) and never generates an
  attendance event, because identity authorization is impossible
  without the real recognition chain. Nothing is downloaded,
  enrolled, or fabricated automatically.
* ``--frame-size WIDTHxHEIGHT`` (``--live`` only) — optional camera
  frame-size override (e.g. ``640x480``; the ``x`` separator is case-
  insensitive). Omitted → the project's configured default resolution
  is used unchanged. Dimensions must be positive.

Examples::

    python tools/run_integration_demo.py --smoke
    python tools/run_integration_demo.py --live --camera-index 0
    python tools/run_integration_demo.py --live --camera-index 0 --frame-size 640x480
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

# Make "src" importable when invoked as a script from the repo root
# (mirrors pytest.ini's pythonpath = . convention).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import AppSettings  # noqa: E402
from src.integration_demo import (  # noqa: E402
    LiveModeConfig,
    live_prerequisites,
    run_live,
    run_smoke,
)


def parse_frame_size(raw: str) -> tuple[int, int]:
    """Parse a ``WIDTHxHEIGHT`` camera frame-size CLI value.

    The ``x`` separator is accepted case-insensitively (``640x480`` and
    ``640X480``); no internal whitespace is required to be absent, but
    the separator itself must appear exactly once and both dimensions
    must be positive integers. Any malformed, zero, or negative value
    raises :class:`argparse.ArgumentTypeError`, which argparse converts
    into its standard CLI error (exit code 2 with usage text).
    """
    lowered = raw.strip().lower()
    parts = lowered.split("x")
    error_message = (
        "frame-size must be WIDTHxHEIGHT with positive integer "
        f"dimensions (e.g. 640x480), got {raw!r}"
    )
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(error_message)
    try:
        width, height = int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        raise argparse.ArgumentTypeError(error_message) from None
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError(error_message)
    return width, height


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_integration_demo",
        description=(
            "Phase 1 Step 15 integration demo: connects the existing "
            "components (capture → detect → quality → embed → recognize "
            "→ temporal verify → unknown-face handling → attendance) "
            "without adding any new pipeline logic."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "Run the deterministic, hardware-free smoke demo (default). "
            "Uses temporary directories and injected component doubles; "
            "no camera and no real model inference."
        ),
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help=(
            "Opt-in live-camera mode using the real camera and models. "
            "Explicitly opt-in; never runs by default."
        ),
    )
    parser.add_argument(
        "--camera-index",
        type=int,
        default=0,
        help="Camera device index for --live (default: 0).",
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="Run --live headless (no OpenCV preview window).",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=300,
        help=(
            "Frame budget for --live before it stops automatically "
            "(default: 300; uses this to bound unattended runs)."
        ),
    )
    parser.add_argument(
        "--frame-size",
        type=parse_frame_size,
        default=None,
        metavar="WIDTHxHEIGHT",
        help=(
            "Optional camera frame-size override for --live (e.g. "
            "640x480; the x separator is case-insensitive; positive "
            "dimensions required). Omitted: the project's configured "
            "default resolution is used unchanged."
        ),
    )
    return parser


def _run_smoke_mode() -> int:
    """Run the smoke demo in isolated temporary directories (exit 0)."""
    with tempfile.TemporaryDirectory(prefix="attendance-smoke-") as tmp:
        settings = AppSettings(
            data_dir=Path(tmp) / "data", log_dir=Path(tmp) / "logs"
        )
        result = run_smoke(settings=settings)
        print("=== Phase 1 integration demo (smoke mode) ===")
        if result.event_created:
            print(f"attendance event created for employee '{result.event_employee_id}'")
            print(f"event timestamp (UTC): {result.event_occurred_at_iso}")
            print(f"repeat verified frames suppressed: {result.suppressions}")
        else:
            print("no attendance event was created (no valid verified evidence)")
        print("pipeline checkpoints:")
        for outcome in result.outcomes:
            print(f"  [{outcome.stage.value:>12s}] f{outcome.frame_index}: {outcome.detail}")
        print(
            "note: smoke mode uses synthetic vectors; it demonstrates "
            "pipeline correctness, never real-model recognition accuracy."
        )
    return 0


def _run_live_mode(args: argparse.Namespace) -> int:
    """Run the opt-in live demo (exit 0 normally, 1 on failure)."""
    prereqs = live_prerequisites()
    if not prereqs.yunet_available:
        print(
            f"[cli] live demo cannot start: {prereqs.yunet_path} is missing. "
            "Provide the YuNet model manually (never auto-downloaded), "
            "then retry. Use --smoke meanwhile."
        )
        return 1
    if not prereqs.sface_available:
        print(
            "[cli] note: models/sface.onnx is missing — the live run will "
            "be a partial demo (capture → detect → quality only) and will "
            "not generate attendance events."
        )
    config: LiveModeConfig
    if args.frame_size is None:
        config = LiveModeConfig(
            camera_index=args.camera_index,
            open_window=not args.no_window,
        )
    else:
        config = LiveModeConfig(
            camera_index=args.camera_index,
            frame_width=args.frame_size[0],
            frame_height=args.frame_size[1],
            open_window=not args.no_window,
        )
    try:
        frames = run_live(
            settings=AppSettings(),
            config=config,
            max_frames=args.max_frames,
        )
    except Exception as exc:  # noqa: BLE001 - a failed demo is a nonzero exit
        print(f"[cli] live demo failed: {exc}")
        return 1
    print(f"[cli] live demo finished; frames processed: {frames}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.live:
        return _run_live_mode(args)
    return _run_smoke_mode()


if __name__ == "__main__":
    raise SystemExit(main())
