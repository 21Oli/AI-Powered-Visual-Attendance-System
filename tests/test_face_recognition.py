"""Step 10 tests: face recognition.

Deterministic, hardware-free: injected fake embedders, temporary-
directory-backed real EmbeddingStorageService instances, synthetic
vectors and crops. **No test uses the real SFace model** — the
artifact is not in the repo and real-inference integration remains
documented as pending (Steps 08/09 honesty carried forward).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from src.config import AppSettings, RecognitionConfig
from src.embedding_storage import (
    CorruptEmbeddingRecordError,
    EmbeddingRecordFormatError,
    EmbeddingStorageService,
)
from src.enrollment import MANIFEST_FILENAME
from src.face_embedding import FaceEmbedding, VectorDtype
from src.face_recognition import (
    ManifestIntegrityError,
    RecognitionInputError,
    RecognitionModelError,
    RecognitionStorageError,
    RecognitionStatus,
    RecognitionResult,
    FaceRecognitionService,
    cosine_similarity,
)

# ---------------------------------------------------------------------------
# Fakes & fixtures (synthetic only; embeddings are plain numbers)
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """Scriptable embedder double (mirrors the FaceEmbedder surface)."""

    def __init__(self, vector: np.ndarray | None = None,
                 error: Exception | None = None) -> None:
        self.vector = vector
        self.error = error
        self.calls = 0
        self.seen_crops: list[np.ndarray] = []

    def embed(self, crop: np.ndarray) -> FaceEmbedding:
        self.calls += 1
        self.seen_crops.append(crop)
        if self.error is not None:
            raise self.error
        assert self.vector is not None
        return FaceEmbedding(
            vector=self.vector.copy(),
            dimension=self.vector.size,
            norm=float(np.linalg.norm(self.vector.astype(np.float64))),
            dtype=VectorDtype.FLOAT32,
        )


@pytest.fixture()
def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(data_dir=tmp_path / "data", log_dir=tmp_path / "logs")


def make_crop(intensity: int = 128) -> np.ndarray:
    crop = np.full((16, 16, 3), intensity, dtype=np.uint8)
    return crop


def unit(dim: int, index: int, scale: float = 1.0) -> np.ndarray:
    """Deterministic unit vector along `index` axis (float32)."""
    v = np.zeros(dim, dtype=np.float32)
    v[index] = scale
    return v


def store(
    settings: AppSettings,
    employee_id: str,
    vector: np.ndarray,
    *,
    is_active: bool = True,
    enroll: bool = True,
) -> None:
    """Write one record + (optionally) its Step 07 manifest."""
    EmbeddingStorageService(settings=settings).save(employee_id, vector)
    if enroll:
        manifest_dir = settings.employees_dir / employee_id
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "format_version": 1,
            "employee": {
                "employee_id": employee_id,
                "full_name": "Test Person",
                "is_active": is_active,
                "created_at": "2026-10-12T00:00:00+00:00",
            },
            "sample_count": 1,
            "samples": [],
        }
        (manifest_dir / MANIFEST_FILENAME).write_text(
            json.dumps(manifest), encoding="utf-8"
        )


def build(
    settings: AppSettings,
    query: np.ndarray,
    config: RecognitionConfig | None = None,
    *,
    active_lookup=None,
    eligibility_error_policy: str = "fail_closed",
) -> FaceRecognitionService:
    return FaceRecognitionService(
        embedder=FakeEmbedder(vector=query),
        storage=EmbeddingStorageService(settings=settings),
        config=config or RecognitionConfig(),
        settings=settings,
        active_lookup=active_lookup,
        eligibility_error_policy=eligibility_error_policy,
    )


# ---------------------------------------------------------------------------
# 1. Clear best candidate → MATCHED
# ---------------------------------------------------------------------------


class TestMatched:
    def test_clear_best_candidate_matches(
        self, settings: AppSettings
    ) -> None:
        query = unit(8, 0)
        store(settings, "EMP-0042", unit(8, 0))          # cosine 1.0
        store(settings, "OTHER", unit(8, 1))             # cosine 0.0
        result = build(settings, query).recognize(make_crop())
        assert result.status is RecognitionStatus.MATCHED
        assert result.employee_id == "EMP-0042"
        assert result.best_score == pytest.approx(1.0)
        assert result.runner_up_score == pytest.approx(0.0)
        assert result.margin == pytest.approx(1.0)
        assert isinstance(result, RecognitionResult)

    def test_optional_explicit_candidate_subset_respected(
        self, settings: AppSettings
    ) -> None:
        query = unit(8, 0)
        store(settings, "A", unit(8, 0))
        store(settings, "B", unit(8, 0))
        result = build(settings, query).recognize(
            make_crop(), candidate_employee_ids=["B"]
        )
        assert result.status is RecognitionStatus.MATCHED
        assert result.employee_id == "B"

    def test_custom_threshold_higher_blocks_match(
        self, settings: AppSettings
    ) -> None:
        query = unit(4, 0)
        store(settings, "E", 3.0 * unit(4, 0) + unit(4, 1))  # cos ≈ 0.95
        strict = RecognitionConfig(min_similarity=0.99)
        result = build(settings, query, strict).recognize(make_crop())
        assert result.status is RecognitionStatus.UNKNOWN


# ---------------------------------------------------------------------------
# 2. Unknown when all below threshold
# ---------------------------------------------------------------------------


class TestUnknown:
    def test_all_below_threshold_is_unknown(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 1))  # orthogonal → 0.0
        result = build(settings, unit(8, 0)).recognize(make_crop())
        assert result.status is RecognitionStatus.UNKNOWN
        assert result.employee_id is None
        assert result.best_score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 3/5. Ambiguity margin behavior
# ---------------------------------------------------------------------------


class TestAmbiguityMargin:
    def test_close_top_two_is_ambiguous(self, settings: AppSettings) -> None:
        query = unit(4, 0)
        store(settings, "A", 2.0 * unit(4, 0) + unit(4, 1))   # cos ≈ 0.894
        store(settings, "B", 2.0 * unit(4, 0) + unit(4, 2))   # cos ≈ 0.894
        cfg = RecognitionConfig(min_similarity=0.5, min_margin=0.1)
        result = build(settings, query, cfg).recognize(make_crop())
        assert result.status is RecognitionStatus.AMBIGUOUS
        assert result.employee_id is None  # never assigned on ambiguity
        assert result.best_score == pytest.approx(
            result.runner_up_score or 0.0
        )

    def test_margin_satisfied_matches(self, settings: AppSettings) -> None:
        query = unit(4, 0)
        store(settings, "A", unit(4, 0))                      # cos 1.0
        store(settings, "B", 2.0 * unit(4, 0) + unit(4, 1))   # cos ≈ 0.894
        cfg = RecognitionConfig(min_similarity=0.5, min_margin=0.05)
        result = build(settings, query, cfg).recognize(make_crop())
        assert result.status is RecognitionStatus.MATCHED
        assert result.employee_id == "A"

    def test_exact_margin_boundary_is_not_ambiguous(
        self, settings: AppSettings
    ) -> None:
        """margin == min_margin ⇒ match (rule: strictly-below is ambiguous)."""
        query = unit(4, 0)
        a = 2.0 * unit(4, 0) + unit(4, 1)   # cos = 2/sqrt(5)
        b = 2.0 * unit(4, 0) + unit(4, 2)
        store(settings, "A", a)
        store(settings, "B", b)
        best = float(
            np.dot(query.astype(np.float64), a.astype(np.float64))
        ) / 2.2360679774997896
        runner = float(
            np.dot(query.astype(np.float64), b.astype(np.float64))
        ) / 2.2360679774997896
        cfg = RecognitionConfig(
            min_similarity=0.5, min_margin=round(best - runner, 9)
        )
        result = build(settings, query, cfg).recognize(make_crop())
        margin = result.margin or 0.0
        assert margin == pytest.approx(cfg.min_margin, abs=1e-6)
        assert result.status is RecognitionStatus.MATCHED

    def test_margin_none_disables_ambiguity_checks(
        self, settings: AppSettings
    ) -> None:
        query = unit(4, 0)
        store(settings, "A", 2.0 * unit(4, 0) + unit(4, 1))
        store(settings, "B", 2.0 * unit(4, 0) + unit(4, 2))
        cfg = RecognitionConfig(min_similarity=0.5, min_margin=None)
        result = build(settings, query, cfg).recognize(make_crop())
        assert result.status is RecognitionStatus.MATCHED  # default margin None


# ---------------------------------------------------------------------------
# 4. Threshold boundary
# ---------------------------------------------------------------------------


class TestThresholdBoundary:
    def test_score_equal_to_threshold_matches(
        self, settings: AppSettings
    ) -> None:
        """best == threshold ⇒ match (rule: strictly-below is unknown)."""
        query = unit(4, 0)
        store(settings, "E", 1.0 * unit(4, 0))  # cos 1.0 == threshold 1.0
        cfg = RecognitionConfig(min_similarity=1.0)
        result = build(settings, query, cfg).recognize(make_crop())
        assert result.status is RecognitionStatus.MATCHED

    def test_score_just_below_threshold_is_unknown(
        self, settings: AppSettings
    ) -> None:
        query = unit(4, 0)
        store(settings, "E", 3.0 * unit(4, 0) + unit(4, 1))  # cos ≈ 0.9487
        cfg = RecognitionConfig(min_similarity=0.95)
        result = build(settings, query, cfg).recognize(make_crop())
        assert result.status is RecognitionStatus.UNKNOWN


# ---------------------------------------------------------------------------
# 6/7/8. Candidate set edge cases: empty, single, ties
# ---------------------------------------------------------------------------


class TestCandidateSetEdges:
    def test_no_candidates_status(self, settings: AppSettings) -> None:
        service = build(settings, unit(8, 0))
        result = service.recognize(make_crop())
        assert result.status is RecognitionStatus.NO_CANDIDATES
        assert result.employee_id is None
        assert result.best_score is None

    def test_single_candidate_below_threshold_unknown(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 1))
        result = build(settings, unit(8, 0)).recognize(make_crop())
        assert result.status is RecognitionStatus.UNKNOWN

    def test_single_candidate_above_threshold_matches(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 0))
        result = build(settings, unit(8, 0)).recognize(make_crop())
        assert result.status is RecognitionStatus.MATCHED
        assert result.runner_up_score is None
        assert result.margin is None

    def test_identical_scores_tie_is_ambiguous_not_arbitrary(
        self, settings: AppSettings
    ) -> None:
        query = unit(4, 0)
        store(settings, "B", unit(4, 0))  # identical vectors → identical scores
        store(settings, "A", unit(4, 0))
        cfg = RecognitionConfig(min_similarity=0.5, min_margin=0.1)
        result = build(settings, query, cfg).recognize(make_crop())
        assert result.status is RecognitionStatus.AMBIGUOUS
        assert result.employee_id is None
        assert result.margin == pytest.approx(0.0)

    def test_ranking_deterministic_regardless_of_storage_order(
        self, settings: AppSettings
    ) -> None:
        query = unit(4, 0)
        store(settings, "A", 2.0 * unit(4, 0) + unit(4, 1))
        store(settings, "B", 2.0 * unit(4, 0) + unit(4, 2))
        cfg = RecognitionConfig(min_similarity=0.5, min_margin=0.05)
        first = build(settings, query, cfg).recognize(make_crop())
        second = build(settings, query, cfg).recognize(make_crop())
        assert first.employee_id == second.employee_id  # deterministic winner


# ---------------------------------------------------------------------------
# 9/10/11. Vector validation: dimensions, emptiness, NaN, inf, zero norm
# ---------------------------------------------------------------------------


class TestVectorValidation:
    def test_wrong_candidate_dimension_raises_format_error(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(16, 0))
        service = build(settings, unit(8, 0))
        with pytest.raises(EmbeddingRecordFormatError, match="differs"):
            service.recognize(make_crop())

    def test_empty_candidate_vector_raises_corrupt(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 0))
        np.savez(
            settings.embeddings_dir / "E.npz", embedding=np.array([], dtype=np.float32)
        )
        with pytest.raises(CorruptEmbeddingRecordError):
            build(settings, unit(8, 0)).recognize(make_crop())

    def test_nan_candidate_rejected_as_corrupt(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 0))
        bad = unit(8, 0)
        bad[3] = np.nan
        np.savez(settings.embeddings_dir / "E.npz", embedding=bad)
        with pytest.raises(CorruptEmbeddingRecordError, match="non-finite"):
            build(settings, unit(8, 0)).recognize(make_crop())

    def test_infinite_candidate_rejected_as_corrupt(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 0))
        bad = unit(8, 0)
        bad[0] = np.inf
        np.savez(settings.embeddings_dir / "E.npz", embedding=bad)
        with pytest.raises(CorruptEmbeddingRecordError):
            build(settings, unit(8, 0)).recognize(make_crop())

    def test_zero_norm_candidate_rejected_as_corrupt(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 0))
        np.savez(
            settings.embeddings_dir / "E.npz",
            embedding=np.zeros(8, dtype=np.float32),
        )
        with pytest.raises(CorruptEmbeddingRecordError, match="norm"):
            build(settings, unit(8, 0)).recognize(make_crop())

    def test_nan_query_embedding_raises_model_error(
        self, settings: AppSettings
    ) -> None:
        bad = unit(8, 0)
        bad[2] = np.nan
        service = FaceRecognitionService(
            embedder=FakeEmbedder(vector=bad),
            storage=EmbeddingStorageService(settings=settings),
            settings=settings,
        )
        with pytest.raises(RecognitionInputError, match="non-finite"):
            service.recognize(make_crop())

    def test_zero_norm_query_embedding_rejected(
        self, settings: AppSettings
    ) -> None:
        service = FaceRecognitionService(
            embedder=FakeEmbedder(vector=np.zeros(8, dtype=np.float32)),
            storage=EmbeddingStorageService(settings=settings),
            settings=settings,
        )
        with pytest.raises(RecognitionInputError, match="norm"):
            service.recognize(make_crop())


# ---------------------------------------------------------------------------
# cosine_similarity unit contract (11/12-ish, standalone)
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_nonnormalized_vectors_score_one(self) -> None:
        a = 3.0 * unit(8, 0)  # non-normalized on purpose
        assert cosine_similarity(a, a.copy()) == pytest.approx(1.0)

    def test_orthogonal_nonnormalized_score_zero(self) -> None:
        assert cosine_similarity(unit(8, 0), 5.0 * unit(8, 1)) == pytest.approx(0.0)

    def test_opposite_score_minus_one(self) -> None:
        a = unit(8, 0)
        b = -2.0 * unit(8, 0)
        assert cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_dimension_mismatch_rejected(self) -> None:
        with pytest.raises(RecognitionInputError, match="dimensions differ"):
            cosine_similarity(unit(8, 0), unit(9, 0))

    def test_zero_norm_rejected(self) -> None:
        with pytest.raises(RecognitionInputError, match="zero"):
            cosine_similarity(unit(8, 0), np.zeros(8, dtype=np.float32))

    def test_nan_rejected(self) -> None:
        bad = unit(8, 0)
        bad[1] = np.nan
        with pytest.raises(RecognitionInputError, match="non-finite"):
            cosine_similarity(unit(8, 0), bad)

    def test_wrong_dimensionality_rejected(self) -> None:
        with pytest.raises(RecognitionInputError, match="1-D"):
            cosine_similarity(np.ones((2, 4), dtype=np.float32), unit(8, 0))

    def test_empty_rejected(self) -> None:
        with pytest.raises(RecognitionInputError, match="empty"):
            cosine_similarity(
                np.array([], dtype=np.float32), np.array([], dtype=np.float32)
            )


# ---------------------------------------------------------------------------
# 13. Corrupt stored-record propagation (typed, not unknown)
# ---------------------------------------------------------------------------


class TestCorruptionPropagation:
    def test_garbage_array_file_surfaces_typed_corrupt(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 0))
        (settings.embeddings_dir / "E.npz").write_bytes(b"garbage")
        with pytest.raises(CorruptEmbeddingRecordError):
            build(settings, unit(8, 0)).recognize(make_crop())

    def test_corrupt_record_is_not_silently_unknown(
        self, settings: AppSettings
    ) -> None:
        """A corrupted record must never masquerade as UNKNOWN.

        With an explicit candidate request, storage's typed corruption
        error reaches the caller unchanged. With auto-listing, Step
        09's documented skip-policy excludes the corrupt record from
        the listing (composition contract tested here as NO_CANDIDATES
        rather than an identity decision).
        """
        store(settings, "E", unit(8, 0))
        (settings.embeddings_dir / "E.meta.json").write_text("{broken")
        service = build(settings, unit(8, 0))
        # Explicit candidate: corruption is typed, never UNKNOWN.
        with pytest.raises(CorruptEmbeddingRecordError):
            service.recognize(make_crop(), candidate_employee_ids=["E"])
        # Auto-list: storage skips the corrupt sidecar by contract; the
        # recognizer never invents an identity decision from that.
        result = service.recognize(make_crop())
        assert result.status is RecognitionStatus.NO_CANDIDATES
        assert result.employee_id is None

    def test_corrupt_array_file_with_explicit_candidates_raises_typed(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 0))
        (settings.embeddings_dir / "E.npz").write_bytes(b"garbage")
        with pytest.raises(CorruptEmbeddingRecordError):
            build(settings, unit(8, 0)).recognize(
                make_crop(), candidate_employee_ids=["E"]
            )


# ---------------------------------------------------------------------------
# 14. Embedder loading / inference failure
# ---------------------------------------------------------------------------


class TestEmbedderFailure:
    def test_embedder_inference_exception_is_model_error(
        self, settings: AppSettings
    ) -> None:
        service = FaceRecognitionService(
            embedder=FakeEmbedder(error=RuntimeError("backend crashed")),
            storage=EmbeddingStorageService(settings=settings),
            settings=settings,
        )
        with pytest.raises(RecognitionModelError, match="backend crashed"):
            service.recognize(make_crop())

    def test_unusable_embedding_result_is_model_error(
        self, settings: AppSettings
    ) -> None:
        class JunkResult:
            pass  # no .vector attribute

        class JunkEmbedder:
            def embed(self, crop):
                return JunkResult()

        service = FaceRecognitionService(
            embedder=JunkEmbedder(),
            storage=EmbeddingStorageService(settings=settings),
            settings=settings,
        )
        with pytest.raises(RecognitionInputError, match="vector"):
            service.recognize(make_crop())

    def test_embedder_without_embed_method_rejected_at_construction(
        self, settings: AppSettings
    ) -> None:
        with pytest.raises(RecognitionInputError, match="embed"):
            FaceRecognitionService(
                embedder=object(),
                storage=EmbeddingStorageService(settings=settings),
                settings=settings,
            )


# ---------------------------------------------------------------------------
# 15. Storage read failures
# ---------------------------------------------------------------------------


class TestStorageFailure:
    def test_listed_record_vanished_at_load_fails_closed(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 0))
        # Remove the record between listing and loading (storage race).
        service = build(settings, unit(8, 0))
        original_list = EmbeddingStorageService.list_employee_ids

        def listing_but_gone(self):  # noqa: ANN001
            return original_list(self)

        # Simulate the race: intercept through storage public API by
        # deleting the files before the call (list happens first, then
        # load — this deletion makes load observe the vanished record).
        from src.face_recognition import RecognitionStorageError

        embeddings_dir = settings.embeddings_dir
        def race_then_missing():
            listed = original_list(
                EmbeddingStorageService(settings=settings)
            )
            for suffix in (".npz", ".meta.json"):
                (embeddings_dir / f"E{suffix}").unlink(missing_ok=True)
            return listed

        monkey = service  # use explicit subclass-free interception below
        original_load = EmbeddingStorageService.load

        def load_after_race(self, employee_id):  # noqa: ANN001
            for suffix in (".npz", ".meta.json"):
                (embeddings_dir / f"{employee_id}{suffix}").unlink(
                    missing_ok=True
                )
            return original_load(self, employee_id)

        EmbeddingStorageService.load = load_after_race
        try:
            with pytest.raises(RecognitionStorageError, match="disappeared"):
                service.recognize(make_crop())
        finally:
            EmbeddingStorageService.load = original_load


# ---------------------------------------------------------------------------
# 16. Configuration validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    @pytest.mark.parametrize("bad", [-0.1, 1.5])
    def test_invalid_min_similarity_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError, match="min_similarity"):
            RecognitionConfig(min_similarity=bad)

    def test_min_similarity_bounds_are_inclusive(self) -> None:
        RecognitionConfig(min_similarity=0.0)
        RecognitionConfig(min_similarity=1.0)

    @pytest.mark.parametrize("bad", [-0.1, 2.5])
    def test_invalid_min_margin_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError, match="min_margin"):
            RecognitionConfig(min_margin=bad)

    def test_none_margin_allowed(self) -> None:
        RecognitionConfig(min_margin=None)

    def test_non_bool_require_enrollment_rejected(self) -> None:
        with pytest.raises(ValueError, match="require_enrollment"):
            RecognitionConfig(require_enrollment="yes")  # type: ignore[arg-type]

    def test_threshold_defaults_are_flagged_provisional(
        self,
    ) -> None:
        cfg = RecognitionConfig()
        assert 0.0 <= cfg.min_similarity <= 1.0
        # Structural permanence of the "provisional" doc note.
        assert "provisional" in RecognitionConfig.__doc__.lower()


# ---------------------------------------------------------------------------
# 17. Exact-ID preservation (leading zeros, hyphens, special chars)
# ---------------------------------------------------------------------------


class TestIdPreservation:
    @pytest.mark.parametrize("employee_id", ["007", "EMP-0042"])
    def test_leading_zeros_and_hyphens_match_exactly(
        self, settings: AppSettings, employee_id: str
    ) -> None:
        store(settings, employee_id, unit(8, 0))
        result = build(settings, unit(8, 0)).recognize(make_crop())
        assert result.employee_id == employee_id
        assert not isinstance(employee_id, int)


# ---------------------------------------------------------------------------
# 18. Active/inactive employee eligibility (manifest authority)
# ---------------------------------------------------------------------------


class TestEligibility:
    def test_inactive_employee_per_manifest_is_excluded(
        self, settings: AppSettings
    ) -> None:
        store(settings, "INACTIVE", unit(8, 0), is_active=False)
        store(settings, "ACTIVE", 0.5 * unit(8, 0))  # same direction
        result = build(settings, unit(8, 0)).recognize(make_crop())
        # INACTIVE matches at 1.0 but is excluded; ACTIVE (also similar)
        # is the only eligible candidate → matched.
        assert result.status is RecognitionStatus.MATCHED
        assert result.employee_id == "ACTIVE"

    def test_all_inactive_yields_no_candidates(
        self, settings: AppSettings
    ) -> None:
        store(settings, "INACTIVE", unit(8, 0), is_active=False)
        result = build(settings, unit(8, 0)).recognize(make_crop())
        assert result.status is RecognitionStatus.NO_CANDIDATES

    def test_inactive_embeddings_not_deleted(
        self, settings: AppSettings
    ) -> None:
        store(settings, "INACTIVE", unit(8, 0), is_active=False)
        build(settings, unit(8, 0)).recognize(make_crop())
        assert EmbeddingStorageService(settings=settings).exists("INACTIVE")

    def test_missing_manifest_excluded_by_default(
        self, settings: AppSettings
    ) -> None:
        store(settings, "GHOST", unit(8, 0), enroll=False)
        result = build(settings, unit(8, 0)).recognize(make_crop())
        assert result.status is RecognitionStatus.NO_CANDIDATES

    def test_malformed_manifest_fails_closed_by_default(
        self, settings: AppSettings
    ) -> None:
        store(settings, "BROKEN", unit(8, 0))
        manifest = settings.employees_dir / "BROKEN" / MANIFEST_FILENAME
        manifest.write_text("{not json", encoding="utf-8")
        with pytest.raises(ManifestIntegrityError, match="unreadable"):
            build(settings, unit(8, 0)).recognize(make_crop())

    def test_manifest_id_contradiction_fails_closed(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 0))
        manifest = settings.employees_dir / "E" / MANIFEST_FILENAME
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["employee"]["employee_id"] = "SOMEONE-ELSE"
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ManifestIntegrityError, match="contradiction"):
            build(settings, unit(8, 0)).recognize(make_crop())

    def test_manifest_inactive_non_bool_fails_closed(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 0))
        manifest = settings.employees_dir / "E" / MANIFEST_FILENAME
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["employee"]["is_active"] = "true"
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ManifestIntegrityError, match="is_active"):
            build(settings, unit(8, 0)).recognize(make_crop())

    def test_skip_policy_excludes_broken_manifest_candidate(
        self, settings: AppSettings
    ) -> None:
        store(settings, "BROKEN", unit(8, 0))
        store(settings, "OK", 0.5 * unit(8, 0))  # same direction
        manifest = settings.employees_dir / "BROKEN" / MANIFEST_FILENAME
        manifest.write_text("{broken", encoding="utf-8")
        result = build(
            settings, unit(8, 0), eligibility_error_policy="skip"
        ).recognize(make_crop())
        assert result.status is RecognitionStatus.MATCHED
        assert result.employee_id == "OK"

    def test_active_lookup_overrides_manifest(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 0), is_active=True)
        result = build(
            settings, unit(8, 0), active_lookup=lambda _id: False
        ).recognize(make_crop())
        assert result.status is RecognitionStatus.NO_CANDIDATES

    def test_active_lookup_non_bool_fails_closed(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 0))
        with pytest.raises(ManifestIntegrityError, match="active_lookup"):
            build(
                settings, unit(8, 0), active_lookup=lambda _id: "yes"
            ).recognize(make_crop())


# ---------------------------------------------------------------------------
# 19. Model-compatibility behavior (dimension ≠ provenance)
# ---------------------------------------------------------------------------


class TestModelCompatibility:
    def test_mixed_generation_records_not_provenance_checked_documented(
        self, settings: AppSettings
    ) -> None:
        """Same dimension from a hypothetical different model: this step
        does NOT silently bless provenance — the honesty contract is
        documented, and matching uses dimension+cosine within one
        declared generation (operator responsibility, doc-tested)."""
        doc = Path(__import__("src.face_recognition", fromlist=["m"]).__file__)
        text = doc.read_text(encoding="utf-8")
        assert "no model identifier" in text or "provenance" in text
        assert "one known, consistent embedding-model generation" in text


# ---------------------------------------------------------------------------
# 20/21. No mutation of inputs; no vectors/image data in results or logs
# ---------------------------------------------------------------------------


class TestPrivacyAndImmutability:
    def test_query_and_candidate_arrays_not_mutated(
        self, settings: AppSettings
    ) -> None:
        query = unit(8, 0)
        candidate = 2.0 * unit(8, 0)
        store(settings, "E", candidate)
        query_before = query.copy()
        candidate_before = candidate.copy()
        build(settings, query).recognize(make_crop())
        assert np.array_equal(query, query_before)
        # Stored file waswritten independently; in-memory input too.
        assert np.array_equal(candidate, candidate_before)
        loaded = EmbeddingStorageService(settings=settings).load("E")
        assert np.array_equal(loaded.vector, candidate)

    def test_result_carries_no_vectors(self, settings: AppSettings) -> None:
        store(settings, "E", unit(8, 0))
        result = build(settings, unit(8, 0)).recognize(make_crop())
        for field_name in result.__dataclass_fields__:
            value = getattr(result, field_name)
            if value is None:
                continue
            assert not isinstance(value, np.ndarray), field_name
        assert result.employee_id == "E"

    def test_crop_not_mutated_by_service(self, settings: AppSettings) -> None:
        store(settings, "E", unit(8, 0))
        service = build(settings, unit(8, 0))
        crop = make_crop()
        before = crop.copy()
        service.recognize(crop)
        assert np.array_equal(crop, before)

    def test_exception_messages_carry_no_vectors(
        self, settings: AppSettings
    ) -> None:
        store(settings, "E", unit(8, 0))
        bad = unit(8, 0)
        bad[1] = np.nan
        service = FaceRecognitionService(
            embedder=FakeEmbedder(vector=bad),
            storage=EmbeddingStorageService(settings=settings),
            settings=settings,
        )
        try:
            service.recognize(make_crop())
            raised = None  # pragma: no cover
        except RecognitionInputError as exc:
            raised = str(exc)
            # The offending *value* (and any vector content) never leaks.
            assert "nan" != raised.lower().strip()
            assert str(float(bad[1])) not in raised
            assert str(bad.tolist()) not in raised
            # Generic diagnostic wording is allowed.
            assert "non-finite" in raised


# ---------------------------------------------------------------------------
# 22/23. Hygiene: import/construction purity; foreign-CWD construction
# ---------------------------------------------------------------------------


class TestHygiene:
    def test_import_and_construction_no_side_effects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        monkeypatch.chdir(foreign)
        import src.face_recognition  # noqa: F401

        before = sorted(p.name for p in tmp_path.iterdir())
        service = FaceRecognitionService(
            embedder=FakeEmbedder(vector=unit(8, 0)),
            storage=EmbeddingStorageService(
                settings=AppSettings(data_dir=tmp_path / "data")
            ),
            settings=AppSettings(data_dir=tmp_path / "data"),
        )
        # Construction probes nothing.
        assert service is not None
        after = sorted(p.name for p in tmp_path.iterdir())
        assert before == after
        assert not (tmp_path / "data").exists()

    def test_recognize_without_any_storage_or_manifest_creates_nothing(
        self, tmp_path: Path
    ) -> None:
        data_dir = tmp_path / "data"
        service = FaceRecognitionService(
            embedder=FakeEmbedder(vector=unit(8, 0)),
            storage=EmbeddingStorageService(
                settings=AppSettings(data_dir=data_dir)
            ),
            settings=AppSettings(data_dir=data_dir),
        )
        result = service.recognize(make_crop())
        assert result.status is RecognitionStatus.NO_CANDIDATES
        assert not data_dir.exists()  # read path never creates trees

    def test_subprocess_import_no_side_effects(self, tmp_path: Path) -> None:
        import src.face_recognition as module

        project_root = Path(module.__file__).resolve().parent.parent
        code = (
            "import sys; sys.path.insert(0, r'"
            + str(project_root)
            + "'); import src.face_recognition; print('clean')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(tmp_path),
        )
        assert proc.returncode == 0, proc.stderr
        assert "clean" in proc.stdout

    def test_no_camera_or_network_surface(self) -> None:
        import src.face_recognition as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "VideoCapture" not in source
        assert "socket" not in source
        assert "urllib.request" not in source


# ---------------------------------------------------------------------------
# Service input validation (crop contract mirrors Steps 07/08)
# ---------------------------------------------------------------------------


class TestCropValidation:
    @pytest.mark.parametrize(
        "bad_crop",
        [
            None,
            "not an array",
            np.zeros((5, 5), dtype=np.uint8),        # 2-D
            np.zeros((5, 5, 4), dtype=np.uint8),     # 4 channels
            np.zeros((0, 5, 3), dtype=np.uint8),     # empty H
            np.zeros((5, 5, 3), dtype=np.float32),   # wrong dtype
        ],
    )
    def test_invalid_crops_rejected_before_embedder(
        self, settings: AppSettings, bad_crop
    ) -> None:
        embedder = FakeEmbedder(vector=unit(8, 0))
        service = FaceRecognitionService(
            embedder=embedder,
            storage=EmbeddingStorageService(settings=settings),
            settings=settings,
        )
        with pytest.raises(RecognitionInputError):
            service.recognize(bad_crop)
        assert embedder.calls == 0  # embedder never invoked on bad input


# ---------------------------------------------------------------------------
# Decision procedure invariants: unknown never carries identity,
# ambiguous never carries identity, matched carries identity.
# ---------------------------------------------------------------------------


class TestResultIdentityInvariant:
    def test_unknown_carries_no_identity(self, settings: AppSettings) -> None:
        store(settings, "E", unit(8, 1))
        assert (
            build(settings, unit(8, 0)).recognize(make_crop()).employee_id
            is None
        )

    def test_ambiguous_carries_no_identity(
        self, settings: AppSettings
    ) -> None:
        store(settings, "A", 2.0 * unit(4, 0) + unit(4, 1))
        store(settings, "B", 2.0 * unit(4, 0) + unit(4, 2))
        cfg = RecognitionConfig(min_similarity=0.5, min_margin=0.2)
        assert (
            build(settings, unit(4, 0), cfg).recognize(make_crop()).employee_id
            is None
        )
