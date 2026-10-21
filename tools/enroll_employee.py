"""Step 17 CLI: connect an existing Step 07 enrollment to the Step 16
embedding bridge, making one *consenting* employee a recognition
candidate.

This operator tool composes only existing services — it adds no new
recognition math, no thresholds, and no persistence layer:

* :meth:`src.enrollment.EnrollmentService.enroll` — the existing
  bounded capture session (real camera, real loaded YuNet detector,
  real quality assessor). It already refuses duplicates before any
  camera work and never overwrites data.
* :func:`src.enrollment_bridge.embed_published_samples` — the existing
  bridge that reads the published sample PNGs, aggregates them into
  exactly one embedding, and persists it under the
  one-embedding-per-employee ``replace=False`` policy (existing
  records always abort — there is no force-replacement path).
* :meth:`src.embedding_storage.EmbeddingStorageService.
  list_employee_ids` — the same discovery path the live recognizer
  uses, applied here as the final operator-visible proof that the
  employee is now a recognition candidate.

Consent is mandatory and explicit: the operator must pass
``--confirm-consent``. Without it the tool does *nothing at all* — no
prerequisite probing, no camera, no model loading, no validation — and
exits 2. The confirmation must come from the operator who has verified
the on-screen participant has consented to being enrolled.

Biometric discipline: face crops, sample images, vectors, and the
stored embedding are never logged, echoed, or written anywhere by this
tool. Output carries IDs, counts, and dimensions only (the same
bounded-output convention as ``tools/run_integration_demo.py``).

Exit codes:

* ``0`` — enrollment completed and the embedding is discoverable.
* ``1`` — a typed failure at any stage; the failure is reported and no
  success message is printed.
* ``2`` — argparse usage error, or consent was not confirmed (nothing
  ran).

Usage::

    python tools/enroll_employee.py \\
        --employee-id "EMP-0042" --full-name "Ada Lovelace" --confirm-consent

Prerequisites: ``models/yunet.onnx`` and ``models/sface.onnx`` must be
present (operator-supplied; see README). The camera is the project's
configured default camera (``src.config.CAMERA``). An *optional*
``--frame-size WIDTHxHEIGHT`` override changes only the requested
camera frame size for the capture session (e.g. ``640x480`` for a
camera that does not deliver the configured default 1280x720); when
omitted, the configured default resolution is used unchanged.
Validation is never bypassed: the frame-size value is parsed and
validated exactly like the live demo's ``--frame-size`` (the same
parser), and the camera acquisition still fails loudly with its typed
``CameraOpenError`` if the device cannot actually apply the requested
size.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

_PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import AppSettings, SETTINGS  # noqa: E402
from tools.run_integration_demo import parse_frame_size  # noqa: E402
from src.embedding_storage import (  # noqa: E402
    DuplicateEmbeddingRecordError,
    EmbeddingStorageService,
    EmbeddingStorageError,
)
from src.enrollment import (  # noqa: E402
    DuplicateEnrollmentError,
    EnrollmentError,
    EnrollmentResult,
    EnrollmentService,
    EnrollmentStatus,
)
from src.enrollment_bridge import (  # noqa: E402
    EnrollmentBridgeError,
    EnrollmentBridgeResult,
    embed_published_samples,
)
from src.integration_demo import live_prerequisites  # noqa: E402

_CONSENT_HINT = (
    "consent not confirmed: pass --confirm-consent only after the "
    "participant being enrolled has explicitly consented to face "
    "capture, sample storage, and embedding creation. Nothing was run."
)

_EMBEDDING_FAILURE_RECOVERY_NOTE = (
    "the published enrollment artifacts (samples + employee.json) remain "
    "on disk after this failure; they are legitimate Step 07 data and are "
    "never removed by this tool. The employee is NOT a recognition "
    "candidate until a stored embedding for them exists: fix the cause "
    "and re-run this tool (the bridge safely refuses duplicates; delete "
    "the enrollment's data directories manually first if you want a full "
    "clean re-enrollment under the same ID)."
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enroll_employee",
        description=(
            "Operator enrollment: capture face samples for one consenting "
            "employee, then embed the published samples into the single "
            "stored embedding the recognizer discovers. Uses only the "
            "existing enrollment service and embedding bridge."
        ),
    )
    parser.add_argument(
        "--employee-id",
        required=True,
        help=(
            "Exact business identifier of the participant (e.g. '007' or "
            "'EMP-0042'); leading zeros/hyphens/case are preserved."
        ),
    )
    parser.add_argument(
        "--full-name",
        required=True,
        help="Display name of the participant.",
    )
    parser.add_argument(
        "--frame-size",
        type=parse_frame_size,
        default=None,
        metavar="WIDTHxHEIGHT",
        help=(
            "Optional camera frame-size override for the capture session "
            "(e.g. 640x480; the x separator is case-insensitive; positive "
            "dimensions required). Omitted: the project's configured "
            "default resolution is used unchanged."
        ),
    )
    parser.add_argument(
        "--confirm-consent",
        action="store_true",
        help=(
            "REQUIRED confirmation that the participant has explicitly "
            "consented to face capture, sample storage, and embedding "
            "creation. Without this flag the tool does nothing."
        ),
    )
    return parser


def _frame_size_camera_config(frame_size: tuple[int, int]) -> "CameraConfig":
    """Build a temporary, frame-size-matched camera configuration.

    Overrides only the requested frame width/height: the device index
    stays the project's configured default (``CAMERA.index``) and no
    validation is bypassed — ``CameraConfig`` validates the dimensions
    and :class:`src.camera.CameraService.open` still raises its typed
    ``CameraOpenError`` when the device cannot apply the requested size.
    """
    from src.config import CameraConfig, CAMERA

    width, height = frame_size
    return CameraConfig(
        index=CAMERA.index,
        frame_width=width,
        frame_height=height,
    )


def run_employee_enrollment(
    employee_id: str,
    full_name: str,
    *,
    confirm_consent: bool,
    frame_size: tuple[int, int] | None = None,
    settings: AppSettings = SETTINGS,
    enrollment_factory: Callable[[], EnrollmentService] | None = None,
    embedder_factory: Callable[[], object] | None = None,
    storage_factory: Callable[[], EmbeddingStorageService] | None = None,
    bridge: Callable[..., EnrollmentBridgeResult] = embed_published_samples,
    out: Callable[..., None] = print,
) -> int:
    """Run the consent-gated enroll → bridge → verify/operator flow.

    Returns an exit code (0/1/2) and prints bounded, biometric-free
    progress. All service creation is injectable (used by the test
    suite; the defaults construct the real services lazily). When
    ``frame_size`` is given, the *real default* enrollment service is
    built with a camera factory that requests exactly that frame size
    (validation unchanged); injected tests may pass their own factory
    and ignore the override.
    """
    # -- Stage 0: consent gate — before anything else happens at all. --
    if not confirm_consent:
        out(_CONSENT_HINT)
        return 2

    prereqs = live_prerequisites()
    if not prereqs.yunet_available:
        out(
            f"[enroll-cli] aborted: {prereqs.yunet_path} is missing. "
            "Provide the YuNet model manually (never auto-downloaded) "
            "and retry."
        )
        return 1
    if not prereqs.sface_available:
        out(
            f"[enroll-cli] aborted: {prereqs.sface_path} is missing. "
            "Provide the SFace model manually (never auto-downloaded) "
            "and retry."
        )
        return 1

    # -- Stage 1: enrollment service (factory default loads real models). --
    enrollment_service = (
        enrollment_factory()
        if enrollment_factory
        else _make_real_enrollment_service(frame_size=frame_size)
    )

    # -- Stage 2: capture + publish samples (existing bounded session). --
    try:
        enroll_result: EnrollmentResult = enrollment_service.enroll(
            employee_id, full_name
        )
    except DuplicateEnrollmentError as exc:
        out(
            f"[enroll-cli] refused — duplicate enrollment: {exc} "
            "Existing enrollment data is never overwritten; choose a "
            "different employee ID or remove the old enrollment's data "
            "directories manually first."
        )
        return 1
    except EnrollmentError as exc:
        out(f"[enroll-cli] enrollment rejected before capture: {exc}")
        return 1
    if enroll_result.status is not EnrollmentStatus.COMPLETED:
        error = enroll_result.error or "no error message provided"
        # Diagnostic tally (added 2026-10-21): count the recorded
        # per-attempt rejection reasons so a failed session reports
        # *why* frames were rejected, not just how many attempts were
        # consumed. Deterministic: reason names are sorted. Biometric
        # discipline: only reason names and counts are printed — never
        # frame indices, crops, quality metrics, or any other content.
        reason_counts: dict[str, int] = {}
        for rejection in enroll_result.rejections:
            name = rejection.reason.value
            reason_counts[name] = reason_counts.get(name, 0) + 1
        rejection_summary = (
            "; rejections by reason: "
            + ", ".join(
                f"{name}={reason_counts[name]}" for name in sorted(reason_counts)
            )
            if reason_counts
            else ""
        )
        out(
            f"[enroll-cli] enrollment did not complete for employee "
            f"'{employee_id}' (status: {enroll_result.status.value}; "
            f"frames attempted: {enroll_result.attempted_frames}; "
            f"duplicate crops skipped: {enroll_result.duplicates_skipped}"
            f"{rejection_summary}): "
            f"{error} Nothing was published."
        )
        return 1

    out(
        f"[enroll-cli] enrollment completed for employee '{employee_id}' "
        f"with {len(enroll_result.samples)} published sample(s); "
        "embedding them now."
    )

    # -- Stage 3: bridge the published samples into one stored embedding. --
    embedder = embedder_factory() if embedder_factory else _make_real_embedder()
    storage = (
        storage_factory() if storage_factory else _make_real_storage(settings)
    )
    try:
        bridge_result = bridge(
            employee_id,
            embedder=embedder,
            storage=storage,
            settings=settings,
        )
    except DuplicateEmbeddingRecordError as exc:
        out(
            f"[enroll-cli] refused — a stored embedding already exists for "
            f"employee '{employee_id}': {exc} The one-embedding-per-employee "
            "contract forbids re-embedding and nothing was changed. If the "
            "existing embedding must be replaced, that is a manual, "
            "operator-owned action (delete the stored embedding files and "
            "the enrollment data, then re-run this tool)."
        )
        return 1
    except EmbeddingStorageError as exc:
        out(
            f"[enroll-cli] embedding storage failed for employee "
            f"'{employee_id}': {exc} {_EMBEDDING_FAILURE_RECOVERY_NOTE}"
        )
        return 1
    except EnrollmentBridgeError as exc:
        out(
            f"[enroll-cli] embedding bridge failed for employee "
            f"'{employee_id}': {exc} {_EMBEDDING_FAILURE_RECOVERY_NOTE}"
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - mirrors run_integration_demo.py
        # A non-typed bridge/enrollment-surprise is still a careful failure:
        # report it honestly with the recovery path (typed failures above
        # keep their specific wording); never print success here.
        out(
            f"[enroll-cli] unexpected failure while bridging employee "
            f"'{employee_id}': {exc} {_EMBEDDING_FAILURE_RECOVERY_NOTE}"
        )
        return 1

    # -- Stage 4: prove the employee is discoverable (recognition side). --
    if employee_id not in storage.list_employee_ids():
        out(
            f"[enroll-cli] final candidate verification failed for employee "
            f"'{employee_id}': no stored embedding is discoverable through "
            "the recognition pipeline's candidate list (this is a "
            "post-bridge consistency problem, never silently ignored). "
            f"{_EMBEDDING_FAILURE_RECOVERY_NOTE}"
        )
        return 1

    # -- Success: facts only, never biometric content. --
    out(
        f"[enroll-cli] success: employee '{employee_id}' is now a "
        "recognition candidate "
        f"({bridge_result.samples_used} sample(s) embedded into a single "
        f"{bridge_result.dimension}-D embedding). "
        "Run the live integration demo to recognize enrolled employees."
    )
    return 0


def _make_real_enrollment_service(
    frame_size: tuple[int, int] | None = None,
) -> EnrollmentService:
    """Build the real enrollment stack: loader + real services.

    With ``frame_size`` set, the camera factory requests exactly that
    frame width/height (index stays the configured default); without
    it, the pre-existing default camera configuration is used
    unchanged.
    """
    from src.camera import CameraService
    from src.face_detection import FaceDetector
    from src.quality import FaceQualityAssessor

    detector = FaceDetector()
    detector.load()  # fails with its typed error if the model is missing
    if frame_size is None:
        return EnrollmentService(
            detector=detector,
            assessor=FaceQualityAssessor(),
        )

    def camera_factory() -> CameraService:
        return CameraService(_frame_size_camera_config(frame_size))

    return EnrollmentService(
        detector=detector,
        assessor=FaceQualityAssessor(),
        camera_factory=camera_factory,
    )


def _make_real_embedder() -> FaceEmbedder:
    from src.face_embedding import FaceEmbedder

    embedder = FaceEmbedder()  # same defaults as the live recognizer
    embedder.load()
    return embedder


def _make_real_storage(settings: AppSettings) -> EmbeddingStorageService:
    return EmbeddingStorageService(settings=settings)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return run_employee_enrollment(
            args.employee_id,
            args.full_name,
            confirm_consent=args.confirm_consent,
            frame_size=args.frame_size,
            settings=AppSettings(),
        )
    except Exception as exc:  # noqa: BLE001 - a failed run is a nonzero exit
        print(f"[enroll-cli] enrollment run failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
