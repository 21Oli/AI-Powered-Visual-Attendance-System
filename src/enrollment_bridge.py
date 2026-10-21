"""Enrollment-to-recognition bridge (Phase 1 — Step 16).

Connects the *existing* services — nothing else. :func:`embed_published_samples`
reads the face-sample PNG crops that Step 07's ``EnrollmentService`` already
published, embeds each crop via the *existing*
:class:`~src.face_embedding.FaceEmbedder` (unchanged model behavior and
defaults), aggregates the samples into exactly one employee embedding, and
persists it through the *existing*
:class:`~src.embedding_storage.EmbeddingStorageService` under its documented
one-embedding-per-employee ``replace=False`` policy.

Aggregation scheme (verified against source, not invented)
----------------------------------------------------------

Cosine similarity is the only decision metric (``cosine_similarity`` in
``src.face_recognition`` divides by vector norms, so it is scale-invariant),
and storage accepts any positive finite norm (``_validate_vector`` in
``src.embedding_storage`` rejects only zero/non-finite norms). The live
pipeline constructs ``FaceEmbedder()`` with the default
``normalize_output=False`` (raw SFace features), so sample norms can vary
with acquisition conditions while the *direction* carries the identity
information the metric uses. The bridge therefore aggregates as the cosine
centroid:

1. L2-normalize each sample vector (raw features, post-embed);
2. take the arithmetic mean of the unit vectors;
3. L2-normalize the mean again (guarding the degenerate cancellation case)
   and hand that single vector to ``storage.save(..., replace=False)``.

Per-sample normalization is what prevents an arbitrarily high-norm sample
from dominating the mean on the *raw* unnormalized scale; the final stored
vector's own norm is irrelevant to the decision (cosine). No model call,
threshold, dimension, or storage format changes anywhere.

Eligibility consistency
------------------------

The bridge never invents identity: before any embedding it requires the
*published* Step 07 enrollment manifest
(``employees_dir/<encoded-id>/employee.json``, schema ``format_version`` 1)
whose ``employee.employee_id`` matches the requested ID exactly — the same
"enrollment exists" marker ``FaceRecognitionService._is_eligible`` votes on
(``RecognitionConfig.require_enrollment=True``). ``is_active`` is validated
as a bool but **not** used to refuse: embedding an inactive employee's
published enrollment is harmless and consistent — the recognizer's
eligibility logic continues to gate inactive employees at decision time,
unchanged. After a successful save the record is re-read through
``storage.load`` and its discovery via ``storage.list_employee_ids()`` — the
exact path the live recognizer uses — and the run fails loudly if
consistency cannot be proven. A bridge failure therefore never leaves a
*claimed* embedding-recognition state: enrollment artifacts (samples +
manifest) remain legitimately published, but the recognizer still sees
``no_candidates`` until the bridge is completed — never a silent mismatch.

Failure & data rules
---------------------

* Fail-closed: any missing manifest, decode failure, embedder error, or
  storage error aborts the whole run *before* persistence — nothing partial
  is written by this module; storage's own staging/rollback (Step 09)
  covers the write itself.
* Duplicate protection: an existing record aborts before any embedding
  work; ``save`` and its ``replace=False`` refusal remain the authoritative
  guard. There is no force-update path in this module.
* Biometric discipline: sample PNG bytes, crops, vectors, and the
  aggregated embedding are never logged, echoed, or exposed; results
  carry counts and machine facts only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import cv2
import numpy as np

from src.config import AppSettings, SETTINGS
from src.employee import EmployeeValidationError
from src.embedding_storage import (
    DuplicateEmbeddingRecordError,
    EmbeddingStorageService,
)
from src.enrollment import (
    EnrollmentInputError,
    MANIFEST_FILENAME,
    safe_dir_name,
)
from src.logging_setup import get_logger

__all__ = [
    "EnrollmentBridgeError",
    "EnrollmentBridgeInputError",
    "NoEligibleSamplesError",
    "EnrollmentBridgeResult",
    "embed_published_samples",
]

_logger = get_logger(__name__)

#: Reused enrollment convention: same schema version enrollment publishes.
_MANIFEST_FORMAT_VERSION = 1


class EnrollmentBridgeError(Exception):
    """Base class for enrollment-bridge errors."""


class EnrollmentBridgeInputError(EnrollmentBridgeError, ValueError):
    """Raised for invalid bridge input (ID form, contradictory manifest)."""


class NoEligibleSamplesError(EnrollmentBridgeInputError):
    """Raised when no decodable sample is available to embed."""


@dataclass(frozen=True)
class EnrollmentBridgeResult:
    """Read-only outcome of one successful bridge run — facts, not vectors.

    Attributes:
        employee_id: The exact ID embedded.
        dimension: Size of the stored aggregated vector.
        samples_used: Number of decodable samples that were embedded.
    """

    employee_id: str
    dimension: int
    samples_used: int


def _unit(vector: np.ndarray) -> np.ndarray:
    """L2-normalize a finite, non-zero-norm 1-D float vector (read-only input)."""
    norm = float(np.linalg.norm(vector.astype(np.float64)))
    if norm == 0.0 or not np.isfinite(norm):
        raise EnrollmentBridgeInputError(
            "aggregation produced a zero/non-finite-norm vector "
            "(parallel canceling samples?); refusing to persist"
        )
    return vector / norm


def _validate_eligibility_manifest(
    settings: AppSettings, employee_id: str
) -> None:
    """Require the published Step 07 manifest for exactly this identity.

    Mirrors the *existence and identity-consistency* half of
    ``FaceRecognitionService._is_eligible`` (schema version, exact ID
    match, bool ``is_active``); the active/override gating itself stays
    where it belongs — recognition time.
    """
    manifest_path = (
        settings.employees_dir / safe_dir_name(employee_id) / MANIFEST_FILENAME
    )
    if not manifest_path.is_file():
        raise EnrollmentBridgeInputError(
            f"no published enrollment manifest for {employee_id!r} at the "
            "required Step 07 location; run enrollment first"
        )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnrollmentBridgeInputError(
            f"enrollment manifest for {employee_id!r} is unreadable or "
            f"malformed: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise EnrollmentBridgeInputError(
            f"enrollment manifest for {employee_id!r} must be a JSON object"
        )
    if payload.get("format_version") != _MANIFEST_FORMAT_VERSION:
        raise EnrollmentBridgeInputError(
            f"enrollment manifest for {employee_id!r} has unsupported "
            f"'format_version' {payload.get('format_version')!r}"
        )
    employee_section = payload.get("employee")
    if not isinstance(employee_section, dict):
        raise EnrollmentBridgeInputError(
            f"enrollment manifest for {employee_id!r} has no 'employee' section"
        )
    if employee_section.get("employee_id") != employee_id:
        raise EnrollmentBridgeInputError(
            f"enrollment manifest for {employee_id!r} encodes a different "
            f"identity ({employee_section.get('employee_id')!r}); refusing "
            "to bridge"
        )
    if not isinstance(employee_section.get("is_active"), bool):
        raise EnrollmentBridgeInputError(
            f"enrollment manifest for {employee_id!r} carries a non-bool "
            "'is_active'; refusing to bridge inconsistent eligibility state"
        )


def _load_samples(
    settings: AppSettings, employee_id: str
) -> list[np.ndarray]:
    """Read every published sample PNG as a BGR crop (fail-closed)."""
    sample_dir = settings.face_samples_dir / safe_dir_name(employee_id)
    if not sample_dir.is_dir():
        raise NoEligibleSamplesError(
            f"no published face-sample directory for {employee_id!r}; "
            "run enrollment first"
        )
    crops: list[np.ndarray] = []
    for path in sorted(sample_dir.iterdir()):
        if not path.is_file() or not path.name.endswith(".png"):
            continue
        image = cv2.imread(str(path))
        if image is None:
            # One unreadable sample invalidates the whole run: persisting
            # with a silently reduced sample set would under-represent
            # the enrollment's empirical support.
            raise EnrollmentBridgeInputError(
                f"sample file {path.name} is unreadable (decode failed); "
                "refusing to bridge from a partial sample set"
            )
        crops.append(image)
    if not crops:
        raise NoEligibleSamplesError(
            f"published sample directory for {employee_id!r} contains no "
            "decodable PNG samples"
        )
    return crops


def _require_service_types(storage: object) -> None:
    """Type-guard storage (embedder is duck-typed like recognition uses it)."""
    if not isinstance(storage, EmbeddingStorageService):
        raise EnrollmentBridgeInputError(
            "storage must be an EmbeddingStorageService, got "
            f"{type(storage).__name__}"
        )


def embed_published_samples(
    employee_id: str,
    *,
    embedder: object,
    storage: object,
    settings: AppSettings = SETTINGS,
) -> EnrollmentBridgeResult:
    """Convert an enrollment's published samples into one stored embedding.

    Args:
        employee_id: Exact business identifier (``007``, ``EMP-0042``);
            its directory-safe encoding reuses the project-wide scheme
            (percent-encoding; path separators/dot-forms rejected).
        embedder: A loaded :class:`~src.face_embedding.FaceEmbedder`
            (or duck-typed ``embed() -> FaceEmbedding`` equivalent).
        storage: The real
            :class:`~src.embedding_storage.EmbeddingStorageService`.
        settings: :class:`AppSettings` locating the three data roots.

    Returns:
        :class:`EnrollmentBridgeResult` — no biometric content.

    Raises:
        EnrollmentBridgeInputError: invalid ID form, missing/contradictory
            eligibility manifest, zero decodable samples, unreadable
            sample, cross-sample dimension mismatch, or aggregation
            degeneracy — always *before* any write.
        DuplicateEmbeddingRecordError: a record already exists (checked
            *first*, before any embedding work); ``save`` and its
            ``replace=False`` refusal remain the authoritative guard.
        Embedder typed errors (``FaceEmbeddingError`` subclasses) and
            storage typed errors: surface unchanged (fail-closed).
    """
    if not isinstance(employee_id, str) or not employee_id.strip():
        raise EnrollmentBridgeInputError(
            "employee_id must be a non-empty str"
        )
    if not isinstance(settings, AppSettings):
        raise EnrollmentBridgeInputError(
            f"settings must be an AppSettings, got {type(settings).__name__}"
        )
    try:
        encoded_dir_name = safe_dir_name(employee_id)
    except (EmployeeValidationError, EnrollmentInputError) as exc:
        # safe_dir_name screens ID form through the Employee model and
        # rejects path separators/dot-forms with its own typed error.
        raise EnrollmentBridgeInputError(
            f"employee_id violates the Employee/directory-safety rules: {exc}"
        ) from exc

    # -- 1. Duplicate first — no embedding work is done for an existing
    #       record (storage's save remains the authoritative guard).
    _require_service_types(storage)
    if storage.exists(employee_id):
        raise DuplicateEmbeddingRecordError(
            f"employee {employee_id!r} already has a stored embedding; "
            "the one-embedding-per-employee contract forbids re-embedding"
        )

    # -- 2. Eligibility marker must exist and be consistent (fail-closed).
    _validate_eligibility_manifest(settings, employee_id)

    # -- 3. All-or-nothing samples: every published PNG must decode.
    crops = _load_samples(settings, employee_id)
    _logger.debug(
        "bridging employee %s: %d decodable sample(s)",
        employee_id,
        len(crops),
    )

    # -- 4. Embed every sample (typed failures propagate; no save yet).
    vectors: list[np.ndarray] = []
    for index, crop in enumerate(crops, start=1):
        embedding = embedder.embed(crop)
        vector = getattr(embedding, "vector", embedding)
        if not isinstance(vector, np.ndarray) or vector.ndim != 1 or vector.size == 0:
            raise EnrollmentBridgeInputError(
                f"sample #{index} produced a non-usable embedding result"
            )
        vectors.append(np.asarray(vector, dtype=np.float64))

    # -- 5. Aggregate: cosine centroid (unit mean of unit vectors).
    dimensions = {v.size for v in vectors}
    if len(dimensions) != 1:
        raise EnrollmentBridgeInputError(
            "samples embed to different dimensions "
            f"({sorted(dimensions)}); refusing to compare across feature spaces"
        )
    aggregated = np.mean(np.stack([_unit(v) for v in vectors]), axis=0)
    final_vector = _unit(aggregated).astype(np.float32)
    final_vector = np.array(final_vector, copy=True)
    final_vector.setflags(write=False)
    dimension = int(final_vector.size)

    # -- 6. Persist exactly one record under the existing contract.
    storage.save(employee_id, final_vector, replace=False)

    # -- 7. Prove recognition-side consistency through the real discovery
    #       path before claiming success.
    record = storage.load(employee_id)
    if record.dimension != dimension:
        raise EnrollmentBridgeInputError(
            f"stored record dimension {record.dimension} disagrees with the "
            f"aggregated dimension {dimension}; refusing to report success"
        )
    if employee_id not in storage.list_employee_ids():
        raise EnrollmentBridgeInputError(
            f"employee {employee_id!r} is not discoverable through "
            "storage.list_employee_ids() after save; refusing to report "
            "success"
        )
    _logger.info(
        "bridged employee %s: %d sample(s) → dimension %d",
        employee_id,
        len(vectors),
        dimension,
    )
    return EnrollmentBridgeResult(
        employee_id=employee_id,
        dimension=dimension,
        samples_used=len(vectors),
    )
