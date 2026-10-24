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
* ``--report-quality-metrics`` (``--live``) — explicit opt-in
  console-only diagnostic: frames rejected specifically for blur
  additionally report the already-measured sharpness value and the
  configured threshold (measured numbers only; nothing recalculated,
  saved to files, or appended to logs). Disabled by default; with the
  flag absent the output and behavior are exactly the pre-diagnostic
  ones.
* ``--durable`` (both modes) — Phase 2 P2-5 opt-in durable attendance
  journal: cooldown-eligible check-in events the real engine creates
  are composed through the P2-2 ``DurableAttendanceService`` and
  appended (``fsync`` per record) to the P2-1 journal, and the P2-3
  cooldown recovery is replayed from the journal *before* any event
  is processed. Default is **off**: without the flag both modes keep
  their exact pre-P2-5 behavior (journal untouched — smoke uses a
  temporary directory, live never opens ``data/attendance`` outside
  the durable path). Smoke mode's journal lives inside that
  temporary directory (removed with it); live mode journals to the
  configured ``data/attendance`` directory. Recovery or journal-load
  failures stop durable mode with a nonzero exit — never silently
  degraded processing.

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
    parser.add_argument(
        "--durable",
        action="store_true",
        help=(
            "Opt-in durable attendance journal (Phase 2 P2-5), supported "
            "for both modes: created check-in events are persisted with "
            "per-record fsync and previously journaled cooldown anchors "
            "are recovered before any event is processed. Default: off "
            "(no journal is loaded or written; existing behavior is "
            "unchanged)."
        ),
    )
    parser.add_argument(
        "--report-quality-metrics",
        action="store_true",
        help=(
            "Opt-in console-only diagnostic for --live: frames rejected "
            "specifically for blur also report the already-measured "
            "sharpness value and the configured threshold (numbers "
            "only; nothing saved to files or logs). Disabled by default."
        ),
    )
    return parser


def _run_smoke_mode(durable: bool = False) -> int:
    """Run the smoke demo in isolated temporary directories (exit 0).

    With ``durable=True`` (the ``--durable`` opt-in) the smoke run also
    journals its events through the Phase 2 persistence composition;
    a clearly-labeled durable summary is printed after the regular
    smoke output. Default-off invocations keep the exact pre-P2-5
    behavior and output.
    """
    with tempfile.TemporaryDirectory(prefix="attendance-smoke-") as tmp:
        settings = AppSettings(
            data_dir=Path(tmp) / "data", log_dir=Path(tmp) / "logs"
        )
        if durable:
            # Fail closed, cleanly: a journal-load or recovery error
            # stops durable mode before any event is processed —
            # reported as a nonzero exit, never degraded silently.
            from src.attendance_store import AttendanceStoreError

            try:
                result = run_smoke(settings=settings, durable=True)
            except AttendanceStoreError as exc:
                print(
                    f"[cli] durable smoke mode stopped before any event "
                    f"processing: {exc}"
                )
                return 1
        else:
            result = run_smoke(settings=settings, durable=False)
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
        if durable:
            counts: dict[str, int] = {}
            for status in result.durable_outcomes:
                counts[status] = counts.get(status, 0) + 1
            print("durable mode summary (opt-in attendance journal):")
            print(
                "  cooldown anchors recovered from journal: "
                f"{result.durable_recovered_anchors}"
            )
            print(
                "  persistence outcomes: "
                + " ".join(
                    f"{name}={counts.get(name, 0)}"
                    for name in (
                        "persisted",
                        "persistence_rejected",
                        "persistence_unconfirmed",
                        "no_event",
                    )
                )
            )
            print(
                "  journal location: the smoke temporary directory "
                "(removed with it on exit)"
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
    # Config assembly: only operator-chosen overrides reach the config,
    # so the flag-off CLI keeps the exact pre-diagnostic construction.
    config_kwargs: dict[str, object] = {
        "camera_index": args.camera_index,
        "open_window": not args.no_window,
    }
    if args.frame_size is not None:
        config_kwargs["frame_width"] = args.frame_size[0]
        config_kwargs["frame_height"] = args.frame_size[1]
    if args.report_quality_metrics:
        config_kwargs["report_quality_metrics"] = True
    if args.durable:
        config_kwargs["durable"] = True
    config = LiveModeConfig(**config_kwargs)
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
    return _run_smoke_mode(durable=args.durable)


if __name__ == "__main__":
    raise SystemExit(main())
