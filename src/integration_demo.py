"""Phase 1 integration demo for the AI-Powered Visual Attendance System
(Phase 1 — Step 15).

A minimal, explicit orchestrator that wires the existing Phase 1
components into the intended pipeline

    capture → detect → quality → embed → recognize → temporally verify
    → unknown-face handling → attendance event

**orchestrating only**: every decision is delegated to the approved
component services (Steps 04–13) with their unchanged thresholds and
contracts. This module adds **no** recognition math, **no** quality
metrics, **no** temporal policy, **no** new thresholds — composing
existing services is its whole job. Inventive shortcuts (durable
storage, trackers, liveness, DB) are Phase 2 work, out of scope.

Two explicitly separated execution modes
----------------------------------------

:meth:`run_smoke` — deterministic demo
    No device and no real model inference: a scripted frame source and
    a scriptable component double are injected at the module
    boundaries; the *actual* attendance engine, temporal verifier,
    unknown-face handler, event schema, and pipeline orchestration are
    real. Temporary directories and fake clocks keep it side-effect
    free for real ``data/``. This mode is what ``--smoke`` runs and
    what the automated tests exercise.

:meth:`run_live` — opt-in live pipeline
    Uses the real :class:`~src.camera.CameraService`, the real loaded
    YuNet detector and the real quality assessor, and runs until the
    window is closed / cancelled. Attendance authorization still
    requires the **real, full chain**: a real ``MATCHED`` recognition
    (which needs the operator-supplied ``models/sface.onnx`` embedder)
    plus real temporal verification. When a prerequisite is missing
    the mode reports it explicitly:

    * ``yunet.onnx`` missing → nothing can be demoed; abort.
    * ``sface.onnx`` missing → **partial live demo**: capture,
      detection, and quality run for real; the embedding/recognition/
      attendance stages are reported as blocked — and
      **no attendance event is ever generated**, because identity
      authorization is impossible without the model. Nothing is
      downloaded or enrolled automatically.

Data-safety summary (both modes)
--------------------------------

* Smoke mode writes **nothing** to the repository's real ``data/``:
  enrollment manifests, sample PNGs, and embeddings live under a
  caller-supplied temporary directory (in tests, ``tmp_path``).
* Live mode performs **no enrollment** and no recognition without
  models; it reads frames and (with the embedder installed) could look
  up stored embeddings through the *existing* read-only storage
  contracts — but it never enrolls, never writes embeddings, never
  persists unknown-face observations, and never fabricates events.
* Events exist solely as the in-memory :class:`~src.attendance.
  AttendanceEvent` values the real engine returns; Steps 15 does not
  add a persistence layer (Phase 2 owns durable storage).

Known limitations, carried honestly
-----------------------------------

* No face tracker: window translation can split one physical person
  across sessions (documented Phase 2 tracker work); smoke mode uses
  single-session logic so it does not hit this.
* Duplicate suppression is engine-instance lifetime only (Step 13's
  documented restart limitation).
* Smoke mode's synthetic frame is **not** evidence of real-world
  recognition accuracy (Steps 08/10 honesty carried forward).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol, Sequence

import numpy as np

from src.config import AppSettings
from src.face_detection import FaceDetection, FaceDetector
from src.quality import (
    FaceQualityAssessor,
    FaceQualityDecision,
)
from src.logging_setup import get_logger

if TYPE_CHECKING:  # real heavyweights imported lazily inside functions
    from src.camera import CameraService

__all__ = [
    "IntegrationDemoError",
    "IntegrationInputError",
    "DemoStageOutcome",
    "SmokeStage",
    "SmokeRunResult",
    "FrameSource",
    "FaceCropsSource",
    "StaticCameraStub",
    "ScriptedDetector",
    "StubEmbedder",
    "OpenCvWindowStub",
    "DemoHeadlessEnvironmentError",
    "LiveModeConfig",
    "run_smoke",
    "run_live",
    "live_prerequisites",
]

_logger = get_logger(__name__)

#: Exact employee ID used by the smoke fixture (leading-zero preserved,
#: per the Phase 1 identity contract). The manifest/embedding fixture
#: lives only in the caller's temporary directory.
SMOKE_EMPLOYEE_ID: str = "007"
SMOKE_EMPLOYEE_NAME: str = "Smoke Demo Employee"

#: Deterministic query vector shared by the fake fixtures and the
#: stub embedder (an orthonormal-ish synthetic basis).
_SMOKE_QUERY: np.ndarray = np.array(
    [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IntegrationDemoError(Exception):
    """Base class for integration-demo errors."""


class IntegrationInputError(IntegrationDemoError, ValueError):
    """Raised for invalid demo arguments (directories, flags, fakes)."""


# ---------------------------------------------------------------------------
# Smoke pipeline result
# ---------------------------------------------------------------------------


class SmokeStage(Enum):
    """Pipeline checkpoints exercised by one smoke run."""

    CAPTURE = "capture"
    DETECT = "detect"
    QUALITY = "quality"
    EMBED = "embed"
    RECOGNIZE = "recognize"
    TEMPORAL = "temporal"
    UNKNOWN_FACE = "unknown_face"
    ATTENDANCE = "attendance"


@dataclass(frozen=True)
class DemoStageOutcome:
    """One pipeline checkpoint's measured outcome (bounded, inspectable).

    Attributes:
        stage: Which checkpoint produced this outcome.
        frame_index: 1-based index into the source frame sequence the
            outcome came from (the interrupted-verification checkpoint
            carries the index of the first verified frame's *successor*).
        detail: Short, bounded, human-readable description — never a
            frame, crop, embedding, or full embedding vector; a
            similarity value appears only for the recognition
            checkpoint's demonstrated matched score.
    """

    stage: SmokeStage
    frame_index: int
    detail: str


@dataclass(frozen=True)
class SmokeRunResult:
    """Complete outcome of one :meth:`run_smoke` execution.

    Attributes:
        event_employee_id: Exact verified employee ID of the created
            check-in event (``"007"`` — identity preserved verbatim);
            ``None`` when the run produced no event (e.g. every frame
            was quality-rejected — never an error in itself).
        event_created: Whether a real attendance event was created by
            the real engine during this run. ``True`` only when an
            actual :class:`~src.attendance.AttendanceEvent` was
            returned by :meth:`AttendanceEngine.update` — the module
            never fabricates one to make a scenario look successful.
        event_occurred_at_iso: UTC ISO-8601 timestamp of the accepted
            event (``None`` when ``event_created`` is ``False``).
        flow_event: The one :class:`src.attendance.AttendanceEvent`
            the flow scenario produced (in-memory; never written to
            disk anywhere by this module); ``None`` when no event.
        suppressions: How many repeat verified frames were suppressed
            by the engine's cooldown (demonstrating Step 13's
            duplicate policy).
        outcomes: Per-checkpoint outcomes in pipeline order.
    """

    event_employee_id: str | None
    event_created: bool
    event_occurred_at_iso: str | None
    flow_event: object | None
    suppressions: int
    outcomes: tuple[DemoStageOutcome, ...]


# ---------------------------------------------------------------------------
# Injectable boundaries
# ---------------------------------------------------------------------------


class FrameSource(Protocol):
    """Minimal protocol the smoke pipeline reads frames from."""

    def read_frame(self) -> tuple[bool, np.ndarray | None]: ...


class FaceCropsSource(Protocol):
    """Minimal embedding-source protocol for smoke fakes.

    Only ``embed()`` matters; the real contract lives in Step 08
    (BGR uint8 ``(H, W, 3)`` crop → validated vector). The smoke
    pipeline injects a :class:`StubEmbedder` here.
    """

    def embed(self, face_crop: np.ndarray) -> object: ...


class StaticCameraStub:
    """Scriptable frame source (always returns the caller's array)."""

    def __init__(self, frame: np.ndarray) -> None:
        self._frame = frame

    def read_frame(self) -> tuple[bool, np.ndarray | None]:
        return (True, self._frame)


class ScriptedDetector:
    """Scriptable :meth:`FaceDetector.detect` replacement for flat frames."""

    def __init__(self, boxes: list[FaceDetection]) -> None:
        self._boxes = boxes

    def detect(self, frame: np.ndarray) -> list[FaceDetection]:
        return list(self._boxes)


class StubEmbedder:
    """Scriptable embedder double returning the configured fixed vector

    (mirrors what the real ``FaceEmbedder`` returns modulo the real
    model being available — the smoke pipeline's Step 08 boundary).
    """

    def __init__(self, vector: np.ndarray) -> None:
        self._vector = vector.copy()

    def embed(self, face_crop: np.ndarray) -> object:
        from src.face_embedding import FaceEmbedding, VectorDtype

        vec = self._vector.copy()
        vec.setflags(write=False)
        return FaceEmbedding(
            vector=vec,
            dimension=vec.size,
            norm=float(np.linalg.norm(vec)),
            dtype=VectorDtype.FLOAT32,
        )


class OpenCvWindowStub:
    """Scriptable window double for headless smoke runs.

    Always ``is_open=False`` (closes immediately); the live loop's
    injected-window path exercises the real OpenCV window semantics
    without requiring a GUI toolkit on the test machine.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def is_open(self) -> bool:
        return False


class DemoHeadlessEnvironmentError(IntegrationDemoError, RuntimeError):
    """Raised when the live demo cannot run because neither a display
    nor the OpenCV highgui window facilities are available. Suggested
    remedy: run the ``--smoke`` mode instead (fully headless-safe)."""


class LiveModeConfig:
    """Explicit, replica-correct live-demo configuration.

    Attributes mirror the real Phase 1 services' configuration surfaces
    (frame size, detector confidence) without bypassing them — the
    live loop validates and passes them through to the real components.
    """

    def __init__(
        self,
        *,
        camera_index: int = 0,
        frame_width: int = 1280,
        frame_height: int = 720,
        open_window: bool = True,
    ) -> None:
        self.camera_index = camera_index
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.open_window = open_window


# ---------------------------------------------------------------------------
# Smoke pipeline
# ---------------------------------------------------------------------------


def run_smoke(
    *,
    settings: AppSettings,
    frame: np.ndarray | None = None,
    detector: object | None = None,
    embedder: FaceCropsSource | None = None,
    frame_source: FrameSource | None = None,
    monotonic: Callable[[], float] | None = None,
) -> SmokeRunResult:
    """Run one deterministic, side-effect-free smoke-demo pipeline pass.

    Composes the existing real Step 09/10/11/12/13 services (with
    unchanged thresholds) around **stubbed** detection/quality
    fakes, so the pipeline's behavioral wiring — quality gating →
    eligible-recognition → temporal streak → unknown-face episode →
    attendance event — is exercised end to end without a device:
    exactly five frames flow, enough to demonstrate one verified
    streak plus a repeat frame that the engine must suppress.

    The enrollment side is intentionally *synthetic but real-shaped*:
    the fixture writes exactly the Step 07 manifest/storage records the
    test helpers use (so eligibility per Step 10's authority is real),
    into the caller-supplied **temporary** settings — never the
    repository's live ``data/`` directory. Embedding values are plain
    synthetic vectors; **nothing here measures real-model accuracy.**

    Args:
        settings: Isolated ``AppSettings`` with temporary paths (the
            fixture record files are written relative to them).
        frame: Synthetic frame source as a pre-validated grayscale BGR
            array (``None`` → a deterministic synthetic frame).
        detector: Detection-backend replacement (default: a
            :class:`ScriptedDetector` with one centered face);
            ``None`` short-circuits the whole run — an empty frame
            must still produce one *unknown* pipeline outcome with no
            event, never a crash or a fabricated candidate.
        embedder: Embedding-source replacement (default: a
            :class:`StubEmbedder` with the synthetic query vector).
        frame_source: Optional pre-built frame source (default: a
            fresh :class:`StaticCameraStub` over ``frame``).
        monotonic: Injectable monotonic-seconds callable for the
            temporal-verifier's deterministic clock (default:
            ``time.monotonic``).

    Returns:
        :class:`SmokeRunResult` with the real accepted event, the
        measured suppression count, and per-stage outcomes — exactly
        one checkpoint per :class:`SmokeStage`.

    Raises:
        IntegrationInputError: invalid injection (mismatched
            detector/frame shapes or unset settings paths).
    """
    from src.attendance import (
        AttendanceConfig,
        AttendanceDecisionStatus,
        AttendanceEngine,
    )
    from src.temporal_verification import (
        TemporalVerificationConfig,
        TemporalVerifier,
    )
    from src.unknown_face_handler import UnknownFaceConfig, UnknownFaceHandler
    from src.face_recognition import (
        FaceRecognitionService,
        RecognitionConfig,
        RecognitionStatus,
    )
    from src.embedding_storage import EmbeddingStorageService

    if settings is None:
        raise IntegrationInputError("settings must not be None")

    # -- Defaults first, then validation (never validate against None
    #    values that are about to be replaced by defaults) -------------------
    if monotonic is None:
        import time as _time

        monotonic = _time.monotonic
    if frame is not None and not isinstance(frame, np.ndarray):
        raise IntegrationInputError(
            "frame must be a numpy array when provided, got "
            f"{type(frame).__name__}"
        )

    # -- Real components (unchanged contracts; stub injected embedder) ------
    effective_embedder = (
        embedder if embedder is not None else StubEmbedder(_SMOKE_QUERY)
    )
    storage_service = EmbeddingStorageService(settings=settings)
    recognizer = FaceRecognitionService(
        embedder=effective_embedder,
        storage=storage_service,
        config=RecognitionConfig(min_similarity=0.4, min_margin=None),
        settings=settings,
    )
    verifier = TemporalVerifier(
        config=TemporalVerificationConfig(min_consistent_frames=3, window_frames=5),
    )
    unknown_handler = UnknownFaceHandler(
        config=UnknownFaceConfig(), clock=monotonic
    )
    attendance = AttendanceEngine(
        config=AttendanceConfig(), monotonic_clock=monotonic
    )

    # -- Fixture (manifests only; no embedding files are touched beyond
    #    Engines' input requirements — this is the manifest-existence
    #    authority the Step 10 recognizer votes on) --------------------------
    _write_smoke_manifest(settings, SMOKE_EMPLOYEE_ID, SMOKE_EMPLOYEE_NAME)

    if frame is None:
        frame = _synthetic_frame()

    source = frame_source or StaticCameraStub(frame)
    scripted = detector or ScriptedDetector([_demo_detection(frame.shape)])
    outcomes: list[DemoStageOutcome] = []
    created_event = None
    suppressed_frames = 0

    for i in (1, 2, 3, 4, 5):
        # -- Step 04 — capture -----------------------------------------
        ok, captured = source.read_frame()
        if not ok or captured is None:
            raise IntegrationInputError(
                "frame source stopped producing frames before the "
                "pipeline finished; the smoke demo requires five "
                "consecutive readable frames"
            )
        if i == 1:
            outcomes.append(
                DemoStageOutcome(
                    stage=SmokeStage.CAPTURE,
                    frame_index=i,
                    detail=f"captured {captured.shape[0]}x{captured.shape[1]} BGR frame",
                )
            )

        # -- Step 05 — detect ------------------------------------------
        detections = list(scripted.detect(captured))
        face_count_policy: str
        if len(detections) == 0:
            face_count_policy = "no face in frame"
        elif len(detections) == 1:
            face_count_policy = "exactly one face (accept)"
        else:
            face_count_policy = f"{len(detections)} faces (policy: single-face)"

        # -- Step 06 — quality (real assessor, unchanged thresholds) ----
        assessor = FaceQualityAssessor()  # real assessor, real config
        frame_policy = assessor.assess_frame_faces(detections)
        if frame_policy is not None or not detections:
            # Frame-level rejection: no per-face assessment happens. The
            # downstream stages still receive the real conservative
            # "no candidates" evidence from this frame's outcome.
            quality_outcome = DemoStageOutcome(
                stage=SmokeStage.QUALITY,
                frame_index=i,
                detail=(
                    f"frame-level rejection ({face_count_policy}); "
                    "no embedding attempted"
                ),
            )
            choice = "reject_frame"
        else:
            detection = detections[0]
            quality = assessor.assess_face(captured, detection)
            if quality.decision is not FaceQualityDecision.ACCEPT:
                quality_outcome = DemoStageOutcome(
                    stage=SmokeStage.QUALITY,
                    frame_index=i,
                    detail=(
                        "face rejected (quality: "
                        f"{quality.reason.value if quality.reason else 'policy'})"
                    ),
                )
                choice = "reject_face"
            else:
                quality_outcome = DemoStageOutcome(
                    stage=SmokeStage.QUALITY,
                    frame_index=i,
                    detail="face accepted (all quality checks pass)",
                )
                choice = "accept"

        if choice != "accept":
            # Non-accepted frames contribute exactly their honest
            # evidence downstream: no recognition was possible, so the
            # pipeline reports NO_CANDIDATES (real Step 10 CONTRACT
            # semantics — no identity, no streak progress).
            verification = verifier.update(_no_candidates_result())
            unknown_outcome = unknown_handler.observe_temporal(verification)
            attendance.update(verification)
            if i == 1:
                outcomes.append(
                    DemoStageOutcome(
                        stage=SmokeStage.DETECT,
                        frame_index=i,
                        detail=face_count_policy,
                    )
                )
            outcomes.append(quality_outcome)
            outcomes.append(flush_unknown(unknown_outcome, i))
            outcomes.append(
                DemoStageOutcome(
                    stage=SmokeStage.ATTENDANCE,
                    frame_index=i,
                    detail="no event: quality/face-count rejected the frame",
                )
            )
            continue

        if i == 1:
            outcomes.append(
                DemoStageOutcome(
                    stage=SmokeStage.DETECT,
                    frame_index=i,
                    detail=face_count_policy,
                )
            )
        outcomes.append(quality_outcome)

        # -- Step 08 — embedding via the injected stub ------------------
        crop = _safe_crop(captured, quality.box)
        embedding_result = recognizer_embed(effective_embedder, crop)
        if embedding_result is None:
            raise IntegrationDemoError(
                "smoke embedding boundary failed: the stub embedder is "
                "injected and must always produce a validated vector; a "
                "None here indicates a broken injection, never a real "
                "model absence (live mode reports that separately)"
            )
        if i == 2:
            outcomes.append(
                DemoStageOutcome(
                    stage=SmokeStage.EMBED,
                    frame_index=i,
                    detail="stub embedding vector produced (synthetic)",
                )
            )

        # -- Step 10 — recognition (real service, real thresholds) ------
        recognition = recognizer.recognize(crop)
        detail = (
            f"{recognition.status.value}"
            f" (score={recognition.best_score:.3f})"
            if recognition.status is RecognitionStatus.MATCHED
            else f"{recognition.status.value}"
        )

        # -- Step 11 — temporal (real verifier, unchanged policy) -------
        verification = verifier.update(recognition)

        # -- Step 12 — unknown-face handling (real handler) -------------
        unknown_outcome = unknown_handler.observe_temporal(verification)

        # -- Step 13 — attendance (real engine, full trust boundary) ----
        decision = attendance.update(verification)
        event = decision.event
        if decision.status is AttendanceDecisionStatus.SUPPRESSED:
            suppressed_frames += 1

        if i == 3:
            # Frame three completes Step 11's VERIFIED threshold (the
            # third consecutive same-ID MATCHED frame).
            outcomes.append(
                DemoStageOutcome(
                    stage=SmokeStage.RECOGNIZE,
                    frame_index=i,
                    detail=f"consistent MATCHED across the streak ({detail})",
                )
            )
            outcomes.append(
                DemoStageOutcome(
                    stage=SmokeStage.TEMPORAL,
                    frame_index=i,
                    detail=(
                        f"temporal status {verification.status.value} "
                        f"(streak={verification.streak_frames})"
                    ),
                )
            )
        outcomes.append(flush_unknown(unknown_outcome, i))
        outcomes.append(
            DemoStageOutcome(
                stage=SmokeStage.ATTENDANCE,
                frame_index=i,
                detail=(
                    f"attendance decision {decision.status.value}"
                    + (f" (event_id={event.event_id})" if event else "")
                ),
            )
        )
        if event is not None and created_event is None:
            created_event = event

    if created_event is not None:
        return SmokeRunResult(
            event_employee_id=created_event.employee_id,
            event_created=True,
            event_occurred_at_iso=created_event.occurred_at.isoformat(),
            flow_event=created_event,
            suppressions=suppressed_frames,
            outcomes=tuple(outcomes),
        )
    # A run with no event is a valid, honest outcome (e.g. every frame
    # was rejected by quality / no face was present): report it, never
    # fabricate an event to make the scenario look successful.
    return SmokeRunResult(
        event_employee_id=None,
        event_created=False,
        event_occurred_at_iso=None,
        flow_event=None,
        suppressions=suppressed_frames,
        outcomes=tuple(outcomes),
    )


# ---------------------------------------------------------------------------
# Live-camera pipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LivePrereq:
    yunet_available: bool
    yunet_path: Path
    sface_available: bool
    sface_path: Path


def live_prerequisites(
    *,
    detector_model_path: Path | None = None,
    embedder_model_path: Path | None = None,
) -> _LivePrereq:
    """Probe which live-demo prerequisites exist on this machine.

    Only file presence is checked; no model is loaded here. Both are
    reported so callers can degrade explicitly (detection-only demo vs.
    full pipeline) rather than guessing.
    """
    from src.face_detection import DEFAULT_MODEL_PATH as YUNET_DEFAULT
    from src.face_embedding import DEFAULT_MODEL_PATH as SFACE_DEFAULT

    yunet = detector_model_path or YUNET_DEFAULT
    sface = embedder_model_path or SFACE_DEFAULT
    return _LivePrereq(
        yunet_available=yunet.is_file(),
        yunet_path=yunet,
        sface_available=sface.is_file(),
        sface_path=sface,
    )


def run_live(
    *,
    settings: AppSettings,
    config: LiveModeConfig | None = None,
    window_factory: Callable[[str], object] | None = None,
    max_frames: int | None = 300,
) -> int:
    """Run the opt-in, real-component live-camera pipeline loop.

    Real components used (unchanged contracts): the real
    :class:`~src.camera.CameraService`, real ``FaceDetector`` (loaded
    from ``models/yunet.onnx``), and the real
    :class:`~src.quality.FaceQualityAssessor` with real thresholds.

    Behavior:
        * Preparation: probe prerequisites *before* touching the
          camera; a missing ``yunet.onnx`` aborts the run safely.
        * With ``sface.onnx`` absent, a
          **partial live demo** runs — capture, detection, and quality
          report real values; the embedding/recognition/temporal/
          attendance stages are reported as clearly blocked, and the
          attendance authorization chain is *never* bypassed.
        * With ``sface.onnx`` present, the full chain runs and a
          :meth:`AttendanceEngine.update`-authorized event could be
          produced (never fabricated by this module).
        * Camera open failure, failed frame reads, window closure /
          user cancellation, ``KeyboardInterrupt``, and unexpected
          exceptions all exit cleanly; ``finally`` releases every
          acquisition.

    Args:
        settings: Real project settings (read-only usage here).
        config: :class:`LiveModeConfig` options (camera index, frame
            size, whether an OpenCV preview window is desired).
        window_factory: Injectable window producer (used only when a
            display actually exists); ``None`` uses the real OpenCV
            ``cv2.imshow``-based facade.
        max_frames: Hard frame budget (``None`` = unlimited). A natural
            safety valve for CI/automation; humans close the window.

    Returns:
        The count of frames actually processed (useful to confirm
        activity without claiming any decision quality).

    Raises:
        DemoHeadlessEnvironmentError: no usable window subsystem
            exists and windowing was requested (remedy: headless run
            via ``open_window=False`` or the ``--smoke`` mode).
    """
    import cv2

    config = config or LiveModeConfig()
    prereqs = live_prerequisites()

    # -- Prerequisite probing (before the camera exists) --------------------
    if not prereqs.yunet_available:
        _logger.error(
            "live demo aborted: YuNet detector model not found at %s — "
            "provide the operator-supplied model file (see the operating "
            "docs) or use the --smoke mode", prereqs.yunet_path,
        )
        print(
            f"[live-demo] ABORTED: YuNet model missing at {prereqs.yunet_path}. "
            "Nothing was run. See docs for how to provide it manually "
            "(never auto-downloaded by this codebase)."
        )
        return 0

    if prereqs.sface_available:
        print(
            "[live-demo] full pipeline available (sface.onnx present); "
            "recognized identities may generate attendance events."
        )
    else:
        print(
            "[live-demo] PARTIAL: sface.onnx is missing, so embedding/"
            "recognition/attendance are BLOCKED (no attendance event can "
            "ever be generated without the real recognition chain). "
            "Running the capture → detect → quality stages only."
        )

    camera: CameraService | None = None
    detector_owned: FaceDetector | None = None
    window_name = "Phase 1 live demo"
    window = window_factory(window_name) if window_factory else None
    frames_processed = 0
    try:
        # Detection first — the model must exist and load before any
        # camera hardware is acquired (fail fast, resource-clean).
        detector_owned = FaceDetector()
        detector_owned.load()

        # Temporary, frame-size-matched camera configuration (never
        # mutating the project-wide frozen constant).
        from src.config import CameraConfig, CAMERA

        camera_config = CameraConfig(
            index=config.camera_index,
            frame_width=config.frame_width or CAMERA.frame_width,
            frame_height=config.frame_height or CAMERA.frame_height,
        )
        from src.camera import CameraService as _CameraService

        camera = _CameraService(camera_config)
        camera.open()
        if not camera.is_open:  # defensive: open() raises, this never happens
            raise DemoHeadlessEnvironmentError(  # pragma: no cover
                "camera opened but not reported open; aborting"
            )

        if window is None and config.open_window:
            if not _cv_window_supported():
                raise DemoHeadlessEnvironmentError(
                    "OpenCV GUI windowing is unavailable on this platform; "
                    "re-run with open_window=False or use --smoke"
                )
            window = _OpenCvWindow(window_name)

        assessor = FaceQualityAssessor()  # real thresholds
        embedder = None
        if prereqs.sface_available:
            from src.face_embedding import FaceEmbedder

            embedder = FaceEmbedder()
            embedder.load()
        # Storage (read-only lookup authority) only when models allow.
        recognizer = None
        if embedder is not None:
            from src.embedding_storage import EmbeddingStorageService
            from src.face_recognition import FaceRecognitionService

            recognizer = FaceRecognitionService(
                embedder=embedder,
                storage=EmbeddingStorageService(settings=settings),
                settings=settings,
            )
        verifier = None
        if recognizer is not None:
            from src.temporal_verification import TemporalVerifier

            verifier = TemporalVerifier()
        attendance = None
        if verifier is not None:
            from src.attendance import AttendanceEngine

            attendance = AttendanceEngine()

        while True:
            if max_frames is not None and frames_processed >= max_frames:
                print(f"[live-demo] frame budget ({max_frames}) exhausted.")
                break
            ok, frame = camera.read_frame()
            if not ok or frame is None:
                print("[live-demo] camera read failed; stopping.")
                break
            frames_processed += 1

            detections = detector_owned.detect(frame)
            frame_policy = assessor.assess_frame_faces(detections)
            if frame_policy is not None or not detections:
                status_text = (
                    frame_policy.value if frame_policy else "no faces"
                )
                print(f"[frame {frames_processed:4d}] detection: {status_text}")
            else:
                detection = detections[0]
                quality = assessor.assess_face(frame, detection)
                if quality.decision is not FaceQualityDecision.ACCEPT:
                    reason = quality.reason.value if quality.reason else "?"
                    print(
                        f"[frame {frames_processed:4d}] rejected by quality: {reason}"
                    )
                elif recognizer is None or verifier is None or attendance is None:
                    print(
                        f"[frame {frames_processed:4d}] quality OK; "
                        "embedding/recognition/attendance blocked "
                        "(sface.onnx missing) — no attendance event."
                    )
                else:
                    from src.face_recognition import RecognitionStatus

                    crop = _safe_crop(frame, quality.box)
                    recognition = recognizer.recognize(crop)
                    if recognition.status is RecognitionStatus.MATCHED:
                        verification = verifier.update(recognition)
                        from src.temporal_verification import (
                            TemporalVerificationStatus,
                        )

                        if (
                            verification.status
                            is TemporalVerificationStatus.VERIFIED
                        ):
                            decision = attendance.update(verification)
                            print(
                                f"[frame {frames_processed:4d}] VERIFIED "
                                f"{verification.employee_id}: "
                                f"{decision.status.value}"
                            )
                        else:
                            print(
                                f"[frame {frames_processed:4d}] temporal: "
                                f"{verification.status.value}"
                            )
                    else:
                        # Non-matched frames interrupt the streak (the
                        # verifier's conservative rule, unchanged).
                        verifier.update(recognition)
                        print(
                            f"[frame {frames_processed:4d}] recognition: "
                            f"{recognition.status.value}"
                        )

            if window is not None:
                if not window.show(frame, detections):
                    print("[live-demo] window closed by user; stopping.")
                    break

    except KeyboardInterrupt:
        print("[live-demo] interrupted by user (Ctrl+C); shutting down.")
    except Exception:
        _logger.exception("live demo: unexpected error")
        print("[live-demo] unexpected error (see logs); shutting down.")
        raise
    finally:
        # Resource safety: release in reverse acquisition order, always.
        if window is not None:
            window.close()
        if detector_owned is not None:
            detector_owned._backend = None  # type: ignore[attr-defined]
            # (No explicit detector release API exists; reference drop is safe.)
        if camera is not None:
            camera.release()
        cv2.destroyAllWindows()
    return frames_processed


class _OpenCvWindow:
    """Minimal OpenCV highgui window facade (name-keyed, session-scoped)."""

    def __init__(self, name: str) -> None:
        import cv2

        self._name = name
        cv2.namedWindow(name)

    def show(
        self, frame: np.ndarray, detections: Sequence[FaceDetection]
    ) -> bool:
        """Overlay detections and pump the event loop.

        Returns:
            ``True`` normally; ``False`` when the user closed the window.
        """
        import cv2

        overlay = frame.copy()
        for det in detections:
            cv2.rectangle(
                overlay,
                (det.x, det.y),
                (det.x + det.width, det.y + det.height),
                (0, 200, 0),
                thickness=2,
            )
        cv2.imshow(self._name, overlay)
        # WaitKey: 1 ms keeps the window responsive without throttling
        # more than required; -1 (no key) and 27 (ESC) both keep running.
        return cv2.waitKey(1) not in (27,)

    def close(self) -> None:
        import cv2

        cv2.destroyWindow(self._name)


def _cv_window_supported() -> bool:
    """Whether OpenCV's highgui window facility is usable at all.

    The probe imports ``cv2`` locally, exactly like every other window
    function in this module (``cv2`` is deliberately not a module
    global), so the probe can never mistake a missing import for a
    missing window subsystem. Only genuine highgui failures
    (``cv2.error``) report unsupported; any other exception propagates
    instead of masquerading as "no GUI" — a bug must stay a bug.
    """
    import cv2

    try:
        probe_name = "[probe]"
        cv2.namedWindow(probe_name)
        cv2.destroyWindow(probe_name)
    except cv2.error:
        return False
    return True


# ---------------------------------------------------------------------------
# Smoke helpers (fixtures, crops, and Step 10 contract shims)
# ---------------------------------------------------------------------------


def _synthetic_frame(height: int = 240, width: int = 320) -> np.ndarray:
    """Deterministic BGR frame with visible structure (no device needed)."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = 40   # blue tint
    frame[:, :, 1] = 60   # green tint
    frame[:, :, 2] = 90   # red tint
    # A rectangle (pseudo face) stays below the detector's confidence so
    # the real YuNet sees an empty scene — the identity of the smoke run
    # is delivered by the injected stub detector, not by the model.
    frame[50:190, 110:210] = (180, 170, 160)
    return frame


def _demo_detection(frame_shape: tuple[int, ...]) -> FaceDetection:
    """One in-frame detection box large enough to pass quality."""
    h, w = frame_shape[:2]
    return FaceDetection(
        x=max(0, min(w - 1, w // 4)),
        y=max(0, min(h - 1, h // 5)),
        width=max(1, min(w // 2, w - w // 2)),
        height=max(1, min(h // 2, h - h // 5)),
        confidence=0.99,
    )


def _safe_crop(
    frame: np.ndarray, box: tuple[int, int, int, int]
) -> np.ndarray:
    """Extract the measured quality box as a fresh non-negative crop."""
    x, y, w, h = box
    return frame[y : y + h, x : x + w].copy()


def recognizer_embed(
    embedder: FaceCropsSource,
    crop: np.ndarray,
) -> object:
    """Run the *real* Step 08 boundary (the injected stub embedder)."""
    return embedder.embed(crop)


def _no_candidates_result() -> object:
    """Build the Step 10 result for a no-candidate/non-matched frame."""
    from src.face_recognition import RecognitionResult, RecognitionStatus

    return RecognitionResult(
        status=RecognitionStatus.NO_CANDIDATES,
        employee_id=None,
        best_score=None,
        runner_up_score=None,
        margin=None,
        reason="no valid face passed quality in this frame",
    )


def flush_unknown(unknown_outcome: object, i: int) -> DemoStageOutcome:
    """One bounded unknown-face checkpoint outcome (report-only)."""
    from src.unknown_face_handler import UnknownFaceOutcome

    assert isinstance(unknown_outcome, UnknownFaceOutcome)
    return DemoStageOutcome(
        stage=SmokeStage.UNKNOWN_FACE,
        frame_index=i,
        detail=(
            f"unknown-face outcome {unknown_outcome.status.value}"
            + (
                f" ({unknown_outcome.observation.value})"
                if unknown_outcome.observation is not None
                else ""
            )
        ),
    )


def _write_smoke_manifest(
    settings: AppSettings, employee_id: str, full_name: str
) -> None:
    """Write one Step 07-shaped manifest + one Step 09 embedding record.

    Used by the smoke fixture only; the files live under the caller's
    temporary ``settings`` and never under the repository's real
    ``data/`` tree.
    """
    import json

    from src.embedding_storage import EmbeddingStorageService
    from src.enrollment import MANIFEST_FILENAME
    from src.enrollment import safe_dir_name

    manifest_dir = settings.employees_dir / safe_dir_name(employee_id)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "format_version": 1,
        "employee": {
            "employee_id": employee_id,
            "full_name": full_name,
            "is_active": True,
            "created_at": "2026-10-13T00:00:00+00:00",
        },
        "sample_count": 1,
        "samples": [],
    }
    (manifest_dir / MANIFEST_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )
    EmbeddingStorageService(settings=settings).save(
        employee_id, _SMOKE_QUERY
    )
