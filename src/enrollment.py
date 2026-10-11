"""Multi-image face enrollment for the AI-Powered Visual Attendance
System (Phase 1 — Step 07).

Orchestrates one controlled capture session for a single employee:
validate identity → refuse duplicates → open the camera → read frames →
detect faces → apply the existing frame face-count policy and per-face
quality assessor → collect valid samples → persist atomically-ish —
nothing else. This module does **not** generate embeddings (Step 08),
recognize identities (Step 10), detect liveness, record attendance
(Step 13), or start Step 08 work: it is the last Phase 1 stage before
embeddings, consuming only the existing services.

Dependencies are injected and duck-typed
----------------------------------------

* ``camera_factory`` — zero-arg callable returning a ``CameraService``
  (or any object with ``open`` / ``read_frame`` / ``release``). Tests
  inject fakes; no physical webcam is required anywhere in this suite.
* ``detector`` — anything with ``detect(frame) -> list[FaceDetection]``
  (default: ``FaceDetector()``, which the operator must ``load()`` once
  from ``models/yunet.onnx`` before enrolling — model work is never
  hidden here and is never auto-downloaded).
* ``assessor`` — anything with the ``FaceQualityAssessor`` interface
  (``assess_frame_faces`` + ``assess_face``); default: the real one.
* ``settings`` — :class:`AppSettings` locating ``employees_dir`` and
  ``face_samples_dir``.

Lifecycle & termination (documented, bounded)
---------------------------------------------

Each :meth:`EnrollmentService.enroll` call runs at most
``EnrollmentConfig.max_capture_attempts`` frame attempts, checking the
caller's ``should_cancel()`` before every attempt, and stops as soon as
``min_samples`` valid samples are collected:

* **COMPLETED** — the minimum was reached and persistence succeeded.
* **CANCELLED** — the caller cancelled before the minimum was reached;
  nothing is persisted (never a half-published enrollment).
* **FAILED** — the attempt budget ran out, a camera error occurred, or
  the detector/assessor raised. Camera read failures and frame-level
  rejections count as attempts (bounded — never an endless retry loop);
  detector/assessor exceptions and camera open/read errors are fatal
  session errors, not sample rejections, so they can never be mistaken
  for "keep trying" feedback.

The camera is released on every exit path (success, cancellation,
failure, exception).

Employee validation reuses the ``Employee`` model exactly: IDs keep
leading zeros, hyphens, and case (``007``, ``EMP-0042``); empty or
whitespace-only IDs/names are rejected; nothing is normalized into a
different identity. IDs that would create path traversal (containing
``/`` or ``\\``, or being ``.``/``..``) are rejected before any
filesystem or camera work. Employee rows are recorded with their
provided ``is_active`` semantics (gating *recognition* of inactive
employees is a later step's concern).

Persistence format & safeguards
-------------------------------

Layout (both roots are git-ignored via ``.gitignore``'s content rules):

.. code-block:: text

    <data>/employees/<encoded-id>/employee.json
    <data>/face_samples/<encoded-id>/sample_001.png … sample_NNN.png

``<encoded-id>`` is a **deterministic, reversible, collision-free
filesystem encoding** of the exact employee ID: every character that is
unsafe for a directory name is percent-encoded (``.`` included, so
``..`` and Windows-trailing-dot IDs cannot exist as literal dots). The
exact ID is preserved inside ``employee.json``.

``employee.json`` (``format_version: 1``) records the full ``Employee``
serialization, the sample count, and per-sample ``file``, ISO-8601
``captured_at`` (UTC), and the measured quality metrics. No extra
personal data is stored.

Safety rules:

* An existing enrollment is **never overwritten** — presence of the
  employee record, its sample directory, or the manifest raises
  :class:`DuplicateEnrollmentError` before the camera is touched.
* Samples are staged in hidden ``.staging-`` directories under the two
  roots, every PNG is encoded, written, and re-decoded for verification,
  and the manifest is written **before** anything is published.
* Publish order: sample directory first (directory rename), then the
  employee manifest (``os.replace``) — the manifest is the marker that
  makes an enrollment "exist", so a crash between the two publishes
  leaves orphaned samples without a recognized enrollment rather than a
  declared enrollment without images.
* On persistence failure only artifacts created **by the current
  attempt** are removed; pre-existing files and unrelated employees are
  untouched. Atomicity across the two trees is best-effort, not
  transactional (documented limitation).
* Face crops are sensitive biometric data: they are written only to the
  ignored data roots, never logged, never echoed to the console, and
  never uploaded anywhere. Logs carry only counts and machine reasons.

Exact-duplicate suppression: the accepted face crops are hashed
(sha-256 over crop bytes) and an absolutely identical crop is skipped —
a coarse frame-level safeguard against saving the *same frame* twice,
not a biometric similarity system (that needs embeddings, Step 08+).
Duplicates are reported in the result, not silently hidden, and never
remove an already-accepted sample.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Final, Sequence
from urllib.parse import quote

import cv2
import numpy as np

from src.camera import CameraService
from src.config import ENROLLMENT, SETTINGS, AppSettings, EnrollmentConfig
from src.employee import Employee, EmployeeValidationError
from src.face_detection import FaceDetection, FaceDetector
from src.logging_setup import get_logger
from src.quality import (
    FaceQuality,
    FaceQualityAssessor,
    FaceQualityDecision,
    FaceQualityReason,
)

__all__ = [
    "EnrollmentError",
    "EnrollmentInputError",
    "DuplicateEnrollmentError",
    "EnrollmentPersistenceError",
    "EnrollmentStatus",
    "EnrollmentResult",
    "SampleRecord",
    "SampleRejection",
    "EnrollmentService",
    "MANIFEST_FILENAME",
]

_logger = get_logger(__name__)

#: Name of the published employee manifest, identified by existence.
MANIFEST_FILENAME: Final[str] = "employee.json"

#: On-disk manifest schema version. Bump only with a documented migration.
MANIFEST_FORMAT_VERSION: Final[int] = 1

#: Characters never allowed raw in an encoded directory name. Percent
#: encoding intentionally keeps letters, digits, ``-``, ``_`` and ``~``;
#: everything else — including ``.``, ``/`` and ``\\``, which never reach
#: the encoder anyway (see :class:`EnrollmentInputError`) — is encoded.
_SAFE_DIR_EXTRA: Final[str] = ""


class EnrollmentError(Exception):
    """Base class for enrollment errors."""


class EnrollmentInputError(EnrollmentError, ValueError):
    """Raised for invalid enrollment input before any capture happens."""


class DuplicateEnrollmentError(EnrollmentError):
    """Raised when an employee ID already has enrollment data.

    Existing data is never overwritten; the caller must choose another
    ID or explicitly delete the old enrollment first.
    """


class EnrollmentPersistenceError(EnrollmentError):
    """Raised when staging or publishing enrollment files fails.

    Only artifacts created by the failing attempt are cleaned up;
    pre-existing data is preserved.
    """


class EnrollmentStatus(Enum):
    """Explicit enrollment outcome — never surfaced as a bare boolean."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, eq=False)
class SampleRejection:
    """One non-accepting frame attempt, kept for inspection/tests.

    Attributes:
        frame_index: 1-based index of the frame attempt in the session.
        reason: Machine-readable rejection reason (face-count policy or
            per-face quality). Fatal errors are not recorded here.
        quality: The measured :class:`FaceQuality` when rejection came
            from the per-face assessor; ``None`` for face-count policy
            rejections.
    """

    frame_index: int
    reason: FaceQualityReason
    quality: FaceQuality | None = None


@dataclass(frozen=True, eq=False)
class SampleRecord:
    """One accepted face sample, in-memory during the session.

    Attributes:
        filename: Deterministic name assigned in acceptance order
            (``sample_001.png`` …), matching the published layout.
        frame_index: 1-based index of the frame attempt that produced it.
        captured_at: UTC timestamp of acceptance.
        quality: Measured quality of the accepted face.
        image: The face crop (memory only — never written elsewhere or
            logged).
        path: Published file path, set only after a successful persist.
    """

    filename: str
    frame_index: int
    captured_at: datetime
    quality: FaceQuality
    image: np.ndarray | None
    path: Path | None = None


@dataclass(frozen=True, eq=False)
class EnrollmentResult:
    """Outcome of one enrollment session — inspectable and testable.

    Attributes:
        status: COMPLETED, CANCELLED, or FAILED.
        employee: The validated employee; ``None`` never happens after
            validation, but is kept optional so type-checkers force
            callers to handle the pre-validation case.
        samples: Accepted samples (empty on CANCELLED-before-success).
        rejections: Every non-accepting frame attempt with its reason.
        attempted_frames: Total frame attempts made (includes rejected
            frames and camera read failures — bounded by config).
        duplicates_skipped: Exact-duplicate crops skipped (byte-level,
            not biometric).
        error: Explanation for FAILED/CANCELLED sessions; ``None`` for
            COMPLETED. Never contains image data.
    """

    status: EnrollmentStatus
    employee: Employee | None
    samples: tuple[SampleRecord, ...]
    rejections: tuple[SampleRejection, ...]
    attempted_frames: int
    duplicates_skipped: int
    error: str | None = None


class EnrollmentService:
    """Capture and persist multi-image enrollments for unique employees.

    Constructing the service performs no camera, model, or filesystem
    work (the default ``FaceDetector`` is created unloaded, the default
    camera factory is instantiated only when a session opens it). All
    real work happens in :meth:`enroll`.
    """

    def __init__(
        self,
        *,
        camera_factory: Callable[[], CameraService] | None = None,
        detector: FaceDetector | None = None,
        assessor: FaceQualityAssessor | None = None,
        enrollment_config: EnrollmentConfig = ENROLLMENT,
        settings: AppSettings = SETTINGS,
    ) -> None:
        self._camera_factory = camera_factory or (lambda: CameraService())
        #: Detection service; the operator must have called ``load()``
        #: once before enrolling (a default unloaded detector raises a
        #: typed ``FaceDetectorError`` on first use, never silently).
        self._detector = detector if detector is not None else FaceDetector()
        #: Per-face quality + frame face-count policy (Step 06).
        self._assessor = (
            assessor if assessor is not None else FaceQualityAssessor()
        )
        if not isinstance(enrollment_config, EnrollmentConfig):
            raise EnrollmentInputError(
                "enrollment_config must be an EnrollmentConfig, got "
                f"{type(enrollment_config).__name__}"
            )
        self._enrollment_config = enrollment_config
        if not isinstance(settings, AppSettings):
            raise EnrollmentInputError(
                f"settings must be an AppSettings, got {type(settings).__name__}"
            )
        self._settings = settings

    # ------------------------------------------------------------------
    # Public session entry point
    # ------------------------------------------------------------------

    def enroll(
        self,
        employee_id: str,
        full_name: str,
        *,
        is_active: bool = True,
        should_cancel: Callable[[], bool] | None = None,
    ) -> EnrollmentResult:
        """Run one bounded capture session and (on success) persist it.

        Args:
            employee_id: Exact business identifier, preserved (``007``,
                ``EMP-0042``). Empty/whitespace-only is rejected; IDs
                containing path separators or equal to ``.``/``..`` are
                rejected before any capture.
            full_name: Display name.
            is_active: Recorded with the employee (recognition gating of
                inactive employees is a later step, not enrollment's).
            should_cancel: Optional pollable cancellation predicate,
                checked before every frame attempt. Cancelling during
                capture yields CANCELLED **without persisting**, even if
                the minimum was already collected mid-session.

        Returns:
            :class:`EnrollmentResult` with the explicit status, accepted
            samples, per-frame rejections, attempt count, and duplicate
            skips.

        Raises:
            EnrollmentInputError: invalid identity/id-path input or
                invalid service configuration.
            DuplicateEnrollmentError: the employee already has data.
            EnrollmentPersistenceError: staging/publishing failed after
                cleanup of current-attempt artifacts (camera already
                released at that point).
        """
        employee = self._validated_employee(
            employee_id, full_name, is_active
        )
        dir_name = self._safe_dir_name(employee.employee_id)
        self._reject_existing_enrollment(dir_name, employee.employee_id)

        camera: CameraService | None = None
        status: EnrollmentStatus | None = None
        error_message: str | None = None
        samples: list[SampleRecord] = []
        rejections: list[SampleRejection] = []
        seen_digests: set[bytes] = set()
        duplicates_skipped = 0
        attempted_frames = 0

        try:
            camera = self._camera_factory()
        except Exception as exc:  # noqa: BLE001 - fatal acquisition error
            return EnrollmentResult(
                status=EnrollmentStatus.FAILED,
                employee=employee,
                samples=(),
                rejections=(),
                attempted_frames=0,
                duplicates_skipped=0,
                error=f"camera acquisition failed: {exc}",
            )

        try:
            camera.open()
        except Exception as exc:  # noqa: BLE001 - fatal open error
            camera.release()
            _logger.error(
                "enrollment camera open failed for employee %s: %s",
                employee.employee_id,
                exc,
            )
            return EnrollmentResult(
                status=EnrollmentStatus.FAILED,
                employee=employee,
                samples=(),
                rejections=(),
                attempted_frames=0,
                duplicates_skipped=0,
                error=f"camera open failed: {exc}",
            )

        try:
            while True:
                if should_cancel is not None and should_cancel():
                    status = EnrollmentStatus.CANCELLED
                    error_message = (
                        "cancelled by caller after "
                        f"{attempted_frames} frame attempt(s)"
                    )
                    break
                if len(samples) >= self._enrollment_config.min_samples:
                    status = EnrollmentStatus.COMPLETED
                    break
                if attempted_frames >= (
                    self._enrollment_config.max_capture_attempts
                ):
                    status = EnrollmentStatus.FAILED
                    error_message = (
                        "capture attempt budget exhausted: "
                        f"{attempted_frames} attempt(s) reached before "
                        f"collecting {self._enrollment_config.min_samples} "
                        f"valid sample(s) ({len(samples)} accepted, "
                        f"{duplicates_skipped} duplicate(s) skipped)"
                    )
                    break

                attempted_frames += 1
                ok, frame = camera.read_frame()
                if not ok or frame is None:
                    _logger.debug(
                        "frame read failure (attempt %d) — counted as an "
                        "attempt, not a sample rejection",
                        attempted_frames,
                    )
                    continue

                detections = self._detector.detect(frame)
                face_count_policy = self._assessor.assess_frame_faces(
                    detections
                )
                if face_count_policy is not None:
                    rejections.append(
                        SampleRejection(
                            frame_index=attempted_frames,
                            reason=face_count_policy,
                            quality=None,
                        )
                    )
                    continue

                quality = self._assessor.assess_face(frame, detections[0])
                if quality.decision is not FaceQualityDecision.ACCEPT:
                    rejections.append(
                        SampleRejection(
                            frame_index=attempted_frames,
                            reason=(
                                quality.reason
                                if quality.reason is not None
                                else FaceQualityReason.EMPTY_CROP
                            ),
                            quality=quality,
                        )
                    )
                    continue

                crop = self._crop(frame, quality.box)
                digest = hashlib.sha256(crop.tobytes()).digest()
                if digest in seen_digests:
                    duplicates_skipped += 1
                    _logger.debug(
                        "exact-duplicate crop skipped (not biometric dedupe)"
                    )
                    continue
                seen_digests.add(digest)

                samples.append(
                    SampleRecord(
                        filename=self._sample_filename(len(samples)),
                        frame_index=attempted_frames,
                        captured_at=datetime.now(UTC),
                        quality=quality,
                        image=crop,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - fatal session error
            # Detector/assessor/camera runtime failures are fatal for the
            # session — never upgrades to a sample rejection and never
            # persists partial data.
            status = EnrollmentStatus.FAILED
            error_message = f"session error: {exc}"
            _logger.exception(
                "enrollment session error for employee %s",
                employee.employee_id,
            )
        finally:
            camera.release()

        if status is EnrollmentStatus.CANCELLED:
            return EnrollmentResult(
                status=EnrollmentStatus.CANCELLED,
                employee=employee,
                samples=tuple(samples),
                rejections=tuple(rejections),
                attempted_frames=attempted_frames,
                duplicates_skipped=duplicates_skipped,
                error=error_message,
            )
        if status is EnrollmentStatus.FAILED:
            return EnrollmentResult(
                status=EnrollmentStatus.FAILED,
                employee=employee,
                samples=(),
                rejections=tuple(rejections),
                attempted_frames=attempted_frames,
                duplicates_skipped=duplicates_skipped,
                error=error_message,
            )
        # COMPLETED — persist before reporting success. Nothing above
        # touches the data directories: the employee record is *never*
        # written before the configured minimum of valid samples exists.
        try:
            published_paths = self._persist(employee, samples)
        except EnrollmentPersistenceError as exc:
            _logger.error(
                "enrollment persistence failed for employee %s: %s",
                employee.employee_id,
                exc,
            )
            return EnrollmentResult(
                status=EnrollmentStatus.FAILED,
                employee=employee,
                samples=(),
                rejections=tuple(rejections),
                attempted_frames=attempted_frames,
                duplicates_skipped=duplicates_skipped,
                error=f"persistence failed: {exc}",
            )
        published = tuple(
            SampleRecord(
                filename=record.filename,
                frame_index=record.frame_index,
                captured_at=record.captured_at,
                quality=record.quality,
                image=record.image,
                path=published_paths[index],
            )
            for index, record in enumerate(samples)
        )
        return EnrollmentResult(
            status=EnrollmentStatus.COMPLETED,
            employee=employee,
            samples=published,
            rejections=tuple(rejections),
            attempted_frames=attempted_frames,
            duplicates_skipped=duplicates_skipped,
            error=None,
        )

    # ------------------------------------------------------------------
    # Validation helpers (deterministic, independently testable)
    # ------------------------------------------------------------------

    @staticmethod
    def _validated_employee(
        employee_id: str, full_name: str, is_active: bool
    ) -> Employee:
        """Build the Employee model; normalize errors to enrollment's."""
        try:
            return Employee(
                employee_id=employee_id,
                full_name=full_name,
                is_active=is_active,
            )
        except EmployeeValidationError as exc:
            raise EnrollmentInputError(
                f"invalid employee input: {exc}"
            ) from exc

    @staticmethod
    def _safe_dir_name(employee_id: str) -> str:
        """Deterministic, traversal-proof directory name for an ID.

        Percent-encodes every character that is not letters/digits/``-``
        / ``_``/ ``~`` (including ``.``, so ``..`` and Windows trailing
        dots can never exist literally). IDs containing path separators
        or equal to ``.``/``..`` are structurally invalid and rejected
        outright.
        """
        if "/" in employee_id or "\\" in employee_id:
            raise EnrollmentInputError(
                "employee_id must not contain path separators "
                f"('/' or '\\\\'), got {employee_id!r}"
            )
        if employee_id in (".", ".."):
            raise EnrollmentInputError(
                "employee_id must not be '.' or '..'"
            )
        encoded = quote(employee_id, safe=_SAFE_DIR_EXTRA).replace(".", "%2E")
        if not encoded:
            raise EnrollmentInputError(
                "employee_id produced an empty directory name"
            )
        return encoded

    @staticmethod
    def _sample_filename(index: int, max_samples: int = 999) -> str:
        if index < 0 or index >= max_samples:
            raise EnrollmentInputError(
                f"sample index {index} out of range (0..{max_samples - 1})"
            )
        return f"sample_{index + 1:03d}.png"

    @staticmethod
    def _crop(
        frame: np.ndarray, box: tuple[int, int, int, int]
    ) -> np.ndarray:
        x, y, w, h = box
        if w <= 0 or h <= 0:
            raise EnrollmentInputError(
                f"accepted quality box has non-positive area: {box!r}"
            )
        return frame[y : y + h, x : x + w]

    # ------------------------------------------------------------------
    # Duplicate & path-safety checks
    # ------------------------------------------------------------------

    def _reject_existing_enrollment(
        self, dir_name: str, employee_id: str
    ) -> None:
        employees_root = self._settings.employees_dir
        samples_root = self._settings.face_samples_dir
        existing_manifest = employees_root / dir_name / MANIFEST_FILENAME
        existing_samples = samples_root / dir_name
        if existing_manifest.exists() or existing_samples.exists():
            raise DuplicateEnrollmentError(
                f"employee {employee_id!r} already has enrollment data; "
                "refusing to overwrite"
            )

    # ------------------------------------------------------------------
    # Persistence (staged, verified, minimal-footprint cleanup)
    # ------------------------------------------------------------------

    def _persist(
        self, employee: Employee, samples: Sequence[SampleRecord]
    ) -> list[Path]:
        """Write manifest and samples into their final locations.

        Raises:
            EnrollmentPersistenceError: on any staging/encoding/verify
                failure, after cleaning up only this attempt's artifacts.
        """
        employees_root = self._settings.employees_dir
        samples_root = self._settings.face_samples_dir
        dir_name = self._safe_dir_name(employee.employee_id)
        employee_dir = employees_root / dir_name
        samples_dir = samples_root / dir_name

        # Re-checked immediately before staging (cheap, deterministic).
        self._reject_existing_enrollment(dir_name, employee.employee_id)

        staging_suffix = f".staging-{uuid.uuid4().hex[:12]}"
        staging_samples = samples_root / f"{dir_name}{staging_suffix}"
        staging_employee = employees_root / f"{dir_name}{staging_suffix}"

        # Only artifacts created by THIS attempt are ever cleaned.
        created_this_attempt: list[Path] = []
        manifest_published = False
        samples_published = False

        try:
            # Roots are created explicitly here (not at import): directory
            # creation is a side effect of a real enrollment run.
            samples_root.mkdir(parents=True, exist_ok=True)
            employees_root.mkdir(parents=True, exist_ok=True)
            staging_samples.mkdir(parents=True)
            staging_employee.mkdir(parents=True)
            created_this_attempt.extend([staging_samples, staging_employee])

            sample_records = self._stage_samples(staging_samples, samples)

            manifest = {
                "format_version": MANIFEST_FORMAT_VERSION,
                "employee": employee.to_dict(),
                "sample_count": len(sample_records),
                "samples": sample_records,
            }
            staged_manifest = staging_employee / MANIFEST_FILENAME
            staged_manifest.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            # Publish: samples first, manifest last (the manifest is the
            # "enrollment exists" marker — see module docstring).
            # Path.rename() returns the destination path; rename fails
            # loudly if the destination exists (checked just before).
            published_samples_dir = staging_samples.rename(samples_dir)
            samples_published = True
            created_this_attempt.append(published_samples_dir)

            employee_dir.mkdir(parents=True)
            created_this_attempt.append(employee_dir)
            os.replace(staged_manifest, employee_dir / MANIFEST_FILENAME)
            manifest_published = True
        except Exception as exc:
            self._rollback_attempt(
                created_paths=created_this_attempt,
                employee_dir=employee_dir,
                samples_dir=samples_dir,
                samples_published=samples_published,
                manifest_published=manifest_published,
            )
            # Always surface a single consistent enrollment-persistence
            # error with the underlying cause attached, so callers need
            # not distinguish staging-stage vs publish-stage messages.
            message = (
                f"enrollment persistence failed for employee "
                f"{employee.employee_id!r}: {exc}"
            )
            raise EnrollmentPersistenceError(message) from exc
        finally:
            # Staging remnants (already published ones no longer exist).
            shutil.rmtree(staging_samples, ignore_errors=True)
            shutil.rmtree(staging_employee, ignore_errors=True)

        _logger.info(
            "enrollment persisted: employee=%s, samples=%d, samples_dir=%s",
            employee.employee_id,
            len(samples),
            samples_dir,
        )
        return [
            samples_dir / record.filename
            for record in samples
        ]

    def _stage_samples(
        self,
        staging_dir: Path,
        samples: Sequence[SampleRecord],
    ) -> list[dict[str, str | float | None]]:
        """Encode, write, and verify every sample into ``staging_dir``."""
        records: list[dict[str, str | float | None]] = []
        for index, sample in enumerate(samples, start=1):
            if sample.image is None:
                raise EnrollmentPersistenceError(
                    f"sample {index} has no image; refusing to persist "
                    "an empty sample"
                )
            target_name = self._sample_filename(index - 1)
            if sample.filename != target_name:
                raise EnrollmentPersistenceError(
                    f"sample order/name mismatch: expected {target_name!r} "
                    f"at position {index}, got {sample.filename!r}"
                )
            staged_path = staging_dir / target_name
            encoded = self._encode_png(sample.image)
            if encoded is None:
                raise EnrollmentPersistenceError(
                    f"PNG encoding failed for sample {index}"
                )
            staged_path.write_bytes(encoded)
            decoded = cv2.imread(str(staged_path))
            if decoded is None or decoded.shape != sample.image.shape:
                raise EnrollmentPersistenceError(
                    f"staged sample {index} failed verification "
                    "(unreadable or shape mismatch); refusing to publish"
                )
            records.append(
                {
                    "file": target_name,
                    "captured_at": sample.captured_at.isoformat(),
                    "quality_decision": sample.quality.decision.value,
                    "sharpness": sample.quality.sharpness,
                    "brightness": sample.quality.brightness,
                }
            )
        return records

    @staticmethod
    def _encode_png(image: np.ndarray) -> bytes | None:
        """Encode a BGR image to PNG bytes; ``None`` on failure."""
        ok, buffer = cv2.imencode(".png", image)
        if not ok or buffer is None:
            return None
        return buffer.tobytes()

    @staticmethod
    def _rollback_attempt(
        *,
        created_paths: list[Path],
        employee_dir: Path,
        samples_dir: Path,
        samples_published: bool,
        manifest_published: bool,
    ) -> None:
        """Best-effort removal of artifacts created by this attempt.

        Never touches anything that existed before the attempt (we only
        delete paths added to ``created_paths`` plus the unpublished/
        half-published final locations owned by the current attempt).
        """
        if manifest_published:
            # A published manifest means the enrollment already exists
            # on disk; that is not a failed attempt — leave it alone.
            _logger.error(
                "manifest published but persist still failed; "
                "manual inspection of %s recommended",
                employee_dir,
            )
            return
        if samples_published:
            shutil.rmtree(samples_dir, ignore_errors=True)
            _logger.warning(
                "removed samples directory from failed attempt: %s",
                samples_dir,
            )
        for path in reversed(created_paths):
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                elif path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort, logged
                _logger.exception(
                    "cleanup of %s from the failed attempt failed", path
                )
