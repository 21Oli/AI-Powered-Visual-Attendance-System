"""Face recognition for the AI-Powered Visual Attendance System
(Phase 1 — Step 10).

Compares **one** validated query embedding (exactly the
:class:`~src.face_embedding.FaceEmbedding` contract Step 08 produces)
against enrolled employees' stored embeddings (exactly the
:class:`~src.embedding_storage.StoredEmbedding` records Step 09 keeps)
and returns an explicit, typed decision: ``MATCHED`` / ``UNKNOWN`` /
``NO_CANDIDATES`` / ``AMBIGUOUS``. This module does **not** decide
anything temporal or liveness-related (Step 11+), capture unknown
faces, create attendance events, enroll anyone, write any file, or
touch the network. A similarity score is a decision input — **never**
proof of identity.

Similarity measure (documented, cos-scaled)
-------------------------------------------

score(a, b) = (a · b) / (‖a‖₂ ‖b‖₂) — pure cosine similarity (in
[-1, 1]), computed after dimension-equality and finiteness checks,
never dividing by zero (zero-norm vectors are rejected before the
division, with a typed error, both for the query and for every
candidate). Raw Step 08 embeddings are typically non-negative, so
scores in [0, 1] are expected but the full [-1, 1] range is handled.

**Honesty note (model compatibility, D12/D13).** Step 09's sidecar
records no model identifier (SFace exposes no version API at inference
time), so stored records cannot be provenance-checked at load.
Dimension equality alone is **not** claimed to guarantee model
compatibility (a 128-D vector from another model would also fit).
No provenance is invented for existing records; the documented,
non-invasive remedy is operational: all records used together must
come from one known, consistent embedding-model generation (re-embed
after any model change). This module's recognition results carry the
measured scores and the decision only — never a provenance claim.

Threshold policy (provisional until calibrated)
-----------------------------------------------

The decision procedure is documented in :meth:`FaceRecognitionService.
recognize`. The default ``min_similarity`` (and the optional
``min_margin``) are **provisional starting points**, explicitly NOT
production thresholds: calibration against representative positive and
negative footage is Phase 2 work (see ARCHITECTURE.md D5 / DEVELOPMENT_STATUS.md).

Employee eligibility (Step 07 authority)
----------------------------------------

Stored candidate records are used only when the employee's published
enrollment manifest (Step 07 ``employee.json``) exists — the very
marker whose publication defines "enrollment exists". Eligibility is
assembled from the **existing authoritative sources only**:

* *enrollment field* — the manifest is present (no second source of
  truth; a record without a manifest is skipped from candidates, and
  with ``require_enrollment=True`` a manifest-less *requested* ID is
  reported as unknown/invalid rather than silently enrolled);

* *active field* — read **only** from the manifest ``employee``
  serialization (Step 07 ``is_active``), trusted or overridden by an
  injectable ``active_lookup`` dependency for deployments with a
  different authority (none is invented here);
* *manifest integrity* — untrusted input: a malformed/unknown-schema/
  contradictory manifest raises a typed error (never silently treated
  as low-similarity or excluded) unless ``allow_malformed_manifests``
  is explicitly set to ``True`` (an operator escape hatch whose
  candidates are then eligible-only by storage, not by identity).

Privacy: errors and logs carry counts, IDs (as decision inputs),
statuses, reasons and scores only — never crops, never full vectors,
never per-candidate raw values.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Final, Sequence

import numpy as np

from src.config import RECOGNITION, AppSettings, RecognitionConfig, SETTINGS
from src.embedding_storage import (
    CorruptEmbeddingRecordError,
    EmbeddingRecordFormatError,
    EmbeddingStorageService,
    EmbeddedRecordMetadata,
    StoredEmbedding,
)
from src.enrollment import MANIFEST_FILENAME, safe_dir_name
from src.logging_setup import get_logger

__all__ = [
    "FaceRecognitionError",
    "RecognitionInputError",
    "RecognitionModelError",
    "RecognitionStorageError",
    "ManifestIntegrityError",
    "RecognitionStatus",
    "RecognitionResult",
    "CandidateScore",
    "FaceRecognitionService",
    "cosine_similarity",
]

_logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FaceRecognitionError(Exception):
    """Base class for face-recognition errors."""


class ManifestIntegrityError(FaceRecognitionError, RuntimeError):
    """Raised when an employee's published enrollment manifest is
    unreadable, malformed, schema-incompatible, or contradicts the
    identity it encodes.

    Fail-closed: a decision is never made on identity metadata whose
    integrity cannot be established. Storage/embedding corruption has
    its own typed errors from Step 09 and is surfaced unchanged.
    """


class RecognitionInputError(FaceRecognitionError, ValueError):
    """Raised for invalid query input, configuration, or identity form."""


class RecognitionModelError(FaceRecognitionError, RuntimeError):
    """Raised when the embedding model cannot be used for the query."""


class RecognitionStorageError(FaceRecognitionError, RuntimeError):
    """Raised when candidate records cannot be read/validated safely."""

    # CorruptEmbeddingRecordError and EmbeddingRecordFormatError are
    # re-raised *typed as their own classes* (never wrapped into this
    # one) so callers can distinguish storage-correctness from
    # recognizer-misuse — see ``recognize``.


# ---------------------------------------------------------------------------
# Result types (frozen; no vectors ever carried)
# ---------------------------------------------------------------------------


class RecognitionStatus(Enum):
    """Explicit recognition outcome — never surfaced as a bare bool."""

    MATCHED = "matched"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"
    NO_CANDIDATES = "no_candidates"


@dataclass(frozen=True)
class CandidateScore:
    """One eligible candidate's similarity score (decision input only).

    Attributes:
        employee_id: Exact enrolled employee identifier (no coercion).
        score: Cosine similarity to the query embedding, in [-1, 1].
        eligible: Whether this candidate passed the active/enrolled
            gating. Ineligible candidates are **never** returned by the
            service (kept private in decision logic); this type is
            public for tests only.
    """

    employee_id: str
    score: float
    eligible: bool = True


@dataclass(frozen=True)
class RecognitionResult:
    """Outcome of one recognition attempt — inspectable and testable.

    Attributes:
        status: MATCHED / UNKNOWN / AMBIGUOUS / NO_CANDIDATES.
        employee_id: Exact matched employee ID (``None`` when UNKNOWN,
            AMBIGUOUS (no tie-break), or NO_CANDIDATES — an identity is
            only ever assigned on an unambiguous MATCHED result).
        best_score: Best cosine similarity when candidates existed
            (``None`` for NO_CANDIDATES or a pre-decision error path).
        runner_up_score: Second-best score when two or more valid
            candidates existed; ``None`` otherwise.
        margin: ``best_score - runner_up_score`` when both exist and
            are finite.
        reason: Short diagnostic code (never biometric detail).
    """

    status: RecognitionStatus
    employee_id: str | None = None
    best_score: float | None = None
    runner_up_score: float | None = None
    margin: float | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Similarity function (independently unit-testable)
# ---------------------------------------------------------------------------


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Pure cosine similarity between two validated 1-D float vectors.

    Validates (typed ``RecognitionInputError``): both arrays numpy,
    1-D, non-empty, equal size, floating dtype (any float kind —
    values are read, never mutated), finite values, and non-zero norms
    (a zero-norm vector can never be compared). Returns the exact
    float64 dot-product-based ratio, in [-1, 1].
    """
    for name, v in (("a", a), (("b"), b)):
        if not isinstance(v, np.ndarray):
            raise RecognitionInputError(
                f"cosine_similarity: {name} must be a numpy array, got "
                f"{type(v).__name__}"
            )
        if v.ndim != 1:
            raise RecognitionInputError(
                f"cosine_similarity: {name} must be 1-D, got shape "
                f"{v.shape}"
            )
        if v.size == 0:
            raise RecognitionInputError(
                f"cosine_similarity: {name} must not be empty"
            )
        if not np.issubdtype(v.dtype, np.floating):
            raise RecognitionInputError(
                f"cosine_similarity: {name} must be floating-point, got "
                f"{v.dtype}"
            )
        if not np.isfinite(v).all():
            raise RecognitionInputError(
                f"cosine_similarity: {name} contains non-finite values "
                "(NaN or infinity)"
            )
    if a.size != b.size:
        raise RecognitionInputError(
            f"cosine_similarity: dimensions differ ({a.size} vs {b.size}); "
            "vectors from different dimension contracts cannot be compared"
        )
    norm_a = float(np.linalg.norm(a.astype(np.float64)))
    norm_b = float(np.linalg.norm(b.astype(np.float64)))
    if norm_a == 0.0 or not math.isfinite(norm_a):
        raise RecognitionInputError(
            "cosine_similarity: 'a' has a zero or non-finite norm"
        )
    if norm_b == 0.0 or not math.isfinite(norm_b):
        raise RecognitionInputError(
            "cosine_similarity: 'b' has a zero or non-finite norm"
        )
    score = float(
        np.dot(a.astype(np.float64), b.astype(np.float64)) / (norm_a * norm_b)
    )
    if not math.isfinite(score):
        raise RecognitionInputError(
            f"cosine_similarity: computed score is non-finite ({score})"
        )
    # Numerical noise can push the value a hair outside [-1, 1]; the
    # documented range is enforced before any consumer sees it.
    return max(-1.0, min(1.0, score))


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class FaceRecognitionService:
    """Recognize one face crop against enrolled employees' embeddings.

    Constructing the service performs no camera/model/filesystem
    network work. The embedder must have been ``load()``-ed before
    :meth:`recognize` (see Step 08 lifecycle); the storage service and
    settings come injected or default to the project-wide ones.

    Decision procedure (documented, deterministic)
    ----------------------------------------------

    1. Validate the caller's crop **shape contract** (no inference
       happens here — that belongs to the injected embedder) and
       embed it.
    2. Assemble eligible candidates: every stored record whose
       employee's published enrollment manifest exists (optional
       gating by ``require_enrollment``) and whose identity passes
       the injected ``active_lookup`` (when supplied).
    3. Validate each candidate record's vector (dimension equality
       with the query — never a silent zero similarity for a wrong
       dimension; a malformed record raises a typed storage error).
    4. Score every eligible candidate (deterministic cosine).
    5. Rank candidates by ``(-score, employee_id)`` — deterministic
       and stable, with no arbitrary tie resolution into identity.
    6. Decide:
       * no eligible candidates → ``NO_CANDIDATES``;
       * best score < ``min_similarity`` → ``UNKNOWN``;
       * ``min_margin`` configured and
         ``best - runner_up < min_margin`` → ``AMBIGUOUS``;
       * otherwise → ``MATCHED(best_candidate)``.

    A single eligible candidate has no runner-up and can match or be
    UNKNOWN/AMBIGUOUS-free-but-threshold-failing; identical top-two
    scores yield margin zero which is *at* the boundary (documented
    known behavior: tie ⇒ AMBIGUOUS — the tie-breaking is explicit,
    not arbitrary).
    """

    def __init__(
        self,
        *,
        embedder: object,
        storage: EmbeddingStorageService,
        config: RecognitionConfig = RECOGNITION,
        settings: AppSettings = SETTINGS,
        active_lookup: Callable[[str], bool] | None = None,
        eligibility_error_policy: str = "fail_closed",
    ) -> None:
        """
        Args:
            embedder: A loaded :class:`~src.face_embedding.FaceEmbedder`
                (or duck-typed equivalent with ``embed()`` returning a
                ``FaceEmbedding``-like ``vector``). Injected — tests
                use deterministic fakes; no real model is ever loaded
                by this module.
            storage: :class:`~src.embedding_storage.EmbeddingStorageService`
                reading enrolled embeddings (no private-file access —
                the recognizer uses only the public storage API).
            config: :class:`~src.config.RecognitionConfig` thresholds
                and gating switches (validated at construction).
            settings: :class:`~src.config.AppSettings` locating
                ``employees_dir`` (manifest-existence eligibility).
            active_lookup: Optional override of "is this employee
                currently active?"; receives the exact ID, must return
                a real ``bool``. ``None`` means: trust the enrollment
                manifest's stored ``is_active`` (Step 07 semantics).
            eligibility_error_policy: The failure posture for malformed
                manifests / unreadable active-status sources:
                ``"fail_closed"`` (default — a typed error is raised
                rather than a decision being made on unknown data) or
                ``"skip"`` (the affected candidate is excluded from
                THIS decision, reported in the result's ``reason`` —
                operator escape hatch, never the default).
        """

        if embedder is None or not hasattr(embedder, "embed"):
            raise RecognitionInputError(
                "embedder must expose an embed() method"
            )
        self._embedder = embedder
        if not isinstance(storage, EmbeddingStorageService):
            raise RecognitionInputError(
                "storage must be an EmbeddingStorageService, got "
                f"{type(storage).__name__}"
            )
        self._storage = storage
        if not isinstance(settings, AppSettings):
            raise RecognitionInputError(
                f"settings must be an AppSettings, got {type(settings).__name__}"
            )
        self._settings = settings
        self._active_lookup = active_lookup
        if eligibility_error_policy not in ("fail_closed", "skip"):
            raise RecognitionInputError(
                "eligibility_error_policy must be 'fail_closed' or "
                f"'skip', got {eligibility_error_policy!r}"
            )
        self._fail_closed = eligibility_error_policy == "fail_closed"
        if not isinstance(config, RecognitionConfig):
            raise RecognitionInputError(
                "config must be a RecognitionConfig, got "
                f"{type(config).__name__}"
            )
        self._config = config

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def recognize(
        self,
        face_crop: object,
        *,
        candidate_employee_ids: Sequence[str] | None = None,
    ) -> RecognitionResult:
        """Run one recognition decision (no persistence, no camera).

        Args:
            face_crop: BGR uint8 ``(H, W, 3)`` face crop — exactly the
                Step 08/07 sample format. Shape is validated *here*
                before the embedder sees it.
            candidate_employee_ids: Optional explicit candidate subset
                (e.g. an operator-curated allow-list). ``None`` means
                "every enrolled employee recorded in storage".

        Returns:
            :class:`RecognitionResult` with an explicit status, the
            matched ID only when a single winner is unambiguous, and
            the measured top (and, when meaningful, runner-up) scores.

        Raises:
            RecognitionInputError: invalid crop shape, or a storage
                record whose data is malformed (never downgraded).
            RecognitionModelError: embedder not loaded or inference
                failed.
            ManifestIntegrityError: an eligible employee's published
                manifest is unreadable/malformed/contradictory, under
                the default fail-closed policy.
        """
        # -- 1. Crop contract (shape only; inference is the embedder's job).
        self._validate_crop_shape(face_crop)

        # -- 2. Query embedding.
        try:
            embedding = self._embedder.embed(face_crop)
        except Exception as exc:  # noqa: BLE001 - wrap embedder failure
            _logger.error("recognition embedding failed: %s", exc)
            raise RecognitionModelError(
                f"query embedding failed: {exc}"
            ) from exc
        query_vector = self._extract_vector(embedding)
        _vector_contract(query_vector, label="query")
        # Dimension check between query and every candidate is done
        # candidate-by-candidate below (never silently treated as low
        # similarity).

        # -- 3. Candidate IDs.
        if candidate_employee_ids is None:
            requested_ids = list(self._storage.list_employee_ids())
        else:
            requested_ids = list(candidate_employee_ids)
            for requested_id in requested_ids:
                if not isinstance(requested_id, str):
                    raise RecognitionInputError(
                        f"candidate_employee_ids entries must be str, got "
                        f"{type(requested_id).__name__}"
                    )

        # -- 4. Eligibility (enrollment manifest presence, active status).
        candidates: list[tuple[str, StoredEmbedding]] = []
        for employee_id in requested_ids:
            try:
                if not self._is_eligible(employee_id):
                    continue
            except ManifestIntegrityError:
                if self._fail_closed:
                    raise
                _logger.warning(
                    "candidate %s skipped: manifest integrity (skip policy)",
                    employee_id,
                )
                continue
            try:
                record = self._storage.load(employee_id)
            except KeyError as exc:
                # Listed by storage but unreadable now — treat as a
                # storage race/absence contract violation, fail closed
                # (typed Step 09 corruption/format errors surface
                # unchanged; a vanished record is its own failure).
                raise RecognitionStorageError(
                    f"employee {employee_id!r} was listed but its record "
                    "disappeared before load; refusing to decide on "
                    "incomplete data"
                ) from exc
            candidates.append((employee_id, record))

        # -- 5. Score & rank (deterministic; malformed vectors fail loud).
        scores: list[CandidateScore] = []
        for employee_id, record in candidates:
            candidate_vector = self._extract_vector(record)
            dim_mismatch = candidate_vector.size != query_vector.size
            if dim_mismatch:
                # A different dimension is NOT a low similarity — it is
                # an incompatible feature-space record.
                raise EmbeddingRecordFormatError(
                    f"employee {employee_id!r}: stored embedding dimension "
                    f"{candidate_vector.size} differs from the query "
                    f"dimension {query_vector.size}; refusing to compare "
                    "across feature spaces"
                )
            try:
                _vector_contract(candidate_vector, label=f"candidate:{employee_id}")
            except RecognitionInputError as exc:
                # A malformed stored value is a storage-contract
                # violation, not a valid comparison input.
                raise CorruptEmbeddingRecordError(
                    f"employee {employee_id!r}: {exc}"
                ) from exc
            score = cosine_similarity(query_vector, candidate_vector)
            if not math.isfinite(score):
                raise RecognitionStorageError(
                    f"employee {employee_id!r}: non-finite similarity"
                )
            scores.append(CandidateScore(employee_id, score))

        # -- 6. Decide (documented order; deterministic ties).
        if not scores:
            return RecognitionResult(
                status=RecognitionStatus.NO_CANDIDATES,
                employee_id=None,
                best_score=None,
                runner_up_score=None,
                margin=None,
                reason="no eligible candidates stored/enrolled",
            )
        ranked = sorted(scores, key=lambda c: (-c.score, c.employee_id))
        best = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else None
        margin = (
            best.score - runner_up.score
            if runner_up is not None
            else None
        )

        if best.score < self._config.min_similarity:
            return RecognitionResult(
                status=RecognitionStatus.UNKNOWN,
                employee_id=None,
                best_score=best.score,
                runner_up_score=runner_up.score if runner_up else None,
                margin=margin,
                reason="best score below acceptance threshold",
            )
        if runner_up is not None and self._config.min_margin is not None:
            if margin is not None and margin < self._config.min_margin:
                return RecognitionResult(
                    status=RecognitionStatus.AMBIGUOUS,
                    employee_id=None,
                    best_score=best.score,
                    runner_up_score=runner_up.score,
                    margin=margin,
                    reason="top two scores within the ambiguity margin",
                )
        return RecognitionResult(
            status=RecognitionStatus.MATCHED,
            employee_id=best.employee_id,
            best_score=best.score,
            runner_up_score=runner_up.score if runner_up else None,
            margin=margin,
            reason="best score above threshold and margin satisfied",
        )

    # ------------------------------------------------------------------
    # Eligibility (Step 07 manifest authority + optional active override)
    # ------------------------------------------------------------------

    def _is_eligible(self, employee_id: str) -> bool:
        """Whether one stored record may compete in this decision.

        Reads the *existing published enrollment manifest* (Step 07)
        — storage's own ID-encode is reused so the two subsystems
        agree byte-exactly. Manifest content is untrusted: schema and
        field types are validated, and any contradiction raises
        :class:`ManifestIntegrityError` under the fail-closed policy.
        """
        manifest_path = (
            self._settings.employees_dir
            / safe_dir_name(employee_id)
            / MANIFEST_FILENAME
        )
        if not manifest_path.is_file():
            if self._config.require_enrollment:
                _logger.debug(
                    "candidate %s skipped: no published enrollment manifest",
                    employee_id,
                )
                return False
            # require_enrollment=False: manifest-free storage records
            # stay eligible (operator escape hatch, documented).
            return self._resolved_active(employee_id, manifest_present=False)
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestIntegrityError(
                f"employee {employee_id!r}: enrollment manifest unreadable "
                f"or malformed: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ManifestIntegrityError(
                f"employee {employee_id!r}: enrollment manifest must be a "
                "JSON object"
            )
        version = payload.get("format_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise ManifestIntegrityError(
                f"employee {employee_id!r}: manifest 'format_version' "
                "must be an int"
            )
        if version != 1:
            raise ManifestIntegrityError(
                f"employee {employee_id!r}: manifest schema version "
                f"{version} is not supported"
            )
        employee_section = payload.get("employee")
        if not isinstance(employee_section, dict):
            raise ManifestIntegrityError(
                f"employee {employee_id!r}: manifest has no 'employee' "
                "section"
            )
        stored_id = employee_section.get("employee_id")
        if not isinstance(stored_id, str) or not stored_id:
            raise ManifestIntegrityError(
                f"employee {employee_id!r}: manifest 'employee_id' must "
                "be a non-empty str"
            )
        # The manifest must describe the same identity it encodes.
        if stored_id != employee_id:
            raise ManifestIntegrityError(
                f"employee {employee_id!r}: manifest carries employee_id "
                f"{stored_id!r}; identity contradiction — refusing to "
                "recognize against this record"
            )
        active = employee_section.get("is_active")
        if not isinstance(active, bool):
            raise ManifestIntegrityError(
                f"employee {employee_id!r}: manifest 'is_active' must be "
                "a bool"
            )
        return self._resolved_active(
            employee_id, manifest_present=True, manifest_active=active
        )

    def _resolved_active(
        self,
        employee_id: str,
        *,
        manifest_present: bool,
        manifest_active: bool | None = None,
    ) -> bool:
        """Resolve the active gate (manifest value or injectable override)."""
        if self._active_lookup is not None:
            result = self._active_lookup(employee_id)
            if not isinstance(result, bool):
                raise ManifestIntegrityError(
                    f"employee {employee_id!r}: active_lookup returned "
                    f"{type(result).__name__}, expected bool"
                )
            return result
        if manifest_present and manifest_active is False:
            _logger.debug(
                "candidate %s skipped: employee inactive per manifest",
                employee_id,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Input & extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_crop_shape(face_crop: object) -> None:
        """Mirror the Step 08 crop contract (BGR uint8 (H, W, 3))."""
        import numpy as _np

        if face_crop is None:
            raise RecognitionInputError("face_crop must not be None")
        if not isinstance(face_crop, _np.ndarray):
            raise RecognitionInputError(
                f"face_crop must be a numpy array, got "
                f"{type(face_crop).__name__}"
            )
        if face_crop.size == 0:
            raise RecognitionInputError("face_crop must not be empty")
        if face_crop.ndim != 3 or face_crop.shape[2] != 3:
            raise RecognitionInputError(
                f"face_crop must be (H, W, 3) BGR, got shape "
                f"{face_crop.shape}"
            )
        if face_crop.shape[0] <= 0 or face_crop.shape[1] <= 0:
            raise RecognitionInputError(
                f"face_crop dimensions must be positive, got "
                f"{face_crop.shape[:2]}"
            )
        if face_crop.dtype != _np.uint8:
            raise RecognitionInputError(
                f"face_crop dtype must be uint8, got {face_crop.dtype}"
            )

    @staticmethod
    def _extract_vector(embedding: object) -> np.ndarray:
        """Pull the raw 1-D vector out of a Step 08/09 result object."""
        vector = getattr(embedding, "vector", None)
        if vector is None and isinstance(embedding, np.ndarray):
            vector = embedding  # StoredEmbedding exposes .vector anyway
        if not isinstance(vector, np.ndarray):
            raise RecognitionInputError(
                "embedding result carries no usable 'vector' attribute"
            )
        return vector


# ---------------------------------------------------------------------------
# Vector contract (shared by query & candidates; no mutation anywhere)
# ---------------------------------------------------------------------------


def _vector_contract(vector: np.ndarray, *, label: str) -> None:
    """Validate a comparison input (1-D, non-empty, finite, non-zero)."""
    if vector.ndim != 1:
        raise RecognitionInputError(
            f"{label}: embedding must be 1-D, got shape {vector.shape}"
        )
    if vector.size == 0:
        raise RecognitionInputError(f"{label}: embedding must not be empty")
    if not np.isfinite(vector).all():
        raise RecognitionInputError(
            f"{label}: embedding contains non-finite values (NaN/infinity)"
        )
    norm = float(np.linalg.norm(vector.astype(np.float64)))
    if norm == 0.0 or not math.isfinite(norm):
        raise RecognitionInputError(
            f"{label}: embedding has a zero or non-finite norm"
        )
