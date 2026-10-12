"""Embedding storage for the AI-Powered Visual Attendance System
(Phase 1 — Step 09).

Persists *validated* face embeddings (exactly the :class:`FaceEmbedding`
result contract from Step 08) under the existing configured
``AppSettings.embeddings_dir`` — one record file pair per employee —
and loads them back after full validation. This module does **not**
generate embeddings (Step 08), match faces or recognize identities
(Step 10), consult or modify enrollment manifests, create employee
records, or touch the network. Storage proves nothing about identity.

Storage format (single decision, no competing formats)
------------------------------------------------------

**NumPy ``.npz`` (uncompressed, ``allow_pickle=False``) — one array
file per employee** at ``<data>/embeddings/<encoded-id>.npz``, plus a
JSON metadata sidecar ``<encoded-id>.meta.json``. Rationale:

* ``np.savez`` / ``np.load(..., allow_pickle=False)`` stores exactly
  one numeric array and refuses object arrays — no ``pickle``
  deserialization surface, satisfying the no-pickle rule without
  inventing a text codec that could silently corrupt float32 values.
* JSON metadata (schema ``format_version: 1``) carries the exact
  employee ID, declared dimension, dtype marker, and UTC storage
  timestamp — the validated sidecar without reading binary payloads.
* One record-file pair per employee makes the no-overwrite policy
  trivially safe: publication is one atomic operation per file after
  both files are staged and flushed. No database, no new dependency.
* Raw face images are never stored here (Step 07 owns PNG crops);
  model artifacts stay under ``models/`` (never here).

Safety rules
------------

* Employee IDs validate through the exact ``Employee`` model rules,
  then percent-encode into directory-safe record-name stems (dots
  encoded, path separators and ``.``/``..`` rejected outright) — the
  same scheme Step 07 uses, so ``007`` and ``EMP-0042`` survive
  byte-exactly.
* An existing record is **never overwritten silently**: ``save()``
  refuses when either record file exists; an *explicit* ``replace=``
  flag (documented update policy) is the only overwrite path.
* On save failure only the current attempt's staging artifacts are
  removed; pre-existing and unrelated files are untouched.
* All validation happens before any write; stored files are treated
  as untrusted input when loading — corruption is never silently
  converted into an empty vector or a missing record.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Final
from urllib.parse import quote

import numpy as np

from src.config import AppSettings, SETTINGS
from src.employee import Employee, EmployeeValidationError
from src.logging_setup import get_logger

__all__ = [
    "EmbeddingStorageError",
    "EmbeddingStorageInputError",
    "DuplicateEmbeddingRecordError",
    "EmbeddingRecordNotFoundError",
    "CorruptEmbeddingRecordError",
    "EmbeddingRecordFormatError",
    "EmbeddingPersistenceError",
    "StorageDtype",
    "EmbeddedRecordMetadata",
    "StoredEmbedding",
    "EmbeddingStorageService",
    "RECORD_SUFFIX",
    "METADATA_SUFFIX",
    "EMBEDDING_STORAGE_FORMAT_VERSION",
]

_logger = get_logger(__name__)

#: Record-array file (NumPy .npz, numeric arrays only — no pickle).
RECORD_SUFFIX: Final[str] = ".npz"

#: Metadata sidecar (JSON, schema version below).
METADATA_SUFFIX: Final[str] = ".meta.json"

#: On-disk metadata schema version. Bump only with a documented migration.
EMBEDDING_STORAGE_FORMAT_VERSION: Final[int] = 1

#: Characters allowed raw in an encoded record stem (mirrors Step 07).
_SAFE_STEM_EXTRA: Final[str] = ""

_VECTOR_KEY: Final[str] = "embedding"


class EmbeddingStorageError(Exception):
    """Base class for embedding-storage errors."""


class EmbeddingStorageInputError(EmbeddingStorageError, ValueError):
    """Raised when a save/load request violates the documented input contract."""


class DuplicateEmbeddingRecordError(EmbeddingStorageError):
    """Raised when a record exists and ``replace`` was not requested."""


class EmbeddingRecordNotFoundError(EmbeddingStorageError, KeyError):
    """Raised when no stored record exists for the requested employee ID."""


class CorruptEmbeddingRecordError(EmbeddingStorageError):
    """Raised when a stored record exists but fails validation on load."""


class EmbeddingRecordFormatError(EmbeddingStorageError):
    """Raised when a stored record is from an incompatible format."""


class EmbeddingPersistenceError(EmbeddingStorageError, RuntimeError):
    """Raised when staging or publishing a record fails.

    Only artifacts created by the failing attempt are cleaned up;
    pre-existing data is preserved.
    """


class StorageDtype(Enum):
    """Documented on-disk dtype contract (mirrors Step 08's VectorDtype)."""

    FLOAT32 = "float32"


@dataclass(frozen=True)
class StoredEmbedding:
    """One validated embedding record as loaded from storage.

    Attributes:
        employee_id: Exact stored business identifier (leading zeros,
            hyphens and case preserved — verified to match the ID that
            was requested).
        vector: Write-protected float32 1-D embedding array; callers
            can never corrupt storage state by mutating it.
        dimension: ``vector.size`` — validated against both the
            record's declared metadata dimension and (when configured)
            the service's dimension contract.
        metadata: The validated :class:`EmbeddedRecordMetadata` sidecar
            content (schema version, dtype marker, storage timestamp).
    """

    employee_id: str
    vector: np.ndarray
    dimension: int
    metadata: "EmbeddedRecordMetadata"


@dataclass(frozen=True)
class EmbeddedRecordMetadata:
    """Validated metadata sidecar content (what the JSON carries).

    Attributes:
        employee_id: Exact employee identifier this record belongs to.
        dimension: Declared embedding dimension; must equal the stored
            vector's size (validated against it on both write and load).
        dtype: Explicit dtype marker (always ``StorageDtype.FLOAT32``).
        stored_at: Timezone-aware UTC timestamp of persistence.
        format_version: On-disk metadata schema version.
    """

    employee_id: str
    dimension: int
    dtype: StorageDtype
    stored_at: datetime
    format_version: int = EMBEDDING_STORAGE_FORMAT_VERSION


def _validate_vector(employee_id: str, embedding: np.ndarray) -> np.ndarray:
    """Shared vector validation used identically on save and load.

    Rejects (typed ``EmbeddingStorageInputError``): non-array input,
    wrong dimensionality, empty vectors, non-floating dtypes,
    non-finite values, and zero-norm or numerically unstable vectors.
    The validated array is returned unchanged (never copied here —
    callers control the copy), and ``employee_id`` is type-checked
    only (identity validation happens in :func:`_safe_record_stem`).
    """
    del employee_id  # type-safety signature only; see _safe_record_stem
    if not isinstance(embedding, np.ndarray):
        raise EmbeddingStorageInputError(
            f"embedding must be a numpy array, got {type(embedding).__name__}"
        )
    if embedding.ndim != 1:
        raise EmbeddingStorageInputError(
            f"embedding must be a 1-D vector, got shape {embedding.shape}"
        )
    if embedding.size == 0:
        raise EmbeddingStorageInputError("embedding must not be empty")
    if not np.issubdtype(embedding.dtype, np.floating):
        raise EmbeddingStorageInputError(
            f"embedding dtype must be floating-point, got {embedding.dtype}"
        )
    if not np.isfinite(embedding).all():
        raise EmbeddingStorageInputError(
            "embedding contains non-finite values (NaN or infinity)"
        )
    norm = float(np.linalg.norm(embedding.astype(np.float64)))
    if not np.isfinite(norm):
        raise EmbeddingStorageInputError(
            "embedding L2 norm is non-finite; refusing to store a "
            "numerically unstable vector"
        )
    if norm == 0.0:
        raise EmbeddingStorageInputError(
            "embedding has zero norm; refusing to store a degenerate vector"
        )
    return embedding


def _safe_record_stem(employee_id: str) -> str:
    """Deterministic, traversal-proof record-name stem for an ID.

    Mirrors Step 07's scheme exactly: path separators and ``.``/``..``
    are rejected outright; every other character outside
    letters/digits/``-``/``_``/``~`` (dots included) is percent-encoded.
    The ID must additionally satisfy the exact ``Employee`` model
    rules, so storage never blesses an identity enrollment would
    refuse.
    """
    if not isinstance(employee_id, str) or not employee_id:
        raise EmbeddingStorageInputError(
            f"employee_id must be a non-empty str, got {employee_id!r}"
        )
    if "/" in employee_id or "\\" in employee_id:
        raise EmbeddingStorageInputError(
            "employee_id must not contain path separators "
            f"('/' or '\\\\'), got {employee_id!r}"
        )
    if employee_id in (".", ".."):
        raise EmbeddingStorageInputError(
            "employee_id must not be '.' or '..'"
        )
    try:
        Employee(employee_id=employee_id, full_name="storage validation")
    except EmployeeValidationError as exc:
        raise EmbeddingStorageInputError(
            f"employee_id violates the Employee model rules: {exc}"
        ) from exc
    encoded = quote(employee_id, safe=_SAFE_STEM_EXTRA).replace(".", "%2E")
    if not encoded:
        raise EmbeddingStorageInputError(
            "employee_id produced an empty record name"
        )
    return encoded


class EmbeddingStorageService:
    """Save, load, and list validated face embeddings per employee.

    Constructing the service performs no filesystem work: directories
    are created only when a save operation actually needs them, and no
    file is touched on import or construction (mirrors Steps 07/08).

    Example::

        storage = EmbeddingStorageService(settings=AppSettings(...))
        storage.save("007", face_embedding)   # FaceEmbedding or ndarray
        record = storage.load("007")           # fully re-validated
        storage.exists("007")                  # → True
        storage.list_employee_ids()            # → ("007",)
    """

    def __init__(
        self,
        *,
        settings: AppSettings = SETTINGS,
        expected_dimension: int = 0,
    ) -> None:
        """
        Args:
            settings: :class:`AppSettings` locating ``embeddings_dir``.
            expected_dimension: Configured embedding-dimension contract
                enforced on save **and** load (``0`` = accept any
                positive dimension). Makes cross-model dimension
                compatibility explicit rather than assumed; different
                models legitimately produce different dimensions, so a
                record from a mismatched contract is rejected with a
                typed :class:`EmbeddingRecordFormatError` instead of
                being silently compared across feature spaces.
        """
        if not isinstance(settings, AppSettings):
            raise EmbeddingStorageInputError(
                f"settings must be an AppSettings, got {type(settings).__name__}"
            )
        if not isinstance(expected_dimension, int) or isinstance(
            expected_dimension, bool
        ):
            raise EmbeddingStorageInputError(
                "expected_dimension must be an int, got "
                f"{type(expected_dimension).__name__}"
            )
        if expected_dimension < 0:
            raise EmbeddingStorageInputError(
                "expected_dimension must be a non-negative int, got "
                f"{expected_dimension}"
            )
        self._settings = settings
        self._expected_dimension = expected_dimension

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(
        self,
        employee_id: str,
        embedding: object,
        *,
        replace: bool = False,
    ) -> StoredEmbedding:
        """Validate and persist one embedding record for ``employee_id``.

        Args:
            employee_id: Exact business identifier (``007``,
                ``EMP-0042``); validated through the ``Employee`` model
                rules and traversal-screened before any write.
            embedding: A 1-D finite floating-point vector — a Step 08
                :class:`~src.face_embedding.FaceEmbedding` (its
                write-protected vector is used) or a raw ``numpy``
                array. Values are normalized to the on-disk float32
                contract; zero-norm and non-finite vectors are
                rejected. Never mutated.
            replace: Explicit overwrite policy (the *only* update
                path). ``False`` (default) refuses to touch any
                existing record; ``True`` is a deliberate update that
                atomically replaces both record files.

        Returns:
            :class:`StoredEmbedding` as re-read and re-validated from
            the published files (write-protecting the caller's copy of
            the truth).

        Raises:
            EmbeddingStorageInputError: invalid ID/vector/``replace``
                type (nothing written).
            DuplicateEmbeddingRecordError: record exists and
                ``replace=False``.
            EmbeddingPersistenceError: staging/publication failed,
                after cleanup of this attempt's artifacts only.
        """
        if not isinstance(replace, bool):
            raise EmbeddingStorageInputError(
                f"replace must be a bool, got {type(replace).__name__}"
            )
        vector_attr = getattr(embedding, "vector", None)
        raw = vector_attr if isinstance(vector_attr, np.ndarray) else embedding
        validated = _validate_vector(employee_id, raw)
        if (
            self._expected_dimension
            and validated.size != self._expected_dimension
        ):
            raise EmbeddingRecordFormatError(
                f"embedding dimension {validated.size} does not match "
                f"the configured contract {self._expected_dimension}; "
                "refusing to store an incompatible record"
            )
        vector = (
            validated.astype(np.float32)
            if validated.dtype != np.float32
            else validated.copy()
        )
        vector.setflags(write=False)
        stem = _safe_record_stem(employee_id)
        return self._persist(stem, employee_id, vector, replace=replace)

    def load(self, employee_id: str) -> StoredEmbedding:
        """Load and fully re-validate one stored record.

        Stored files are treated as untrusted input: the sidecar
        schema, the exact-ID association, array shape/dtype, and every
        numeric value are validated on every load. Corruption is never
        silently converted into an empty vector or a missing record.

        Raises:
            EmbeddingStorageInputError: invalid employee ID.
            EmbeddingRecordNotFoundError: nothing stored for the ID.
            EmbeddingRecordFormatError: incompatible schema version or
                an embedding dimension outside the configured contract.
            CorruptEmbeddingRecordError: incomplete record (one file
                without the other), unreadable array, unexpected
                content, ID mismatch, non-finite values, wrong dtype,
                or a declared dimension disagreeing with the array.
        """
        stem = _safe_record_stem(employee_id)
        return self._read_record(stem, employee_id)

    def exists(self, employee_id: str) -> bool:
        """Whether a complete record (array + sidecar) exists.

        Incomplete records (one file without the other) are **not**
        reported as existing — a half-written record is not a record.
        """
        stem = _safe_record_stem(employee_id)
        return (
            self._record_path(stem).is_file()
            and self._metadata_path(stem).is_file()
        )

    def list_employee_ids(self) -> tuple[str, ...]:
        """Exact employee IDs from complete, schema-valid records.

        A corrupted sidecar or an array-file-without-sidecar is
        skipped rather than crashing the listing (loading a specific
        corrupted ID still raises its typed error). Sorted for
        deterministic order.
        """
        embeddings_dir = self._settings.embeddings_dir
        if not embeddings_dir.is_dir():
            return ()
        ids: list[str] = []
        for metadata_file in sorted(embeddings_dir.iterdir()):
            if (
                metadata_file.is_file()
                and metadata_file.name.endswith(METADATA_SUFFIX)
                and metadata_file.name != METADATA_SUFFIX
            ):
                try:
                    meta = self._read_metadata(metadata_file)
                except EmbeddingStorageError:
                    continue  # corrupt sidecar → skip, not crash
                stem = _safe_record_stem(meta.employee_id)
                if (
                    self._record_path(stem).is_file()
                    and self._metadata_path(stem) == metadata_file
                ):
                    ids.append(meta.employee_id)
        return tuple(sorted(set(ids)))

    # ------------------------------------------------------------------
    # Paths (record-name stems are already traversal-proof)
    # ------------------------------------------------------------------

    def _record_path(self, stem: str) -> Path:
        return self._settings.embeddings_dir / f"{stem}{RECORD_SUFFIX}"

    def _metadata_path(self, stem: str) -> Path:
        return self._settings.embeddings_dir / f"{stem}{METADATA_SUFFIX}"

    # ------------------------------------------------------------------
    # Save path: validate → stage → publish (no silent overwrite)
    # ------------------------------------------------------------------

    def _persist(
        self,
        stem: str,
        employee_id: str,
        vector: np.ndarray,
        *,
        replace: bool,
    ) -> StoredEmbedding:
        embeddings_dir = self._settings.embeddings_dir
        record_path = self._record_path(stem)
        metadata_path = self._metadata_path(stem)

        if not replace and (record_path.exists() or metadata_path.exists()):
            raise DuplicateEmbeddingRecordError(
                f"embedding record for employee {employee_id!r} already "
                "exists; refusing to overwrite (pass replace=True for an "
                "explicit, intentional update)"
            )

        stored_at = datetime.now(UTC)
        record_tmp = embeddings_dir / f"{stem}.rec-{uuid.uuid4().hex[:12]}.tmp"
        metadata_tmp = embeddings_dir / f"{stem}.meta-{uuid.uuid4().hex[:12]}.tmp"

        # Only artifacts created by THIS attempt are ever cleaned.
        created_this_attempt: list[Path] = []

        try:
            # Directory creation happens only inside a real save run.
            embeddings_dir.mkdir(parents=True, exist_ok=True)

            # Stage the array file: np.savez with a plain numeric array
            # never embeds object/pickle content by construction.
            with open(record_tmp, "wb") as handle:
                np.savez(handle, **{_VECTOR_KEY: vector})
                handle.flush()
                os.fsync(handle.fileno())
            created_this_attempt.append(record_tmp)

            # Stage the metadata sidecar.
            payload = {
                "format_version": EMBEDDING_STORAGE_FORMAT_VERSION,
                "employee_id": employee_id,
                "dimension": int(vector.size),
                "dtype": StorageDtype.FLOAT32.value,
                "stored_at": stored_at.isoformat(),
            }
            with open(metadata_tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            created_this_attempt.append(metadata_tmp)

            # Publish: array first, sidecar last. The sidecar is the
            # "record exists" marker — a crash between the two
            # publishes leaves an unpublished array (never a declared
            # record without its validated metadata). Hard-link
            # publication fails loudly if a target materialized
            # concurrently (TOCTOU-safe no-overwrite); replace mode
            # uses atomic os.replace.
            if replace:
                os.replace(record_tmp, record_path)
            else:
                os.link(record_tmp, record_path)
            created_this_attempt.append(record_path)

            if replace:
                os.replace(metadata_tmp, metadata_path)
            else:
                os.link(metadata_tmp, metadata_path)
            created_this_attempt.append(metadata_path)
        except FileExistsError as exc:
            self._rollback_attempt(created_this_attempt)
            raise DuplicateEmbeddingRecordError(
                f"embedding record for employee {employee_id!r} already "
                "exists; refusing to overwrite"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - wrap persistence failure
            self._rollback_attempt(created_this_attempt)
            raise EmbeddingPersistenceError(
                f"embedding persistence failed for employee "
                f"{employee_id!r}: {exc}"
            ) from exc
        finally:
            # Staging remnants (published ones no longer exist).
            record_tmp.unlink(missing_ok=True)
            metadata_tmp.unlink(missing_ok=True)

        _logger.info(
            "embedding persisted: employee=%s, dimension=%d, replace=%s",
            employee_id,
            vector.size,
            replace,
        )
        return self._read_record(stem, employee_id)

    # ------------------------------------------------------------------
    # Load path: treat stored files as untrusted input
    # ------------------------------------------------------------------

    def _read_record(self, stem: str, requested_id: str) -> StoredEmbedding:
        record_path = self._record_path(stem)
        metadata_path = self._metadata_path(stem)
        if not record_path.is_file() and not metadata_path.is_file():
            raise EmbeddingRecordNotFoundError(
                f"no stored embedding record for employee {requested_id!r}"
            )
        if not metadata_path.is_file():
            raise CorruptEmbeddingRecordError(
                f"embedding record for employee {requested_id!r} has no "
                "metadata sidecar; the record is incomplete and not loaded"
            )
        if not record_path.is_file():
            raise CorruptEmbeddingRecordError(
                f"embedding record for employee {requested_id!r} has a "
                "metadata sidecar but no array file; the record is "
                "incomplete and not loaded"
            )

        metadata = self._read_metadata(metadata_path)

        # The record must belong to the requested employee — never
        # return a mismatched identity's vector under another ID.
        if metadata.employee_id != requested_id:
            raise CorruptEmbeddingRecordError(
                f"stored embedding record is associated with employee "
                f"{metadata.employee_id!r}, not the requested "
                f"{requested_id!r}; refusing to return a mismatched "
                "identity"
            )
        if metadata.dimension <= 0:
            raise CorruptEmbeddingRecordError(
                f"stored metadata declares invalid dimension "
                f"{metadata.dimension}; refusing to load"
            )
        if (
            self._expected_dimension
            and metadata.dimension != self._expected_dimension
        ):
            raise EmbeddingRecordFormatError(
                f"stored embedding dimension {metadata.dimension} does "
                f"not match the configured contract "
                f"{self._expected_dimension}"
            )

        try:
            with np.load(record_path, allow_pickle=False) as archive:
                if _VECTOR_KEY not in archive.files:
                    raise CorruptEmbeddingRecordError(
                        "stored record array file has no 'embedding' "
                        "array; refusing to load"
                    )
                array = archive[_VECTOR_KEY]
        except CorruptEmbeddingRecordError:
            raise
        except Exception as exc:  # noqa: BLE001 - unreadable/invalid file
            raise CorruptEmbeddingRecordError(
                f"stored embedding record is unreadable, corrupt, or "
                f"contains disallowed (pickled) content: {exc}"
            ) from exc

        if not isinstance(array, np.ndarray):
            raise CorruptEmbeddingRecordError(
                "stored record 'embedding' entry is not a numpy array"
            )
        if array.dtype.hasobject:
            raise CorruptEmbeddingRecordError(
                "stored record contains object-dtype arrays; refusing "
                "to deserialize potential pickle content"
            )

        # Untrusted-input validation: identical rigor to save.
        try:
            _validate_vector(requested_id, array)
        except EmbeddingStorageInputError as exc:
            raise CorruptEmbeddingRecordError(
                f"stored embedding values violate the vector contract: {exc}"
            ) from exc
        if array.dtype != np.float32:
            raise CorruptEmbeddingRecordError(
                f"stored embedding dtype must be float32, got {array.dtype}"
            )
        if array.ndim != 1:
            raise CorruptEmbeddingRecordError(
                f"stored embedding must be 1-D, got shape {array.shape}"
            )
        if array.size != metadata.dimension:
            raise CorruptEmbeddingRecordError(
                f"stored embedding dimension {array.size} disagrees with "
                f"the metadata-declared dimension {metadata.dimension}; "
                "refusing to load"
            )

        vector = np.array(array, copy=True)
        vector.setflags(write=False)
        return StoredEmbedding(
            employee_id=metadata.employee_id,
            vector=vector,
            dimension=metadata.dimension,
            metadata=metadata,
        )

    def _read_metadata(self, metadata_file: Path) -> EmbeddedRecordMetadata:
        """Parse and validate one metadata sidecar file (untrusted)."""
        try:
            text = metadata_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise CorruptEmbeddingRecordError(
                f"embedding metadata sidecar unreadable: {exc}"
            ) from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CorruptEmbeddingRecordError(
                f"embedding metadata sidecar is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise CorruptEmbeddingRecordError(
                "embedding metadata sidecar must be a JSON object"
            )
        version = payload.get("format_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise CorruptEmbeddingRecordError(
                "embedding metadata 'format_version' must be an int"
            )
        if version != EMBEDDING_STORAGE_FORMAT_VERSION:
            raise EmbeddingRecordFormatError(
                f"embedding metadata schema version {version} is "
                "incompatible with the supported version "
                f"{EMBEDDING_STORAGE_FORMAT_VERSION}"
            )
        employee_id = payload.get("employee_id")
        if not isinstance(employee_id, str) or not employee_id:
            raise CorruptEmbeddingRecordError(
                "embedding metadata 'employee_id' must be a non-empty str"
            )
        dimension = payload.get("dimension")
        if not isinstance(dimension, int) or isinstance(dimension, bool):
            raise CorruptEmbeddingRecordError(
                "embedding metadata 'dimension' must be an int"
            )
        dtype_raw = payload.get("dtype")
        if not isinstance(dtype_raw, str):
            raise CorruptEmbeddingRecordError(
                "embedding metadata 'dtype' must be a str"
            )
        try:
            dtype = StorageDtype(dtype_raw)
        except ValueError as exc:
            raise CorruptEmbeddingRecordError(
                f"embedding metadata 'dtype' {dtype_raw!r} is not a "
                "supported storage dtype"
            ) from exc
        stored_at_raw = payload.get("stored_at")
        if not isinstance(stored_at_raw, str):
            raise CorruptEmbeddingRecordError(
                "embedding metadata 'stored_at' must be an ISO-8601 str"
            )
        try:
            stored_at = datetime.fromisoformat(stored_at_raw)
        except ValueError as exc:
            raise CorruptEmbeddingRecordError(
                f"embedding metadata 'stored_at' is not a valid ISO-8601 "
                f"timestamp: {exc}"
            ) from exc
        if stored_at.tzinfo is None or stored_at.utcoffset() is None:
            raise CorruptEmbeddingRecordError(
                "embedding metadata 'stored_at' must be timezone-aware"
            )
        return EmbeddedRecordMetadata(
            employee_id=employee_id,
            dimension=dimension,
            dtype=dtype,
            stored_at=stored_at,
            format_version=version,
        )

    # ------------------------------------------------------------------
    # Failure cleanup (current attempt only, mirrors Step 07)
    # ------------------------------------------------------------------

    @staticmethod
    def _rollback_attempt(created_paths: list[Path]) -> None:
        """Best-effort removal of artifacts created by this attempt.

        Never touches anything that existed before the attempt or any
        unrelated file — only paths appended to ``created_paths``.
        """
        for path in reversed(created_paths):
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort, logged
                _logger.exception(
                    "cleanup of %s from the failed attempt failed", path
                )
